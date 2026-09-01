"""JSON schemas for the agent tools - one source of truth for the MCP server
and the in-app runtime (Anthropic `tools` / OpenAI `functions` both take JSON
Schema). Descriptions are written for the model: what the tool is for and
when to reach for it, not how it is implemented."""

import taxonomy

_DIM_NAMES = ", ".join(taxonomy.DIMENSIONS)
_TAGS_FILTER = {
    "type": "object",
    "description": (f"Filter on taxonomy tags, e.g. {{\"hook.type\": \"contrarian\", \"cta.type\": [\"share_send\", \"none\"]}}. "
                    f"Dimensions: {_DIM_NAMES}, delivery.wpm_band, duration_band. A reel matches when every given "
                    "dimension matches (any listed value)."),
    "additionalProperties": True,
}

TOOL_SCHEMAS = [
    {
        "name": "get_project_overview",
        "description": ("START HERE for any question about the library. Returns project settings, counts, the "
                        "library baseline (median reach multiple, views, sends/1k, engagement), the own-account "
                        "baseline if any, top/bottom reels by reach, the last formula run, the PLAYBOOK (number-backed "
                        "summary of what works here), saved notes, and a data caveat when stats are thin."),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "search_reels",
        "description": ("Find reels in this project by tags, outcome band, source (own/reference), creator handle "
                        "or free text (caption/transcript/topic). Returns compact cards (id, creator, views, "
                        "reach multiple, sends/1k, outcome band, hook line, key tags, diagnosis one-liner). "
                        "Use it to gather evidence before making claims."),
        "input_schema": {
            "type": "object",
            "properties": {
                "tags": _TAGS_FILTER,
                "outcome_band": {"type": "string", "enum": ["breakout", "above", "baseline", "below"]},
                "source": {"type": "string", "enum": ["own", "reference"]},
                "creator": {"type": "string", "description": "Instagram handle, with or without @"},
                "query": {"type": "string", "description": "substring to look for in transcript, caption, topic, diagnosis"},
                "sort": {"type": "string", "enum": ["reach", "views", "sends", "engagement", "recent"], "default": "reach"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 15},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_reel",
        "description": ("Everything about one reel: metrics with project percentiles and outcome band, the "
                        "hook/promise/validation/cta breakdown, taxonomy tags, the diagnosis (what drove it, what "
                        "held it back, one change), caption, visual skeleton (scenes, on-screen text), the owner's "
                        "Insights if entered, and which formulas cite it. Pass reel_id (from search results) or a URL. "
                        "Set include_transcript for the full timestamped script."),
        "input_schema": {
            "type": "object",
            "properties": {
                "reel_id": {"type": "string"},
                "url": {"type": "string"},
                "include_transcript": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "similar_reels",
        "description": ("Reels most like a given one by tag overlap and topic, each with its reach - the 'others "
                        "that did the same thing got X' evidence for a diagnosis."),
        "input_schema": {
            "type": "object",
            "properties": {"reel_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}},
            "required": ["reel_id"], "additionalProperties": False,
        },
    },
    {
        "name": "compare_reels",
        "description": "Side-by-side of 2-4 reels: compact cards, every tag difference between each pair, and their diagnoses.",
        "input_schema": {
            "type": "object",
            "properties": {"reel_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4}},
            "required": ["reel_ids"], "additionalProperties": False,
        },
    },
    {
        "name": "lever_stats",
        "description": ("The deterministic table: for every taxonomy value with n >= min_n reels, median reach / "
                        "sends / engagement, LIFT over the library median, breakout rate, confidence (from n), and "
                        "top example reel ids. This is counted, not modelled - cite it. Optionally one dimension "
                        f"only (one of: {_DIM_NAMES}, delivery.wpm_band, duration_band)."),
        "input_schema": {
            "type": "object",
            "properties": {"dimension": {"type": "string"}, "min_n": {"type": "integer", "minimum": 2, "default": 3}},
            "additionalProperties": False,
        },
    },
    {
        "name": "contrast_pairs",
        "description": ("Pairs of reels that share a hook type / structure / topic but landed >3x apart on reach, "
                        "with the tag differences between them. Where 'what separates a hit from a miss' comes from."),
        "input_schema": {
            "type": "object",
            "properties": {"dimension": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_formulas",
        "description": ("The formula library: reusable patterns with a fill-in-the-blank template, the taxonomy "
                        "levers they are made of, a MEASURED lift (median reach of cited reels / library median), "
                        "effect, confidence, and evidence (reel ids with the template filled in from that reel). "
                        "Ranked by confidence, then lift. Filter by category (hook / script / talking / "
                        "cta_how_it_ends), levers, min_confidence. Avoid filtering by effect when drafting: a "
                        "formula that cites one reel is 'neutral' by definition, so effect=positive returns almost "
                        "nothing. If nothing matches, filters are relaxed one at a time and the result names them "
                        "in relaxed_filters. Build scripts from these: fill the template."),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "effect": {"type": "string", "enum": ["positive", "neutral", "negative"]},
                "min_confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "levers": _TAGS_FILTER,
                "limit": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "own_baseline",
        "description": ("The user's own reels (project handle): medians, best/worst, and a voice profile (wpm, "
                        "duration, hook/structure/CTA habits, energy, format). n=0 when no own reels - say so."),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "algorithm_brief",
        "description": ("The researched brief on how Instagram distributes Reels (ranking signals, suppression, "
                        "format guidance, community reports, open questions), with its date and sources. Platform "
                        "background, not data about these reels - use it to explain WHY a lever plausibly works."),
        "input_schema": {"type": "object", "properties": {"section": {"type": "string"}}, "additionalProperties": False},
    },
    {
        "name": "tag_text",
        "description": ("Taxonomy tags for any text (a draft, a hook, a caption) plus this library's lever "
                        "stats for those tags - a predicted-lift SKETCH (clearly caveated) - AND shape_check: the "
                        "text measured in words and seconds (hook length, script length, sentence length, CTA "
                        "position, you-density) against the medians and middle half of the library, its top "
                        "quartile by reach, and the creator's own reels. Use it to critique a draft or self-check "
                        "one you wrote; cite the shape rows that come back ABOVE / BELOW."),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "kind": {"type": "string", "enum": ["script", "hook", "caption"], "default": "script"}},
            "required": ["text"], "additionalProperties": False,
        },
    },
    {
        "name": "analyze_url",
        "description": ("Diagnose ANY Instagram reel URL WITHOUT adding it to the project. Downloads, transcribes "
                        "and runs the full breakdown + Reel Card, ranks it against this library (reach multiple, "
                        "percentile, sends per 1k, outcome band), then throws the working copy away - it never "
                        "enters the library, the exports or the lever stats. USE THIS for 'why did this reel flop / "
                        "work?' about a link that isn't saved, which is the normal case. Blocks up to ~90s; if it "
                        "comes back with ready=false, call it again with the same url to keep waiting. Use "
                        "ingest_reel instead only when the creator explicitly wants the reel KEPT."),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"},
                           "wait_sec": {"type": "integer", "minimum": 5, "maximum": 170,
                                        "description": "How long to block waiting for the pipeline. Default 90."},
                           "include_transcript": {"type": "boolean", "default": True}},
            "required": ["url"], "additionalProperties": False,
        },
    },
    {
        "name": "ingest_reel",
        "description": ("SAVE an Instagram reel URL into this project permanently - download, transcribe, frames, "
                        "breakdown and Reel Card all run unattended, and the reel joins the library, the exports "
                        "and the lever statistics. Only for reels the creator wants KEPT as a reference; to just "
                        "answer a question about a link, use analyze_url. Returns the reel id; poll get_reel until "
                        "status is done (usually 1-3 minutes)."),
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False},
    },
    {
        "name": "web_research",
        "description": ("Live web search (only if a search key is configured on the server). Treat results as "
                        "Tier 2/3 evidence and cite URLs. Prefer algorithm_brief and the library for anything they cover."),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "recency_days": {"type": "integer"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 6}},
            "required": ["query"], "additionalProperties": False,
        },
    },
    {
        "name": "save_note",
        "description": ("Remember something durable about this project/creator (posting time, constraints, "
                        "preferences, a confirmed learning). Only after the user states or confirms it - never "
                        "your own speculation. Notes are shown to you in every future conversation."),
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
    },
    {
        "name": "list_notes",
        "description": "All saved project notes.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "delete_note",
        "description": "Delete a project note by id (only when the user asks).",
        "input_schema": {"type": "object", "properties": {"note_id": {"type": "integer"}}, "required": ["note_id"], "additionalProperties": False},
    },
    {
        "name": "save_draft",
        "description": ("Save a script you wrote as a structured Script card so the user can find, copy and later "
                        "compare it against the posted reel. script = {hook, promise, beats: [..], cta, "
                        "on_screen_text: [..], caption, levers: {...}, expected_signals: {...}, why, "
                        "duration_target_sec, format, notes}. Pass predicted_tags from tag_text when you ran it."),
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "script": {"type": "object"}, "predicted_tags": {"type": "object"}},
            "required": ["script"], "additionalProperties": False,
        },
    },
    {
        "name": "list_drafts",
        "description": "Saved drafts (id, title, hook, dates).",
        "input_schema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}}, "additionalProperties": False},
    },
    {
        "name": "get_draft",
        "description": "One saved draft in full.",
        "input_schema": {"type": "object", "properties": {"draft_id": {"type": "integer"}}, "required": ["draft_id"], "additionalProperties": False},
    },
    {
        "name": "delete_draft",
        "description": "Delete a draft by id (only when the user asks).",
        "input_schema": {"type": "object", "properties": {"draft_id": {"type": "integer"}}, "required": ["draft_id"], "additionalProperties": False},
    },
]

SCHEMAS_BY_NAME = {s["name"]: s for s in TOOL_SCHEMAS}


def openai_tools():
    """Same schemas in OpenAI's chat-completions `tools` shape."""
    return [{"type": "function", "function": {"name": s["name"], "description": s["description"],
                                               "parameters": s["input_schema"]}} for s in TOOL_SCHEMAS]


def anthropic_tools():
    return [{"name": s["name"], "description": s["description"], "input_schema": s["input_schema"]} for s in TOOL_SCHEMAS]
