# Analysis v2 — plan

Goal: make per-reel analysis and group analysis good enough to be the knowledge base for an
agent that can answer "why didn't my reel work?" and "draft me a script for X" with real
Instagram knowledge and formulas backed by data — not vibes.

Status: Phases **0, 1 and 2 are done** (see bottom). Phases 3 and 4 have been merged into
one "knowledge + agent" phase, planned in detail in **`docs/agent-plan.md`** (3a knowledge
layer = the old Phase 3; 3b–3d = the agent). §6 and §8 below are the original sketches.

---

## 1. Where we are, honestly

What exists today (branch `feat/neon-analysis-formulas`):

- Per reel: transcript → HPVC split (`analysis`) → optional vision pass (`visual`: scenes,
  hook_type, editing, on-screen text) → `compute_metrics()` (wpm, filler, ER, reshares/1k…).
- Group: per-category LLM run over chunks of 12 reels that maintains a library of free-text
  `formulas` (name, description, `template` with [blanks], evidence quotes verified against
  the reel).
- Context: `research/instagram-algorithm.md` (monthly agent-maintained brief).

What's wrong with it as a base for an agent:

| Problem | Why it matters |
|---|---|
| **Performance is confounded by creator size.** 38 reels from ~36 accounts, tiered on absolute views. No follower count captured. | "13M views" says the creator is big, not that the hook worked. Every formula drawn from it is really "things big accounts do". Nothing transfers to *your* account. |
| **Formulas are free text + a template.** No shared vocabulary across runs, projects, or niches. | The agent can't query "how do contrarian hooks perform for me?" — it can only re-read prose. Can't compute lift. Can't compare two projects. |
| **Individual analysis is thin.** HPVC split + a few stats. No diagnosis, no structured tags, no "what this reel is doing and why". | "Why didn't my reel work" needs a per-reel diagnosis grounded in the platform's known signals. Today there's nothing to point at. |
| **No notion of "my reels" vs "reference reels".** | The two questions the agent must answer are about *your* account. Own reels need own-account baselines and the creator's private insights (retention, non-follower %). |
| **Caption / hashtags not captured.** | The caption is a second hook (text hook) and the topic signal. Missing entirely. |
| Algorithm brief was never injected (placeholder missing); visual context leaked into HPVC output. | Fixed in Phase 0. |

Everything below is designed so the **same schema works for any niche, any account size, any
project** — that's what "formulas that work for anything" has to mean in practice: a fixed
vocabulary of *levers*, a size-normalised *performance vector*, and stats computed from tags,
with the LLM doing interpretation on top rather than being the only source of truth.

---

## 2. Target architecture (three layers)

```
┌──────────────────────────────────────────────────────────────────┐
│ 3. AGENT LAYER (later)                                           │
│    tools: get_reel_card · search_reels · lever_stats · formulas  │
│           algorithm_brief · own_account_baseline                 │
│    modes: Diagnose("why didn't X work")  Draft("script for Y")   │
├──────────────────────────────────────────────────────────────────┤
│ 2. GROUP ANALYSIS                                                │
│    a) deterministic lever stats (no LLM): lift per lever value   │
│    b) contrast pairs (same topic/hook type, different outcome)   │
│    c) LLM formula extraction, seeded with (a)+(b)+taxonomy,      │
│       formulas carry structured `levers` + template + evidence   │
├──────────────────────────────────────────────────────────────────┤
│ 1. INDIVIDUAL ANALYSIS ("Reel Card")                             │
│    HPVC · taxonomy tags · hook mechanics · structure · CTA ·     │
│    format · diagnosis · performance vector (size-normalised)     │
│    + creator record (followers) · caption · own-account insights │
└──────────────────────────────────────────────────────────────────┘
```

Rule: **anything the agent will reason over must be structured and queryable** (tags, numbers,
enums), with prose kept for explanation only.

---

## 3. The universal taxonomy ("levers")

A closed vocabulary the per-reel pass tags against. Every value list ends with `other`, and
the model must give a one-line `other_note` when it uses it — that's how the taxonomy grows
deliberately instead of drifting. Stored in `reels.tags` (jsonb), versioned (`taxonomy_v`).

