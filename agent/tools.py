"""The agent's tools - pure functions over the library.

Every function takes (user_id, project_id, ...) and returns JSON-able data.
Nothing here knows about HTTP, MCP or a chat loop; app.py's request handlers,
the MCP server and the in-app runtime all call these identically, so the agent
behaves the same whichever door it comes through.

Project scoping is enforced here, on every query, not left to the caller:
a tool that leaks another project's reels into a chat is the one bug that
would poison every answer after it.

`app` is imported lazily - importing it starts the worker threads and loads
the store, which is what we want in the MCP server process and already true
inside the web app.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import psycopg2.extras

import levers
import taxonomy

_app = None


def _a():
    global _app
    if _app is None:
        import app as _module
        _app = _module
    return _app


# ---------------------------------------------------------------- helpers ----

def _rows(user_id, project_id):
    a = _a()
    return a._rank_rows(a._done_jobs(user_id, project_id), project_id)


def _row_by_id(rows, reel_id):
    for r in rows:
        if r["job"]["id"] == reel_id:
            return r
    return None


def _jsonify(v):
    return _a()._jsonify_row(v)


def _compact_card(r):
    """What a list entry needs: enough to choose, not the whole reel."""
    job, m = r["job"], r["metrics"]
    tags = job.get("tags") or {}
    return {
        "id": job["id"],
        "url": job.get("url"),
        "creator": m.get("creator"),
        "source": job.get("source") or "reference",
        "views": m.get("view_count"),
        "followers": m.get("follower_count"),
        "reach_multiple": m.get("reach_multiple"),
        "sends_per_1k": m.get("reshares_per_1k_views"),
        "engagement_pct": m.get("engagement_rate_pct"),
        "outcome_band": m.get("outcome_band"),
        "reach_pct_in_project": m.get("reach_multiple_pct_in_project"),
        "duration_sec": m.get("duration_sec"),
        "hook": ((job.get("analysis") or {}).get("hook") or "")[:200],
        "tags": {k: tags.get(k) for k in ("hook.type", "hook.devices", "structure.type", "cta.type",
                                           "format.type", "emotion.primary", "duration_band") if tags.get(k)},
        "topic": tags.get("topic.niche"),
        "one_line": (job.get("diagnosis") or {}).get("one_line"),
        "has_card": bool(tags.get("taxonomy_v")),
    }


def _full_card(r, include_transcript=False):
    a = _a()
    job, m = r["job"], r["metrics"]
    visual = job.get("visual") or {}
    card = {
        **_compact_card(r),
        "caption": job.get("caption"),
        "hashtags": job.get("hashtags"),
        "posted_dow": m.get("posted_dow"), "posted_hour_utc": m.get("posted_hour_utc"),
        "metrics": {k: m.get(k) for k in a.CARD_PROMPT_METRIC_KEYS if m.get(k) is not None},
        "breakdown": job.get("analysis"),
        "tags": job.get("tags"),
        "diagnosis": job.get("diagnosis"),
        "insights": job.get("insights"),
        "visual": {
            "hook_type": visual.get("hook_type"), "visual_opening": visual.get("visual_opening"),
            "editing": visual.get("editing"), "on_screen_text": (visual.get("on_screen_text") or [])[:10],
            "scenes": [{"label": s.get("label"), "title": s.get("title"), "start": s.get("start"),
                        "end": s.get("end"), "goal": s.get("goal")} for s in (visual.get("scenes") or [])],
            "why_it_works": visual.get("why_it_works"),
        } if visual else None,
    }
    if include_transcript:
        card["transcript"] = (a.segments_to_script(job.get("segments")) if job.get("segments")
                              else job.get("transcript"))
    with a.db_cursor() as cur:
        cur.execute("SELECT f.id, f.category, f.name FROM formula_evidence fe JOIN formulas f ON f.id = fe.formula_id "
                    "WHERE fe.reel_id = %s AND fe.project_id = %s", (job["id"], job.get("project_id")))
        card["evidence_for_formulas"] = [dict(x) for x in cur.fetchall()]
    return card


def _resolve_reel_id(user_id, project_id, reel_id=None, url=None):
    a = _a()
    if reel_id:
        return reel_id if reel_id.startswith(f"{project_id}_") else f"{project_id}_{reel_id}"
    if url:
        return a.reel_id_for(url, project_id)
    return None


# ------------------------------------------------------------------ tools ----

def get_project_overview(user_id, project_id):
    a = _a()
    project = a.get_project(project_id, user_id)
    if not project:
        return {"error": "project not found"}
    rows = _rows(user_id, project_id)
    base = levers.project_baseline(rows)
    own = levers.own_baseline(rows)
    by_reach = sorted([r for r in rows if r["metrics"].get("reach_multiple") is not None],
                      key=lambda r: -r["metrics"]["reach_multiple"])
    with a.db_cursor() as cur:
        cur.execute("SELECT generated_at, summary, caveats FROM analysis_runs WHERE project_id = %s "
                    "ORDER BY generated_at DESC LIMIT 1", (project_id,))
        run = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM formulas WHERE project_id = %s", (project_id,))
        formula_count = cur.fetchone()["n"]
        cur.execute("SELECT text, created_by, created_at FROM project_notes WHERE project_id = %s ORDER BY created_at",
                    (project_id,))
        notes = [_jsonify(x) for x in cur.fetchall()]
    return {
        "project": {"id": project["id"], "name": project["name"], "description": project.get("description"),
                    "instructions": project.get("instructions"), "own_username": project.get("own_username"),
                    "outcome_mode": project.get("outcome_mode")},
        "counts": {"reels_done": len(rows), "carded": base.get("n_carded"), "own": own.get("n", 0),
                   "formulas": formula_count},
        "baseline": base,
        "own_baseline": {k: own.get(k) for k in ("n", "median_reach", "median_sends_1k", "median_er")} if own.get("n") else None,
        "top_by_reach": [_compact_card(r) for r in by_reach[:5]],
        "bottom_by_reach": [_compact_card(r) for r in by_reach[-5:][::-1]] if len(by_reach) > 5 else [],
        "last_run": _jsonify(run) if run else None,
        "playbook": (project.get("playbook") or {}).get("text"),
        "playbook_generated_at": (project.get("playbook") or {}).get("generated_at"),
        "notes": notes,
        "thresholds": {"good_views": a.GOOD_VIEW_THRESHOLD, "amazing_views": a.AMAZING_VIEW_THRESHOLD,
                       "reach_breakout": a.REACH_BREAKOUT, "reach_above": a.REACH_ABOVE, "reach_baseline": a.REACH_BASELINE},
        "data_caveat": ("Fewer than 15 carded reels - lever stats are thin; say so."
                        if (base.get("n_carded") or 0) < 15 else None),
    }


SORT_KEYS = {"reach": "reach_multiple", "views": "view_count", "sends": "reshares_per_1k_views",
             "engagement": "engagement_rate_pct", "recent": None}


def search_reels(user_id, project_id, tags=None, outcome_band=None, source=None, creator=None,
                 query=None, sort="reach", limit=15):
    """Structured search over the cards. `tags` is {dimension: value|[values]};
    a reel matches when every given dimension matches (any of the listed values)."""
    rows = _rows(user_id, project_id)
    limit = max(1, min(int(limit or 15), 25))
    q = (query or "").strip().lower()
    out = []
    for r in rows:
        job, m = r["job"], r["metrics"]
        t = job.get("tags") or {}
        if outcome_band and m.get("outcome_band") != outcome_band:
            continue
        if source and (job.get("source") or "reference") != source:
            continue
        if creator and (m.get("creator") or "").lower() != creator.lstrip("@").lower():
            continue
        if tags:
            ok = True
            for dim, want in tags.items():
                want = [str(w).lower() for w in (want if isinstance(want, list) else [want])]
                have = t.get(dim)
                have = [str(h).lower() for h in (have if isinstance(have, list) else ([have] if have else []))]
                if not set(want) & set(have):
                    ok = False
                    break
            if not ok:
                continue
        if q:
            blob = " ".join([job.get("transcript") or "", job.get("caption") or "", t.get("topic.niche") or "",
                             " ".join(t.get("topic.tags") or []), (job.get("diagnosis") or {}).get("one_line") or ""]).lower()
            if q not in blob:
                continue
        out.append(r)
    key = SORT_KEYS.get(sort or "reach", "reach_multiple")
    if key:
        out.sort(key=lambda r: -(r["metrics"].get(key) or 0))
    else:
        out.sort(key=lambda r: r["job"].get("created_at") or "", reverse=True)
    return {"count": len(out), "returned": min(len(out), limit), "reels": [_compact_card(r) for r in out[:limit]]}


def get_reel(user_id, project_id, reel_id=None, url=None, include_transcript=False):
    rid = _resolve_reel_id(user_id, project_id, reel_id, url)
    if not rid:
        return {"error": "give reel_id or url"}
    rows = _rows(user_id, project_id)
    r = _row_by_id(rows, rid)
    if not r:
        a = _a()
        job = a.store_cache.get(rid) or a.jobs.get(rid)
        if job and job.get("user_id") == user_id and job.get("project_id") == project_id:
            return {"id": rid, "status": job.get("status"), "error": job.get("error"),
                    "note": "reel exists but is not done yet - poll again"}
        return {"error": f"reel {rid} not found in this project"}
    return _full_card(r, include_transcript=bool(include_transcript))


def compare_reels(user_id, project_id, reel_ids):
    rows = _rows(user_id, project_id)
    picked = []
    for rid in (reel_ids or [])[:4]:
        r = _row_by_id(rows, _resolve_reel_id(user_id, project_id, rid))
        if r:
            picked.append(r)
    if len(picked) < 2:
        return {"error": "need at least two reels from this project"}
    cards = [_compact_card(r) for r in picked]
    diffs = []
    for i in range(len(picked)):
        for j in range(i + 1, len(picked)):
            diffs.append({"a": picked[i]["job"]["id"], "b": picked[j]["job"]["id"],
                          "tag_diff": levers._tag_diff(picked[i]["job"].get("tags"), picked[j]["job"].get("tags"))})
    return {"reels": cards, "tag_diffs": diffs,
            "diagnoses": {r["job"]["id"]: r["job"].get("diagnosis") for r in picked}}


def similar_reels(user_id, project_id, reel_id, limit=5):
    rows = _rows(user_id, project_id)
    rid = _resolve_reel_id(user_id, project_id, reel_id)
    me = _row_by_id(rows, rid)
    if not me:
        return {"error": f"reel {rid} not found"}
    mine = set(taxonomy.flat_tag_pairs(me["job"].get("tags")))
    my_topic = levers._niche_tokens(me["job"].get("tags"))
    scored = []
    for r in rows:
        if r["job"]["id"] == rid or not (r["job"].get("tags") or {}).get("taxonomy_v"):
            continue
        theirs = set(taxonomy.flat_tag_pairs(r["job"]["tags"]))
        overlap = len(mine & theirs)
        topic = len(my_topic & levers._niche_tokens(r["job"]["tags"]))
        score = overlap + 2 * topic
        if score:
            scored.append((score, overlap, topic, r))
    scored.sort(key=lambda x: -x[0])
    return {"reel": _compact_card(me),
            "similar": [{**_compact_card(r), "shared_tags": o, "shared_topic_terms": t}
                        for _, o, t, r in scored[:max(1, min(int(limit or 5), 10))]]}


def lever_stats(user_id, project_id, dimension=None, min_n=3):
    rows = _rows(user_id, project_id)
    table = levers.lever_table(rows, min_n=max(2, int(min_n or 3)), dimension=dimension or None)
    return table


def contrast_pairs(user_id, project_id, dimension=None, limit=10):
    rows = _rows(user_id, project_id)
    return {"pairs": levers.contrast_pairs(rows, limit=max(1, min(int(limit or 10), 20)), dimension=dimension or None)}


def get_formulas(user_id, project_id, category=None, effect=None, min_confidence=None, levers_filter=None, limit=30):
    """The formula library, ranked for use (confidence first, then measured lift,
    then how many reels cite it - so a 200x lift seen on one reel does not outrank
    a 2x lift seen on six). Filters are applied strictly first; if they leave
    nothing, they are relaxed one at a time (levers -> min_confidence -> effect ->
    category) and the result says which were dropped, so a drafting agent always
    gets formulas to build from instead of an empty list."""
    a = _a()
    with a.db_cursor() as cur:
        cur.execute(
            """
            SELECT f.id, f.category, f.name, f.description, f.reason, f.rating, f.status, f.template,
                   f.levers, f.effect, f.lift, f.times_seen, f.updated_at,
                   COALESCE(json_agg(json_build_object('reel_id', fe.reel_id, 'breakdown', fe.breakdown,
                                                       'creator', r.meta->>'username',
                                                       'views', (r.meta->>'view_count')::numeric,
                                                       'followers', (r.meta->>'follower_count')::numeric)
                            ORDER BY fe.added_at) FILTER (WHERE fe.reel_id IS NOT NULL), '[]') AS evidence
            FROM formulas f
            LEFT JOIN formula_evidence fe ON fe.formula_id = f.id
            LEFT JOIN reels r ON r.id = fe.reel_id
            WHERE f.project_id = %s
            GROUP BY f.id ORDER BY f.lift DESC NULLS LAST, f.times_seen DESC
            """, (project_id,))
        rows = [_jsonify(x) for x in cur.fetchall()]
    rank = {"low": 0, "medium": 1, "high": 2}
    for f in rows:
        lift = float(f["lift"]) if f.get("lift") is not None else None
        f["lift"] = lift
        f["ref"] = f"[F{f['id']}]"
        f["confidence"] = levers.formula_confidence(f["times_seen"], len(f["evidence"]), lift)
        f["evidence_n"] = len(f["evidence"])
        for e in f["evidence"]:
            if e.get("views") and e.get("followers"):
                e["reach_multiple"] = round(float(e["views"]) / float(e["followers"]), 2)
    rows.sort(key=lambda f: (rank.get(f["confidence"], 0), f["lift"] if f["lift"] is not None else -1.0,
                             f["evidence_n"], f.get("times_seen") or 0), reverse=True)

    def _levers_ok(f):
        fl = f.get("levers") or {}
        for dim, want in (levers_filter or {}).items():
            want = [str(w).lower() for w in (want if isinstance(want, list) else [want])]
            have = fl.get(dim)
            have = [str(h).lower() for h in (have if isinstance(have, list) else ([have] if have else []))]
            if not set(want) & set(have):
                return False
        return True

    def _apply(use_category, use_effect, use_conf, use_levers):
        out = []
        for f in rows:
            if use_category and category and f["category"] != category:
                continue
            if use_effect and effect and f.get("effect") != effect:
                continue
            if use_conf and min_confidence and rank.get(f["confidence"], 0) < rank.get(min_confidence, 0):
                continue
            if use_levers and levers_filter and not _levers_ok(f):
                continue
            out.append(f)
        return out

    # Strict first, then relax one filter at a time until something comes back.
    plans = [
        ((True, True, True, True), []),
        ((True, True, True, False), ["levers"]),
        ((True, True, False, False), ["levers", "min_confidence"]),
        ((True, False, False, False), ["levers", "min_confidence", "effect"]),
        ((False, False, False, False), ["levers", "min_confidence", "effect", "category"]),
    ]
    out, relaxed = [], []
    for flags, dropped in plans:
        out = _apply(*flags)
        if out:
            relaxed = [d for d in dropped if {"levers": levers_filter, "min_confidence": min_confidence,
                                               "effect": effect, "category": category}.get(d)]
            break
    n_effect_neutral = sum(1 for f in rows if f.get("effect") == "neutral")
    result = {"count": len(out), "library_total": len(rows),
              "formulas": out[:max(1, min(int(limit or 30), 60))]}
    if relaxed:
        result["relaxed_filters"] = relaxed
        result["note"] = ("No formula matched every filter; dropped " + ", ".join(relaxed) +
                          " to return the closest ones. Say so if it matters, and still build from these.")
    if effect == "positive" and rows and n_effect_neutral / len(rows) > 0.6:
        result["effect_caveat"] = (f"{n_effect_neutral} of {len(rows)} formulas are 'neutral' only because they cite "
                                   "one reel (effect needs n>=2), not because they failed. Judge by confidence, lift "
                                   "and the evidence reels' reach_multiple rather than by the effect label.")
    return result


def own_baseline(user_id, project_id):
    rows = _rows(user_id, project_id)
    return levers.own_baseline(rows)


def algorithm_brief(section=None):
    a = _a()
    brief = a.load_algorithm_brief() or {}
    sections = brief.get("sections") or {}
    if section:
        match = next((k for k in sections if k.lower() == section.lower()), None)
        return {"updated": brief.get("updated"), "section": match, "text": sections.get(match) if match else None,
                "available_sections": list(sections)}
    return {"updated": brief.get("updated"), "sections": sections, "sources": brief.get("sources"),
            "available": bool(brief.get("updated") and brief.get("prompt_text"))}


TAG_TEXT_PROMPT = """You are tagging a DRAFT short-form video script (or a hook / caption) against a fixed vocabulary so it can be compared with a library of real reels. There are no performance numbers - this has not been posted.
{taxonomy}

