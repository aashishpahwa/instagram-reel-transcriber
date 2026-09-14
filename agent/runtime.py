"""The in-app agent loop: one turn = user message -> (model -> tools)* -> answer.

Provider-neutral on purpose - the account's configured provider (OpenAI chat
completions or Anthropic Messages) is called over plain HTTP exactly like the
rest of app.py, with the same tool schemas the MCP server publishes. Every
message, tool call and tool result is persisted to agent_messages so a thread
can be reopened, replayed and audited ("which reels did it actually look at").

`run_turn` is a generator of events so the HTTP layer can stream them as SSE:
    {"type": "status", "text": "..."}                       progress line
    {"type": "tool_call", "id", "name", "args"}             a tool is about to run
    {"type": "tool_result", "id", "name", "summary", "ms"}  it ran
    {"type": "message", "text", "message_id"}              the final answer
    {"type": "usage", "tokens_in", "tokens_out", "rounds"}
    {"type": "error", "text"}
"""

import json
import sys
import time
from datetime import datetime, timezone

import psycopg2.extras
import requests

from agent import prompts, schemas, tools

MAX_ROUNDS = 8            # model <-> tools round trips per turn
TOOL_RESULT_CHARS = 7000  # per tool result, before it goes back to the model
HISTORY_MESSAGES = 40     # most recent rows replayed into the context
ANTHROPIC_MAX_TOKENS = 6000

_app = None


def _a():
    global _app
    if _app is None:
        import app as _module
        _app = _module
    return _app


# ------------------------------------------------------------- persistence ----

def create_thread(user_id, project_id, title=None):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute("INSERT INTO agent_threads (user_id, project_id, title) VALUES (%s, %s, %s) "
                    "RETURNING id, title, created_at, updated_at", (user_id, project_id, (title or "")[:120] or None))
        return a._jsonify_row(cur.fetchone())