| Dimension | Values (v1) |
|---|---|
| `hook.type` | question · bold_claim · contrarian · curiosity_gap · story_open · stat_or_number · listicle_promise · callout (you/if-you) · result_first (show the payoff) · pain_point · authority_credential · trend_or_news · challenge_or_dare · other |
| `hook.devices[]` (multi) | open_loop · specificity · negativity_or_fear · novelty · social_proof · direct_address · pattern_interrupt_visual · text_hook_on_screen · humor · other |
| `hook.seconds_to_first_payoff` | number (from visual scenes / segments) |
| `promise.type` | how_to · list · story_payoff · reveal_or_secret · comparison · warning · opinion · none · other |
| `structure.type` | listicle · story (setup→turn→payoff) · tutorial_steps · rant · review · myth_bust · reaction · demo · comparison · commentary · skit · other |
| `structure.rehook_count` | number — how many times it re-opens a loop mid-video |
| `cta.type` | none · follow · comment_keyword · save · share_send · link_in_bio · dm · watch_next · other |
| `cta.position` | none · early · mid · end |
| `format.type` | talking_head · screen_recording · voiceover_broll · text_only · vlog · interview · skit · ugc_testimonial · other |
| `format.captions` | none · auto_style · designed |
| `delivery.energy` | low · medium · high |
| `delivery.wpm_band` | <120 · 120-160 · 160-200 · 200+ (derived, not LLM) |
| `duration_band` | <15 · 15-30 · 30-60 · 60-90 · 90+ (derived) |
| `audience.awareness` | unaware · problem_aware · solution_aware · product_aware |
| `emotion.primary` | curiosity · fear_fomo · aspiration · humor · outrage · relatability · utility · awe · other |
| `topic.niche` | free text (short) + `topic.tags[]` (3–6) |
| LLM-rated 1–5 (use cautiously) | `specificity`, `novelty`, `clarity`, `hook_strength` |

Why these: they're the things a creator can *change* next time, and they map onto the signals
the platform says it ranks on (watch time ← hook/structure/pacing; sends ← emotion/utility;
likes ← relatability/clarity). They're niche-agnostic by construction.

---

## 4. The performance vector (size-normalised)

Replace the single absolute tier with a vector, all computed in `compute_metrics()`:

| Metric | Formula | Needs |
|---|---|---|
| `reach_multiple` | views / creator follower_count | follower count (Phase 1) |
| `reach_multiple_log` | log10(reach_multiple) | — |
| `sends_per_1k` | reshares / views × 1000 | exists |
| `likes_per_1k`, `comments_per_1k` | … | exists |
| `er_pct` | (likes+comments)/views | exists |
| `views_pct_in_project`, `reach_pct_in_project` | percentile rank within project | — |
| `reach_pct_in_creator` | percentile within same creator (only if ≥3 reels of theirs) | — |
| `outcome_band` | `breakout` / `above` / `baseline` / `below` from reach_multiple (defaults: ≥3× / ≥1× / ≥0.3× / <0.3×) | — |

Why reach_multiple: it's the one number that makes a 50k-follower account and a 5M-follower
account comparable. A reel that did 3× its follower count travelled to non-followers — that's
the "sends" lever working, and it's true at any size. Absolute views stay as a secondary
column (the env thresholds remain for users who want them), but the **tier the model reasons
on becomes `outcome_band`**.

For the user's **own** reels, add optional manual inputs from IG Insights (`reels.insights`
jsonb): `avg_watch_time_s`, `retention_3s_pct`, `completion_pct`, `non_follower_pct`, `saves`,
`profile_visits`, `follows`. These are the ground truth for "why didn't it work" and are
unavailable publicly — a small form on the reel page.

---

## 5. Individual analysis v2 — the Reel Card

One LLM call per reel (text + optional frames) that replaces today's HPVC-only pass and
produces a single JSON "reel card", stored across `reels.analysis` (HPVC, unchanged shape so
existing UI keeps working), `reels.tags` (taxonomy above), `reels.diagnosis`:

```json
"diagnosis": {
  "one_line": "Strong contrarian hook, payoff lands at 0:38 — too late for a 60s reel; CTA asks for a follow, not a send.",
  "what_drove_it":    ["hook.type=contrarian with specificity", "on-screen text mirrors the claim"],
  "what_held_it_back":["first payoff at 38s", "no share-worthy line", "licensed audio, low-res"],
  "signals": {"watch_time": "likely_weak", "sends": "weak", "likes": "ok"},
  "one_change": "Move the example to 0:08 and end on the 'send this to…' line.",
  "confidence": "medium"
}
```