Respond with ONLY a strict JSON object: {{"tags": {{"hook.type": "...", "hook.devices": [...], "promise.type": "...", "structure.type": "...", "cta.type": "...", "cta.position": "...", "format.type": "...", "format.captions": "...", "delivery.energy": "...", "audience.awareness": "...", "emotion.primary": "...", "topic.niche": "...", "topic.tags": [...], "scores": {{...}}, "numbers": {{...}}, "other_notes": {{...only when you used "other"...}}}}, "read": "2-3 sentences: what this draft is doing, in lever terms"}}. Use the dotted key names exactly as written (e.g. "hook.type", not a nested "hook" object).

Kind of text: {kind}
Text:
\"\"\"
{text}
\"\"\"
"""


def tag_text(user_id, project_id, text, kind="script"):
    """Taxonomy tags for arbitrary text plus the library's lever stats for
    those tags - a predicted-lift sketch, clearly labelled as such."""
    a = _a()
    text = (text or "").strip()
    if len(text) < 15:
        return {"error": "give at least a sentence of text"}
    settings = a.with_platform_fallback(a.load_settings(user_id))
    if not settings.get("api_key"):
        return {"error": "no AI API key configured for this account"}
    prompt = TAG_TEXT_PROMPT.format(taxonomy=taxonomy.prompt_block(), kind=kind or "script", text=text[:6000])
    result = a.call_ai(settings, prompt, max_tokens=1500)
    words = len(text.split())
    tags, problems = taxonomy.clean_tags(result.get("tags"), {"wpm": None, "duration_sec": None})
    table = levers.lever_table(_rows(user_id, project_id))
    by_key = {(e["dimension"], e["value"]): e for e in table.get("levers") or []}
    matched = []
    for dim, val in taxonomy.flat_tag_pairs(tags):
        e = by_key.get((dim, val))
        if e:
            matched.append({"lever": f"{dim}={val}", "n": e["n"], "lift_reach": e["lift_reach"],
                            "lift_sends": e["lift_sends"], "confidence": e["confidence"]})
    lifts = [m["lift_reach"] for m in matched if m["lift_reach"] is not None]
    # The draft measured in words and seconds against this library's shape norms
    # (hook length, script length, pace, sentence length, CTA position) - same
    # numbers Rate-my-script shows. wpm: own median, else top quartile, else 150.
    shape = None
    try:
        profile = levers.shape_profile(_rows(user_id, project_id))
        groups = profile.get("groups") or {}
        wpm = next((int(round((groups.get(g) or {}).get("wpm", {}).get("median")))
                    for g in ("own", "top", "all") if ((groups.get(g) or {}).get("wpm") or {}).get("median")), 150)
        draft = levers.script_shape(text, wpm)
        checks = levers.shape_check(draft, profile)
        if draft:
            shape = {"draft": {k: v for k, v in draft.items() if k != "spoken_text"}, "wpm_assumed": wpm,
                     "checks": checks,
                     "norms_n": {g: (groups.get(g) or {}).get("n", 0) for g in ("all", "top", "own")},
                     "read_as": "verdict = against the top quartile's middle half (p25-p75) when it has >=4 reels, else the library's; "
                                "ABOVE/BELOW carry % off that median. Cite these numbers when judging hook length, duration, pace."}
    except Exception as exc:  # never let a measurement error hide the tags
        shape = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "tags": tags, "read": result.get("read"), "validation_problems": problems, "word_count": words,
        "shape_check": shape,
        "lever_matches": matched,
        "predicted_lift_sketch": {
            "geometric_mean_reach_lift": (round(__import__("math").exp(sum(__import__("math").log(x) for x in lifts) / len(lifts)), 2)
                                          if lifts else None),
            "levers_with_data": len(matched),
            "caveat": "A sketch from tag-level medians in this library, not a prediction. Tags with n<5 are weak.",
        },
        "baseline": table.get("baseline"),
    }


def ingest_reel(user_id, project_id, url):
    """Queue a reel exactly as the Add page does. Returns the id to poll with get_reel."""
    a = _a()
    url = (url or "").strip()
    if not a.INSTAGRAM_URL_RE.search(url):
        return {"error": "not an Instagram reel/post URL"}
    if not a.get_project(project_id, user_id):
        return {"error": "project not found"}
    rid = a.reel_id_for(url, project_id)
    with a.jobs_lock:
        existing = a.jobs.get(rid)
    cached = a.store_cache.get(rid)
    if cached and cached.get("status") == "done":
        return {"id": rid, "status": "done", "note": "already in the library"}
    if existing and existing.get("status") not in ("done", "error"):
        return {"id": rid, "status": existing.get("status"), "note": "already in progress"}
    a.new_job(rid, url, user_id, project_id, status="queued")
    a.download_queue.put((rid, url))
    return {"id": rid, "status": "queued",
            "note": "download -> transcribe -> visual -> breakdown -> card runs unattended; poll get_reel"}


# A scratch analysis takes as long as the pipeline takes - roughly download +
# transcribe + two model calls. Waiting inside one tool call keeps the agent from
# burning its round budget on polling, but the wait is bounded so a stuck reel
# can never hang the chat; the agent is told to call again to keep waiting.
SCRATCH_WAIT_DEFAULT = 90
SCRATCH_WAIT_MAX = 170
SCRATCH_GIVE_UP_PARTIAL_SEC = 12   # 'done' with no AI pass pending = no AI configured


def _scratch_card(user_id, project_id, rid, include_transcript=True):
    """Rank one scratch reel against the library without joining it. The library
    rows are the real ones; the scratch reel is appended only for this ranking,
    so its percentile is 'where it would sit' rather than a permanent entry."""
    a = _a()
    with a.jobs_lock:
        job = dict(a.jobs.get(rid) or {})
    if not job:
        return {"error": f"scratch analysis {rid} expired - call analyze_url again"}
    library = a._done_jobs(user_id, project_id)
    rows = a._rank_rows(library + [job], project_id)
    r = _row_by_id(rows, rid)
    if not r:
        return {"id": rid, "status": job.get("status"), "error": job.get("error")}
    card = _full_card(r, include_transcript=include_transcript)
    card["scratch"] = True
    card["compared_against"] = {"library_reels": len(library),
                                "note": "percentile and outcome band are this reel's position among those reels"}
    card["note"] = ("not saved to the library - transcript, breakdown and card exist only for this "
                    "conversation and are discarded. Use ingest_reel if the creator wants to keep it.")
    missing = [k for k in ("analysis", "visual", "tags") if not job.get(k)]
    if missing:
        card["incomplete"] = {"missing": missing,
                              "why": "these passes need an AI provider configured for this account, "
                                     "or they are still running"}
    return card


def analyze_url(user_id, project_id, url, wait_sec=None, include_transcript=True):
    """Diagnose ANY Instagram reel URL without adding it to the project.

    Runs the same pipeline as ingest_reel - download, transcript, frames, visual
    pass, four-part breakdown, Reel Card - but the result is never written to the
    database, never appears in the library or the exports, and never moves the
    lever statistics. It is discarded a few hours later."""
    a = _a()
    url = (url or "").strip()
    if not a.INSTAGRAM_URL_RE.search(url):
        return {"error": "not an Instagram reel/post URL",
                "accepted_shapes": ["instagram.com/reel/<code>/", "instagram.com/<username>/reel/<code>/",
                                    "instagram.com/p/<code>/"]}
    if not a.get_project(project_id, user_id):
        return {"error": "project not found"}

    # If the creator already saved this reel, there is nothing to download -
    # answer from the library copy, which has its real percentile and evidence.
    saved = _row_by_id(_rows(user_id, project_id), a.reel_id_for(url, project_id))
    if saved:
        card = _full_card(saved, include_transcript=bool(include_transcript))
        card["note"] = "already in the library - answered from the saved copy, nothing was re-downloaded"
        return card

    rid, _ = a.start_scratch_job(url, user_id, project_id)
    budget = max(5, min(int(wait_sec or SCRATCH_WAIT_DEFAULT), SCRATCH_WAIT_MAX))
    started = time.monotonic()
    while True:
        with a.jobs_lock:
            job = dict(a.jobs.get(rid) or {})
        status, waited = job.get("status"), time.monotonic() - started
        if status == "error":
            return {"id": rid, "scratch": True, "status": "error", "error": job.get("error")}
        if status == "done" and job.get("tags"):
            break
        # 'done' means the transcript landed; the AI passes chain on from there.
        # If none is pending after a beat, no provider is configured - hand back
        # the transcript and numbers rather than spinning to the deadline.
        if status == "done" and not job.get("ai_pending") and waited > SCRATCH_GIVE_UP_PARTIAL_SEC:
            break
        if waited >= budget:
            return {"id": rid, "scratch": True, "status": status, "stage": job.get("ai_pending"),
                    "ready": False,
                    "note": "still running - call analyze_url again with the same url to keep waiting"}
        time.sleep(2)

    return _scratch_card(user_id, project_id, rid, include_transcript=bool(include_transcript))


def save_note(user_id, project_id, text, created_by="agent"):
    a = _a()
    text = (text or "").strip()[:1000]
    if not text:
        return {"error": "empty note"}
    with a.db_cursor() as cur:
        cur.execute("INSERT INTO project_notes (user_id, project_id, text, created_by) VALUES (%s, %s, %s, %s) "
                    "RETURNING id, text, created_by, created_at",
                    (user_id, project_id, text, created_by if created_by in ("user", "agent") else "agent"))
        return _jsonify(cur.fetchone())


def list_notes(user_id, project_id):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute("SELECT id, text, created_by, created_at FROM project_notes WHERE project_id = %s AND user_id = %s "
                    "ORDER BY created_at", (project_id, user_id))
        return {"notes": [_jsonify(x) for x in cur.fetchall()]}


def delete_note(user_id, project_id, note_id):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute("DELETE FROM project_notes WHERE id = %s AND project_id = %s AND user_id = %s RETURNING id",
                    (note_id, project_id, user_id))
        return {"deleted": bool(cur.fetchone())}


SCRIPT_KEYS = ("hook", "promise", "beats", "cta", "on_screen_text", "caption", "levers", "expected_signals",
               "why", "duration_target_sec", "format", "notes")


def save_draft(user_id, project_id, title, script, predicted_tags=None, thread_id=None):
    a = _a()
    if not isinstance(script, dict) or not script.get("hook"):
        return {"error": "script must be an object with at least a 'hook'"}
    script = {k: script.get(k) for k in SCRIPT_KEYS if script.get(k) is not None}
    title = (title or script["hook"][:60]).strip()[:120]
    with a.db_cursor() as cur:
        cur.execute("INSERT INTO drafts (user_id, project_id, title, script, predicted_tags, thread_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, title, created_at",
                    (user_id, project_id, title, psycopg2.extras.Json(script),
                     psycopg2.extras.Json(predicted_tags) if predicted_tags else None, thread_id))
        return _jsonify(cur.fetchone())


def list_drafts(user_id, project_id, limit=20):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute("SELECT id, title, script->>'hook' AS hook, created_at, updated_at, posted_reel_id FROM drafts "
                    "WHERE project_id = %s AND user_id = %s ORDER BY created_at DESC LIMIT %s",
                    (project_id, user_id, max(1, min(int(limit or 20), 50))))
        return {"drafts": [_jsonify(x) for x in cur.fetchall()]}


def get_draft(user_id, project_id, draft_id):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute("SELECT * FROM drafts WHERE id = %s AND project_id = %s AND user_id = %s", (draft_id, project_id, user_id))
        row = cur.fetchone()
    return _jsonify(row) if row else {"error": "draft not found"}


def delete_draft(user_id, project_id, draft_id):
    a = _a()
    with a.db_cursor() as cur:
        cur.execute("DELETE FROM drafts WHERE id = %s AND project_id = %s AND user_id = %s RETURNING id",
                    (draft_id, project_id, user_id))
        return {"deleted": bool(cur.fetchone())}


def web_research(user_id, project_id, query, recency_days=None, max_results=6):
    """Live search through the account's configured provider (Settings: Tavily,
    LangSearch or Brave; platform env keys as fallback). Results carry URLs so
    the agent can cite them; it is told to treat them as Tier 2/3 evidence.
    On the OpenAI Responses path the model also has OpenAI's hosted web_search;
    this tool is the provider-independent one."""
    import requests
    a = _a()
    query = (query or "").strip()
    if not query:
        return {"error": "empty query"}
    settings = a.with_platform_fallback(a.load_settings(user_id))
    provider, key = (settings.get("search_provider") or "").lower(), settings.get("search_api_key") or ""
    max_results = max(1, min(int(max_results or 6), 10))
    if not (provider and key):
        return {"error": "no search provider configured - add a Tavily, LangSearch or Brave key in Settings "
                         "(or use the hosted web search if your model offers it); otherwise work from the "
                         "algorithm brief and the library"}
    try:
        if provider == "tavily":
            body = {"api_key": key, "query": query, "max_results": max_results, "search_depth": "basic"}
            if recency_days:
                body["days"] = int(recency_days)
                body["topic"] = "news"
            resp = requests.post("https://api.tavily.com/search", json=body, timeout=30)
            resp.raise_for_status()
            return {"provider": "tavily", "results": [
                {"title": r.get("title"), "url": r.get("url"), "snippet": (r.get("content") or "")[:600],
                 "published": r.get("published_date")} for r in resp.json().get("results", [])]}
        if provider == "langsearch":
            body = {"query": query, "count": max_results, "summary": True}
            if recency_days:
                body["freshness"] = ("oneDay" if recency_days <= 1 else "oneWeek" if recency_days <= 7
                                     else "oneMonth" if recency_days <= 31 else "oneYear")
            resp = requests.post("https://api.langsearch.com/v1/web-search", json=body,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=30)
            resp.raise_for_status()
            pages = (((resp.json().get("data") or {}).get("webPages") or {}).get("value")) or []
            return {"provider": "langsearch", "results": [
                {"title": r.get("name"), "url": r.get("url"),
                 "snippet": (r.get("summary") or r.get("snippet") or "")[:800],
                 "published": r.get("datePublished") or r.get("dateLastCrawled"), "site": r.get("siteName")}
                for r in pages]}
        if provider == "brave":
            params = {"q": query, "count": max_results}
            if recency_days:
                params["freshness"] = "pd" if recency_days <= 1 else ("pw" if recency_days <= 7 else "pm")
            resp = requests.get("https://api.search.brave.com/res/v1/web/search", params=params,
                                headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=30)
            resp.raise_for_status()
            web = (resp.json().get("web") or {}).get("results") or []
            return {"provider": "brave", "results": [
                {"title": r.get("title"), "url": r.get("url"), "snippet": (r.get("description") or "")[:600],
                 "published": r.get("age")} for r in web]}
    except Exception as exc:
        return {"error": f"search failed ({provider}): {exc}"}
    return {"error": f"unknown search provider {provider!r}"}


# What the MCP server and the runtime expose, by name. Order = the order the
# agent sees them; the first few are the ones it should reach for first.
TOOL_FUNCTIONS = {
    "get_project_overview": get_project_overview,
    "search_reels": search_reels,
    "get_reel": get_reel,
    "similar_reels": similar_reels,
    "compare_reels": compare_reels,
    "lever_stats": lever_stats,
    "contrast_pairs": contrast_pairs,
    "get_formulas": get_formulas,
    "own_baseline": own_baseline,
    "algorithm_brief": algorithm_brief,
    "tag_text": tag_text,
    "ingest_reel": ingest_reel,
    "analyze_url": analyze_url,
    "web_research": web_research,
    "save_note": save_note,
    "list_notes": list_notes,
    "delete_note": delete_note,
    "save_draft": save_draft,
    "list_drafts": list_drafts,
    "get_draft": get_draft,
    "delete_draft": delete_draft,
}

# Tools that don't take the (user_id, project_id) prefix.
UNSCOPED = {"algorithm_brief"}


def call_tool(name, user_id, project_id, args):
    """Dispatch by name with project scoping applied. Used by the runtime and MCP."""
    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    args = dict(args or {})
    if name == "get_formulas" and "levers" in args:
        args["levers_filter"] = args.pop("levers")
    try:
        if name in UNSCOPED:
            return fn(**args)
        return fn(user_id, project_id, **args)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
