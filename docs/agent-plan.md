# Phase 3 — the Reel agent (plan)

Companion to `analysis-v2-plan.md`. Phases 0–2 gave every reel a size-normalised
performance vector and a Reel Card (closed-taxonomy tags + diagnosis). Phase 3 turns that
into an agent that can **answer** ("why didn't my reel work?"), **write** ("draft me a
script about X"), **research** ("what's working in my niche?") and **act** ("analyse this
URL", "save this as a draft").

Status: **3a and 3b done** (2026-08-21) — see "Progress" at the bottom. 3c (in-app chat) and
3d (drafts/notes UI, eval set) are next.

---

## 1. What the agent has to be good at

| Mode | Example ask | What it needs |
|---|---|---|
| **Diagnose** | "Why did my reel on X flop?" | the reel's card + metrics, own-account baseline, lever stats, the 3–5 most similar reels and how they did, the algorithm brief |
| **Draft** | "Write me a reel about Y" / "…in the style of @z" | formulas with positive lift + templates + evidence quotes, lever stats, exemplar reels, my voice profile, project instructions |
| **Critique** | "Here's my script — will it work?" | tag the draft against the taxonomy, predict from lever stats, compare to exemplars |
| **Research** | "What hooks are travelling in this niche right now?" | lever stats, contrast pairs, top/bottom reels, the brief; optionally live web search |
| **Library Q&A** | "Show me breakout reels with a comment-keyword CTA" | structured search over cards |
| **Act** | "Analyse this URL", "save that script", "remember that I post at 7pm IST" | ingest pipeline, drafts, project notes |

Non-negotiables, baked into the system prompt and enforced by tools:
- **Numbers first, then levers.** Every verdict states reach multiple / percentile / sends per 1k before any craft talk.
- **Cites the library.** Claims about "what works" reference reel ids or formula ids the user can click. The UI renders those as links.
- **Knows its own limits.** If the project has < ~15 carded reels, says the stats are thin; never invents a lift.
- **No generic advice.** Same ban list as the card prompt.
- **Scripts are structured**, not prose: hook / promise / validation beats / CTA + on-screen text + caption + the levers chosen and why + the signals it's designed to hit.

---

## 2. Architecture

```
                 ┌──────────────────────────────────────────────────────────┐
                 │  KNOWLEDGE (Phase 3a — deterministic, recomputed on demand)│
                 │  lever_stats · contrast_pairs · formulas v2 (levers/lift) │
                 │  own_baseline + voice_profile · playbook (LLM summary)    │
                 │  reel cards (Phase 2) · creators · algorithm brief        │
                 └──────────────────────────────────────────────────────────┘
                                             ▲
                 ┌──────────────────────────────────────────────────────────┐
                 │  agent/tools.py  — ~14 pure functions over the DB (3b)    │
                 │  agent/schemas.py — JSON schemas for those functions      │
                 └──────────────────────────────────────────────────────────┘
                        ▲                                  ▲
        ┌───────────────┴───────────────┐   ┌──────────────┴──────────────────┐
        │ agent/mcp_server.py (3b)      │   │ agent/runtime.py (3c)            │
        │ FastMCP, stdio; Claude Code / │   │ provider-agnostic tool loop      │
        │ Desktop drive it; web search  │   │ (Anthropic tool_use / OpenAI     │
        │ comes from the host for free  │   │ tools), SSE streaming            │
        └───────────────────────────────┘   │ /api/agent/* + Chat page (3c)    │
                                            └──────────────────────────────────┘
```

One tool layer, two front doors. The **MCP server** is the fastest path to a working agent
(you're already in Claude Code; the host model brings web research); the **in-app chat** is
the product path (every account, the user's own configured provider, threads saved per
project). Both call identical functions, so behaviour doesn't drift.

---

## 3. Phase 3a — the knowledge layer (group analysis v2)

### Lever stats — `GET /api/analysis/levers`
For every `(dimension, value)` from `taxonomy.flat_tag_pairs` across the project's carded
reels, with n ≥ 3:

| field | meaning |
|---|---|
| `n` | reels with that value |
| `median_reach`, `median_sends_1k`, `median_er` | medians for the value |
| `lift_reach`, `lift_sends`, `lift_er` | value median ÷ project median |
| `breakout_rate` | share of those reels in `outcome_band ∈ {breakout, above}` |
| `confidence` | `low` n<5 · `medium` n<10 · `high` otherwise |
| `examples` | top 3 reel ids by reach for that value |

Deterministic, no LLM, recomputed per request (same pattern as `_rank_rows`). Shown on the
Analysis page as "Levers" (table, sortable by lift, filter by dimension). This is the part that
"works for anything" — it is just counting, it improves with every reel, and it never
hallucinates.

### Contrast pairs
Pairs of carded reels sharing `topic.niche` (fuzzy) or `hook.type` whose `reach_multiple`
differs by >3×, with the tag diff between them. Surfaced on the Levers page and fed to both
the formula run and the agent ("what separates these two").

### Formulas v2 (evolve the existing run)
- Prompt is seeded with lever stats + contrast pairs for its category and the reels' tags.
- Every formula must declare `levers` (e.g. `{"hook.type":"contrarian","hook.devices":["specificity"]}`);
  validated against the taxonomy.
- `effect` ∈ positive/neutral/negative and `lift` are **computed**, not asked: from the
  evidence reels' reach vs project median. `status working/not_working` is derived from
  `effect` for backward compatibility.
- `confidence` v2 = f(times_seen, evidence_count, n of the lever combo, |lift|).
- Default categories stay (hook / script / talking); lever stats cover the rest of the
  taxonomy without needing a tab each.

### Own baseline + voice profile — `own_baseline()`
From `source='own'` reels: median reach/sends/ER, best and worst 3, and a **voice profile**
(median WPM, energy, typical hook types, CTA habits, avg duration, typical on-screen text
use). Stored nowhere — computed. Empty if `own_username` unset; the agent then says so and
works from reference reels only.

### Playbook — `projects.playbook` (jsonb)
A short LLM-written summary regenerated at the end of every formula run: "what works in this
library, in this order, with numbers" (≤ 400 words, every claim tied to a lever stat or
formula id). The agent reads it first; it is the cheap context that keeps every chat from
re-deriving the library.

Migration `004_agent.sql`: `projects.playbook jsonb`, `agent_threads`, `agent_messages`,
`project_notes`, `drafts` (below).

---

## 4. Phase 3b — tools + MCP server

`agent/tools.py`, pure functions; every one takes `(user_id, project_id, …)` and returns
JSON-able dicts. `agent/schemas.py` holds the JSON schemas (shared by MCP and runtime).

| tool | args | returns |
|---|---|---|
| `get_project_overview` | — | counts (reels / carded / own), own handle, outcome mode, baseline medians, last run summary, **playbook**, top 5 / bottom 5 by reach |
| `search_reels` | `tags?`, `outcome_band?`, `source?`, `creator?`, `query?` (caption/transcript/topic text), `sort` (reach/views/sends/er/recent), `limit≤25` | compact cards: id, creator, views, reach×, band, hook line, tags subset, one_line |
| `get_reel` | `reel_id` or `url`; `include_transcript?` | full card: HPVC, tags, diagnosis, metrics, visual skeleton, caption, insights, formulas it's evidence for |
| `compare_reels` | `reel_ids[2..4]` | side-by-side metrics + tag diff + the diagnosis lines |
| `similar_reels` | `reel_id`, `limit` | nearest by tag overlap (+ topic), each with reach — the "others like it did X" evidence for Diagnose |
| `lever_stats` | `dimension?`, `min_n=3` | §3 table |
| `contrast_pairs` | `dimension?`, `limit` | §3 pairs |
| `get_formulas` | `category?`, `effect?`, `min_confidence?`, `levers?` | formulas with template, lift, confidence, evidence (reel id + filled breakdown) |
| `own_baseline` | — | §3 |
| `algorithm_brief` | `section?` | the researched sections + sources (tier, date) |
| `tag_text` | `text`, `kind` (script/hook/caption) | taxonomy tags for arbitrary text + the lever stats for those tags → a predicted-lift sketch. Used by Critique and by Draft to self-check |
| `analyze_url` | `url`, `wait_sec`, `include_transcript` | runs the full pipeline as a **scratch** reel - never persisted, never in the library/exports/lever stats - and ranks it against the library; blocks up to ~90s, `ready:false` means call again |
| `ingest_reel` | `url` | queues via the existing submit path; returns reel_id + status; agent polls `get_reel` |
| `web_research` | `query`, `recency_days?` | **optional**: only when a search key is configured (Tavily or Brave in Settings); returns title/url/snippet/date; agent must cite URLs. In MCP mode the host's own search is used instead |
| `save_note` / `list_notes` | `text` / — | project memory the user confirms ("I post at 7pm IST", "never use trend audio") — injected into the system prompt |
| `save_draft` / `list_drafts` / `get_draft` | structured script | `drafts` table; Draft mode writes here, UI renders copy-ready |

Guardrails in the layer, not the prompt: project scoping on every query; `limit` caps;
`web_research` disabled without a key; `ingest_reel` respects the same cookie/account pin as
the UI; no tool mutates reels except `ingest_reel` (create) and insights are left to the UI.
`analyze_url` creates nothing durable - `persist_reel()` skips the DB and `store_cache` for a
scratch reel, and `purge_scratch_jobs()` drops it with its media after six hours, after twenty
of them, or at the next restart.

**MCP server** `agent/mcp_server.py` — FastMCP, stdio, tools above (minus `web_research`,
the host has its own). Auth: reads `DATABASE_URL` + a `MCP_USER_ID` / `MCP_PROJECT_ID` from env
(single-user local use), or `--project <id>` flag. A `claude mcp add` one-liner in the README.
This gives you the full agent in Claude Code/Desktop in a day, with Claude's web search for the
research mode.

---

## 5. Phase 3c — in-app agent

- `agent/runtime.py`: one loop for both providers — Anthropic `tool_use` / OpenAI `tools`;
  max 8 tool rounds per turn; tool results trimmed to ~6k chars each; streams tokens and
  tool-call events over SSE. Uses the account's configured provider/key (platform fallback as
  elsewhere). The agent uses whatever provider/model Settings holds - OpenAI (`gpt-5.6-luna` by
  default) is the tested path; Anthropic and OpenAI-compatible hosts also work.