Rules for the prompt:
- It gets: transcript (timestamped), HPVC, visual summary, the performance vector, the
  creator's follower count, the algorithm brief, project instructions, and (for own reels)
  the insights form.
- It must tag from the closed lists; `other` requires `other_note`.
- Diagnosis must tie each claim to a lever *and* a platform signal (watch time / likes /
  sends). No generic advice ("post consistently").
- Per-reel citations: every `what_drove_it` item quotes ≤12 words from the reel; verified the
  same way evidence is verified today.

UI: the reel page gets a card — tags as chips, performance vector as a small stat row with the
project percentile, the diagnosis block, and an "Insights" form for own reels.

---

## 6. Group analysis v2

### 6a. Deterministic lever stats (no LLM, recomputed live like `_tiered_rows`)

For each `(dimension, value)` with n ≥ 3 in the project:
`n`, `median reach_multiple`, `median sends_per_1k`, `median er_pct`,
`lift = median(value) / median(project)` on each, and a `confidence` from n.
Exposed as `/api/analysis/levers` and shown as a table on the Analysis page
("Hooks that travel", "CTAs that get sends", …). This is the part that *works for anything* —
it's just statistics over tags; it gets better with every reel and never hallucinates.

### 6b. Contrast pairs

Pairs of reels sharing `topic.niche` or `hook.type` whose `reach_multiple` differs by >3×.
Fed to the formula prompt as "explain this difference" — contrast is where formulas come from.

### 6c. LLM formula extraction (evolved from today's run)

Keep: per-category runs, chunking, existing-library merge, template + verified evidence.
Change:
- Seed the prompt with the lever stats and contrast pairs for that category.
- Every formula must declare `levers` (jsonb): the taxonomy values it's made of, e.g.
  `{"hook.type":"contrarian","hook.devices":["specificity"],"cta.type":"share_send"}`.
  This is what makes formulas comparable across projects and lets the agent retrieve by lever.
- Formula `confidence` = f(times_seen, evidence_count, n from lever stats, lift) — not just
  the first two.
- Replace `status working/not_working` with `effect`: `positive` / `neutral` / `negative`,
  with the lift number that justifies it.
- Default categories become the taxonomy's top-level dimensions (Hooks, Structure, CTA,
  Format, Delivery) — users can still add custom tabs.

---

## 7. Data model changes (one migration, `003_analysis_v2.sql`)

```sql
CREATE TABLE creators (
  username text PRIMARY KEY,
  follower_count int, following_count int, media_count int,
  full_name text, is_verified bool,
  fetched_at timestamptz
);
ALTER TABLE reels ADD COLUMN source text NOT NULL DEFAULT 'reference'
  CHECK (source IN ('own','reference'));          -- "my reel" vs "reel I saved"
ALTER TABLE reels ADD COLUMN caption text;
ALTER TABLE reels ADD COLUMN hashtags text[];
ALTER TABLE reels ADD COLUMN tags jsonb;           -- taxonomy, with taxonomy_v
ALTER TABLE reels ADD COLUMN diagnosis jsonb;
ALTER TABLE reels ADD COLUMN insights jsonb;       -- own-reel IG Insights, manual
ALTER TABLE projects ADD COLUMN own_username text; -- which account is "me" here
ALTER TABLE formulas ADD COLUMN levers jsonb;
ALTER TABLE formulas ADD COLUMN effect text CHECK (effect IN ('positive','neutral','negative'));
ALTER TABLE formulas ADD COLUMN lift numeric;
ALTER TABLE formulas ADD COLUMN taxonomy_v int;
CREATE INDEX reels_tags_gin ON reels USING gin (tags);
```

Fetching follower counts: `GET /api/v1/users/web_profile_info/?username=` with the same
cookie session, cached in `creators` for 7 days, one call per distinct username (36 calls for
the current library, rate-limited like stats). Caption: `item["caption"]["text"]` is already in
the media response we fetch — parse it, extract `#tags`.

Backfill: a one-off script to (1) fetch creators for existing usernames, (2) re-run the Reel
Card pass over the 38 reels (costs ~38 calls), (3) recompute lever stats (free).

---