def list_threads(user_id, project_id, limit=50):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.title, t.created_at, t.updated_at,
                   (SELECT count(*) FROM agent_messages m WHERE m.thread_id = t.id AND m.role IN ('user','assistant')) AS message_count
            FROM agent_threads t WHERE t.user_id = %s AND t.project_id = %s
            ORDER BY t.updated_at DESC LIMIT %s
            """, (user_id, project_id, limit))
        return [a._jsonify_row(r) for r in cur.fetchall()]


def get_thread(user_id, project_id, thread_id):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute("SELECT id, title, created_at, updated_at FROM agent_threads WHERE id = %s AND user_id = %s AND project_id = %s",
                    (thread_id, user_id, project_id))
        return a._jsonify_row(cur.fetchone())


def delete_thread(user_id, project_id, thread_id):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute("DELETE FROM agent_threads WHERE id = %s AND user_id = %s AND project_id = %s RETURNING id",
                    (thread_id, user_id, project_id))
        return bool(cur.fetchone())


def load_messages(thread_id, limit=None):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute("SELECT id, role, content, tool_calls, tool_results, tokens_in, tokens_out, created_at "
                    "FROM agent_messages WHERE thread_id = %s ORDER BY id", (thread_id,))
        rows = [a._jsonify_row(r) for r in cur.fetchall()]
    return rows[-limit:] if limit else rows


def _save_message(thread_id, role, content=None, tool_calls=None, tool_results=None, tokens_in=None, tokens_out=None):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute(
            "INSERT INTO agent_messages (thread_id, role, content, tool_calls, tool_results, tokens_in, tokens_out) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (thread_id, role, content,
             psycopg2.extras.Json(tool_calls) if tool_calls else None,
             psycopg2.extras.Json(tool_results) if tool_results else None,
             tokens_in, tokens_out))
        mid = cur.fetchone()["id"]
        cur.execute("UPDATE agent_threads SET updated_at = now() WHERE id = %s", (thread_id,))
        return mid


def _maybe_title(thread_id, text):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute("UPDATE agent_threads SET title = %s WHERE id = %s AND title IS NULL",
                    ((text or "").strip().replace("\n", " ")[:80] or None, thread_id))


# ------------------------------------------------------------ system prompt ----

def build_system_prompt(user_id, project_id, project, context=None):
    """Operating instructions + a compact, per-turn snapshot of the project so the
    model doesn't have to call get_project_overview for the basics (it still can).
    `context` is what the user has on screen right now (e.g. {"reel_id": ...}) so
    "this reel" resolves without them pasting an id."""
    overview = tools.get_project_overview(user_id, project_id)
    snapshot = {k: overview.get(k) for k in ("project", "counts", "baseline", "own_baseline", "thresholds", "data_caveat")}
    parts = [prompts.SYSTEM_PROMPT, "\n\nPROJECT SNAPSHOT (live, JSON):\n" + json.dumps(snapshot, ensure_ascii=False, default=str)]
    if overview.get("playbook"):
        parts.append("\n\nPLAYBOOK (written after the last formula run; every claim carries its numbers):\n" + overview["playbook"])
    try:
        norms = tools.levers.shape_norms_block(tools.levers.shape_profile(tools._rows(user_id, project_id)))
        if norms:
            parts.append("\n\n" + norms + "\nUse these when you draft (hook words/seconds, length, pace, CTA placement) and when you critique; "
                         "tag_text returns the same comparison for any text as shape_check.")
    except Exception as exc:
        print(f"[agent] shape norms skipped: {exc}", file=sys.stderr)
    if overview.get("notes"):
        parts.append("\n\nPROJECT NOTES (saved by the user or by you, on their say-so):\n" +
                     "\n".join(f"- {n['text']}" for n in overview["notes"]))
    instructions = ((project or {}).get("instructions") or "").strip()
    if instructions:
        parts.append("\n\nPROJECT INSTRUCTIONS (from the user):\n" + instructions[:4000])
    reel_id = (context or {}).get("reel_id") if isinstance(context, dict) else None
    if reel_id:
        try:
            card = tools.get_reel(user_id, project_id, reel_id=reel_id)
            if card and not card.get("error"):
                brief = {k: card.get(k) for k in ("id", "creator", "source", "views", "followers", "reach_multiple",
                                                   "sends_per_1k", "outcome_band", "reach_pct_in_project", "hook", "one_line")}
                parts.append("\n\nON SCREEN RIGHT NOW: the user has this reel open - \"this reel\", \"it\", \"this one\" "
                             "refer to it unless they name another. Call get_reel on it for the full card when the "
                             "question needs more than this summary:\n" + json.dumps(brief, ensure_ascii=False, default=str))
        except Exception as exc:
            print(f"[agent] context reel failed: {exc}", file=sys.stderr)
    idea_id = (context or {}).get("idea_id") if isinstance(context, dict) else None
    if idea_id:
        try:
            a = _a()
            with a.db_cursor() as cur:
                cur.execute(
                    "SELECT id, title, notes, status, tags, script, refs FROM ideas "
                    "WHERE id = %s AND user_id = %s AND project_id = %s",
                    (int(idea_id), user_id, project_id),
                )
                row = cur.fetchone()
            if row:
                idea = dict(row)
                # The browser includes the live textarea because its latest
                # keystrokes may still be inside the autosave debounce window.
                live_draft = (context or {}).get("draft")
                if isinstance(live_draft, str):
                    idea["notes"] = live_draft[:12000]
                live_script = (context or {}).get("script")
                if isinstance(live_script, dict):
                    idea["script"] = {k: str(live_script.get(k) or "")[:12000]
                                      for k in ("hook", "promise", "validation", "cta", "full")}
                idea["refs"] = len(idea.get("refs") or [])
                parts.append(
                    "\n\nON SCREEN RIGHT NOW: the user is writing this Creative Corner draft. "
                    '"this draft", "this script", and "it" refer to it unless they name another. '
                    "Help in one concise response and do not call tools unless the user explicitly asks for "
                    "library evidence, comparison, or factual research. Never overwrite or save the draft without "
                    "the user's clear request:\n" + json.dumps(idea, ensure_ascii=False, default=str)
                )
        except Exception as exc:
            print(f"[agent] context idea failed: {exc}", file=sys.stderr)
    parts.append("\n\nToday is " + datetime.now(timezone.utc).strftime("%Y-%m-%d") + " (UTC).")
    return "".join(parts)


# ------------------------------------------------------------- providers ----

def _history_to_openai(rows):
    out = []
    for r in rows:
        if r["role"] == "user":
            out.append({"role": "user", "content": r["content"] or ""})
        elif r["role"] == "assistant":
            msg = {"role": "assistant", "content": r["content"] or None}
            if r.get("tool_calls"):
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": json.dumps(c.get("args") or {})}}
                                     for c in r["tool_calls"]]
            out.append(msg)
        elif r["role"] == "tool":
            for res in (r.get("tool_results") or []):
                out.append({"role": "tool", "tool_call_id": res["id"], "content": res.get("result") or ""})
    return out


def _history_to_anthropic(rows):
    out = []
    for r in rows:
        if r["role"] == "user":
            out.append({"role": "user", "content": r["content"] or ""})
        elif r["role"] == "assistant":
            blocks = []
            if r.get("content"):
                blocks.append({"type": "text", "text": r["content"]})
            for c in (r.get("tool_calls") or []):
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("args") or {}})
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif r["role"] == "tool":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": res["id"], "content": res.get("result") or ""}
                for res in (r.get("tool_results") or [])]})
    return out


def _call_openai(settings, system, messages, allow_tools=True):
    body = {"model": settings["model"],
            "messages": [{"role": "system", "content": system}] + messages}
    if allow_tools:
        body["tools"] = schemas.openai_tools()
        body["tool_choice"] = "auto"
    resp = requests.post(f"{settings['base_url'].rstrip('/')}/chat/completions",
                         headers={"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"},
                         json=body, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    msg = data["choices"][0]["message"]
    calls = []
    for c in (msg.get("tool_calls") or []):
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": c["id"], "name": c["function"]["name"], "args": args})
    usage = data.get("usage") or {}
    return {"text": msg.get("content") or "", "tool_calls": calls,
            "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens")}


def _call_anthropic(settings, system, messages, allow_tools=True):
    body = {"model": settings["model"], "max_tokens": ANTHROPIC_MAX_TOKENS, "system": system, "messages": messages}
    if allow_tools:
        body["tools"] = schemas.anthropic_tools()
    resp = requests.post(f"{settings['base_url'].rstrip('/')}/v1/messages",
                         headers={"x-api-key": settings["api_key"], "anthropic-version": "2023-06-01",
                                  "Content-Type": "application/json"},
                         json=body, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    text, calls = [], []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            calls.append({"id": block["id"], "name": block["name"], "args": block.get("input") or {}})
    usage = data.get("usage") or {}
    return {"text": "".join(text), "tool_calls": calls,
            "tokens_in": usage.get("input_tokens"), "tokens_out": usage.get("output_tokens"),
            "stop_reason": data.get("stop_reason")}


def _uses_responses_api(settings):
    """GPT-5-family models on api.openai.com refuse function tools on
    /chat/completions unless reasoning is off; /v1/responses keeps both. Other
    models and OpenAI-compatible hosts stay on chat completions."""
    base = (settings.get("base_url") or "").lower()
    model = (settings.get("model") or "").lower()
    return "api.openai.com" in base and model.startswith(("gpt-5", "o1", "o3", "o4"))


def _history_to_responses(rows):
    """Responses API input items, rebuilt from our persisted rows."""
    out = []
    for r in rows:
        if r["role"] == "user":
            out.append({"role": "user", "content": r["content"] or ""})
        elif r["role"] == "assistant":
            if r.get("content"):
                out.append({"role": "assistant", "content": r["content"]})
            for c in (r.get("tool_calls") or []):
                out.append({"type": "function_call", "call_id": c["id"], "name": c["name"],
                            "arguments": json.dumps(c.get("args") or {})})
        elif r["role"] == "tool":
            for res in (r.get("tool_results") or []):
                out.append({"type": "function_call_output", "call_id": res["id"], "output": res.get("result") or ""})
    return out


import re as _re

# OpenAI's hosted web search marks citations in the text with private-use
# characters: U+E200 "cite" U+E202 turn0search3 U+E202 ... U+E201. They render
# as boxes; the url_citation annotations carry the same information, which we
# turn into a Sources list instead.
_CITE_SPAN_RE = _re.compile("[^]*")
_PUA_RE = _re.compile("[-]")


def _strip_citation_tokens(text):
    if not text:
        return text
    text = _CITE_SPAN_RE.sub("", text)
    text = _PUA_RE.sub("", text)
    text = _re.sub(r"[ \t]+([.,;:!?])", r"\1", text)   # "word ." left behind by a removed token
    return _re.sub(r"[ \t]{2,}", " ", text)


def _call_openai_responses(settings, system, input_items, allow_tools=True, previous_response_id=None, new_items=None):
    body = {"model": settings["model"], "instructions": system,
            "reasoning": {"effort": settings.get("agent_reasoning_effort") or "medium"},
            "store": True}
    if previous_response_id:
        # Continue the server-side chain: reasoning state carries over, and we
        # only send what is new since the last response (the tool outputs).
        body["previous_response_id"] = previous_response_id
        body["input"] = new_items or []
    else:
        body["input"] = input_items
    if allow_tools:
        body["tools"] = [{"type": "function", "name": s["name"], "description": s["description"],
                          "parameters": s["input_schema"]} for s in schemas.TOOL_SCHEMAS]
        # OpenAI's hosted web search runs server-side inside the same turn - the
        # agent's "research" mode without a separate search key. Results come
        # back as web_search_call items plus url_citation annotations on the text.
        body["tools"].append({"type": "web_search"})
        body["tool_choice"] = "auto"
    else:
        body["tool_choice"] = "none"
    resp = requests.post(f"{settings['base_url'].rstrip('/')}/responses",
                         headers={"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"},
                         json=body, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    text, calls, hosted, citations = [], [], [], []
    for item in data.get("output") or []:
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text.append(part.get("text") or "")
                    for ann in part.get("annotations") or []:
                        if ann.get("type") == "url_citation" and ann.get("url"):
                            citations.append({"title": ann.get("title") or ann["url"], "url": ann["url"]})
        elif item.get("type") == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": item["call_id"], "name": item["name"], "args": args})
        elif item.get("type") == "web_search_call":
            action = item.get("action") or {}
            query = action.get("query") or (", ".join(action.get("queries") or [])) or "searched the web"
            hosted.append({"id": item.get("id") or f"ws_{len(hosted)}", "name": "web_search", "summary": query[:120]})
    answer = _strip_citation_tokens("".join(text))
    # Append a compact source list when the model cited pages but didn't list them.
    seen, sources = set(), []
    for c in citations:
        if c["url"] not in seen:
            seen.add(c["url"])
            sources.append(c)
    if sources and "http" not in answer:
        answer = answer.rstrip() + "\n\nSources:\n" + "\n".join(f"- {s['title']} — {s['url']}" for s in sources[:8])
    usage = data.get("usage") or {}
    return {"text": answer, "tool_calls": calls, "hosted": hosted, "response_id": data.get("id"),
            "tokens_in": usage.get("input_tokens"), "tokens_out": usage.get("output_tokens")}


def _call(settings, system, messages, allow_tools=True, **kw):
    if settings.get("provider") == "anthropic":
        return _call_anthropic(settings, system, messages, allow_tools)
    if _uses_responses_api(settings):
        return _call_openai_responses(settings, system, messages, allow_tools, **kw)
    return _call_openai(settings, system, messages, allow_tools)


# ---------------------------------------------------------------- the turn ----

def _summarise(name, result):
    """One line for the UI chip."""
    if isinstance(result, dict):
        if "error" in result and len(result) == 1:
            return f"error: {result['error'][:120]}"
        for key in ("count", "returned"):
            if key in result:
                return f"{result.get('returned', result.get('count'))} of {result.get('count')} results"
        if name == "lever_stats":
            return f"{len(result.get('levers') or [])} levers"
        if name == "get_reel":
            return f"{result.get('creator') or result.get('id')}: {result.get('views')} views, {result.get('reach_multiple')}x"
        if name == "similar_reels":
            return f"{len(result.get('similar') or [])} similar reels"
        if name == "contrast_pairs":
            return f"{len(result.get('pairs') or [])} pairs"
        if name == "tag_text":
            return f"{len(result.get('lever_matches') or [])} levers matched"
        if name == "get_project_overview":
            return f"{(result.get('counts') or {}).get('carded')} carded reels"
    return "done"


_IDEA_TOOL_REQUEST_RE = _re.compile(
    r"\b(?:search|research|fact[ -]?check|verify|sources?|citations?|evidence|compare|comparison|"
    r"benchmark|metrics?|stats?|data|library|top reels?|best reels?|saved reels?|similar reels?)\b",
    _re.I,
)


def _idea_request_needs_tools(text):
    """Keep normal draft coaching to one schema-free model call.

    The full library tools remain available when the user clearly asks for
    evidence, research, reel comparisons or project data.
    """
    return bool(_IDEA_TOOL_REQUEST_RE.search(text or ""))


def run_turn(user_id, project_id, thread_id, user_text, settings, project, max_rounds=MAX_ROUNDS, context=None):
    """Generator of events (see module docstring). Persists as it goes."""
    user_text = (user_text or "").strip()
    if not user_text:
        yield {"type": "error", "text": "empty message"}
        return
    _save_message(thread_id, "user", content=user_text)
    _maybe_title(thread_id, user_text)

    try:
        system = build_system_prompt(user_id, project_id, project, context=context)
    except Exception as exc:
        print(f"[agent] system prompt failed: {exc}", file=sys.stderr)
        system = prompts.SYSTEM_PROMPT
    focused_idea = isinstance(context, dict) and bool(context.get("idea_id"))
    idea_wants_tools = focused_idea and _idea_request_needs_tools(user_text)
    history_limit = 8 if focused_idea else HISTORY_MESSAGES
    tool_result_chars = 3500 if focused_idea else TOOL_RESULT_CHARS
    if focused_idea:
        max_rounds = min(max_rounds, 2)
    history = load_messages(thread_id, limit=history_limit)
    provider = settings.get("provider") or "openai"
    responses_api = provider != "anthropic" and _uses_responses_api(settings)
    to_provider = (_history_to_anthropic if provider == "anthropic"
                   else _history_to_responses if responses_api else _history_to_openai)
    messages = to_provider(history)
    chain = {}   # responses API: previous_response_id + the new items for the next call

    total_in = total_out = 0
    rounds = 0
    yield {"type": "status", "text": "Thinking…"}
    while True:
        rounds += 1
        allow_tools = rounds <= max_rounds and (not focused_idea or idea_wants_tools)
        try:
            reply = _call(settings, system, messages, allow_tools=allow_tools, **chain)
        except requests.HTTPError as exc:
            detail = exc.response.text[:400] if exc.response is not None else str(exc)
            yield {"type": "error", "text": f"model call failed: {detail}"}
            return
        except Exception as exc:
            yield {"type": "error", "text": f"model call failed: {exc}"}
            return
        total_in += reply.get("tokens_in") or 0
        total_out += reply.get("tokens_out") or 0
        for h in reply.get("hosted") or []:
            # Server-side searches already ran; surface them as chips.
            yield {"type": "tool_call", "id": h["id"], "name": h["name"], "args": {"query": h["summary"]}}
            yield {"type": "tool_result", "id": h["id"], "name": h["name"], "summary": h["summary"], "ms": 0}

        if not reply["tool_calls"] or not allow_tools:
            text = reply["text"].strip() or "(no answer)"
            mid = _save_message(thread_id, "assistant", content=text, tokens_in=reply.get("tokens_in"), tokens_out=reply.get("tokens_out"))
            yield {"type": "message", "text": text, "message_id": mid}
            yield {"type": "usage", "tokens_in": total_in, "tokens_out": total_out, "rounds": rounds}
            return

        # Tool round: persist the assistant's request, run every call, persist results, loop.
        _save_message(thread_id, "assistant", content=reply["text"] or None, tool_calls=reply["tool_calls"],
                      tokens_in=reply.get("tokens_in"), tokens_out=reply.get("tokens_out"))
        if reply["text"].strip():
            yield {"type": "status", "text": reply["text"].strip()[:200]}
        results = []
        for call in reply["tool_calls"]:
            yield {"type": "tool_call", "id": call["id"], "name": call["name"], "args": call["args"]}
            started = time.time()
            try:
                result = tools.call_tool(call["name"], user_id, project_id, call["args"])
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
            text = json.dumps(result, ensure_ascii=False, default=str)
            if len(text) > tool_result_chars:
                text = text[:tool_result_chars] + f'... [truncated, {len(text)} chars total - narrow the query]'
            results.append({"id": call["id"], "name": call["name"], "result": text})
            yield {"type": "tool_result", "id": call["id"], "name": call["name"],
                   "summary": _summarise(call["name"], result), "ms": int((time.time() - started) * 1000)}
        _save_message(thread_id, "tool", tool_results=results)

        if responses_api and reply.get("response_id"):
            # Continue the server-side chain (keeps the model's reasoning state
            # across tool rounds); only the new outputs are sent.
            chain = {"previous_response_id": reply["response_id"],
                     "new_items": [{"type": "function_call_output", "call_id": r["id"], "output": r["result"]}
                                   for r in results]}
        else:
            # Rebuild provider messages from the persisted rows so both paths stay identical.
            history = load_messages(thread_id, limit=history_limit)
            messages = to_provider(history)
        if rounds >= max_rounds:
            yield {"type": "status", "text": "Wrapping up (tool budget reached)…"}