- `agent/prompts.py`: system prompt = persona + non-negotiables (§1) + tool usage rules +
  output formats per mode + project instructions + notes + the playbook + a 10-line
  project overview. Modes are not buttons — the prompt recognises the ask; the UI offers
  starter chips ("Why did … flop?", "Draft a reel about…", "What's working here?").
- Endpoints: `POST /api/agent/threads`, `GET /api/agent/threads`, `GET /api/agent/threads/<id>`,
  `POST /api/agent/threads/<id>/messages` (SSE), `DELETE`. Threads/messages persisted per
  project (`agent_threads`, `agent_messages` incl. tool calls/results for replay).
- **Chat page** (new top-level tab next to Analysis): thread list, streaming answer, tool
  chips ("searched 12 reels", "read lever stats"), reel ids rendered as links that open the
  reel, script answers rendered as a **Script card** (hook / beats / CTA / on-screen / caption
  / levers / expected signals) with Copy and Save-as-draft; drafts list with "critique this".
- Cost control: per-thread token counter shown; `max_tokens` per turn; tool results cached
  within a turn.

---

## 6. Phase 3d — drafts, notes, eval

- `drafts`: id, project, title, script jsonb (the Script card), source thread, created_at;
  `tag_text` result stored with it so a draft carries its predicted levers. "Post it" later
  closes the loop: when an own reel is ingested whose transcript matches a draft, link them and
  compare predicted vs actual (Phase 4 material, schema ready now).