## 8. Agent base (Phase 4 — design now, build after 1–3)

Expose the data as tools, not prose. Two ways; **recommend the MCP server** since you already
live in Claude Code / Claude Desktop and it gives you the agent for free, with the in-app chat
as a later add:

| Tool | Returns |
|---|---|
| `get_reel_card(reel_id)` | HPVC + tags + performance vector + diagnosis + visual skeleton |
| `search_reels(project, filters{tags, outcome_band, source, creator}, sort, limit)` | cards |
| `lever_stats(project, dimension?)` | the 6a table |
| `get_formulas(project, category?, min_confidence?, levers?)` | formulas with evidence |
| `own_account_baseline(project)` | own reels' median reach_multiple, sends/1k, ER, best/worst |
| `algorithm_brief()` | the researched sections + sources |
| `compare_reels(a, b)` | side-by-side cards with diffs highlighted |

Two system prompts:
- **Diagnose**: given a reel id (or URL → ingest), produce the diagnosis *against the own-account
  baseline and the project's lever stats*, citing reels and the brief. Ends with one change.
- **Draft**: given a topic/brief, pick 2–3 high-confidence positive formulas whose levers fit,
  compose HPVC using their templates, state the expected lever mix, cite the evidence reels,
  and flag the platform signals it's designed to hit.

Acceptance: a golden set of ~10 questions over the existing 38-reel project, checked by hand
("why did 7_Db5Rx2TsiYr underperform?", "draft a contrarian-hook tutorial about X").

---

## 9. Sequencing

| Phase | What | Effort | Unblocks |
|---|---|---|---|
| **0 — done** | Inject algorithm brief into formula prompt; fix visual-context leak into HPVC; re-run the 3 contaminated breakdowns | tiny | correctness |
| **1 — data foundation** | `creators` + follower fetch; caption/hashtags; `source`/`own_username`; performance vector + `outcome_band`; migration 003; backfill script | ~1 day | everything |
| **2 — Reel Card** | taxonomy module (`taxonomy.py`, versioned); Reel Card prompt + validator; `tags`/`diagnosis` storage; reel page UI; insights form | ~2 days | 3, 4 |
| **3 — Group v2** | lever stats endpoint + table; contrast pairs; formula prompt v2 with `levers`/`effect`/`lift`; confidence v2; default categories | ~2 days | 4 |
| **4 — Agent base** | MCP server over Neon (read-only tools above); Diagnose + Draft prompts; golden-question set | ~2 days | the product |

Decisions I'd like you to confirm before Phase 1 (defaults in bold if you don't):
1. Normalise by **follower count** (cheap, one fetch per creator) vs per-creator recent-reel
   median (better, but ~12 extra fetches per creator) — **followers first, creator median later**.
2. Which account is "own" for project 7 — set `projects.own_username` when you have it;
   reference-only projects are fine.
3. Agent surface: **MCP server** first, in-app chat second.

## Phase 0 — done in this branch

- `FORMULA_ANALYSIS_PROMPT_TEMPLATE` now contains `{algorithm_context}` (it was being passed
  to `.format()` but had no placeholder, so the research brief never reached the model).
- `build_analysis_prompt()` inserts the visual context *before* the `Transcript:` marker.
  The three contaminated rows had `analysis` reset to null; the app re-runs them on next load.
- **Hook run yielding 3 formulas for 36 reels** — three causes, all addressed:
  1. Formula count was anchored at a fixed "2–5 per chunk". Now scales with the chunk:
     min `ceil(n/3)`, max `n` (12 reels → 4–12), with an explicit "when in doubt split, never
     merge" and "one formula per 2–3 reels" instruction.
  2. Formulas found earlier in the same run were fed forward with `"id": null` while the model
     was told to only reuse ids from the list — later chunks' matches were silently dropped.
     They're now a separate "found earlier in this run" block and are re-emitted in `new` by
     exact name so `_collapse_new` merges them.
  3. No category-specific framing. `CATEGORY_GUIDANCE` now tells the hook run: any reel ≥
     `GOOD_VIEW_THRESHOLD` (50k) had a hook that did its job — don't debate it, catalogue its
     *shape*, and name formulas after sentence shape not topic. Script and talking tabs get
     their own framing; custom tabs a generic one.
  Re-run **Generate formulas** on the project to see the difference (costs the usual calls).