- `project_notes`: id, project, text, created_by (user/agent), created_at.
- **Eval set** `tests/agent_golden.md`: ~12 questions over the current project with the
  expected shape of a good answer (cites ≥2 reels, states reach multiple, names levers, no
  banned phrases). Run by hand after each prompt change; a small script greps the transcript
  for the banned list and for reel-id citations.

---

## 7. Sequencing and effort

| Step | What | Effort | Unblocks |
|---|---|---|---|
| **3a** | lever stats + contrast pairs + Levers page; formulas v2 (levers/effect/lift, confidence v2); own baseline + voice profile; playbook; migration 004 | ~2 days | everything |
| **3b** | `agent/tools.py` + schemas; MCP server; README `claude mcp add`; first golden runs from Claude Code | ~1.5 days | a usable agent |
| **3c** | runtime (both providers, SSE) + endpoints + Chat page + Script card | ~2.5 days | the product |
| **3d** | drafts + notes UI, optional `web_research`, eval set | ~1 day | polish |

Decisions to confirm before starting (defaults in **bold**):
1. Surface order: **3a → 3b (MCP, usable from Claude Code immediately) → 3c (in-app)**, or go straight to in-app?
2. Live web research in-app: **optional, Tavily or Brave key in Settings**; MCP mode relies on the host's search. Or skip in-app research for now.
3. In-app agent model: **the account's configured provider** (OpenAI `gpt-5.6-luna` as configured). Decided: no separate agent-model setting for now.
4. Your own handle on the project — needed for Diagnose to have a baseline; without it the agent works from reference reels and says so.

---

## Progress

### 3a — done (2026-08-21)
- `levers.py`: `lever_table` (n, medians, lift_reach/sends/er, breakout_rate, confidence from n,
  examples), `contrast_pairs` (shared hook/structure/topic, >3× apart, tag diff), `own_baseline`
  + voice profile, `formula_effect` (measured lift/effect from evidence reels), `formula_confidence`
  v2, `clean_levers`, `lever_context_for_prompt`. Unit-tested on the 38-reel library.
- `GET /api/analysis/levers` (+ `project_knowledge()` shared helper); **Levers** tab on the
  Analysis page (baseline tiles, sortable/filterable lever table with lift badges and example
  reels, contrast pairs, own-account block); lift badge + lever chips on formula cards.
- Formulas v2: prompt seeded with lever stats + contrast pairs per category; `levers` required and
  validated; `effect`/`lift` measured and written; `status` follows the measured effect.
- Formula targets rebalanced (one per 3–4 reels per chunk) after a run produced one formula per
  reel; **consolidation pass** per category (`consolidate_formulas`, `POST /api/analysis/consolidate`)
  merges same-shape formulas across chunks, runs automatically at the end of generate.
- **Playbook**: `generate_playbook` → `projects.playbook`, rewritten after every run,
  `GET/POST /api/analysis/playbook`, shown at the top of Overview/Levers. First one written for
  project 7 — every claim carries its lift and n.
- Migration `004_agent.sql` applied: `projects.playbook`, `agent_threads`, `agent_messages`,
  `project_notes`, `drafts`.
- Model: account + app default switched to `gpt-5.6-luna` (per request); the OpenAI call path
  needed no changes.

### 3b — done (2026-08-21)
- `agent/tools.py`: 20 tools (§4 list plus `delete_note`/`delete_draft`), all project-scoped,
  `call_tool` dispatcher; `agent/schemas.py`: JSON schemas + OpenAI/Anthropic adapters;
  `agent/prompts.py`: `SYSTEM_PROMPT` (non-negotiables, per-mode procedures, Script card format)
  + starter prompts.
- `agent/mcp_server.py`: low-level `mcp.server.Server` (mcp 2.0 callback API — FastMCP is gone in
  2.0), stdio, tools + `reel-agent` prompt + `reel://overview` resource. Smoke-tested with an MCP
  client: all 20 tools listed, overview/lever_stats/search/similar/formulas calls return real
  data. `tag_text` verified on a draft (tags + predicted-lift sketch).
- Not done: `claude mcp add` registration itself (it's the user's config — command is in the README),
  `web_research` keys (env-based, optional).

### 3c — done (2026-08-21)
- `agent/runtime.py`: provider-neutral turn loop as a generator of events (status / tool_call /
  tool_result / message / usage / error). OpenAI GPT-5-family → `/v1/responses` (function tools +
  reasoning; `previous_response_id` chains tool rounds — `/chat/completions` refuses tools on
  `gpt-5.6-luna` unless reasoning is off), other OpenAI-compatible → `/chat/completions`, Anthropic →
  `/v1/messages` `tool_use`. 8 rounds max, 7k chars per tool result, last 40 rows replayed. Persists
  user / assistant(+tool_calls) / tool(+results) rows to `agent_messages`; system prompt = operating
  instructions + live project snapshot + playbook + notes + instructions.
- Endpoints: threads CRUD, SSE `POST /api/agent/threads/<id>/messages`, drafts, notes.
- **Agent** page: thread list, streamed turn with tool chips, light markdown, reel-id and `[F12]`
  links, starter prompts, Saved drafts as Script cards with copy, composer (Enter to send).
- **Ask-agent drawer** (vidIQ-style): floating button on every signed-in page opens a slide-over
  sharing the page's conversations; when a reel is open its id/creator/numbers/hook go into the
  system prompt for that turn ("this reel" resolves), with quick prompts. Verified a context turn
  ("what would you change about this reel?") resolving to the open reel without an id in the text.
- Verified: a real Diagnose turn on project 7 (`gpt-5.6-luna`) — 4 rounds, 10 tool calls, 43 s,
  numbers-first answer citing reels and formulas; thread replay endpoints return the persisted rows.
  UI not browser-verified in this session (needs a signed-in Neon Auth session).
- Observed on the formula re-run: consolidation merged 7 groups; hooks remain 36 single-reel
  templates (36 different creators → genuinely different sentence shapes); script/talking/CTA got
  multi-reel formulas with measured effects. The aggregate hook view is the Levers tab.

### Research (2026-08-21)
- OpenAI hosted `web_search` added to the Responses-API calls: searches surface as `web_search`
  chips, `url_citation` annotations become a Sources list under the answer. Verified on a real
  "research this and complete the script" prompt: it found the July 2026 OpenAI/Hugging Face
  disclosure, the AISI cheating finding, and wrote a sourced Script card (87 s, 4 rounds).
- `web_research` now per-account: Settings → **Agent web research** (Tavily / LangSearch / Brave +
  key; platform env keys as fallback; migration 005 adds `settings.search_provider/search_api_key`).
  Prompt now says: fact-check a claim before writing on it; hedge what can't be verified.

### 3d — remaining
- Eval set (`tests/agent_golden.md`), notes UI on the Agent page (API exists), "Save as draft"
  button on an answer (today the agent saves on request via `save_draft`), token streaming of
  the final answer.