Your "≥50k views ⇒ hook is good" rule is kept as the hook-tab default in v2 as well: the
`outcome_band` in §4 is configurable per project between absolute views (your rule) and
reach-multiple (for mixed-size libraries).

## Phase 1 — done in this branch (2026-08-21)

- `migrations/003_analysis_v2.sql` applied to Neon: `creators` table; `reels.source /
  caption / hashtags / tags / diagnosis / insights`; `projects.own_username / outcome_mode`;
  `formulas.levers / effect / lift / taxonomy_v`. `schema.sql` flattened to match.
- `STATS_VERSION` 2 → 3: every stats fetch now captures caption + hashtags and the creator's
  follower count (`fetch_creator_profile` → `creators`, weekly refresh; og:description
  fallback for accounts whose JSON endpoint 400s). Backfilled: 38/38 reels have captions and
  follower counts, 36 creators cached.
- `compute_metrics()` adds `creator`, `follower_count`, `reach_multiple`, `likes_per_1k_views`,
  `comments_per_1k_views`, `source`, `caption_word_count`, `hashtag_count`.
  `_tiered_rows()` → `_annotate_outcomes()` adds `*_pct_in_project` percentiles,
  `views_pct_in_creator` (≥3 reels of one creator), `outcome_band` + `outcome_basis` per the
  project's `outcome_mode`. Formula prompt explains how to read them.
- Project modal: "Your Instagram handle" (marks own reels, re-applied on change) and "How
  'did well' is decided" (views / reach). Reel page: followers, reach multiple, "your reel"
  badge. Overview table: Outcome, Followers, Reach× columns. CSV: reshares, followers,
  reach_multiple, source, caption.
- Observed on the 38-reel library: reach multiple reorders everything — 13M views at 513×
  (26k followers) vs 6.8M at 2× (3.3M followers); several "≥50k" reels sit at 0.1× of their
  own audience. Deferred from Phase 1: per-creator recent-reel median (needs ~12 fetches per
  creator) and the own-reel Insights form (lands with the Reel Card in Phase 2).

## Phase 2 — done in this branch (2026-08-21)

- `taxonomy.py`: `TAXONOMY_VERSION = 1`, 11 closed dimensions (§3 as designed, `cta.position`
  and `format.captions` included), 4 scores, 2 numbers, 2 derived bands; `prompt_block()`,
  `clean_tags()` (drops unknown slugs, flags `other` without a note), `flat_tag_pairs()` for
  the Phase-3 stats.
- `REEL_CARD_PROMPT_TEMPLATE` + `build_card_prompt()` / `run_reel_card()` / `clean_diagnosis()`
  in app.py: one text call per reel → `reels.tags` + `reels.diagnosis`. Diagnosis quotes are
  verified against the reel's text (≤14 words, verbatim) and dropped otherwise. The prompt
  forces a number-first verdict (reach multiple, percentile, sends/1k) and bans generic advice.
- Pipeline: `card_queue` + `card_worker` (×2); chained after the breakdown in
  `analysis_worker`; picked up by `autoqueue_pending_ai` (any reel with a breakdown and no
  tags) and by `/api/analysis/prepare`; manual `POST /api/card/<rid>`.
- `PATCH /api/reels/<rid>/insights` + the Insights form on own reels (plays, reach,
  non-follower %, avg watch time, 3s retention, completion, sends, saves, profile visits,
  follows, note). Fed to the card prompt and stated to outrank public proxies.
- `/api/jobs` attaches project-relative `metrics` to every done reel (`_rank_rows` shared with
  the Analysis page); the formula batch payload carries `tags` + diagnosis one-liner.
- UI: new **Reel card** tab — outcome band, views/reach/sends/engagement with project
  percentiles, "vs own reels" percentile, tag chips by group (other → shows the note), judgement
  dots, topic, diagnosis block, build/regenerate button, Insights form for own reels.
  `ai_pending = "card"` shows "Tagging & diagnosing".
- Observed with `gpt-4o-mini`: tags are consistent and validate cleanly; diagnosis is
  number-correct after the prompt tightening but causally thin ("reach is low → it reached
  followers"). A stronger model for the card pass is the cheap fix; a per-pass model override
  in Settings is a small follow-up. Browser verification of the tab was not possible in this
  session (needs a signed-in Neon Auth session).
