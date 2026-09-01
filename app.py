import base64
import collections
import csv
import glob
import hashlib
import io
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool
import requests
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, request, send_from_directory

load_dotenv()

from auth import NEON_AUTH_BASE_URL, require_login  # noqa: E402 - after load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
VIDEOS_DIR = DATA_DIR / "videos"

# Maintained by the monthly research agent (research/instagram-algorithm.agent.md),
# not by this app. Read fresh whenever it changes on disk - a research run should
# take effect on the next analysis without a restart.
ALGORITHM_BRIEF_FILE = BASE_DIR / "research" / "instagram-algorithm.md"

import taxonomy  # noqa: E402  - the closed tag vocabulary for the Reel Card pass
import levers    # noqa: E402  - deterministic group stats over the cards (lifts, contrast pairs, baselines)

# Sections fed to the AI, in this order. Sources/Superseded/Change log are
# deliberately excluded: the model doesn't need citations to reason with, and
# feeding it superseded claims alongside current ones invites it to cite the
# wrong one.
ALGORITHM_PROMPT_SECTIONS = ["Current model", "Ranking signals", "What suppresses reach",
                             "Format guidance", "Community signal", "Open questions"]
# Generous enough that a brief written to the agent's ~700-word budget survives
# whole; the cut lands on a line boundary so a runaway file degrades into fewer
# complete bullets rather than a sentence that stops mid-word.
ALGORITHM_PROMPT_LIMIT = 6000
DATABASE_URL = os.environ["DATABASE_URL"]

# Optional platform-wide fallback keys, used only when a user hasn't configured
# their own in Settings. With open self-serve signup this means every account
# can use the app with zero setup, at the cost of every account's usage being
# billed to whoever owns these keys - there's no per-account spend cap on this
# yet (app_users.transcription_quota exists as a hook for one, unused so far).
PLATFORM_API_KEY = os.environ.get("PLATFORM_API_KEY", "")
PLATFORM_GROQ_API_KEY = os.environ.get("PLATFORM_GROQ_API_KEY", "")

MIN_BULK_REELS = 6

# Reels per AI call when generating formulas. Small enough that the model can
# genuinely read every reel in the call and account for it, rather than skimming
# a forty-reel dump and citing the two it happened to notice.
FORMULA_CHUNK_SIZE = 12

# Absolute engagement thresholds (likes+comments composite), not relative rank -
# an "amazing" reel today should still read as amazing next month regardless of
# what else is in the batch. Tune freely; every reel always gets one of the three
# tiers and all of them are shown to the AI - nothing is excluded from analysis.
# Tiers are absolute and view-based, because views are the outcome the platform
# actually decides: a reel either travelled or it didn't. Two earlier attempts
# were both wrong. Fixed thresholds on likes+comments put 34 of 37 reels in
# "amazing" - no contrast at all. Ranking on engagement rate instead inverted
# reality, labelling a 13.3M-view reel "good" and a 46k-view reel "amazing",
# because rate measures how well a reel converted the reach it got, not how much
# reach it got. Rate is still reported per reel as a separate quality signal; it
# just isn't what decides the tier.
#
# Set these to whatever "did well" means for the accounts you track - a 50k floor
# suits large creators and would flatter a small one.
GOOD_VIEW_THRESHOLD = int(os.environ.get("GOOD_VIEW_THRESHOLD", 50_000))
AMAZING_VIEW_THRESHOLD = int(os.environ.get("AMAZING_VIEW_THRESHOLD", 1_000_000))

# Fallback for reels whose view count could not be read (no session, or the
# creator hid their counts). Likes+comments is a poor proxy for reach, so these
# reels are tiered among themselves and labelled as such in the batch.
GOOD_ENGAGEMENT_THRESHOLD = 1000
AMAZING_ENGAGEMENT_THRESHOLD = 2000

# Storyboard frames are pulled locally with ffmpeg the moment a reel finishes
# downloading - that costs nothing, so every reel gets them. The vision pass over
# those frames does cost API tokens, so it stays opt-in (per reel, or in bulk from
# the Analysis page) rather than running automatically.
FRAME_MAX = 12          # upper bound on frames per reel, whatever its length
FRAME_MIN_GAP_SEC = 1.0  # never sample closer together than this
FRAME_WIDTH = 480        # stored width; enough for the grid and for vision at low detail
VISION_MAX_FRAMES = 10   # frames actually sent to the model per visual analysis

DOWNLOAD_WORKERS = 3
# Instagram hands out the same reel under two URL shapes: the bare
# instagram.com/reel/<code>/ and the profile-scoped
# instagram.com/<username>/reel/<code>/ that the app's share sheet produces.
# The username segment is optional and guarded by a lookahead so "reel/" or
# "p/" can never be swallowed as a username. Group 1 is the shortcode, which is
# the whole identity of the post - reel_id_for() reads it from here.
INSTAGRAM_URL_RE = re.compile(
    r"instagram\.com/(?:(?!reels?/|p/|tv/)[A-Za-z0-9_.]+/)?(?:reels?|p|tv)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# Instagram serves an empty media response for anything it decides needs a login
# (age-gated, private-ish, or just rate-limited by IP), and yt-dlp surfaces that
# as "Instagram sent an empty media response". The fix is authenticated cookies.
# Two ways to supply them, checked in this order:
#   YTDLP_COOKIES_FILE            - path to a Netscape cookies.txt export
#   YTDLP_COOKIES_FROM_BROWSER    - e.g. "chrome", "firefox", "chrome:Profile 1"
# With neither set, data/cookies.txt is used if it happens to exist. Reading from
# a browser only works where that browser is installed, so a deployed instance
# wants the file; local dev can use either.
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "")
YTDLP_COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "")
DEFAULT_COOKIES_FILE = DATA_DIR / "cookies.txt"

# Which Instagram account this app is allowed to act as, as the numeric id in the
# `ds_user_id` cookie. Reading cookies live from a browser means the app follows
# whoever happens to be logged in there - so if you deliberately use a throwaway
# account for this, pin it here and the app refuses to touch Instagram at all
# under any other account rather than silently switching to your main one.
# Leave unset to accept whatever session the browser has.
INSTAGRAM_ACCOUNT_ID = os.environ.get("INSTAGRAM_ACCOUNT_ID", "").strip()

DEFAULT_SETTINGS = {"provider": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-5.6-luna",
                     "api_key": "", "groq_api_key": "",
                     # Agent web research: 'tavily' | 'langsearch' | 'brave' + key. Blank =
                     # platform/env key if any, else only OpenAI's hosted web_search.
                     "search_provider": "", "search_api_key": ""}
PLATFORM_SEARCH_KEYS = {"tavily": os.environ.get("TAVILY_API_KEY", ""),
                        "langsearch": os.environ.get("LANGSEARCH_API_KEY", ""),
                        "brave": os.environ.get("BRAVE_API_KEY", "")}

# Seeded into every new project. Projects own their category tabs from then on -
# renaming or deleting one here only affects the project it belongs to.
DEFAULT_CATEGORIES = [("hook", "Hooks", 1), ("script", "Scripts", 2), ("talking", "Talking Style", 3)]

ANALYSIS_PROMPT = """You are analyzing the transcript of a short-form video (an Instagram Reel). Split it into four parts and respond with ONLY a strict JSON object with these exact keys: hook, promise, validation, cta.
{project_context}

- hook: the opening line(s) that grab attention
- promise: the build-up/tease of what's coming next (empty string "" if there isn't one)
- validation: the main body that delivers the content/value
- cta: the call to action at the end (empty string "" if there isn't one)

Use the transcript's own wording for each part verbatim - don't summarize, paraphrase, or add commentary. Every part of the transcript should be accounted for across the four fields, in order, with no overlap.

Transcript:
"""

FORMULA_ANALYSIS_PROMPT_TEMPLATE = """You are maintaining a library of reusable content formulas extracted from a batch of short-form video (Instagram Reel) transcripts, each already split into hook, promise, validation and cta.
{project_context}
THIS RUN COVERS ONE CATEGORY ONLY: "{category}" ({category_description}). Ignore patterns that belong to other categories - they are handled by their own runs. Everything you return must be a {category} formula.

You are looking at {reel_count} reels{chunk_note}. Each carries:
- engagement data: likes, comments, and where available views, reshares, engagement rate (likes+comments as a percentage of views) and reshares per 1,000 views. Prefer RATES over raw totals - raw counts mostly rank reels by how big the creator already is, which no formula can explain. Reshares per 1k views is the closest thing here to the "sends" signal the platform weights most for reaching people who don't follow the account.
- delivery metrics: words per minute, sentence length, filler-word density, direct-address density, question count, number/stat mentions, hook length, and how far into the video the CTA lands.
- production and distribution facts: duration, resolution (`is_full_hd` - low-resolution reels are documented to get less reach), original audio vs a licensed track, collab and paid-partnership flags, cross-posting, and posting time (`posted_dow`, `posted_hour_utc` - UTC, and the creator's timezone is unknown, so treat timing patterns as weak unless very stark).
- a `visual` object on reels that have had a vision pass: the scene-by-scene structure of the video, how it was shot and edited, and its on-screen text. That is a direct read of the video, so use it - it shows things a transcript cannot, like how many beats a reel runs, how long the hook holds before the first payoff, and what goes on screen as text. Reels without it simply haven't had it run; absence is not evidence.

TIERS ARE ABSOLUTE AND BASED ON VIEWS - how far the reel actually travelled. "amazing" is {amazing_views:,}+ views, "good" is {good_views:,}+, "low" is below {good_views:,}. A batch can be all one tier; that is a real finding, not a problem to correct for. `rank_in_batch` gives each reel's position by views.

SIZE MATTERS, SO READ THE NORMALISED NUMBERS TOO. Each reel carries its creator (`creator`), their `follower_count`, and `reach_multiple` = views / followers. Raw views mostly say how big the account already is; reach_multiple says how far past its existing audience the reel travelled - a 3x reel from a 40k account and a 3x reel from a 4M account did the same thing, and a 0.2x reel from a huge account did NOT "work" no matter how large its view count. `outcome_band` (breakout / above / baseline / below, with `outcome_basis` saying whether it was decided on views or on reach_multiple) is the band to reason with when it is present; `*_pct_in_project` fields are percentiles within this library, and `views_pct_in_creator` is the reel's rank among the same creator's other reels when we have three or more. Reels marked `source: "own"` are the user's own account - weigh what separates those from the reference reels. Missing follower counts simply haven't been fetched; never treat that as zero.

Engagement rate is a SEPARATE signal and must not be confused with the tier. It measures how well a reel converted the reach it got, not how much reach it got - a reel can convert beautifully to a small audience or poorly to a huge one, and both are informative. When a reel has high views and low rate, or the reverse, that contrast is worth a formula in itself. Reels with no view count are tiered on likes+comments instead and say so in `ranked_on`; do not compare their tier against a view-tiered reel's.

{algorithm_context}
WHAT THE NUMBERS ALREADY SAY. Every reel below carries `tags` - its read against a fixed taxonomy (hook.type, hook.devices, structure.type, cta.type, format.type, ...). These lever statistics were computed over the whole library before this call; use them to decide which patterns are worth a formula and to phrase formulas in the same vocabulary, and cite the numbers in `reason` when they support a formula:
{lever_context}

Your job: find recurring {category} formulas - reusable patterns phrased as "this person did X", not one-off observations about a single reel.
{category_guidance}
HOW MANY: return {min_formulas}-{max_formulas} formulas - roughly one for every three or four reels. Work like this: first group the batch's {category} patterns by SHAPE (the sentence skeleton once the specifics are blanked out), then write one formula per group. A group of two or more reels is a formula. A single-reel group is a formula only when the shape is clearly reusable by someone else - and no more than a third of what you return may be single-reel. The two failure modes, both seen on real runs: collapsing genuinely different shapes into one vague formula ("Bold claim hook" covering everything), and the opposite, one formula per reel with nobody grouped. "Here are [n] [things]" and "[n] [things] nobody tells you about [topic]" are two shapes; "Here are 5 tools" and "Here are 3 habits" are one. Only return fewer than {min_formulas} if the batch genuinely does not contain that many distinct shapes, and say so in your caveats if you do.

COVERAGE IS REQUIRED. Work through the batch reel by reel - all {reel_count} of them, not just the ones that come to mind. Every reel must end up either cited as evidence on at least one formula, or listed in "unmatched" with a one-line reason. A run that cites eight reels and silently ignores the rest has not done the job.

REQUIRED on every formula, matched or new: a "template" - one literal, directly reusable sentence written the way a creator would jot it in a swipe file, with 2-4 [bracketed] blanks marking what changes each time. NOT an abstract description - it must read like an actual line someone could copy, fill in and post. Name each blank with a short CONCRETE noun phrase (e.g. [number], [tool name], [surprising outcome], [common mistake]) - never vague category words like [Hook] or [Statement]. Example: "Here are [number] [things] that [surprising specific outcome]" - NOT "Number + Promised outcome".

Then for every reel you cite, REQUIRED: a "breakdown" filling that SAME template's blanks with that reel's actual words - an array of exactly as many {{"label", "text"}} pairs as the template has [blanks], in the same order, where "label" matches a bracket name exactly and "text" is VERBATIM wording from that reel.

Quotes are checked. Each "text" is matched against that reel's own transcript after the fact, and citations whose wording is not found in the reel they name are discarded - so quote what the reel actually said rather than what it should have said, and never attach one reel's words to another reel's id. If a reel fits the pattern but you cannot quote it, leave it out.

Worked example of one complete entry:
{{"formula_id": 7, "rating": 4, "status": "working", "reason": "Numbered lists set expectations and make viewers commit to watching all N items.", "template": "Here are [number] [things] that [surprising specific outcome]", "evidence": [{{"reel_id": "abc123", "breakdown": [{{"label": "number", "text": "five tools"}}, {{"label": "things", "text": "tools"}}, {{"label": "surprising specific outcome", "text": "actually replaced my job"}}]}}]}}

You are extending an EXISTING library, not starting fresh. Formulas already recorded in this category (id, name, description, template):
{existing_formulas}

Formulas found EARLIER IN THIS SAME RUN, from other batches of the same library - they have no id yet (name, description, template):
{found_this_run}

Re-scan EVERY reel in the batch against each formula's template above and list ALL reels that genuinely match it, old or new - this is a merge into what is recorded, not a replacement. Only reuse an existing formula_id when the template would be the SAME sentence, near word-for-word, with different blanks filled - not merely "the same trick" in the abstract. Never invent a formula_id that is not in the recorded list. When a reel matches a formula found earlier in this run (no id), put it in "new" again using that formula's EXACT name and template - it is merged by name. Do not just re-confirm the existing library unchanged; actively look for patterns not already listed.

Every formula, matched or new, also carries "levers": the taxonomy values it is made of, as an object keyed by dimension - e.g. {{"hook.type": "contrarian", "hook.devices": ["specificity", "direct_address"], "cta.type": "share_send"}}. Use only dimensions and slugs that appear in the reels' own `tags`; one to four entries. This is what lets the formula be counted against the lever statistics above.

Respond with ONLY a strict JSON object with these exact keys:
- matched: array of {{"formula_id": <int from the list above>, "rating": 1-5, "status": "working"|"not_working", "reason": why, updated for this run, "template": "..." (REQUIRED), "levers": {{...}} (REQUIRED), "evidence": [{{"reel_id": <id from this batch>, "breakdown": [...] (REQUIRED)}}]}}
- new: array of {{"name": short label for scanning a list (e.g. "Countdown Hook"), "description": the reusable pattern in plain English, "reason": why it likely works or does not, "rating": 1-5, "status": "working"|"not_working", "template": "..." (REQUIRED), "levers": {{...}} (REQUIRED), "evidence": [{{"reel_id": <id>, "breakdown": [...] (REQUIRED)}}]}}
- unmatched: array of {{"reel_id": <id>, "reason": "one line on why no {category} formula fits it"}} - every reel not cited above must appear here
- summary: 2-4 sentence plain-English takeaway about {category} in this batch
- caveats: 1-3 sentences on the limits of this analysis

Batch data - every reel in the library, tagged with its relative tier (JSON):
{batch}
"""

BACKFILL_TEMPLATE_PROMPT_TEMPLATE = """Each formula below is an already-confirmed content pattern - you do not need to judge whether it's real.
{project_context} Your ONLY job: for each one, write a "template" - one literal, directly reusable sentence with 2-4 [bracketed] blanks, written the way a creator would jot it in a swipe file (not an abstract description). Name each blank with a short concrete noun for what changes there (e.g. [number], [tool name], [surprising outcome]) - never vague words like [Hook] or [Statement]. Then, using the reel text already attached to each formula below, fill that same template's blanks in with each reel's actual words as a "breakdown": an array of exactly as many {{"label", "text"}} pairs as the template has blanks, using verbatim wording (not a paraphrase). Do this for every formula listed and every reel attached to it - none should be skipped.

Formulas needing a template, each with the reel data already tied to it (JSON):
{formulas}

Respond with ONLY a strict JSON object: {{"formulas": [{{"formula_id": <id from above>, "template": "...", "evidence": [{{"reel_id": <id>, "breakdown": [{{"label": "...", "text": "..."}}]}}]}}]}}
"""

VISUAL_PROMPT_TEMPLATE = """You are breaking down a single short-form video (an Instagram Reel) into its scenes and its structure, so the breakdown can later be compared against other reels. You get {frame_count} still frames sampled in order from the video (attached, in order), the video's spoken transcript, and its total duration - {duration} seconds.
{project_context}

The attached frames are at these timestamps in seconds: {timestamps}

Work from BOTH inputs. The transcript tells you what is said and when; the frames tell you what is on screen - framing, setting, on-screen text, cuts, energy. Never restate the transcript as your answer, and never describe a visual you cannot actually see in a frame. If something is ambiguous (a cut vs. a camera move, text you can't fully read), say so plainly instead of inventing detail. Quote on-screen text verbatim.

Split the video into its scenes - the beats a creator would name while planning it - NOT one scene per frame and NOT one per sentence. Most reels have 3-7. The scenes must be in order, must not overlap, and must together cover 0 to {duration} seconds with no gaps.

Name each beat after what actually happens in THIS video. Reels come in many shapes and the names must follow the shape you are actually looking at:
- a numbered tips video has Tip beats
- a rant or list of grievances has one beat per grievance, named for it ("SD cards", "Right to repair", "Seed patents")
- a story has setup, turn, payoff
- a demo or tutorial has the steps, named for the step
- a review has the verdicts; a myth-bust has claim then debunk; a reaction has the thing then the reaction
The "label" is what a person sees on a chip in a timeline, so it has to say something about THIS video. Every label must contain at least one concrete noun from the video's own subject matter.

BANNED as labels, with no exceptions - these describe position, not content: Intro, Introduction, Opening, Hook, Body, Main Content, Main Point, Point 1, Part 1, Section 1, Tip 1 (unless the creator is literally counting off numbered tips), Conclusion, Outro, Ending, Wrap Up, Call to Action, CTA, Summary, Final Thoughts.

Put the function in "title" instead, paired with the content - "Hook - SD cards taken away", "Ask - what did they take from you". That way the beat's role is still recorded without the chip going generic.

Before you answer, test each label: if it would fit a completely different video on a completely different topic, it has failed - rename it after what is specifically happening in that stretch. If two of your labels could be swapped without anyone noticing, neither is specific enough yet.

Respond with ONLY a strict JSON object with these exact keys:
- summary: 2-4 sentences on what happens in the video, start to finish, covering both what is said and what is shown
- hook_type: 2-4 word label for the kind of opening this is (e.g. "Insider secret", "Bold claim", "Myth bust", "Before/after", "Direct question")
- visual_opening: short phrase for what the first seconds put on screen (e.g. "Talking head, close-up, bold on-screen captions")
- visual_hook: 1-2 sentences on why what's on screen in the opening would stop a scroll
- why_it_works: 1-2 sentences on why this video holds attention, grounded in the specific structure and visuals above - not generic advice
- key_topics: array of 3-6 short topic labels covered in the video
- setting: where this appears to be shot and how it is lit (1 sentence)
- subject: who or what is on screen, framing (selfie/handheld/tripod, close-up/wide), wardrobe or props that matter (1-2 sentences)
- editing: cut rhythm, camera movement, zooms, b-roll, overlays, caption style - how it is put together (1-2 sentences)
- on_screen_text: array of strings, each a piece of text visible in the frames, quoted verbatim, in order (empty array if there is none)
- scenes: array, in order, of:
    {{"label": "1-3 word chip name for this beat, specific to this video's content",
      "title": "the same beat named more fully, still specific - never a generic placeholder",
      "start": <seconds>, "end": <seconds>,
      "goal": "what this beat is trying to do to the viewer, e.g. Stop the scroll",
      "beat": "one sentence on what actually happens here, said and shown",
      "description": "1-2 sentences on what is VISIBLE in this stretch and how it changed from the previous beat"}}

Transcript (timestamped where available):
{transcript}
"""


REEL_CARD_PROMPT_TEMPLATE = """You are tagging ONE short-form video (an Instagram Reel) against a fixed vocabulary and diagnosing how it performed, so it can be compared with every other reel in a library and so an assistant can later answer "why did this work / not work" about it.
{project_context}
{taxonomy}

Respond with ONLY a strict JSON object with exactly two keys, "tags" and "diagnosis".

"tags": an object with one key per taxonomy dimension above (exact key names, e.g. "hook.type"), plus "topic.niche", "topic.tags", "scores" (object of the four 1-5 integers) and "numbers" (object of the two integers). Use ONLY the listed slugs. When nothing fits, use "other" AND add the reason in "other_notes": {{"<dimension>": "<what it actually is, 3-10 words>"}}. Tag what the reel actually does, not what it is about: a video about hooks is not automatically a "curiosity_gap" hook.

"diagnosis": {{
  "one_line": "one sentence: what this reel is doing and how that squares with its numbers",
  "what_drove_it": [{{"point": "one specific thing that helped, tied to a lever", "lever": "<dimension>=<slug> or a metric name", "quote": "<=12 words VERBATIM from the transcript or on-screen text, or null"}}],
  "what_held_it_back": [same shape],
  "signals": {{"watch_time": "strong|ok|weak|unknown", "likes": "strong|ok|weak|unknown", "sends": "strong|ok|weak|unknown"}},
  "one_change": "the single most valuable change for next time, concrete enough to act on",
  "confidence": "low|medium|high"
}}

How to reason:
- Start from the NUMBERS, and say them. `outcome_band` says how far it travelled ({outcome_basis_note}); `reach_multiple` is views / the creator's followers; `*_pct_in_project` are percentiles within this library (p90 = top 10%); `reshares_per_1k_views` is the closest public proxy for the platform's "sends" signal; `engagement_rate_pct` says how well it converted the reach it got. A reel can travel far and convert badly, or the reverse - say which.
- `one_line` MUST open with the verdict in numbers, then what the reel is doing. Shape: "<views> views = <reach_multiple>x followers (p<percentile> in this library), <reshares_per_1k> sends/1k - <verdict> - <what it does>", e.g. "92,109 views = 0.09x followers (p11 in this library), 0.5 sends/1k - under-reached - a five-tip listicle on prompting". A reel with reach_multiple under 1 did not reach past its own followers: the verdict is "under-reached", never "effective", whatever the craft looks like. A reel over 3x travelled to strangers: say so and explain what carried it.
- Then explain the numbers with the LEVERS. Every item in what_drove_it / what_held_it_back names a lever (a taxonomy dimension=slug, or a metric like hook.seconds_to_first_payoff) and the platform signal it plausibly moved (watch time, likes, sends) - write the point as "<lever> -> <signal>: <why>". No generic advice - "post consistently", "use trending audio", "make the hook more engaging" and the like are banned; say WHICH hook shape, WHICH beat, WHICH second. 2-4 items each when the numbers are mixed; when reach is weak, 1-2 honest positives and the weight on what_held_it_back; when reach is strong, the reverse.
- Quotes: pick the exact words from the transcript that show the lever (the opening line for a hook point, the ask for a CTA point). They are checked against the reel after the fact and discarded when absent, so copy verbatim - never paraphrase - or use null.
- Example of a good item: {{"point": "hook.type=callout -> watch time: opens on 'if you use ChatGPT for work' which filters to the exact viewer and holds them through the list", "lever": "hook.type=callout", "quote": "if you use ChatGPT for work"}}
- Signals are your read of which ranking inputs this reel likely scored on, given its structure AND its numbers. Use "unknown" freely when the data can't say.
- `source: "own"` means this is the user's own reel; then `insights` (if present) are their private Instagram Insights and outrank any public proxy.
- "confidence" is about your diagnosis, not the reel: high only when numbers and structure clearly agree.
{algorithm_context}
The reel (JSON):
{reel}
"""


ALGORITHM_SOURCE_RE = re.compile(
    r"^-\s*\[(?P<ref>[^\]]+)\]\s*(?P<title>.+?)\s*[-\u2014]+\s*(?P<url>https?://\S+)"
    r"(?:\s*\((?P<meta>[^)]*)\))?\s*$")
ALGORITHM_PLACEHOLDER_RE = re.compile(r"^[-*]?\s*_?\(?(empty|none|nothing)\b", re.IGNORECASE)

_algorithm_cache = {"mtime": None, "value": None}
_algorithm_lock = threading.Lock()


def _parse_algorithm_brief(text):
    """Markdown in, structured brief out. The agent writes prose for humans; this
    pulls out the two things the app needs - the sections worth putting in a
    prompt, and the sources worth showing on screen."""
    front = {}
    body = text
    if text.startswith("---"):
        _, _, rest = text.partition("---")
        raw_front, _, body = rest.partition("---")
        for line in raw_front.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                front[key.strip().lower()] = value.strip()

    sections, current = {}, None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current:
            sections[current].append(line)

    def clean(name):
        lines = [ln.rstrip() for ln in sections.get(name, [])]
        kept = [ln for ln in lines if ln.strip() and not ALGORITHM_PLACEHOLDER_RE.match(ln.strip())]
        return "\n".join(kept).strip()

    sources = []
    for line in sections.get("Sources", []):
        match = ALGORITHM_SOURCE_RE.match(line.strip())
        if not match:
            continue
        meta = (match.group("meta") or "").split(",")
        sources.append({
            "ref": match.group("ref"),
            "title": match.group("title"),
            "url": match.group("url").rstrip(").,"),
            "tier": meta[0].strip() if meta else "",
            "date": meta[1].strip() if len(meta) > 1 else "",
        })

    parts = [f"{name}:\n{clean(name)}" for name in ALGORITHM_PROMPT_SECTIONS if clean(name)]
    prompt_text = None
    if parts:
        joined = "\n\n".join(parts)
        if len(joined) > ALGORITHM_PROMPT_LIMIT:
            joined = joined[:ALGORITHM_PROMPT_LIMIT].rsplit("\n", 1)[0] + "\n[brief truncated]"
        prompt_text = joined

    updated = front.get("updated") or ""
    return {
        "updated": None if updated.lower() in ("", "never") else updated,
        "next_review": front.get("next_review") or None,
        "sections": {name: clean(name) for name in ALGORITHM_PROMPT_SECTIONS if clean(name)},
        "sources": sources,
        "prompt_text": prompt_text,
    }


def load_algorithm_brief():
    """Cached on the file's mtime, so an agent rewriting it mid-run is picked up
    on the next request rather than at the next restart."""
    try:
        mtime = ALGORITHM_BRIEF_FILE.stat().st_mtime
    except OSError:
        return None

    with _algorithm_lock:
        if _algorithm_cache["mtime"] == mtime:
            return _algorithm_cache["value"]
    try:
        parsed = _parse_algorithm_brief(ALGORITHM_BRIEF_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[algorithm brief] could not parse {ALGORITHM_BRIEF_FILE}: {exc}", file=sys.stderr)
        parsed = None
    with _algorithm_lock:
        _algorithm_cache.update({"mtime": mtime, "value": parsed})
    return parsed


def algorithm_prompt_block():
    """The brief as a prompt fragment, or empty string when it has nothing in it
    yet - an un-researched file must not inject an empty 'here is the algorithm'
    heading that the model then tries to reason from."""
    brief = load_algorithm_brief()
    # An un-researched file still has prose in it (the skeleton explains itself),
    # so the gate is the `updated:` date, not whether text was found.
    if not brief or not brief.get("updated") or not brief.get("prompt_text"):
        return ""
    return (
        "\n\nEXTERNAL CONTEXT - how Instagram distributes Reels, as researched on "
        f"{brief['updated'] or 'an unknown date'} from Instagram's own posts, trade press, and "
        "creator communities. This is background on the platform, NOT data about these reels. "
        "Use it to explain WHY a pattern in the batch might be working, and to prefer formulas "
        "whose payoff a ranking signal can plausibly explain. Never let it override what the "
        "batch data actually shows: if the two disagree, trust the reels and say so in your "
        "caveats. Anything marked as a creator/community report is anecdote, not mechanics, and "
        "the platform may have changed since this was written.\n"
        f"{brief['prompt_text']}\n"
    )


def ensure_ffmpeg_on_path():
    """Make sure `ffmpeg` is callable. Falls back to the standard winget install
    location on Windows, since a just-installed PATH entry often isn't picked up
    by processes (like this one) that were already running when it was added."""
    if shutil.which("ffmpeg"):
        return
    if os.name == "nt":
        matches = glob.glob(os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin\ffmpeg.exe"
        ))
        if matches:
            os.environ["PATH"] = str(Path(matches[0]).parent) + os.pathsep + os.environ["PATH"]
            return
    sys.exit(
        "ffmpeg was not found on PATH. Install it (e.g. `winget install Gyan.FFmpeg` on Windows, "
        "`brew install ffmpeg` on macOS, or `apt install ffmpeg` on Linux), then restart your shell."
    )


ensure_ffmpeg_on_path()

VIDEOS_DIR.mkdir(parents=True, exist_ok=True)


def probe_duration(video_path):
    """Video length in seconds, or None if ffprobe can't read the file."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=20,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def extract_frames(rid, video_path, duration=None):
    """Evenly spaced storyboard frames, written next to the video as
    <rid>.f00.jpg, <rid>.f01.jpg ... The .f* suffix keeps them inside the
    `<rid>.*` glob the delete route already sweeps.

    One ffmpeg seek per frame rather than a single fps-filter pass: reel lengths
    vary too much for one fps value to land on a predictable frame count, and a
    keyframe seek is cheap at this scale. Existing frames for the reel are
    cleared first so a re-extract at a different count can't leave orphans.
    """
    duration = duration or probe_duration(video_path)
    if not duration or duration <= 0:
        return []

    count = max(1, min(FRAME_MAX, int(duration // FRAME_MIN_GAP_SEC)))
    step = duration / count

    for stale in VIDEOS_DIR.glob(f"{rid}.f*.jpg"):
        stale.unlink(missing_ok=True)

    frames = []
    for i in range(count):
        # +0.25s into each slot: seeking to exactly 0 on a reel usually lands on
        # a black or half-faded frame.
        t = round(min(i * step + 0.25, max(duration - 0.1, 0.0)), 2)
        out_path = VIDEOS_DIR / f"{rid}.f{i:02d}.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(t), "-i", str(video_path), "-vframes", "1",
                 "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", "-update", "1", str(out_path)],
                capture_output=True, timeout=30,
            )
        except Exception:
            continue
        if out_path.exists():
            frames.append({"file": out_path.name, "t": t})
    return frames

app = Flask(__name__)
jobs_lock = threading.Lock()
jobs = {}  # id -> job dict; the live, authoritative state for this server process

download_queue = queue.Queue()
transcribe_queue = queue.Queue()
analysis_queue = queue.Queue()
visual_queue = queue.Queue()
card_queue = queue.Queue()      # Reel Card pass: taxonomy tags + diagnosis, after the breakdown
link_queue = queue.Queue()

_whisper_model = None
_whisper_lock = threading.Lock()
_local_whisper_broken = False

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_WHISPER_MODEL = "whisper-large-v3"


def get_whisper_model():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            import whisper
            _whisper_model = whisper.load_model("small", device="cuda")
    return _whisper_model


def transcribe_with_groq(video_path, user_id):
    """Cloud fallback via Groq-hosted Whisper - for deployments (e.g. a cloud box
    with no local GPU/whisper install) where local transcription isn't available.
    Extracts audio first to keep the upload small and format-safe."""
    settings = with_platform_fallback(load_settings(user_id))
    groq_key = settings.get("groq_api_key")
    if not groq_key:
        raise RuntimeError("Local Whisper is unavailable and no Groq API key is configured for the cloud fallback.")

    audio_path = video_path.with_name(video_path.stem + ".fallback.m4a")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "aac", "-b:a", "64k", str(audio_path)],
        capture_output=True, timeout=60, check=True,
    )
    try:
        with open(audio_path, "rb") as f:
            resp = requests.post(
                GROQ_TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {groq_key}"},
                files={"file": (audio_path.name, f, "audio/m4a")},
                data=[("model", GROQ_WHISPER_MODEL), ("response_format", "verbose_json"),
                      ("timestamp_granularities[]", "segment")],
                timeout=180,
            )
        resp.raise_for_status()
    finally:
        audio_path.unlink(missing_ok=True)

    result = resp.json()
    text = (result.get("text") or "").strip()
    segments = [{"start": seg["start"], "end": seg["end"], "text": seg["text"].strip()}
                for seg in result.get("segments", [])]
    return text, segments


def transcribe_audio(video_path, user_id):
    """Local Whisper when available; falls back to Groq's hosted Whisper (e.g. when
    running on a cloud box with no GPU/whisper install). Once local Whisper fails
    once in this process, skip straight to the fallback on later calls instead of
    retrying an expensive, doomed load_model() every time."""
    global _local_whisper_broken
    if not _local_whisper_broken:
        try:
            model = get_whisper_model()
            result = model.transcribe(str(video_path))
            segments = [{"start": seg["start"], "end": seg["end"], "text": seg["text"].strip()}
                        for seg in result.get("segments", [])]
            return result["text"].strip(), segments
        except Exception as exc:
            _local_whisper_broken = True
            print(f"[transcribe_audio] local Whisper unavailable ({exc}) - "
                  f"falling back to Groq for the rest of this process", file=sys.stderr)
    return transcribe_with_groq(video_path, user_id)


# ---------- Database ----------
# jsonb columns come back as native dicts/lists without manual json.loads.
psycopg2.extras.register_default_jsonb(globally=True)
psycopg2.extras.register_default_json(globally=True)

db_pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=15, dsn=DATABASE_URL)

REEL_COLUMNS = ["id", "url", "status", "transcript", "segments", "video_file", "thumb_file",
                "frames", "visual", "created_at", "meta", "analysis", "error", "user_id",
                "project_id", "source", "caption", "hashtags", "tags", "diagnosis", "insights"]
REEL_JSON_COLUMNS = ("segments", "meta", "analysis", "frames", "visual", "tags", "diagnosis", "insights")


# Every round trip to Neon costs ~250 ms from here, so the cursor layer is built
# to spend as few as possible:
#   - the liveness probe only runs when a pooled connection has sat idle long
#     enough that Neon may have dropped it (PROBE_AFTER_IDLE_SEC), not per call;
#   - reads run in autocommit (one round trip). The cursor watches what it is
#     asked to execute and switches the connection into a real transaction on
#     the first write, so a block that SELECTs then INSERTs/UPDATEs still commits
#     its writes atomically - and a pure-read block never pays BEGIN + COMMIT.
# Before this, a single SELECT cost four round trips (probe, BEGIN, query,
# COMMIT) - a second per request, on every request.
PROBE_AFTER_IDLE_SEC = 45
_conn_last_used = {}      # id(conn) -> monotonic time of last successful use
_conn_last_used_lock = threading.Lock()
_READ_VERBS = ("select", "show", "explain", "values")


def _is_write(sql):
    head = (sql or "").lstrip()
    while head.startswith("--"):
        head = head.split("\n", 1)[1].lstrip() if "\n" in head else ""
    head = head.lstrip("( \t\r\n").lower()
    return not head.startswith(_READ_VERBS)


class _TrackingCursor(psycopg2.extras.RealDictCursor):
    """RealDictCursor that drops the connection out of autocommit on the first
    write statement, so writes are transactional and reads are cheap."""

    def _arm(self, sql):
        conn = self.connection
        if conn.autocommit and _is_write(sql if isinstance(sql, str) else str(sql)):
            conn.autocommit = False   # next statement opens a transaction

    def execute(self, sql, vars=None):
        self._arm(sql)
        return super().execute(sql, vars)

    def executemany(self, sql, vars_list):
        self._arm(sql)
        return super().executemany(sql, vars_list)


def _get_live_conn():
    """A pooled connection, probed only if it has been idle long enough that
    Neon may have dropped it. A dead connection is discarded and replaced."""
    while True:
        conn = db_pool.getconn()
        try:
            if not conn.autocommit:
                # A fresh pooled connection starts in transaction mode; flip it
                # before anything runs so the probe itself doesn't open a
                # transaction (which would make the later autocommit flip fail).
                if conn.status != psycopg2.extensions.STATUS_READY:
                    conn.rollback()
                conn.autocommit = True
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            db_pool.putconn(conn, close=True)
            continue
        with _conn_last_used_lock:
            last = _conn_last_used.get(id(conn), 0)
        if time.monotonic() - last < PROBE_AFTER_IDLE_SEC and not conn.closed:
            return conn
        try:
            with conn.cursor() as probe:
                probe.execute("SELECT 1")
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            with _conn_last_used_lock:
                _conn_last_used.pop(id(conn), None)
            db_pool.putconn(conn, close=True)


@contextmanager
def db_cursor():
    conn = _get_live_conn()
    close_conn = False
    try:
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=_TrackingCursor)
        try:
            yield cur
            if not conn.autocommit:
                conn.commit()
            with _conn_last_used_lock:
                _conn_last_used[id(conn)] = time.monotonic()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            close_conn = True
            with _conn_last_used_lock:
                _conn_last_used.pop(id(conn), None)
            raise
        except Exception:
            if not conn.autocommit:
                conn.rollback()
            raise
        finally:
            cur.close()
            if not close_conn and not conn.autocommit:
                # Leave the connection clean for the next borrower.
                try:
                    conn.autocommit = True
                except psycopg2.Error:
                    close_conn = True
    finally:
        db_pool.putconn(conn, close=close_conn)


# In-memory mirrors of the `reels`/`settings` tables, so the frequently-polled
# /api/jobs route (every 1.2s while anything is active) never hits the DB directly.
# store_cache stays global (reels.id is globally unique); reads are filtered by
# user_id at each route. settings_cache is keyed per user_id since every account
# brings its own AI provider config.
store_cache = {}
store_cache_lock = threading.Lock()
settings_cache = {}  # user_id -> settings dict
settings_cache_lock = threading.Lock()

_reel_locks = collections.defaultdict(threading.Lock)
_reel_locks_guard = threading.Lock()


def _lock_for(rid):
    with _reel_locks_guard:
        return _reel_locks[rid]


def load_store():
    """DB read of every reel - only ever called once, at startup, to seed store_cache."""
    with db_cursor() as cur:
        cur.execute(f"SELECT {', '.join(REEL_COLUMNS)} FROM reels")
        result = {}
        for row in cur.fetchall():
            d = dict(row)
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat()  # keep the same ISO-string shape jobs/JSON use everywhere else
            result[d["id"]] = d
        return result


def upsert_reel(job):
    row = {col: job.get(col) for col in REEL_COLUMNS}
    if row.get("created_at"):
        row["created_at"] = datetime.fromisoformat(row["created_at"])  # explicit parse, not implicit string coercion
    for col in REEL_JSON_COLUMNS:
        row[col] = psycopg2.extras.Json(row[col])
    row["source"] = row.get("source") or "reference"
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO reels (id, url, status, transcript, segments, video_file, thumb_file,
                               frames, visual, created_at, meta, analysis, error, user_id, project_id,
                               source, caption, hashtags, tags, diagnosis, insights)
            VALUES (%(id)s, %(url)s, %(status)s, %(transcript)s, %(segments)s, %(video_file)s,
                    %(thumb_file)s, %(frames)s, %(visual)s, %(created_at)s, %(meta)s, %(analysis)s,
                    %(error)s, %(user_id)s, %(project_id)s,
                    %(source)s, %(caption)s, %(hashtags)s, %(tags)s, %(diagnosis)s, %(insights)s)
            ON CONFLICT (id) DO UPDATE SET
                url = EXCLUDED.url, status = EXCLUDED.status, transcript = EXCLUDED.transcript,
                segments = EXCLUDED.segments, video_file = EXCLUDED.video_file,
                thumb_file = EXCLUDED.thumb_file, frames = EXCLUDED.frames,
                visual = EXCLUDED.visual, meta = EXCLUDED.meta,
                analysis = EXCLUDED.analysis, error = EXCLUDED.error, user_id = EXCLUDED.user_id,
                project_id = EXCLUDED.project_id, source = EXCLUDED.source,
                caption = EXCLUDED.caption, hashtags = EXCLUDED.hashtags, tags = EXCLUDED.tags,
                diagnosis = EXCLUDED.diagnosis, insights = EXCLUDED.insights
            """,
            row,
        )


def persist_reel(rid):
    """Snapshot the in-memory job and write it through to the DB + store_cache,
    serialized per reel id so a worker thread and a manual re-trigger touching
    the same reel can't blind-overwrite each other."""
    with _lock_for(rid):
        with jobs_lock:
            snapshot = dict(jobs[rid])
        # A scratch reel lives only in `jobs`, for as long as the chat that
        # asked about it needs it. Skipping both writes here is what keeps it
        # out of every library read, all of which go through the DB or
        # store_cache.
        if snapshot.get("scratch"):
            return snapshot
        upsert_reel(snapshot)
        with store_cache_lock:
            store_cache[rid] = snapshot
    return snapshot


def load_settings(user_id):
    with settings_cache_lock:
        cached = settings_cache.get(user_id)
        if cached:
            return dict(cached)
    with db_cursor() as cur:
        cur.execute("SELECT provider, base_url, model, api_key, groq_api_key, search_provider, search_api_key "
                    "FROM settings WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    s = {**DEFAULT_SETTINGS, **{k: v for k, v in (dict(row) if row else {}).items() if v is not None}}
    with settings_cache_lock:
        settings_cache[user_id] = s
    return dict(s)


def save_settings(user_id, settings):
    payload = {**settings, "user_id": user_id}
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO settings (user_id, provider, base_url, model, api_key, groq_api_key, search_provider, search_api_key)
            VALUES (%(user_id)s, %(provider)s, %(base_url)s, %(model)s, %(api_key)s, %(groq_api_key)s,
                    %(search_provider)s, %(search_api_key)s)
            ON CONFLICT (user_id) DO UPDATE SET
                provider = EXCLUDED.provider, base_url = EXCLUDED.base_url,
                model = EXCLUDED.model, api_key = EXCLUDED.api_key, groq_api_key = EXCLUDED.groq_api_key,
                search_provider = EXCLUDED.search_provider, search_api_key = EXCLUDED.search_api_key
            """,
            {**{"search_provider": "", "search_api_key": ""}, **payload},
        )
    with settings_cache_lock:
        settings_cache[user_id] = dict(settings)


def with_platform_fallback(settings):
    """AI calls fall back to the platform-wide key when a user hasn't configured
    their own, so nobody has to set anything up to use the app. A user's own key,
    once they set one, always takes precedence. Only applied at the point of use
    (AI/transcription calls) - GET /api/settings deliberately shows the user's
    own raw settings so the UI accurately reflects what THEY have configured."""
    s = dict(settings)
    if not s.get("api_key"):
        s["api_key"] = PLATFORM_API_KEY
    if not s.get("groq_api_key"):
        s["groq_api_key"] = PLATFORM_GROQ_API_KEY
    if not (s.get("search_provider") and s.get("search_api_key")):
        # First platform search key wins, in this order.
        for provider in ("tavily", "langsearch", "brave"):
            if PLATFORM_SEARCH_KEYS.get(provider):
                s["search_provider"], s["search_api_key"] = provider, PLATFORM_SEARCH_KEYS[provider]
                break
    return s


# ---------- Projects ----------
# A project is the scoping unit for everything a user creates: reels, formulas,
# formula categories, analysis runs. The active project travels on every API call
# as the X-Project-Id header (see require_project), so no route can accidentally
# read across projects - the filter is applied at the query, not the response.


def _seed_categories(cur, project_id):
    for slug, display_name, sort_order in DEFAULT_CATEGORIES:
        cur.execute(
            "INSERT INTO formula_categories (project_id, slug, display_name, sort_order) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (project_id, slug, display_name, sort_order),
        )


PROJECT_COLUMNS = "id, name, emoji, description, instructions, own_username, outcome_mode, created_at"


def clean_own_username(value):
    value = (value or "").strip().lstrip("@").lower()
    return value[:60] or None


def create_project(user_id, name, emoji=None, description=None, instructions=None,
                   own_username=None, outcome_mode="views"):
    """Returns the new project row, or None if the name is already taken for
    this user (the caller turns that into a 409)."""
    with db_cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO projects (user_id, name, emoji, description, instructions, own_username, outcome_mode)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, lower(btrim(name))) DO NOTHING
            RETURNING {PROJECT_COLUMNS}
            """,
            (user_id, name, emoji, description, instructions, clean_own_username(own_username),
             outcome_mode if outcome_mode in ("views", "reach") else "views"),
        )
        row = cur.fetchone()
        if row:
            _seed_categories(cur, row["id"])
        return row


def ensure_default_project(user_id):
    """Every account needs at least one project for the UI to have something
    selected. Called on the projects listing rather than on every request, so a
    normal API call never pays for this check."""
    with db_cursor() as cur:
        cur.execute("SELECT id FROM projects WHERE user_id = %s LIMIT 1", (user_id,))
        if cur.fetchone():
            return
    create_project(user_id, "General", emoji="📁")


def list_projects(user_id):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.name, p.emoji, p.description, p.instructions, p.own_username,
                   p.outcome_mode, p.created_at,
                   (SELECT count(*) FROM reels r WHERE r.project_id = p.id) AS reel_count,
                   (SELECT count(*) FROM reels r WHERE r.project_id = p.id AND r.source = 'own') AS own_reel_count,
                   (SELECT count(*) FROM formulas f WHERE f.project_id = p.id) AS formula_count
            FROM projects p WHERE p.user_id = %s ORDER BY p.created_at
            """,
            (user_id,),
        )
        return [_jsonify_row(r) for r in cur.fetchall()]


# Projects are read on EVERY request (require_project) and by every worker, and
# they change rarely (rename, instructions, handle, playbook). Cached in memory
# for PROJECT_CACHE_SEC; every write path calls invalidate_project().
PROJECT_CACHE_SEC = 60
_project_cache = {}        # project_id -> (monotonic expiry, row dict)
_project_cache_lock = threading.Lock()


def invalidate_project(project_id):
    with _project_cache_lock:
        _project_cache.pop(project_id, None)


def get_project(project_id, user_id=None):
    """user_id=None is for worker threads, which have no request context but are
    only ever handed a project_id that was already ownership-checked at submit.
    The ownership check is applied to the cached row too."""
    if project_id is None:
        return None
    with _project_cache_lock:
        hit = _project_cache.get(project_id)
    if hit and hit[0] > time.monotonic():
        row = hit[1]
    else:
        with db_cursor() as cur:
            cur.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
        row = dict(row) if row else None
        with _project_cache_lock:
            _project_cache[project_id] = (time.monotonic() + PROJECT_CACHE_SEC, row)
    if not row:
        return None
    if user_id is not None and str(row.get("user_id")) != str(user_id):
        return None
    return dict(row)


def require_project(fn):
    """Resolves + ownership-checks the active project. Always stacked under
    @require_login, which is what puts g.user_id in place."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        raw = request.headers.get("X-Project-Id") or request.args.get("project_id")
        try:
            project_id = int(raw)
        except (TypeError, ValueError):
            return jsonify({"error": "No project selected."}), 400
        project = get_project(project_id, g.user_id)
        if not project:
            return jsonify({"error": "Project not found."}), 404
        g.project_id = project_id
        g.project = project
        return fn(*args, **kwargs)
    return wrapper


# What each default category is actually asking for. Keyed by slug; custom tabs
# get the generic fallback. The hook note encodes a rule of thumb worth stating
# outright: a reel that travelled past the "good" line had a hook that did its one
# job (stop the scroll), so the run's question there is "what shape is it", not
# "did it work" - the model otherwise spends the batch debating the latter and
# returns three formulas for thirty-six reels.
CATEGORY_GUIDANCE = {
    "hook": (
        "WHAT COUNTS HERE: the opening line(s) - the first sentence or two, plus any on-screen "
        "text in the first seconds - and nothing after. Any reel at or above {good_views:,} views "
        "had a hook that demonstrably did its job, so do not debate whether it worked: catalogue "
        "its SHAPE. Every such reel's hook belongs to some formula, and hooks that share a sentence "
        "shape go together while hooks that merely share a topic do not. Name the formula after the "
        "shape (\"If you [do X], stop - [consequence]\"), not after the topic."
    ),
    "script": (
        "WHAT COUNTS HERE: how the body is built after the hook - the promise, the order of the "
        "beats, how the payoff is delayed or delivered, where the CTA lands and what it asks for. "
        "A script formula describes a SEQUENCE (\"[claim] -> [proof 1] -> [proof 2] -> [twist] -> "
        "[ask]\"), not a single line."
    ),
    "talking": (
        "WHAT COUNTS HERE: delivery - pace (see wpm), sentence length, direct address, questions, "
        "repetition, energy, pauses, how numbers and names are dropped, how the speaker refers to "
        "themselves and the viewer. NOT the opening line - hooks have their own tab, and a run that "
        "returns hook sentences here has failed. A talking formula's template describes a delivery "
        "move with blanks for the content it is applied to, e.g. \"[short claim]. [one-word pause]. "
        "[longer explanation that re-uses the same noun]\" or \"asks [question] and answers it "
        "themselves within [n] seconds\". Evidence quotes show the move in action anywhere in the reel."
    ),
}


def category_guidance_block(slug, display_name):
    text = CATEGORY_GUIDANCE.get(slug)
    if not text:
        text = (f"WHAT COUNTS HERE: patterns a creator would file under \"{display_name}\" and "
                f"reuse deliberately next time. Ignore anything that belongs to another tab.")
    return "\n" + text.format(good_views=GOOD_VIEW_THRESHOLD) + "\n"


def project_prompt_context(project):
    """The user's own steer for this project, injected into every AI prompt it
    drives. Deliberately framed as context rather than instruction so a stray
    'reply in prose' in the box can't break the JSON contract the parser needs."""
    instructions = ((project or {}).get("instructions") or "").strip()
    if not instructions:
        return ""
    return (
        "\nContext about this specific content library, written by the user who owns it. "
        "Use it to judge what counts as a meaningful pattern, which audience the content targets, "
        "and what vocabulary to expect. It never changes the output format required below - "
        "if it conflicts with the response format, the response format wins.\n"
        f"\"\"\"\n{instructions[:4000]}\n\"\"\"\n"
    )


def slugify_category(display_name, existing_slugs):
    base = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")[:32] or "category"
    slug = base
    n = 2
    while slug in existing_slugs:
        slug = f"{base}_{n}"
        n += 1
    return slug


def format_timestamp(seconds):
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def segments_to_script(segments):
    if not segments:
        return ""
    return "\n".join(f"[{format_timestamp(seg['start'])}] {seg['text'].strip()}" for seg in segments)


def reel_id_for(url, project_id):
    """Project-prefixed so the same Instagram reel can sit in two projects as two
    independent rows - each with its own transcript, breakdown and evidence -
    instead of the second project's copy overwriting the first on the primary key.
    The prefix also keeps the media filenames (<id>.mp4/.jpg) from colliding."""
    match = INSTAGRAM_URL_RE.search(url or "")
    shortcode = match.group(1) if match else hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]
    return f"{project_id}_{shortcode}"


FILLER_WORDS_RE = re.compile(r"\b(um+|uh+|like|basically|actually|literally|you know|kind of|sort of)\b", re.IGNORECASE)
YOU_RE = re.compile(r"\b(you|your|you're|youre)\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d+")
SENTENCE_SPLIT_RE = re.compile(r"[.!?]+")


def compute_metrics(job):
    """Pure function over a job's existing fields - nothing here is persisted to the
    store, it's cheap enough to recompute per request."""
    transcript = (job.get("transcript") or "").strip()
    segments = job.get("segments") or []
    analysis = job.get("analysis") or {}
    meta = job.get("meta") or {}

    word_count = len(transcript.split())
    # The platform's own duration beats the last transcript segment, which stops
    # at the last spoken word and misses a silent outro.
    duration_sec = meta.get("video_duration") or (segments[-1]["end"] if segments else None)
    duration_sec = round(duration_sec, 1) if duration_sec else None
    wpm = round(word_count / (duration_sec / 60), 1) if duration_sec else None

    sentences = [s for s in SENTENCE_SPLIT_RE.split(transcript) if s.strip()]
    sentence_count = len(sentences)
    avg_sentence_len = round(word_count / sentence_count, 1) if sentence_count else None

    filler_count = len(FILLER_WORDS_RE.findall(transcript))
    you_count = len(YOU_RE.findall(transcript))

    hook_word_count = None
    cta_position_pct = None
    if analysis:
        hook_text = analysis.get("hook") or ""
        hook_word_count = len(hook_text.split())
        pre_cta_len = len(hook_text) + len(analysis.get("promise") or "") + len(analysis.get("validation") or "")
        cta_position_pct = round(pre_cta_len / len(transcript) * 100, 1) if transcript else None

    like_count = meta.get("like_count") or 0
    comment_count = meta.get("comment_count") or 0
    view_count = meta.get("view_count")
    reshare_count = meta.get("reshare_count")

    # Named UTC because that is what it is - we don't know the creator's timezone,
    # and a day-of-week finding drawn from the wrong clock is worse than none.
    posted_at = meta.get("posted_at")
    posted_dow = posted_hour_utc = None
    if posted_at:
        try:
            posted = datetime.fromtimestamp(posted_at, timezone.utc)
            posted_dow = posted.strftime("%a")
            posted_hour_utc = posted.hour
        except (OverflowError, OSError, ValueError):
            pass

    width, height = meta.get("width"), meta.get("height")

    # Size normalisation. Views mostly measure how big the creator already is;
    # views per follower measures how far past the existing audience the reel
    # travelled, which is the thing a formula can actually explain - and it is
    # comparable between a 50k account and a 5M one.
    follower_count = meta.get("follower_count")
    reach_multiple = (round(view_count / follower_count, 3)
                      if view_count is not None and follower_count else None)
    caption = job.get("caption") or meta.get("caption") or meta.get("description") or ""
    hashtags = job.get("hashtags") or meta.get("hashtags") or []

    return {
        "duration_sec": duration_sec,
        "word_count": word_count,
        "wpm": wpm,
        "sentence_count": sentence_count,
        "avg_sentence_len": avg_sentence_len,
        "question_count": transcript.count("?"),
        "filler_per_1k": round(filler_count / word_count * 1000, 1) if word_count else None,
        "you_density": round(you_count / word_count * 100, 1) if word_count else None,
        "number_mentions": len(NUMBER_RE.findall(transcript)),
        "hook_word_count": hook_word_count,
        "cta_position_pct": cta_position_pct,
        "like_count": like_count,
        "comment_count": comment_count,
        "view_count": view_count,
        "reshare_count": reshare_count,
        # Rates, not totals: a reel with 400k views and one with 10M aren't
        # comparable on raw counts, and the platform ranks on rates too.
        "engagement_rate_pct": (round((like_count + comment_count) / view_count * 100, 2)
                                if view_count else None),
        "reshares_per_1k_views": (round(reshare_count / view_count * 1000, 2)
                                  if view_count and reshare_count is not None else None),
        "likes_per_1k_views": round(like_count / view_count * 1000, 2) if view_count else None,
        "comments_per_1k_views": round(comment_count / view_count * 1000, 2) if view_count else None,
        # Who posted it and how big they are - the denominator for reach_multiple.
        "creator": meta.get("username"),
        "follower_count": follower_count,
        "reach_multiple": reach_multiple,
        "source": job.get("source") or "reference",
        "caption_word_count": len(caption.split()) if caption else 0,
        "hashtag_count": len(hashtags),
        # How the reel was made and shipped - the variables a creator can change
        # next time, as opposed to the outcomes they can only measure.
        "resolution": f"{width}x{height}" if width and height else None,
        "is_full_hd": (height >= 1920) if height else None,
        "audio_type": meta.get("audio_type"),
        "audio_title": meta.get("audio_title"),
        "is_original_audio": (meta.get("audio_type") == "original_sounds"
                              if meta.get("audio_type") else None),
        "posted_dow": posted_dow,
        "posted_hour_utc": posted_hour_utc,
        "is_collab": bool(meta.get("coauthors")),
        "is_paid_partnership": meta.get("is_paid_partnership"),
        "cross_posted_to_fb": bool(meta.get("fb_play_count")),
        "engagement_score": like_count + comment_count,
    }


def update_job(rid, **fields):
    with jobs_lock:
        jobs[rid].update(fields)


def mark_ai_pending(rid, stage):
    """In-memory only (never a DB column): which AI pass, if any, this reel is
    waiting on. The UI polls it to show 'Analyzing' between a finished
    transcript and a finished breakdown, and to know when to stop polling.
    Tolerates a missing reel - a delete can land while a worker is mid-flight,
    and a KeyError raised from a worker's finally block would kill the thread."""
    with jobs_lock:
        if rid in jobs:
            jobs[rid]["ai_pending"] = stage


def _cookies_from_browser_spec(spec):
    """Parse yt-dlp's BROWSER[+KEYRING][:PROFILE][::CONTAINER] into its tuple form."""
    spec, _, container = spec.partition("::")
    browser, _, profile = spec.partition(":")
    browser, _, keyring = browser.partition("+")
    return (browser.strip().lower(), profile.strip() or None,
            keyring.strip().upper() or None, container.strip() or None)


def ydl_opts(**overrides):
    """Base yt-dlp options with Instagram cookies wired in when available."""
    opts = {"format": "mp4/best", "quiet": True, "no_warnings": True}
    cookiefile = YTDLP_COOKIES_FILE or (str(DEFAULT_COOKIES_FILE) if DEFAULT_COOKIES_FILE.exists() else "")
    if cookiefile:
        opts["cookiefile"] = cookiefile
    elif YTDLP_COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = _cookies_from_browser_spec(YTDLP_COOKIES_FROM_BROWSER)
    opts.update(overrides)
    return opts


# ---------- Reel view counts ----------
# yt-dlp gives us likes and comments but no view/play count for reels, and every
# anonymous web endpoint is behind a login wall now (verified: /api/v1/media/
# returns the login HTML, graphql/query 403s, the reel page embeds no counts).
# Instagram's own web client reads the numbers from /api/v1/media/<id>/info/,
# which works with the same session cookies this app already uses to download.
# One read-only GET per reel, at most one every STATS_MIN_INTERVAL seconds.
#
# play_count is what the UI calls "views": it is ig_play_count + fb_play_count
# (verified against a reel where 9,849,597 + 495,736 = 10,345,333). All four are
# stored - the split matters for a creator who cross-posts.
INSTAGRAM_WEB_APP_ID = "936619743392459"   # the public web client id instagram.com itself sends
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
STATS_MIN_INTERVAL = 1.5                   # seconds between stats calls, process-wide
# Bump when fetch_reel_stats starts returning something new: reels stamped with an
# older version get re-fetched once, so a schema change backfills itself instead
# of only applying to reels added afterwards.
STATS_VERSION = 3   # v3: caption, hashtags, creator follower count
STATS_COOKIE_TTL = 900                     # re-read the browser cookie DB at most this often

_stats_lock = threading.Lock()
_stats_last_call = 0.0
_stats_cookies = {"at": 0.0, "value": None}


def shortcode_to_media_id(shortcode):
    """Instagram shortcodes are the media id in base64 with a URL-safe alphabet."""
    media_id = 0
    for char in shortcode:
        media_id = media_id * 64 + SHORTCODE_ALPHABET.index(char)
    return media_id


def _instagram_cookies():
    """The same cookies ydl_opts() hands yt-dlp, as a plain name->value dict.
    Cached: pulling them out of a browser profile reads a locked SQLite DB and
    is far too slow to repeat per reel."""
    now = time.time()
    with _stats_lock:
        if _stats_cookies["value"] is not None and now - _stats_cookies["at"] < STATS_COOKIE_TTL:
            return _stats_cookies["value"]

    jar = None
    cookiefile = YTDLP_COOKIES_FILE or (str(DEFAULT_COOKIES_FILE) if DEFAULT_COOKIES_FILE.exists() else "")
    try:
        from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser
        if cookiefile:
            jar = YoutubeDLCookieJar(cookiefile)
            jar.load()
        elif YTDLP_COOKIES_FROM_BROWSER:
            browser, profile, keyring, container = _cookies_from_browser_spec(YTDLP_COOKIES_FROM_BROWSER)
            jar = extract_cookies_from_browser(browser, profile, keyring=keyring, container=container)
    except Exception as exc:
        print(f"[reel stats] could not read cookies: {exc}", file=sys.stderr)

    cookies = ({c.name: c.value for c in jar if "instagram.com" in (c.domain or "")}
               if jar is not None else {})
    with _stats_lock:
        _stats_cookies.update({"at": now, "value": cookies})
    return cookies


def active_instagram_account():
    """The numeric id of the account whose session the cookies belong to."""
    return (_instagram_cookies() or {}).get("ds_user_id")


def instagram_account_ok():
    """(allowed, message). False whenever a pin is configured and the live
    cookies belong to somebody else - including when there is no session at all,
    because 'logged out' must not silently degrade into 'used the wrong account
    later'."""
    if not INSTAGRAM_ACCOUNT_ID:
        return True, ""
    active = active_instagram_account()
    if not active:
        return False, ("No Instagram session found, but INSTAGRAM_ACCOUNT_ID is pinned to "
                       f"{INSTAGRAM_ACCOUNT_ID}. Log that account in, or clear the pin.")
    if str(active) != INSTAGRAM_ACCOUNT_ID:
        return False, (f"Instagram cookies belong to account {active}, but this app is pinned "
                       f"to {INSTAGRAM_ACCOUNT_ID}. Refusing to use the wrong account - log the "
                       "pinned account back in, or update INSTAGRAM_ACCOUNT_ID in .env.")
    return True, ""


def fetch_reel_stats(url):
    """View/play/reshare counts for one reel, or None when they can't be read.

    Never raises: this is an enrichment pass, and a reel with no view count is
    still a perfectly good reel. Returns the raw counts so the caller decides
    what to store."""
    match = INSTAGRAM_URL_RE.search(url or "")
    if not match:
        return None

    allowed, why = instagram_account_ok()
    if not allowed:
        print(f"[reel stats] skipped: {why}", file=sys.stderr)
        return None

    cookies = _instagram_cookies()
    if "sessionid" not in cookies:
        return None   # not logged in - these numbers simply aren't public any more

    shortcode = match.group(1)
    try:
        media_id = shortcode_to_media_id(shortcode)
    except ValueError:
        return None

    global _stats_last_call
    with _stats_lock:
        wait = STATS_MIN_INTERVAL - (time.time() - _stats_last_call)
        if wait > 0:
            time.sleep(wait)
        _stats_last_call = time.time()

    try:
        resp = requests.get(
            f"https://www.instagram.com/api/v1/media/{media_id}/info/",
            headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"),
                "X-IG-App-ID": INSTAGRAM_WEB_APP_ID,
                "X-CSRFToken": cookies.get("csrftoken", ""),
                "Referer": f"https://www.instagram.com/reel/{shortcode}/",
                "Accept": "*/*",
            },
            cookies=cookies,
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"[reel stats] {shortcode}: HTTP {resp.status_code}", file=sys.stderr)
            return None
        items = resp.json().get("items") or []
        if not items:
            return None
    except Exception as exc:
        print(f"[reel stats] {shortcode} failed: {exc}", file=sys.stderr)
        return None

    return parse_media_item(items[0])


def parse_media_item(item):
    """Everything worth keeping from one media object.

    Beyond the counts, this is the stuff that explains *why* a reel travelled:
    what audio it used (original vs a licensed track, and which track), whether
    it was a collab or a paid partnership, what resolution it shipped at - the
    algorithm brief lists low resolution as a suppression trigger - and when it
    actually went out, which is the one variable a creator fully controls.
    """
    clips = item.get("clips_metadata") or {}
    original_sound = clips.get("original_sound_info") or {}
    music = (clips.get("music_info") or {}).get("music_asset_info") or {}
    user = item.get("user") or {}

    def count(key, source=item):
        value = source.get(key)
        return value if isinstance(value, int) else None

    coauthors = [c.get("username") for c in (item.get("coauthor_producers") or [])
                 if isinstance(c, dict) and c.get("username")]

    audio_title = (music.get("title") or original_sound.get("original_audio_title") or None)
    audio_artist = (music.get("display_artist")
                    or (original_sound.get("ig_artist") or {}).get("username") or None)

    taken_at = count("taken_at")

    caption_obj = item.get("caption") if isinstance(item.get("caption"), dict) else {}
    caption = caption_obj.get("text") if isinstance(caption_obj.get("text"), str) else None

    return {
        "stats_v": STATS_VERSION,
        # the caption is the text hook and the topic signal; hashtags parsed off it
        "caption": caption,
        "hashtags": parse_hashtags(caption),
        # counts
        "view_count": count("play_count"),          # what the app shows as views
        "ig_play_count": count("ig_play_count"),
        "fb_play_count": count("fb_play_count"),
        "reshare_count": count("media_repost_count"),
        "like_count": count("like_count"),
        "comment_count": count("comment_count"),
        "fb_like_count": count("fb_like_count"),
        "fb_comment_count": count("fb_comment_count"),
        "counts_hidden": bool(item.get("like_and_view_counts_disabled")),
        # the reel itself
        "posted_at": taken_at,
        "video_duration": item.get("video_duration"),
        "width": count("original_width"),
        "height": count("original_height"),
        "has_audio": item.get("has_audio"),
        "product_type": item.get("product_type"),
        # audio: original vs a licensed track is a genuinely different strategy
        "audio_type": clips.get("audio_type"),
        "audio_title": audio_title,
        "audio_artist": audio_artist,
        "audio_cluster_id": (clips.get("audio_ranking_info") or {}).get("best_audio_cluster_id"),
        # context that explains outliers
        "coauthors": coauthors or None,
        "is_paid_partnership": bool(item.get("is_paid_partnership")),
        "ai_detection": (item.get("gen_ai_detection_method") or {}).get("detection_method"),
        "username": user.get("username"),
        "creator_full_name": user.get("full_name"),
        # numeric id - the fallback key for the profile fetch when the
        # username endpoint refuses (it 400s for some accounts)
        "creator_ig_id": str(user.get("pk")) if user.get("pk") is not None else None,
    }


HASHTAG_RE = re.compile(r"(?<![\w&])#(\w[\w\u0300-\u036F\u0900-\u0DFF]*)")   # \w plus combining marks, so Indic tags survive whole


def parse_hashtags(caption):
    if not caption:
        return None
    seen = []
    for tag in HASHTAG_RE.findall(caption):
        tag = tag.lower()
        if tag not in seen:
            seen.append(tag)
    return seen[:60] or None


# Booleans and the version stamp are always written - False and "no AI detected"
# are real answers. Everything else only overwrites when we actually got a value,
# so a partial response can't blank a field we already had.
STATS_ALWAYS_SET = ("stats_v", "counts_hidden", "is_paid_partnership")

# Creator profiles (follower count) are refreshed when older than this. Follower
# counts drift slowly; a week-old number is fine for a reach multiple, and it
# keeps a 40-creator library at one profile call per creator per week instead
# of one per reel per load.
CREATOR_REFRESH_DAYS = 7


def fetch_creator_profile(username, ig_user_id=None):
    """Follower/following/media counts for one account, or None. Same session,
    same rate limiter and same never-raise contract as fetch_reel_stats.

    Tries the username endpoint first; when that refuses (it answers 400 for
    some accounts) and we know the numeric id from a media item, falls back to
    the by-id endpoint, which reports the same counts in a flatter shape."""
    if not username and not ig_user_id:
        return None
    allowed, why = instagram_account_ok()
    if not allowed:
        print(f"[creator] skipped: {why}", file=sys.stderr)
        return None
    cookies = _instagram_cookies()
    if "sessionid" not in cookies:
        return None

    global _stats_last_call
    with _stats_lock:
        wait = STATS_MIN_INTERVAL - (time.time() - _stats_last_call)
        if wait > 0:
            time.sleep(wait)
        _stats_last_call = time.time()

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"),
        "X-IG-App-ID": INSTAGRAM_WEB_APP_ID,
        "X-CSRFToken": cookies.get("csrftoken", ""),
        "Referer": f"https://www.instagram.com/{username or ''}/",
        "Accept": "*/*",
    }

    def count_of(source, key):
        value = source.get(key)
        return value if isinstance(value, int) else None

    user = None
    if username:
        try:
            resp = requests.get("https://www.instagram.com/api/v1/users/web_profile_info/",
                                params={"username": username}, headers=headers, cookies=cookies, timeout=30)
            if resp.status_code == 200:
                user = (resp.json().get("data") or {}).get("user") or None
            else:
                print(f"[creator] {username}: HTTP {resp.status_code} - trying the profile page",
                      file=sys.stderr)
        except Exception as exc:
            print(f"[creator] {username} failed: {exc}", file=sys.stderr)
    if user:
        return {
            "username": user.get("username") or username,
            "ig_user_id": str(user.get("id")) if user.get("id") is not None else ig_user_id,
            "full_name": user.get("full_name"),
            "follower_count": count_of(user.get("edge_followed_by") or {}, "count"),
            "following_count": count_of(user.get("edge_follow") or {}, "count"),
            "media_count": count_of(user.get("edge_owner_to_timeline_media") or {}, "count"),
            "is_verified": bool(user.get("is_verified")) if "is_verified" in user else None,
        }

    # Fallback: the profile page's og:description ("1.2M Followers, 300 Following,
    # 1,234 Posts"). The JSON endpoint 400s for some business accounts with an
    # Instagram-side schema error, and the by-id endpoints answer HTML on a web
    # session. The og tags are only served to link-preview crawlers and only on
    # an anonymous request - a logged-in session gets the JS shell with no tags -
    # so this goes out without cookies. Abbreviated, so the count is approximate;
    # fine as a denominator.
    if not username:
        return None
    with _stats_lock:
        wait = STATS_MIN_INTERVAL - (time.time() - _stats_last_call)
        if wait > 0:
            time.sleep(wait)
        _stats_last_call = time.time()
    try:
        resp = requests.get(f"https://www.instagram.com/{username}/",
                            headers={"User-Agent": "facebookexternalhit/1.1", "Accept": "text/html"},
                            timeout=30)
        if resp.status_code != 200:
            print(f"[creator] {username} profile page: HTTP {resp.status_code}", file=sys.stderr)
            return None
        match = OG_FOLLOWERS_RE.search(resp.text)
    except Exception as exc:
        print(f"[creator] {username} profile page failed: {exc}", file=sys.stderr)
        return None
    if not match:
        return None
    return {
        "username": username,
        "ig_user_id": ig_user_id,
        "full_name": None,
        "follower_count": parse_abbreviated_count(match.group("followers")),
        "following_count": parse_abbreviated_count(match.group("following")),
        "media_count": parse_abbreviated_count(match.group("posts")),
        "is_verified": None,
    }


OG_FOLLOWERS_RE = re.compile(
    r'content="(?P<followers>[\d.,]+[KMB]?) Followers, (?P<following>[\d.,]+[KMB]?) Following, '
    r'(?P<posts>[\d.,]+[KMB]?) Posts', re.IGNORECASE)


def parse_abbreviated_count(text):
    """'1.2M' -> 1200000, '12,345' -> 12345, '880K' -> 880000."""
    if not text:
        return None
    text = text.strip().upper().replace(",", "")
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(text[-1], 1)
    if mult != 1:
        text = text[:-1]
    try:
        return int(round(float(text) * mult))
    except ValueError:
        return None


def get_creator(username):
    if not username:
        return None
    with db_cursor() as cur:
        cur.execute("SELECT * FROM creators WHERE username = %s", (username.lower(),))
        return cur.fetchone()


def ensure_creator(username, ig_user_id=None):
    """The cached creators row for this handle, fetching/refreshing it when it is
    missing or older than CREATOR_REFRESH_DAYS. Returns None when nothing is known
    (no session, blocked, never fetched) - callers treat that as 'no follower
    count', never as zero."""
    if not username:
        return None
    username = username.lower()
    row = get_creator(username)
    if row and row.get("fetched_at"):
        age = datetime.now(timezone.utc) - row["fetched_at"]
        if age.days < CREATOR_REFRESH_DAYS and row.get("follower_count") is not None:
            return row
    profile = fetch_creator_profile(username, ig_user_id or (row or {}).get("ig_user_id"))
    if not profile:
        return row
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO creators (username, ig_user_id, full_name, follower_count, following_count,
                                  media_count, is_verified, fetched_at)
            VALUES (%(username)s, %(ig_user_id)s, %(full_name)s, %(follower_count)s,
                    %(following_count)s, %(media_count)s, %(is_verified)s, now())
            ON CONFLICT (username) DO UPDATE SET
                ig_user_id = COALESCE(EXCLUDED.ig_user_id, creators.ig_user_id),
                full_name = COALESCE(EXCLUDED.full_name, creators.full_name),
                follower_count = COALESCE(EXCLUDED.follower_count, creators.follower_count),
                following_count = COALESCE(EXCLUDED.following_count, creators.following_count),
                media_count = COALESCE(EXCLUDED.media_count, creators.media_count),
                is_verified = COALESCE(EXCLUDED.is_verified, creators.is_verified),
                fetched_at = now()
            RETURNING *
            """,
            {**profile, "username": username},
        )
        return cur.fetchone()


def enrich_job_from_stats(job, meta):
    """After a stats fetch: snapshot the creator's follower count into meta (so
    compute_metrics stays a pure function of the job), lift caption/hashtags into
    their columns, and mark the reel 'own' when it is from the project's account.
    Mutates and returns meta; returns the extra job fields to update alongside."""
    fields = {}
    username = (meta.get("username") or "").lower() or None
    if username:
        creator = ensure_creator(username, meta.get("creator_ig_id"))
        if creator and creator.get("follower_count") is not None:
            meta["follower_count"] = creator["follower_count"]
            meta["follower_count_at"] = creator["fetched_at"].isoformat() if creator.get("fetched_at") else None
    if meta.get("caption") is not None:
        fields["caption"] = meta["caption"]
        fields["hashtags"] = meta.get("hashtags")
    elif not job.get("caption") and meta.get("description"):
        fields["caption"] = meta["description"]          # yt-dlp's description is the caption too
        fields["hashtags"] = parse_hashtags(meta["description"])
    fields["source"] = reel_source_for(job, username)
    return meta, fields


def reel_source_for(job, username=None):
    """'own' when the reel's creator is the project's own account, else keeps
    whatever the reel already has (default 'reference')."""
    username = (username or (job.get("meta") or {}).get("username") or "").lower()
    if not username:
        return job.get("source") or "reference"
    project = get_project(job.get("project_id"))
    own = ((project or {}).get("own_username") or "").lstrip("@").lower()
    if own and username == own:
        return "own"
    if own and job.get("source") == "own":
        return "reference"   # project handle changed away from this creator
    return job.get("source") or "reference"


def merge_reel_stats(meta, stats):
    """Fold fetched details into a reel's meta."""
    if not stats:
        return meta
    merged = dict(meta or {})
    for key, value in stats.items():
        if key in STATS_ALWAYS_SET or value is not None:
            merged[key] = value
    return merged


def describe_download_error(exc):
    """Turn yt-dlp's login-wall error into something actionable in the UI."""
    msg = str(exc)
    if "empty media response" in msg or "login required" in msg.lower() or "rate-limit reached" in msg.lower():
        have_cookies = bool(YTDLP_COOKIES_FILE or YTDLP_COOKIES_FROM_BROWSER or DEFAULT_COOKIES_FILE.exists())
        hint = ("The configured Instagram cookies were rejected - they have most likely expired, "
                "so export them again." if have_cookies else
                "Instagram requires a logged-in session for this post. Set YTDLP_COOKIES_FILE "
                "(or YTDLP_COOKIES_FROM_BROWSER) and restart the app.")
        return f"Instagram login required: {hint}"
    return msg


def new_job(rid, url, user_id, project_id, status="queued", scratch=False):
    """A reel moving through the pipeline. `scratch` reels run the identical
    passes but are never written to the DB or store_cache and never appear in
    the library, the CSV exports or the lever/formula statistics - they exist to
    answer one question about a URL the creator does not want to save. See
    persist_reel() and purge_scratch_jobs()."""
    job = {"id": rid, "url": url, "user_id": user_id, "project_id": project_id, "status": status,
           "transcript": None, "segments": None,
           "video_file": None, "thumb_file": None, "frames": None, "visual": None,
           "meta": None, "analysis": None, "ai_pending": None,
           "source": "reference", "caption": None, "hashtags": None,
           "tags": None, "diagnosis": None, "insights": None,
           "created_at": None, "error": None, "scratch": bool(scratch)}
    with jobs_lock:
        jobs[rid] = job
    return job


# A scratch reel answers "why did this one do what it did?" about a URL the
# creator has no intention of keeping. Its id carries a "~" - a character no
# Instagram shortcode contains - so it can never collide with the library copy
# of the same reel, and so the two can coexist if the reel is saved later.
SCRATCH_MARK = "~"
SCRATCH_TTL_SEC = 6 * 3600   # a chat is long over by then
SCRATCH_KEEP = 20            # hard cap, in case a session never idles


def scratch_reel_id(url, project_id):
    return f"{project_id}_{SCRATCH_MARK}{reel_id_for(url, project_id).split('_', 1)[1]}"


def purge_scratch_jobs():
    """Drop scratch reels once their chat has moved on, media and all. Called at
    startup and on every new scratch request, so nothing has to run on a timer.

    Also sweeps orphaned media: a scratch reel exists only in memory, so any
    <id>~<code>.* file left on disk by a previous process has no owner and never
    will. SCRATCH_MARK appears in no other reel id, which is what makes that
    glob safe to delete from."""
    now = time.time()
    with jobs_lock:
        scratch = [(j.get("scratch_started_at") or 0, rid)
                   for rid, j in jobs.items() if j.get("scratch")]
        scratch.sort(reverse=True)   # newest first
        stale = {rid for started, rid in scratch[SCRATCH_KEEP:]}
        stale |= {rid for started, rid in scratch
                  if now - started > SCRATCH_TTL_SEC
                  and jobs[rid].get("status") in ("done", "error")}
        for rid in stale:
            jobs.pop(rid, None)
        live = {rid for _, rid in scratch} - stale

    for path in VIDEOS_DIR.glob(f"*{SCRATCH_MARK}*"):
        if path.name.split(".", 1)[0] not in live:
            path.unlink(missing_ok=True)
    return sorted(stale)


def start_scratch_job(url, user_id, project_id):
    """Queue a URL through the ordinary pipeline as a scratch reel, or hand back
    the one already in flight for it. Returns (rid, job)."""
    purge_scratch_jobs()
    rid = scratch_reel_id(url, project_id)
    with jobs_lock:
        existing = jobs.get(rid)
        if existing and existing.get("status") != "error" and existing.get("user_id") == user_id:
            return rid, dict(existing)
    job = new_job(rid, url, user_id, project_id, status="queued", scratch=True)
    update_job(rid, scratch_started_at=time.time())
    download_queue.put((rid, url))
    return rid, dict(job)


def downloader_worker():
    while True:
        rid, url = download_queue.get()
        try:
            with jobs_lock:
                user_id = jobs[rid].get("user_id") if rid in jobs else None
            with store_cache_lock:
                cached = store_cache.get(rid)
            # rid is project-prefixed, so a cache hit is already the same project's
            # copy; the user check keeps a re-processed id from ever handing back
            # another account's data. Submitting the same reel into a second
            # project misses this cache by construction and re-downloads, which is
            # what keeps the two projects' rows independent.
            if cached and cached.get("status") == "done" and cached.get("user_id") == user_id:
                with jobs_lock:
                    jobs[rid] = cached
                continue

            # yt-dlp is handed the same browser cookies, so the pin has to be
            # checked here too - otherwise a browser logged into the wrong
            # account would quietly download as that account.
            allowed, why = instagram_account_ok()
            if not allowed:
                raise RuntimeError(why)

            update_job(rid, status="downloading")

            import yt_dlp
            with yt_dlp.YoutubeDL(ydl_opts(outtmpl=str(VIDEOS_DIR / f"{rid}.%(ext)s"))) as ydl:
                info = ydl.extract_info(url, download=True)

            requested = info.get("requested_downloads") or []
            video_path = Path(requested[0]["filepath"]) if requested else VIDEOS_DIR / f"{rid}.mp4"
            cdn_url = (requested[0].get("url") if requested else None) or info.get("url")

            meta = {
                "uploader": info.get("uploader"),
                "uploader_id": info.get("uploader_id"),
                "channel": info.get("channel"),
                "like_count": info.get("like_count"),
                "comment_count": info.get("comment_count"),
                "view_count": info.get("view_count"),
                "description": info.get("description"),
                "timestamp": info.get("timestamp"),
                "cdn_url": cdn_url,
            }

            # yt-dlp has no view count for reels, so top it up from the web API
            # while we already have this reel's page context.
            meta = merge_reel_stats(meta, fetch_reel_stats(url))

            thumb_path = VIDEOS_DIR / f"{rid}.jpg"
            thumb_file = None
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", "0.3", "-i", str(video_path), "-vframes", "1", "-update", "1", str(thumb_path)],
                    capture_output=True, timeout=20,
                )
                if thumb_path.exists():
                    thumb_file = thumb_path.name
            except Exception:
                pass

            # Local ffmpeg work, no API involved, so every reel gets a storyboard.
            try:
                frames = extract_frames(rid, video_path, info.get("duration"))
            except Exception as exc:
                print(f"[downloader_worker] frame extraction failed for {rid}: {exc}", file=sys.stderr)
                frames = []

            with jobs_lock:
                job_now = dict(jobs.get(rid) or {})
            meta, extra = enrich_job_from_stats(job_now, meta)
            update_job(rid, status="transcribing", meta=meta, thumb_file=thumb_file,
                       video_file=video_path.name, frames=frames, **extra)
            transcribe_queue.put(("new", rid, video_path))

        except Exception as exc:
            update_job(rid, status="error", error=describe_download_error(exc))
        finally:
            download_queue.task_done()


def transcriber_worker():
    while True:
        kind, rid, video_path = transcribe_queue.get()
        try:
            with jobs_lock:
                user_id = jobs[rid].get("user_id") if rid in jobs else None
            transcript, segments = transcribe_audio(video_path, user_id)

            if kind == "new":
                update_job(rid, status="done", transcript=transcript, segments=segments,
                           created_at=datetime.now(timezone.utc).isoformat())
            else:  # backfill: reel is already done, just fill in segments
                update_job(rid, segments=segments)

            persist_reel(rid)

            if kind == "new":
                # Everything a single reel needs runs on its own from here:
                # visuals first, then the four-part breakdown, which reads the
                # scene structure the visual pass produced. Only the cross-reel
                # formula run on the Analysis page stays manual.
                mark_ai_pending(rid, "visual")
                visual_queue.put((rid, True))

        except Exception as exc:
            print(f"[transcriber_worker] {kind} failed for {rid}: {exc}", file=sys.stderr)
            if kind == "new":
                update_job(rid, status="error", error=str(exc))
        finally:
            transcribe_queue.task_done()


def analysis_worker():
    while True:
        rid = analysis_queue.get()
        try:
            with jobs_lock:
                job = jobs.get(rid)
            if job and job.get("transcript") and not job.get("analysis"):
                settings = with_platform_fallback(load_settings(job.get("user_id")))
                if settings.get("api_key"):
                    project = get_project(job.get("project_id"))
                    analysis = call_ai(settings, build_analysis_prompt(project, job["transcript"],
                                                                        visual_context(job)))
                    update_job(rid, analysis=analysis)
                    persist_reel(rid)
        except Exception as exc:
            print(f"[analysis_worker] failed for {rid}: {exc}", file=sys.stderr)
        finally:
            # The Reel Card (tags + diagnosis) reads the breakdown, so it runs
            # after it - chained here rather than queued alongside.
            with jobs_lock:
                job = jobs.get(rid)
            if job and job.get("analysis") and not job.get("tags"):
                mark_ai_pending(rid, "card")
                card_queue.put(rid)
            else:
                mark_ai_pending(rid, None)
            analysis_queue.task_done()


def card_worker():
    """Reel Card pass: taxonomy tags + a per-reel diagnosis. Text-only, one call."""
    while True:
        rid = card_queue.get()
        try:
            with jobs_lock:
                job = jobs.get(rid)
            if job and job.get("analysis") and job.get("status") == "done":
                settings = with_platform_fallback(load_settings(job.get("user_id")))
                if settings.get("api_key"):
                    tags, diagnosis = run_reel_card(job, settings, get_project(job.get("project_id")))
                    update_job(rid, tags=tags, diagnosis=diagnosis)
                    persist_reel(rid)
        except Exception as exc:
            print(f"[card_worker] failed for {rid}: {exc}", file=sys.stderr)
        finally:
            mark_ai_pending(rid, None)
            card_queue.task_done()


def visual_worker():
    """Vision pass over a reel's storyboard frames. Extracts the frames first if
    the reel predates the feature, so a bulk run can pick up an old library."""
    while True:
        rid, chain_analysis = visual_queue.get()
        try:
            with jobs_lock:
                job = jobs.get(rid)
            if job and not job.get("visual") and job.get("video_file"):
                settings = with_platform_fallback(load_settings(job.get("user_id")))
                if settings.get("api_key"):
                    if not job.get("frames"):
                        frames = extract_frames(rid, VIDEOS_DIR / job["video_file"])
                        update_job(rid, frames=frames)
                        persist_reel(rid)  # frames are worth keeping even if the vision call then fails
                        with jobs_lock:
                            job = jobs.get(rid)
                    if job.get("frames"):
                        visual = run_visual_analysis(job, settings, get_project(job.get("project_id")))
                        update_job(rid, visual=visual)
                        persist_reel(rid)
        except Exception as exc:
            print(f"[visual_worker] failed for {rid}: {exc}", file=sys.stderr)
        finally:
            # The breakdown is chained even when the visual pass failed - it
            # reads better with scene context but doesn't need it.
            if chain_analysis:
                mark_ai_pending(rid, "breakdown")
                analysis_queue.put(rid)
            else:
                mark_ai_pending(rid, None)
            visual_queue.task_done()


def link_worker():
    """Refreshes the per-reel things that go stale or arrive late: the signed CDN
    link, and the view/reshare counts yt-dlp can't see."""
    while True:
        rid, want_link = link_queue.get()
        try:
            with jobs_lock:
                job = jobs.get(rid)
            if job and job.get("url"):
                meta = dict(job.get("meta") or {})
                changed = False

                if want_link:
                    import yt_dlp
                    with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
                        info = ydl.extract_info(job["url"], download=False)
                    cdn_url = info.get("url")
                    if cdn_url:
                        meta["cdn_url"] = cdn_url
                        changed = True

                stats = fetch_reel_stats(job["url"])
                extra = {}
                if stats:
                    meta = merge_reel_stats(meta, stats)
                    meta, extra = enrich_job_from_stats(job, meta)
                    changed = True

                if changed:
                    update_job(rid, meta=meta, **extra)
                    persist_reel(rid)
        except Exception as exc:
            print(f"[link_worker] failed for {rid}: {exc}", file=sys.stderr)
        finally:
            link_queue.task_done()


store_cache = load_store()  # global; per-user filtering happens at each route
purge_scratch_jobs()        # scratch media a previous process left behind has no owner

for _ in range(DOWNLOAD_WORKERS):
    threading.Thread(target=downloader_worker, daemon=True).start()
threading.Thread(target=transcriber_worker, daemon=True).start()
for _ in range(2):
    threading.Thread(target=analysis_worker, daemon=True).start()
for _ in range(2):
    threading.Thread(target=visual_worker, daemon=True).start()
for _ in range(2):
    threading.Thread(target=card_worker, daemon=True).start()
for _ in range(3):
    threading.Thread(target=link_worker, daemon=True).start()


@app.route("/")
def index():
    return send_from_directory(BASE_DIR / "templates", "index.html")


@app.route("/media/<path:filename>")
def media(filename):
    return send_from_directory(VIDEOS_DIR, filename)


@app.route("/api/auth-config")
def auth_config():
    """Unauthenticated by design - the frontend needs this before it can sign in
    at all. Keeps the Neon Auth URL out of the HTML so branch->main cutover is a
    one-line .env change, not a template edit."""
    return jsonify({"base_url": NEON_AUTH_BASE_URL})


# Reels transcribed before the pipeline ran the AI passes automatically get
# caught up the first time their owner loads them - but only once per reel per
# process, so a reel the model can never handle (no vision support, say) costs
# one failed attempt rather than one on every poll.
_ai_attempted = set()
_ai_attempted_lock = threading.Lock()
_stats_attempted = set()


def autoqueue_missing_stats(snapshot):
    """Reels downloaded before view counts were fetched have no view_count in
    their meta. Top them up once per process, through the existing per-reel
    worker so the rate limiting is shared."""
    for job in snapshot:
        rid = job.get("id")
        meta = job.get("meta") or {}
        if job.get("status") != "done" or meta.get("stats_v") == STATS_VERSION:
            continue
        with _ai_attempted_lock:
            if rid in _stats_attempted:
                continue
            _stats_attempted.add(rid)
        with jobs_lock:
            if rid not in jobs:
                jobs[rid] = job
        link_queue.put((rid, False))   # counts only - don't burn a yt-dlp call on a fresh CDN link


def autoqueue_pending_ai(snapshot, user_id):
    settings = with_platform_fallback(load_settings(user_id))
    if not settings.get("api_key"):
        return

    for job in snapshot:
        rid = job.get("id")
        if job.get("status") != "done" or job.get("ai_pending"):
            continue
        # bool(), not the truthy string `and` hands back - needs_analysis is put
        # on the queue as the chain flag and should read as a flag.
        needs_visual = bool(not job.get("visual") and job.get("video_file"))
        needs_analysis = bool(not job.get("analysis") and job.get("transcript"))
        needs_card = bool(job.get("analysis") and not job.get("tags"))
        if not (needs_visual or needs_analysis or needs_card):
            continue

        with _ai_attempted_lock:
            if rid in _ai_attempted:
                continue
            _ai_attempted.add(rid)

        with jobs_lock:
            if rid not in jobs:
                jobs[rid] = job

        if needs_visual:
            mark_ai_pending(rid, "visual")
            visual_queue.put((rid, needs_analysis))
        elif needs_analysis:
            mark_ai_pending(rid, "breakdown")   # the card chains off the breakdown
            analysis_queue.put(rid)
        else:
            mark_ai_pending(rid, "card")
            card_queue.put(rid)


@app.route("/api/jobs")
@require_login
@require_project
def all_jobs():
    with store_cache_lock:
        merged = dict(store_cache)
    with jobs_lock:
        merged.update(jobs)
        snapshot = [j for j in merged.values()
                    if j.get("user_id") == g.user_id and j.get("project_id") == g.project_id
                    and not j.get("scratch")]

    autoqueue_pending_ai(snapshot, g.user_id)
    autoqueue_missing_stats(snapshot)

    active = [j for j in snapshot if j.get("status") not in ("done", "error")]
    finished = sorted(
        [j for j in snapshot if j.get("status") in ("done", "error")],
        key=lambda j: j.get("created_at") or "", reverse=True,
    )
    # Project-relative metrics (reach multiple, percentiles, outcome band) ride
    # along on each done reel. Computed per request, never stored - they change
    # whenever any reel in the project does.
    ranked = {r["job"]["id"]: r["metrics"]
              for r in _rank_rows([j for j in finished if j.get("status") == "done"], g.project_id)}
    payload = [({**j, "metrics": ranked[j["id"]]} if j["id"] in ranked else j) for j in active + finished]
    return jsonify(payload)


def call_openai(settings, prompt):
    resp = requests.post(
        f"{settings['base_url'].rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"},
        json={
            "model": settings["model"],
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def call_anthropic(settings, prompt, max_tokens=2048):
    resp = requests.post(
        f"{settings['base_url'].rstrip('/')}/v1/messages",
        headers={
            "x-api-key": settings["api_key"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": settings["model"],
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt +
                          "\n\nRespond with ONLY the JSON object and nothing else - no markdown fencing, no commentary."}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["content"][0]["text"].strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content)


def call_ai(settings, prompt, max_tokens=2048):
    if settings["provider"] == "anthropic":
        return call_anthropic(settings, prompt, max_tokens=max_tokens)
    return call_openai(settings, prompt)


def _frame_b64(path):
    return base64.b64encode(path.read_bytes()).decode("ascii")


def call_openai_vision(settings, prompt, image_paths):
    # detail="low" caps each frame at a fixed, small token cost - the frames are
    # already downscaled to FRAME_WIDTH and we care about what is happening in
    # them, not fine print.
    content = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{_frame_b64(path)}", "detail": "low"}}
                for path in image_paths]
    resp = requests.post(
        f"{settings['base_url'].rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"},
        json={
            "model": settings["model"],
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
        },
        timeout=180,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])


def call_anthropic_vision(settings, prompt, image_paths, max_tokens):
    content = [{"type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": _frame_b64(path)}}
               for path in image_paths]
    content.append({"type": "text", "text": prompt +
                    "\n\nRespond with ONLY the JSON object and nothing else - no markdown fencing, no commentary."})
    resp = requests.post(
        f"{settings['base_url'].rstrip('/')}/v1/messages",
        headers={"x-api-key": settings["api_key"], "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
        json={"model": settings["model"], "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": content}]},
        timeout=180,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def call_ai_vision(settings, prompt, image_paths, max_tokens=3000):
    if settings["provider"] == "anthropic":
        return call_anthropic_vision(settings, prompt, image_paths, max_tokens)
    return call_openai_vision(settings, prompt, image_paths)


def pick_vision_frames(frames):
    """Thin an oversized storyboard down to VISION_MAX_FRAMES, evenly spaced and
    always keeping the first and last frame - the open and the payoff are the two
    beats worth guaranteeing."""
    if len(frames) <= VISION_MAX_FRAMES:
        return frames
    step = (len(frames) - 1) / (VISION_MAX_FRAMES - 1)
    return [frames[round(i * step)] for i in range(VISION_MAX_FRAMES)]


def run_visual_analysis(job, settings, project):
    """Describe what happens on screen, from the reel's frames plus its transcript."""
    frames = pick_vision_frames(job.get("frames") or [])
    paths = [VIDEOS_DIR / f["file"] for f in frames]
    paths = [path for path in paths if path.exists()]
    if not paths:
        raise RuntimeError("No frame images on disk for this reel - extract frames first.")

    segments = job.get("segments") or []
    duration = round(max([f["t"] for f in frames] + [seg["end"] for seg in segments] or [0]), 1) or None
    transcript = segments_to_script(segments) or (job.get("transcript") or "")
    prompt = VISUAL_PROMPT_TEMPLATE.format(
        frame_count=len(paths),
        duration=duration if duration else "unknown",
        project_context=project_prompt_context(project),
        timestamps=", ".join(str(f["t"]) for f in frames[:len(paths)]),
        transcript=transcript or "(no transcript available)",
    )
    return clean_visual(call_ai_vision(settings, prompt, paths), duration)


def clean_visual(result, duration):
    """Trust the model for prose, not for numbers. Scene times come back as
    anything from strings to out-of-order overlaps, and the timeline bar and
    per-scene thumbnails both index straight off them - so clamp, sort and
    close the gaps here rather than defending against it in the UI."""
    if not isinstance(result, dict):
        raise RuntimeError("The model returned an unexpected shape for the visual breakdown.")

    def num(value):
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    scenes = []
    for raw in (result.get("scenes") or []):
        if not isinstance(raw, dict):
            continue
        start, end = num(raw.get("start")), num(raw.get("end"))
        if start is None:
            continue
        scene = {k: (raw.get(k) or "").strip() if isinstance(raw.get(k), str) else ""
                 for k in ("label", "title", "goal", "beat", "description")}
        scene["start"] = start
        scene["end"] = end
        scene["label"] = scene["label"] or scene["title"] or f"Scene {len(scenes) + 1}"
        scenes.append(scene)

    scenes.sort(key=lambda sc: sc["start"])
    for i, scene in enumerate(scenes):
        nxt = scenes[i + 1]["start"] if i + 1 < len(scenes) else duration
        # A missing/overlapping end runs to the next scene's start; the last one
        # runs to the end of the video. Gaps would leave holes in the timeline bar.
        if scene["end"] is None or nxt is None or scene["end"] > nxt or scene["end"] <= scene["start"]:
            scene["end"] = nxt if nxt and nxt > scene["start"] else (duration or scene["start"])
        if duration:
            scene["start"] = min(scene["start"], duration)
            scene["end"] = min(scene["end"], duration)

    cleaned = dict(result)
    cleaned["scenes"] = scenes
    cleaned["duration"] = duration
    for key in ("key_topics", "on_screen_text"):
        values = cleaned.get(key)
        cleaned[key] = [v.strip() for v in values if isinstance(v, str) and v.strip()] if isinstance(values, list) else []
    return cleaned


def visual_context(job, limit=1400):
    """Compact one-line-per-field summary of a reel's visual analysis, for the
    text-only prompts (per-reel breakdown, bulk formulas) that can't see frames."""
    visual = job.get("visual") or {}
    if not visual:
        return None

    fields = ("hook_type", "visual_opening", "visual_hook", "setting", "subject", "editing",
              "why_it_works", "summary")
    lines = [f"{key.replace('_', ' ')}: {visual[key].strip()}"
             for key in fields if isinstance(visual.get(key), str) and visual[key].strip()]

    scenes = visual.get("scenes") or []
    if scenes:
        lines.append("scene structure: " + " -> ".join(
            f"{sc.get('label') or 'scene'} ({format_timestamp(sc.get('start') or 0)}-"
            f"{format_timestamp(sc.get('end') or 0)}, goal: {sc.get('goal') or 'n/a'})" for sc in scenes))
    if visual.get("key_topics"):
        lines.append("key topics: " + ", ".join(visual["key_topics"][:6]))
    if visual.get("on_screen_text"):
        lines.append("on-screen text: " + " | ".join(visual["on_screen_text"][:8]))

    return "; ".join(lines)[:limit] or None


CARD_PROMPT_METRIC_KEYS = (
    "duration_sec", "wpm", "word_count", "sentence_count", "avg_sentence_len", "question_count",
    "filler_per_1k", "you_density", "number_mentions", "hook_word_count", "cta_position_pct",
    "view_count", "like_count", "comment_count", "reshare_count", "engagement_rate_pct",
    "reshares_per_1k_views", "likes_per_1k_views", "comments_per_1k_views",
    "creator", "follower_count", "reach_multiple", "source", "outcome_band", "outcome_basis",
    "view_count_pct_in_project", "reach_multiple_pct_in_project",
    "reshares_per_1k_views_pct_in_project", "engagement_rate_pct_pct_in_project",
    "views_pct_in_creator", "creator_reel_count", "is_full_hd", "is_original_audio",
    "audio_title", "is_collab", "is_paid_partnership", "posted_dow", "posted_hour_utc",
    "caption_word_count", "hashtag_count",
)


def metrics_for_job(job):
    """This reel's metrics with the project-relative annotations (percentiles,
    outcome band) - built by ranking every done reel in its project, the same
    way the Analysis page does. Usable from worker threads (no request context)."""
    user_id, project_id = job.get("user_id"), job.get("project_id")
    done = [j for j in _done_jobs(user_id, project_id)]
    if job.get("id") not in {j["id"] for j in done}:
        done.append(job)
    rows = _rank_rows(done, project_id)
    for r in rows:
        if r["job"]["id"] == job.get("id"):
            return r["metrics"]
    return compute_metrics(job)


def build_card_prompt(job, project, metrics):
    reel = {
        "id": job.get("id"),
        "source": job.get("source") or "reference",
        "metrics": {k: metrics.get(k) for k in CARD_PROMPT_METRIC_KEYS if metrics.get(k) is not None},
        "caption": (job.get("caption") or "")[:600] or None,
        "hashtags": (job.get("hashtags") or [])[:15],
        "breakdown": job.get("analysis") or None,
        "visual": visual_context(job, limit=1800),
        "insights": job.get("insights") or None,
        "transcript": (segments_to_script(job.get("segments")) if job.get("segments")
                       else (job.get("transcript") or ""))[:7000],
    }
    basis_note = ("absolute view thresholds" if metrics.get("outcome_basis") == "views"
                  else "reach multiple bands" if metrics.get("outcome_basis") == "reach_multiple"
                  else "likes+comments, no view count")
    return REEL_CARD_PROMPT_TEMPLATE.format(
        project_context=project_prompt_context(project),
        taxonomy=taxonomy.prompt_block(),
        outcome_basis_note=basis_note,
        algorithm_context=algorithm_prompt_block(),
        reel=json.dumps(reel, ensure_ascii=False),
    )


DIAGNOSIS_SIGNALS = ("watch_time", "likes", "sends")
DIAGNOSIS_LEVELS = ("strong", "ok", "weak", "unknown")
DIAGNOSIS_QUOTE_MAX_WORDS = 14


def clean_diagnosis(raw, reel_blob):
    """Validate the model's diagnosis. Quotes are verified against the reel's
    own text (transcript + breakdown + on-screen text) and dropped when absent -
    the point survives, the fake quote doesn't. Returns (diagnosis, problems)."""
    raw = raw if isinstance(raw, dict) else {}
    problems = []

    def text(value, limit):
        return str(value).strip()[:limit] if isinstance(value, (str, int, float)) and str(value).strip() else None

    def items(value):
        out = []
        for item in (value if isinstance(value, list) else [])[:6]:
            if isinstance(item, str):
                item = {"point": item}
            if not isinstance(item, dict):
                continue
            point = text(item.get("point"), 240)
            if not point:
                continue
            quote = text(item.get("quote"), 200)
            if quote:
                words = quote.split()
                found = _normalise_for_match(quote) in reel_blob
                if len(words) > DIAGNOSIS_QUOTE_MAX_WORDS or not found:
                    problems.append(f"quote dropped ({'too long' if len(words) > DIAGNOSIS_QUOTE_MAX_WORDS else 'not in reel'}): {quote[:60]!r}")
                    quote = None
            out.append({"point": point, "lever": text(item.get("lever"), 80), "quote": quote})
        return out

    signals = raw.get("signals") if isinstance(raw.get("signals"), dict) else {}
    return {
        "one_line": text(raw.get("one_line"), 400),
        "what_drove_it": items(raw.get("what_drove_it")),
        "what_held_it_back": items(raw.get("what_held_it_back")),
        "signals": {k: (str(signals.get(k)).strip().lower() if str(signals.get(k)).strip().lower() in DIAGNOSIS_LEVELS else "unknown")
                    for k in DIAGNOSIS_SIGNALS},
        "one_change": text(raw.get("one_change"), 400),
        "confidence": (str(raw.get("confidence")).strip().lower()
                       if str(raw.get("confidence")).strip().lower() in ("low", "medium", "high") else "low"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, problems


def run_reel_card(job, settings, project):
    """One text call: taxonomy tags + diagnosis for a reel. Returns (tags, diagnosis)."""
    metrics = metrics_for_job(job)
    result = call_ai(settings, build_card_prompt(job, project, metrics), max_tokens=3000)
    tags, tag_problems = taxonomy.clean_tags(result.get("tags"), metrics)
    blob = _reel_text_index([{"job": job}]).get(job["id"], "")
    diagnosis, diag_problems = clean_diagnosis(result.get("diagnosis"), blob)
    for problem in tag_problems + diag_problems:
        print(f"[reel card] {job['id']}: {problem}", file=sys.stderr)
    return tags, diagnosis


def build_analysis_prompt(project, transcript, visual=None):
    prompt = ANALYSIS_PROMPT.format(project_context=project_prompt_context(project))
    if visual:
        # Goes BEFORE the trailing "Transcript:" marker. Appended after it, the
        # model reads the visual summary as the opening of the transcript and
        # returns it verbatim as the hook (this happened to real rows).
        context = ("\nFor context only, here is what happens on screen in this reel - use it to place the "
                   "boundaries between parts (e.g. a cut or a text card often marks where one part ends), "
                   "never as text to include in your output: " + visual + "\n\n")
        marker = "Transcript:\n"
        if prompt.endswith(marker):
            prompt = prompt[:-len(marker)] + context + marker
        else:
            prompt += context
    return prompt + transcript


@app.route("/api/projects", methods=["GET"])
@require_login
def get_projects():
    ensure_default_project(g.user_id)
    return jsonify(list_projects(g.user_id))


@app.route("/api/projects", methods=["POST"])
@require_login
def post_project():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not (1 <= len(name) <= 60):
        return jsonify({"error": "Project name must be 1-60 characters."}), 400

    row = create_project(
        g.user_id, name,
        emoji=(data.get("emoji") or "").strip()[:8] or None,
        description=(data.get("description") or "").strip() or None,
        instructions=(data.get("instructions") or "").strip() or None,
        own_username=data.get("own_username"),
        outcome_mode=data.get("outcome_mode") or "views",
    )
    if not row:
        return jsonify({"error": "You already have a project with that name."}), 409
    return jsonify(_jsonify_row(row)), 201


@app.route("/api/projects/<int:project_id>", methods=["PATCH"])
@require_login
def patch_project(project_id):
    if not get_project(project_id, g.user_id):
        return jsonify({"error": "Project not found."}), 404

    data = request.get_json(force=True)
    fields, params = [], {"id": project_id, "user_id": g.user_id}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not (1 <= len(name) <= 60):
            return jsonify({"error": "Project name must be 1-60 characters."}), 400
        fields.append("name = %(name)s")
        params["name"] = name
    for field, limit in (("emoji", 8), ("description", 2000), ("instructions", 8000)):
        if field in data:
            fields.append(f"{field} = %({field})s")
            params[field] = (data.get(field) or "").strip()[:limit] or None
    own_changed = False
    if "own_username" in data:
        fields.append("own_username = %(own_username)s")
        params["own_username"] = clean_own_username(data.get("own_username"))
        own_changed = True
    if "outcome_mode" in data:
        if data.get("outcome_mode") not in ("views", "reach"):
            return jsonify({"error": "outcome_mode must be 'views' or 'reach'."}), 400
        fields.append("outcome_mode = %(outcome_mode)s")
        params["outcome_mode"] = data["outcome_mode"]
    if not fields:
        return jsonify({"error": "Nothing to update."}), 400

    # The rename collision surfaces as an IntegrityError on projects_user_name_uidx;
    # catching it outside the cursor block lets db_cursor roll the transaction back
    # before we answer.
    try:
        with db_cursor() as cur:
            cur.execute(
                f"UPDATE projects SET {', '.join(fields)}, updated_at = now() "
                "WHERE id = %(id)s AND user_id = %(user_id)s "
                f"RETURNING {PROJECT_COLUMNS}",
                params,
            )
            row = cur.fetchone()
    except psycopg2.IntegrityError:
        return jsonify({"error": "You already have a project with that name."}), 409
    invalidate_project(project_id)
    if own_changed:
        reapply_reel_sources(project_id, params["own_username"])
    return jsonify(_jsonify_row(row))


def reapply_reel_sources(project_id, own_username):
    """own/reference is derived from the project's handle, so a change to the
    handle re-marks every reel in the project - in the DB and in the in-memory
    copies the workers and /api/jobs read from."""
    own = (own_username or "").lower()
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE reels SET source = CASE WHEN %s <> '' AND lower(meta->>'username') = %s
                                           THEN 'own' ELSE 'reference' END
            WHERE project_id = %s RETURNING id, source
            """,
            (own, own, project_id),
        )
        updated = {r["id"]: r["source"] for r in cur.fetchall()}
    with store_cache_lock:
        for rid, source in updated.items():
            if rid in store_cache:
                store_cache[rid] = {**store_cache[rid], "source": source}
    with jobs_lock:
        for rid, source in updated.items():
            if rid in jobs:
                jobs[rid]["source"] = source


@app.route("/api/projects/<int:project_id>", methods=["DELETE"])
@require_login
def delete_project(project_id):
    """Deletes the project and everything scoped to it (reels, formulas,
    evidence, analysis runs, categories) via FK cascade. The last project can't
    be deleted - the UI always needs one selected."""
    if not get_project(project_id, g.user_id):
        return jsonify({"error": "Project not found."}), 404

    with db_cursor() as cur:
        cur.execute("SELECT count(*) FROM projects WHERE user_id = %s", (g.user_id,))
        if cur.fetchone()["count"] <= 1:
            return jsonify({"error": "You need at least one project."}), 400
        cur.execute("SELECT id FROM reels WHERE project_id = %s", (project_id,))
        reel_ids = [r["id"] for r in cur.fetchall()]
        cur.execute("DELETE FROM projects WHERE id = %s AND user_id = %s", (project_id, g.user_id))
    invalidate_project(project_id)

    # Only drop the in-memory/on-disk trail once the DB delete has committed.
    with store_cache_lock:
        for rid in reel_ids:
            store_cache.pop(rid, None)
    with jobs_lock:
        for rid in reel_ids:
            jobs.pop(rid, None)
    for rid in reel_ids:
        for path in VIDEOS_DIR.glob(f"{rid}.*"):
            path.unlink(missing_ok=True)

    return jsonify({"ok": True, "deleted_reels": len(reel_ids)})


@app.route("/api/algorithm")
@require_login
def algorithm_brief():
    """The research agent's current brief: what the app is telling the AI about
    the platform, and the sources behind it. Unauthenticated users have no
    business seeing it, but it isn't per-project - it's the same for everyone."""
    brief = load_algorithm_brief()
    if not brief:
        return jsonify({"available": False})
    return jsonify({
        "available": bool(brief.get("updated") and brief.get("prompt_text")),
        "updated": brief.get("updated"),
        "next_review": brief.get("next_review"),
        "sections": brief.get("sections") or {},
        "sources": brief.get("sources") or [],
    })


@app.route("/api/settings", methods=["GET"])
@require_login
def get_settings():
    s = load_settings(g.user_id)
    platform_search = next((p for p in ("tavily", "langsearch", "brave") if PLATFORM_SEARCH_KEYS.get(p)), None)
    return jsonify({"provider": s["provider"], "base_url": s["base_url"], "model": s["model"],
                     "has_key": bool(s.get("api_key")), "has_groq_key": bool(s.get("groq_api_key")),
                     "search_provider": s.get("search_provider") or "",
                     "has_search_key": bool(s.get("search_api_key")),
                     "platform_search_provider": platform_search})


@app.route("/api/settings", methods=["POST"])
@require_login
def update_settings():
    data = request.get_json(force=True)
    s = load_settings(g.user_id)
    for field in ("provider", "base_url", "model"):
        if data.get(field):
            s[field] = data[field]
    if data.get("api_key"):
        s["api_key"] = data["api_key"]
    if data.get("groq_api_key"):
        s["groq_api_key"] = data["groq_api_key"]
    if "search_provider" in data:
        provider = (data.get("search_provider") or "").strip().lower()
        if provider and provider not in ("tavily", "langsearch", "brave"):
            return jsonify({"error": "search_provider must be tavily, langsearch or brave."}), 400
        s["search_provider"] = provider
        if not provider:
            s["search_api_key"] = ""   # clearing the provider drops the key too
    if data.get("search_api_key"):
        s["search_api_key"] = data["search_api_key"].strip()
    save_settings(g.user_id, s)
    return jsonify({"ok": True})


def job_for_request(rid):
    """Resolve a reel id to its live job dict for the current user+project,
    hydrating it out of the store cache into `jobs` first - update_job and
    persist_reel only ever index into `jobs`. None when it isn't theirs."""
    with jobs_lock:
        job = jobs.get(rid)
    if not job:
        with store_cache_lock:
            job = store_cache.get(rid)
        if job:
            with jobs_lock:
                jobs[rid] = job
    if not job or job.get("user_id") != g.user_id or job.get("project_id") != g.project_id:
        return None
    return job


@app.route("/api/analyze/<rid>", methods=["POST"])
@require_login
@require_project
def analyze(rid):
    settings = with_platform_fallback(load_settings(g.user_id))
    if not settings.get("api_key"):
        return jsonify({"error": "No AI API key configured. Add one in Settings."}), 400

    job = job_for_request(rid)
    if not job:
        return jsonify({"error": "Reel not found."}), 404

    if job.get("status") != "done" or not job.get("transcript"):
        return jsonify({"error": "This reel isn't transcribed yet."}), 400

    try:
        analysis = call_ai(settings, build_analysis_prompt(g.project, job["transcript"],
                                                           visual_context(job)))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    update_job(rid, analysis=analysis)
    persist_reel(rid)

    return jsonify(analysis)


@app.route("/api/card/<rid>", methods=["POST"])
@require_login
@require_project
def reel_card(rid):
    """(Re)run the Reel Card - taxonomy tags + diagnosis - for one reel. Needs the
    four-part breakdown to exist; the visual pass helps but isn't required."""
    settings = with_platform_fallback(load_settings(g.user_id))
    if not settings.get("api_key"):
        return jsonify({"error": "No AI API key configured. Add one in Settings."}), 400
    job = job_for_request(rid)
    if not job:
        return jsonify({"error": "Reel not found."}), 404
    if job.get("status") != "done" or not job.get("analysis"):
        return jsonify({"error": "Run the AI breakdown first - the card reads it."}), 400
    try:
        tags, diagnosis = run_reel_card(job, settings, g.project)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    update_job(rid, tags=tags, diagnosis=diagnosis)
    persist_reel(rid)
    return jsonify({"tags": tags, "diagnosis": diagnosis, "metrics": metrics_for_job(job)})


INSIGHT_FIELDS = {
    # Instagram Insights a creator can read on their own reels and nowhere else.
    "avg_watch_time_s": float, "retention_3s_pct": float, "completion_pct": float,
    "non_follower_pct": float, "saves": int, "profile_visits": int, "follows": int,
    "plays": int, "reach": int, "sends": int,
}


@app.route("/api/reels/<rid>/insights", methods=["PATCH"])
@require_login
@require_project
def reel_insights(rid):
    """Hand-entered Instagram Insights for an own reel. Only the known fields are
    kept; blanks clear a field; everything is numeric."""
    job = job_for_request(rid)
    if not job:
        return jsonify({"error": "Reel not found."}), 404
    data = request.get_json(force=True) or {}
    insights = dict(job.get("insights") or {})
    for key, caster in INSIGHT_FIELDS.items():
        if key not in data:
            continue
        value = data.get(key)
        if value in (None, ""):
            insights.pop(key, None)
            continue
        try:
            insights[key] = caster(value)
        except (TypeError, ValueError):
            return jsonify({"error": f"{key} must be a number."}), 400
    note = data.get("note")
    if note is not None:
        note = str(note).strip()[:500]
        if note:
            insights["note"] = note
        else:
            insights.pop("note", None)
    insights["updated_at"] = datetime.now(timezone.utc).isoformat()
    update_job(rid, insights=insights)
    persist_reel(rid)
    return jsonify({"insights": insights})


@app.route("/api/frames/<rid>", methods=["POST"])
@require_login
@require_project
def reel_frames(rid):
    """Extract (or re-extract) a reel's storyboard. Synchronous - it's a dozen
    local ffmpeg seeks, no API call, so it comes back inside the request."""
    job = job_for_request(rid)
    if not job:
        return jsonify({"error": "Reel not found."}), 404
    if not job.get("video_file"):
        return jsonify({"error": "This reel has no downloaded video to pull frames from."}), 400

    video_path = VIDEOS_DIR / job["video_file"]
    if not video_path.exists():
        return jsonify({"error": "The video file for this reel is missing from disk."}), 400

    try:
        frames = extract_frames(rid, video_path)
    except Exception as exc:
        return jsonify({"error": f"Frame extraction failed: {exc}"}), 500
    if not frames:
        return jsonify({"error": "ffmpeg produced no frames for this video."}), 500

    update_job(rid, frames=frames)
    persist_reel(rid)
    return jsonify({"frames": frames})


@app.route("/api/visual/<rid>", methods=["POST"])
@require_login
@require_project
def reel_visual(rid):
    """Vision pass over the storyboard: what happens on screen, beat by beat."""
    settings = with_platform_fallback(load_settings(g.user_id))
    if not settings.get("api_key"):
        return jsonify({"error": "No AI API key configured. Add one in Settings."}), 400

    job = job_for_request(rid)
    if not job:
        return jsonify({"error": "Reel not found."}), 404
    if job.get("status") != "done":
        return jsonify({"error": "This reel isn't processed yet."}), 400

    frames = job.get("frames")
    if not frames:
        if not job.get("video_file") or not (VIDEOS_DIR / job["video_file"]).exists():
            return jsonify({"error": "This reel has no video on disk to pull frames from."}), 400
        frames = extract_frames(rid, VIDEOS_DIR / job["video_file"])
        if not frames:
            return jsonify({"error": "ffmpeg produced no frames for this video."}), 500
        update_job(rid, frames=frames)
        persist_reel(rid)  # frames are worth keeping even if the vision call then fails
        job = job_for_request(rid)

    try:
        visual = run_visual_analysis(job, settings, g.project)
    except requests.HTTPError as exc:
        detail = exc.response.text[:400] if exc.response is not None else str(exc)
        return jsonify({"error": f"Visual analysis failed - the model may not accept images. {detail}"}), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    update_job(rid, visual=visual)
    persist_reel(rid)
    return jsonify({"visual": visual, "frames": frames})


@app.route("/api/export/prepare", methods=["POST"])
@require_login
@require_project
def export_prepare():
    data = request.get_json(force=True)
    need_timestamps = bool(data.get("timestamps"))
    need_analysis = bool(data.get("analysis"))
    need_link = bool(data.get("link"))

    settings = with_platform_fallback(load_settings(g.user_id))
    has_key = bool(settings.get("api_key"))

    done_jobs = _done_jobs(g.user_id, g.project_id)

    pending = set()
    for j in done_jobs:
        rid = j["id"]
        needs_ts = need_timestamps and not j.get("segments") and j.get("video_file")
        needs_analysis = need_analysis and has_key and not j.get("analysis") and j.get("transcript")
        needs_link = need_link and not (j.get("meta") or {}).get("cdn_url") and j.get("url")
        if not (needs_ts or needs_analysis or needs_link):
            continue

        with jobs_lock:
            if rid not in jobs:
                jobs[rid] = j

        if needs_link:
            link_queue.put((rid, True))
            pending.add(rid)

        if needs_ts:
            video_path = VIDEOS_DIR / j["video_file"]
            if video_path.exists():
                transcribe_queue.put(("backfill", rid, video_path))
                pending.add(rid)
        if needs_analysis:
            mark_ai_pending(rid, "breakdown")
            analysis_queue.put(rid)
            pending.add(rid)

    return jsonify({
        "pending_ids": sorted(pending),
        "needs_key": need_analysis and not has_key,
    })


@app.route("/api/export.csv")
@require_login
@require_project
def export_csv():
    include_link = request.args.get("link") == "1"
    include_metrics = request.args.get("metrics") == "1"
    include_timestamps = request.args.get("timestamps") == "1"
    include_analysis = request.args.get("analysis") == "1"

    rows = _done_jobs(g.user_id, g.project_id)
    rows.sort(key=lambda j: j.get("created_at") or "", reverse=True)

    fieldnames = ["url", "uploader", "transcript"]
    if include_link:
        fieldnames.append("video_cdn_link")
    if include_metrics:
        fieldnames += ["likes", "comments", "views", "reshares", "followers", "reach_multiple",
                       "source", "caption"]
    if include_timestamps:
        fieldnames.append("timestamped_script")
    if include_analysis:
        fieldnames += ["hook", "promise", "validation", "cta"]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for j in rows:
        m = j.get("meta") or {}
        row = {
            "url": j.get("url", ""),
            "uploader": m.get("channel") or m.get("uploader") or "",
            "transcript": j.get("transcript") or "",
        }
        if include_link:
            row["video_cdn_link"] = m.get("cdn_url") or ""
        if include_metrics:
            row["likes"] = m.get("like_count")
            row["comments"] = m.get("comment_count")
            row["views"] = m.get("view_count")
            row["reshares"] = m.get("reshare_count")
            row["followers"] = m.get("follower_count")
            row["reach_multiple"] = (round(m["view_count"] / m["follower_count"], 3)
                                     if m.get("view_count") is not None and m.get("follower_count") else None)
            row["source"] = j.get("source") or "reference"
            row["caption"] = j.get("caption") or m.get("description") or ""
        if include_timestamps:
            row["timestamped_script"] = segments_to_script(j.get("segments"))
        if include_analysis:
            a = j.get("analysis") or {}
            row["hook"] = a.get("hook", "")
            row["promise"] = a.get("promise", "")
            row["validation"] = a.get("validation", "")
            row["cta"] = a.get("cta", "")
        writer.writerow(row)

    filename = re.sub(r"[^A-Za-z0-9_-]+", "_", g.project["name"]).strip("_").lower() or "reels"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}_transcripts.csv"},
    )


def _done_jobs(user_id, project_id, include_scratch=False):
    """Every finished reel in one project. Scratch reels are excluded by default:
    this is the set the library, the exports, the lever stats and the formula
    runs are all computed from, and a one-off diagnosis must not move those
    numbers. analyze_url() passes include_scratch to rank one against them."""
    with store_cache_lock:
        merged = dict(store_cache)
    with jobs_lock:
        merged.update(jobs)
        return [j for j in merged.values()
                if j.get("status") == "done" and j.get("user_id") == user_id
                and j.get("project_id") == project_id
                and (include_scratch or not j.get("scratch"))]


def _jsonify_row(row):
    if row is None:
        return None
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def visual_batch_payload(job):
    """The visual breakdown trimmed to what a text-only model can act on. Scene
    prose is dropped in favour of the structural skeleton - labels, goals and
    time ranges are what compare across reels; a description of one reel's
    background doesn't."""
    visual = job.get("visual") or {}
    if not visual:
        return None

    payload = {key: visual[key] for key in
               ("hook_type", "visual_opening", "editing")
               if isinstance(visual.get(key), str) and visual[key].strip()}
    payload["key_topics"] = (visual.get("key_topics") or [])[:6]
    payload["on_screen_text"] = (visual.get("on_screen_text") or [])[:6]
    payload["scene_count"] = len(visual.get("scenes") or [])
    # Label, goal and timing only. The per-beat prose was ~2,000 characters a
    # reel - half the entire batch - and what compares across reels is the
    # skeleton, not one reel's description of its own background.
    payload["structure"] = [
        {"label": sc.get("label"), "goal": sc.get("goal"),
         "at": round(sc.get("start") or 0, 1)}
        for sc in (visual.get("scenes") or [])
    ]
    return payload


def _performance_value(metrics):
    """Views when we have them, likes+comments when we don't."""
    views = metrics.get("view_count")
    if views is not None:
        return ("views", views)
    return ("engagement", metrics.get("engagement_score") or 0)


def _tier_for(kind, value):
    if kind == "views":
        if value >= AMAZING_VIEW_THRESHOLD:
            return "amazing"
        return "good" if value >= GOOD_VIEW_THRESHOLD else "low"
    if value >= AMAZING_ENGAGEMENT_THRESHOLD:
        return "amazing"
    return "good" if value >= GOOD_ENGAGEMENT_THRESHOLD else "low"


def _tiered_rows(user_id, project_id):
    """All done reels with an AI breakdown, tagged with a tier.

    Tiers are RELATIVE to the batch - terciles of engagement rate - not absolute
    thresholds. Absolute thresholds sound stable but stop discriminating the
    moment a library is all one size of creator: on a real 37-reel library they
    put 34 reels in "amazing" and 1 in "low", which tells the model nothing about
    what separates a hit from a miss. Comparing a reel against its neighbours
    always produces contrast.

    Recomputed live on every call, never persisted."""
    done_jobs = [j for j in _done_jobs(user_id, project_id) if j.get("analysis")]
    return _rank_rows(done_jobs, project_id)


def _rank_rows(done_jobs, project_id):
    """Rank + tier + annotate a set of done reels from one project. Shared by the
    Analysis page (reels with a breakdown) and /api/jobs (every done reel)."""
    rows = [{"job": j, "metrics": compute_metrics(j)} for j in done_jobs]
    if not rows:
        return rows
    outcome_mode = (get_project(project_id) or {}).get("outcome_mode") or "views"

    # Reels with views and reels without are ranked among themselves, so a
    # missing view count can't push an otherwise strong reel to the bottom.
    for kind in ("views", "engagement"):
        group = [r for r in rows if _performance_value(r["metrics"])[0] == kind]
        group.sort(key=lambda r: _performance_value(r["metrics"])[1], reverse=True)
        for position, r in enumerate(group):
            r["tier"] = _tier_for(kind, _performance_value(r["metrics"])[1])
            r["metrics"]["rank_in_batch"] = position + 1
            r["metrics"]["ranked_on"] = ("views" if kind == "views"
                                         else "likes+comments (no view count available)")

    rows.sort(key=lambda r: (_performance_value(r["metrics"])[0] != "views",
                             -_performance_value(r["metrics"])[1]))
    _annotate_outcomes(rows, outcome_mode)
    return rows


# Reach-multiple bands (views / creator followers). 3x the follower count means
# the reel clearly travelled to non-followers; under 0.3x it barely reached the
# people who already follow. Overridable from the environment like the view
# thresholds are.
REACH_BREAKOUT = float(os.environ.get("REACH_BREAKOUT_MULTIPLE", 3.0))
REACH_ABOVE = float(os.environ.get("REACH_ABOVE_MULTIPLE", 1.0))
REACH_BASELINE = float(os.environ.get("REACH_BASELINE_MULTIPLE", 0.3))


def _percentile_ranks(rows, key):
    """0-100 percentile of each row's metrics[key] among rows that have it,
    written back as metrics[key + '_pct_in_project']. Ties share a rank."""
    scored = [(r["metrics"].get(key), r) for r in rows if r["metrics"].get(key) is not None]
    if not scored:
        return
    values = sorted(v for v, _ in scored)
    n = len(values)
    for value, r in scored:
        below = sum(1 for v in values if v < value)
        r["metrics"][key + "_pct_in_project"] = round(below / max(n - 1, 1) * 100) if n > 1 else 50


def _annotate_outcomes(rows, outcome_mode="views"):
    """Percentiles within the project (and within the same creator when there
    are enough of their reels), plus an outcome_band. Band basis is the project's
    outcome_mode: 'views' = the absolute view thresholds (the "50k+ means it did
    its job" rule); 'reach' = reach-multiple bands, falling back per reel to the
    view rule when no follower count is known."""
    _percentile_ranks(rows, "view_count")
    _percentile_ranks(rows, "reach_multiple")
    _percentile_ranks(rows, "reshares_per_1k_views")
    _percentile_ranks(rows, "engagement_rate_pct")

    by_creator = {}
    for r in rows:
        creator = r["metrics"].get("creator")
        if creator and r["metrics"].get("view_count") is not None:
            by_creator.setdefault(creator, []).append(r)
    for creator, group in by_creator.items():
        if len(group) < 3:
            continue
        values = sorted(g["metrics"]["view_count"] for g in group)
        for g in group:
            below = sum(1 for v in values if v < g["metrics"]["view_count"])
            g["metrics"]["views_pct_in_creator"] = round(below / (len(values) - 1) * 100)
            g["metrics"]["creator_reel_count"] = len(values)

    for r in rows:
        m = r["metrics"]
        views, reach = m.get("view_count"), m.get("reach_multiple")
        if outcome_mode == "reach" and reach is not None:
            band = ("breakout" if reach >= REACH_BREAKOUT else "above" if reach >= REACH_ABOVE
                    else "baseline" if reach >= REACH_BASELINE else "below")
            basis = "reach_multiple"
        elif views is not None:
            band = ("breakout" if views >= AMAZING_VIEW_THRESHOLD
                    else "above" if views >= GOOD_VIEW_THRESHOLD else "below")
            basis = "views"
        else:
            band = {"amazing": "breakout", "good": "above"}.get(r.get("tier"), "below")
            basis = "likes+comments (no view count)"
        m["outcome_band"] = band
        m["outcome_basis"] = basis


def compute_confidence(times_seen, evidence_count):
    """Computed from data, not AI-self-reported - a model grading its own
    certainty has no ground truth to anchor to. Re-confirmation across multiple
    runs and citation across multiple distinct reels are things we can actually
    verify."""
    score = min(times_seen, 4) + min(evidence_count, 4)
    if score >= 6:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


@app.route("/api/analysis/categories")
@require_login
@require_project
def analysis_categories():
    with db_cursor() as cur:
        cur.execute(
            "SELECT slug, display_name, sort_order FROM formula_categories "
            "WHERE project_id = %s ORDER BY sort_order, display_name",
            (g.project_id,),
        )
        return jsonify(cur.fetchall())


@app.route("/api/analysis/categories", methods=["POST"])
@require_login
@require_project
def create_analysis_category():
    display_name = (request.get_json(force=True).get("display_name") or "").strip()
    if not (1 <= len(display_name) <= 40):
        return jsonify({"error": "Category name must be 1-40 characters."}), 400

    with db_cursor() as cur:
        cur.execute("SELECT slug, display_name, sort_order FROM formula_categories WHERE project_id = %s",
                    (g.project_id,))
        existing = cur.fetchall()
        if any(r["display_name"].strip().lower() == display_name.lower() for r in existing):
            return jsonify({"error": "That category already exists in this project."}), 409
        if len(existing) >= 12:
            return jsonify({"error": "A project can have at most 12 categories."}), 400

        slug = slugify_category(display_name, {r["slug"] for r in existing})
        sort_order = max([r["sort_order"] for r in existing], default=0) + 1
        cur.execute(
            "INSERT INTO formula_categories (project_id, slug, display_name, sort_order) "
            "VALUES (%s, %s, %s, %s) RETURNING slug, display_name, sort_order",
            (g.project_id, slug, display_name, sort_order),
        )
        return jsonify(cur.fetchone()), 201


@app.route("/api/analysis/categories/<slug>", methods=["PATCH"])
@require_login
@require_project
def rename_analysis_category(slug):
    """Only the label is editable - the slug is what formulas.category stores and
    what the AI is told to use, so renaming a tab never re-buckets its formulas."""
    display_name = (request.get_json(force=True).get("display_name") or "").strip()
    if not (1 <= len(display_name) <= 40):
        return jsonify({"error": "Category name must be 1-40 characters."}), 400
    with db_cursor() as cur:
        cur.execute(
            "UPDATE formula_categories SET display_name = %s WHERE project_id = %s AND slug = %s "
            "RETURNING slug, display_name, sort_order",
            (display_name, g.project_id, slug),
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "Category not found."}), 404
    return jsonify(row)


@app.route("/api/analysis/categories/<slug>", methods=["DELETE"])
@require_login
@require_project
def delete_analysis_category(slug):
    """Takes this project's formulas in that category with it (FK cascade) - a
    formula has no meaning outside its tab. The UI states the count first."""
    with db_cursor() as cur:
        cur.execute("SELECT count(*) FROM formula_categories WHERE project_id = %s", (g.project_id,))
        if cur.fetchone()["count"] <= 1:
            return jsonify({"error": "A project needs at least one category."}), 400
        cur.execute("DELETE FROM formula_categories WHERE project_id = %s AND slug = %s RETURNING slug",
                    (g.project_id, slug))
        if not cur.fetchone():
            return jsonify({"error": "Category not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/analysis/overview")
@require_login
@require_project
def analysis_overview():
    rows = _tiered_rows(g.user_id, g.project_id)
    table = [
        {
            "id": r["job"]["id"],
            "title": ((r["job"].get("meta") or {}).get("username") or (r["job"].get("meta") or {}).get("channel")
                      or (r["job"].get("meta") or {}).get("uploader") or r["job"]["id"]),
            "thumb_file": r["job"].get("thumb_file"),
            "tier": r["tier"],
            **r["metrics"],
        }
        for r in rows
    ]
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, generated_at, amazing_count, good_count, low_count, summary, caveats "
            "FROM analysis_runs WHERE project_id = %s ORDER BY generated_at DESC LIMIT 1",
            (g.project_id,)
        )
        run = cur.fetchone()
    return jsonify({"run": _jsonify_row(run), "table": table})


def project_knowledge(user_id, project_id, dimension=None, min_n=levers.MIN_N):
    """Everything deterministic the group layer knows about a project, computed
    live from the cards: lever table, contrast pairs, baselines. Shared by the
    Levers tab, the formula run and the agent tools."""
    rows = _rank_rows(_done_jobs(user_id, project_id), project_id)
    table = levers.lever_table(rows, min_n=min_n, dimension=dimension)
    return {
        **table,
        "pairs": levers.contrast_pairs(rows, dimension=dimension),
        "own": levers.own_baseline(rows),
        "taxonomy_v": taxonomy.TAXONOMY_VERSION,
    }


@app.route("/api/analysis/levers")
@require_login
@require_project
def analysis_levers():
    dimension = request.args.get("dimension") or None
    try:
        min_n = max(2, int(request.args.get("min_n", levers.MIN_N)))
    except ValueError:
        min_n = levers.MIN_N
    return jsonify(project_knowledge(g.user_id, g.project_id, dimension=dimension, min_n=min_n))


@app.route("/api/analysis/formulas")
@require_login
@require_project
def analysis_formulas():
    category = request.args.get("category")
    query = """
        SELECT f.*, COALESCE(
            json_agg(json_build_object(
                'reel_id', fe.reel_id,
                'title', COALESCE(r.meta->>'channel', r.meta->>'uploader', fe.reel_id),
                'thumb_file', r.thumb_file,
                'url', r.url,
                'like_count', (r.meta->>'like_count')::numeric,
                'comment_count', (r.meta->>'comment_count')::numeric,
                'breakdown', fe.breakdown
            ) ORDER BY fe.added_at) FILTER (WHERE fe.reel_id IS NOT NULL), '[]'
        ) AS evidence,
        ROUND(AVG((r.meta->>'like_count')::numeric))::int AS avg_likes,
        ROUND(AVG((r.meta->>'comment_count')::numeric))::int AS avg_comments
        FROM formulas f
        LEFT JOIN formula_evidence fe ON fe.formula_id = f.id
        LEFT JOIN reels r ON r.id = fe.reel_id
        WHERE f.project_id = %(project_id)s __CATEGORY__
        GROUP BY f.id
        ORDER BY f.category, f.rating DESC NULLS LAST, f.times_seen DESC
    """
    params = {"project_id": g.project_id}
    with db_cursor() as cur:
        if category:
            params["category"] = category
            cur.execute(query.replace("__CATEGORY__", "AND f.category = %(category)s"), params)
        else:
            cur.execute(query.replace("__CATEGORY__", ""), params)
        rows = cur.fetchall()

    result = []
    for r in rows:
        row = _jsonify_row(r)
        lift = float(row["lift"]) if row.get("lift") is not None else None
        row["lift"] = lift
        row["confidence"] = levers.formula_confidence(row["times_seen"], len(row["evidence"]), lift)
        result.append(row)
    return jsonify(result)


@app.route("/api/analysis/formulas", methods=["DELETE"])
@require_login
@require_project
def delete_formulas():
    """Clear out the formula library, either entirely or just the parts nothing
    backs up.

    Generating formulas is a merge - it never deletes - which is right for a
    library that should accumulate confidence across runs, but it means a bad
    run's output sticks around forever, and a formula whose every citation was
    rejected as unverifiable is worse than no formula at all. This is the escape
    hatch. Evidence rows go with their formula through the FK cascade."""
    scope = (request.get_json(silent=True) or {}).get("scope", "unsupported")
    if scope not in ("all", "unsupported"):
        return jsonify({"error": "scope must be 'all' or 'unsupported'."}), 400

    with db_cursor() as cur:
        if scope == "all":
            cur.execute("DELETE FROM formulas WHERE project_id = %s AND user_id = %s RETURNING id",
                        (g.project_id, g.user_id))
        else:
            cur.execute(
                "DELETE FROM formulas f WHERE f.project_id = %s AND f.user_id = %s "
                "AND NOT EXISTS (SELECT 1 FROM formula_evidence e WHERE e.formula_id = f.id) "
                "RETURNING f.id",
                (g.project_id, g.user_id),
            )
        deleted = len(cur.fetchall())
        if scope == "all":
            # The run history describes formulas that no longer exist.
            cur.execute("DELETE FROM analysis_runs WHERE project_id = %s AND user_id = %s",
                        (g.project_id, g.user_id))

    return jsonify({"deleted": deleted, "scope": scope})


@app.route("/api/analysis/prepare", methods=["POST"])
@require_login
@require_project
def analysis_prepare():
    settings = with_platform_fallback(load_settings(g.user_id))
    has_key = bool(settings.get("api_key"))
    done_jobs = _done_jobs(g.user_id, g.project_id)

    want_visuals = bool((request.get_json(silent=True) or {}).get("visuals", True))

    pending = set()
    visual_pending = set()
    for j in done_jobs:
        rid = j["id"]
        needs_analysis = bool(has_key and not j.get("analysis") and j.get("transcript"))
        # Frames are free but the vision call isn't, so this only ever fills in
        # reels that have never had one - it never re-runs an existing breakdown.
        needs_visual = bool(has_key and want_visuals and not j.get("visual") and j.get("video_file"))
        # The Reel Card (tags + diagnosis) is what the formula run reads as
        # structure; a reel with a breakdown but no card gets one here.
        needs_card = bool(has_key and j.get("analysis") and not j.get("tags"))
        if not (needs_analysis or needs_visual or needs_card):
            continue

        with jobs_lock:
            if rid not in jobs:
                jobs[rid] = j

        if needs_visual:
            # Chain rather than enqueue both: the breakdown wants the scene
            # structure, and chaining also stops the two workers racing to make
            # the same call twice. The card chains off the breakdown in turn.
            mark_ai_pending(rid, "visual")
            visual_queue.put((rid, needs_analysis or needs_card))
            visual_pending.add(rid)
            if needs_analysis or needs_card:
                pending.add(rid)
        elif needs_analysis:
            mark_ai_pending(rid, "breakdown")
            analysis_queue.put(rid)
            pending.add(rid)
        elif needs_card:
            mark_ai_pending(rid, "card")
            card_queue.put(rid)
            pending.add(rid)

    return jsonify({
        "pending_ids": sorted(pending),
        "visual_pending_ids": sorted(visual_pending),
        "needs_key": not has_key,
        "eligible_count": len(done_jobs),
    })


TEMPLATE_PLACEHOLDER_RE = re.compile(r"\[([^\[\]]{1,40})\]")


def _clean_template(value):
    """A literal fill-in-the-blank sentence like 'Here are [number] [things] that
    [outcome]' - not an abstract description. Must have at least 2 [blanks]."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not (10 <= len(value) <= 220):
        return None
    if len(TEMPLATE_PLACEHOLDER_RE.findall(value)) < 2:
        return None
    return value


def _clean_breakdown(value):
    if not isinstance(value, list):
        return None
    items = [{"label": str(v["label"]).strip(), "text": str(v["text"]).strip()}
             for v in value if isinstance(v, dict) and v.get("label") and v.get("text")]
    return items[:6] or None


EVIDENCE_NORMALISE_RE = re.compile(r"[^a-z0-9 ]+")
# Below this length a fragment is too generic to verify - "five", "you" and the
# like appear everywhere and rejecting them would cost more than it catches.
EVIDENCE_MIN_VERIFIABLE_CHARS = 12


def _normalise_for_match(text):
    """Lowercase, drop punctuation, collapse whitespace. The collapse matters:
    punctuation becomes a space, so "day, but" in the reel would otherwise not
    match a quote written "day but" and a perfectly good citation gets thrown
    away for a comma."""
    return " ".join(EVIDENCE_NORMALISE_RE.sub(" ", (text or "").lower()).split())


def _reel_text_index(rows):
    """Everything each reel actually said, normalised once, for checking quotes
    against. Built from the transcript plus the four analysis parts - a breakdown
    is supposed to quote the reel verbatim, so it has to appear in here."""
    index = {}
    for r in rows:
        job = r["job"]
        analysis = job.get("analysis") or {}
        visual = job.get("visual") or {}
        # On-screen text and scene labels are shown to the model too, so a quote
        # drawn from them is legitimate and must not be rejected as invented.
        visual_text = ([t for t in (visual.get("on_screen_text") or []) if isinstance(t, str)]
                       + [sc.get(k) or "" for sc in (visual.get("scenes") or [])
                          for k in ("label", "title", "beat", "goal")])
        blob = " ".join([job.get("transcript") or ""]
                        + [analysis.get(k) or "" for k in ("hook", "promise", "validation", "cta")]
                        + visual_text)
        index[job["id"]] = _normalise_for_match(blob)
    return index


def _clean_evidence(value, valid_reel_ids, reel_text=None):
    """Drop citations the reel can't support.

    The model is asked to fill a template with a reel's own words, and it will
    sometimes attach words from a different reel - or words nobody said - to a
    real reel id, which is how a formula ends up showing an example that plainly
    doesn't match. A quote either appears in that reel's text or the citation is
    wrong, and that is cheap to check here rather than trusting the prompt."""
    cleaned = []
    for e in (value or []):
        if not isinstance(e, dict) or e.get("reel_id") not in valid_reel_ids:
            continue
        breakdown = _clean_breakdown(e.get("breakdown"))
        if breakdown and reel_text is not None:
            haystack = reel_text.get(e["reel_id"], "")
            checkable = [part for part in breakdown
                         if len(_normalise_for_match(part.get("text")).strip()) >= EVIDENCE_MIN_VERIFIABLE_CHARS]
            grounded = [part for part in checkable
                        if _normalise_for_match(part.get("text")).strip() in haystack]
            # One stray paraphrase is tolerable; a citation where most of the
            # quoted wording isn't in the reel is not about that reel.
            if checkable and len(grounded) * 2 < len(checkable):
                print(f"[analysis] dropped evidence for {e['reel_id']}: "
                      f"{len(checkable) - len(grounded)}/{len(checkable)} quotes not found in that reel",
                      file=sys.stderr)
                continue
        cleaned.append({"reel_id": e["reel_id"], "breakdown": breakdown})
    return cleaned


def _merge_evidence(existing, incoming):
    """Union two evidence lists by reel, preferring an entry that actually has a
    breakdown - a later chunk citing the same reel without one shouldn't erase
    the filled-in template from an earlier chunk."""
    by_reel = {e["reel_id"]: e for e in existing}
    for e in incoming:
        current = by_reel.get(e["reel_id"])
        if current is None or (not current.get("breakdown") and e.get("breakdown")):
            by_reel[e["reel_id"]] = e
    return list(by_reel.values())


def _collapse_matched(items):
    """One entry per formula id, evidence unioned, latest verdict wins."""
    merged = {}
    for item in items:
        current = merged.get(item["formula_id"])
        if current is None:
            merged[item["formula_id"]] = item
            continue
        current["evidence"] = _merge_evidence(current["evidence"], item["evidence"])
        current["rating"] = item["rating"]
        current["status"] = item["status"]
        current["reason"] = item.get("reason") or current.get("reason")
        current["template"] = current.get("template") or item.get("template")
    return list(merged.values())


def _collapse_new(items):
    """Same, keyed the way the unique index is - category plus normalised name -
    so two chunks proposing the same formula write once, not twice."""
    merged = {}
    for item in items:
        key = (item["category"], item["name"].strip().lower())
        current = merged.get(key)
        if current is None:
            merged[key] = item
            continue
        current["evidence"] = _merge_evidence(current["evidence"], item["evidence"])
        current["rating"] = item["rating"]
        current["status"] = item["status"]
        current["reason"] = item.get("reason") or current.get("reason")
        current["template"] = current.get("template") or item.get("template")
        current["description"] = item.get("description") or current.get("description")
    return list(merged.values())


def _upsert_evidence(cur, formula_id, evidence, user_id, project_id):
    for e in evidence:
        cur.execute(
            """
            INSERT INTO formula_evidence (formula_id, reel_id, breakdown, user_id, project_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (formula_id, reel_id) DO UPDATE SET
                breakdown = COALESCE(EXCLUDED.breakdown, formula_evidence.breakdown)
            """,
            (formula_id, e["reel_id"], psycopg2.extras.Json(e["breakdown"]) if e["breakdown"] else None,
             user_id, project_id),
        )


def _backfill_missing_templates(settings, user_id, project):
    """Formulas the main reconciliation pass left without a template (smaller
    models tend to skip 'optional-feeling' fields on items they're just
    reconfirming rather than discovering) get one focused, narrow AI call whose
    only job is filling it in - so every card gets one after a single Generate
    click instead of gradually over several runs."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT f.id, f.category, f.name, f.description,
                   COALESCE(json_agg(fe.reel_id) FILTER (WHERE fe.reel_id IS NOT NULL), '[]') AS reel_ids
            FROM formulas f
            JOIN formula_evidence fe ON fe.formula_id = f.id
            WHERE f.template IS NULL AND f.project_id = %s
            GROUP BY f.id
            """,
            (project["id"],)
        )
        missing = cur.fetchall()

    if not missing:
        return

    with store_cache_lock:
        cache_snapshot = dict(store_cache)

    payload = []
    for row in missing:
        reels = []
        for rid in row["reel_ids"]:
            job = cache_snapshot.get(rid)
            if not job or not job.get("analysis"):
                continue
            a = job["analysis"]
            reels.append({"id": rid, "hook": a.get("hook", ""), "promise": a.get("promise", ""),
                          "validation": a.get("validation", ""), "cta": a.get("cta", "")})
        if reels:
            payload.append({"formula_id": row["id"], "category": row["category"], "name": row["name"],
                            "description": row["description"], "reels": reels})

    if not payload:
        return

    valid_ids_by_formula = {p["formula_id"]: {r["id"] for r in p["reels"]} for p in payload}
    prompt = BACKFILL_TEMPLATE_PROMPT_TEMPLATE.format(
        project_context=project_prompt_context(project),
        formulas=json.dumps(payload, ensure_ascii=False),
    )

    try:
        result = call_ai(settings, prompt, max_tokens=4096)
    except Exception as exc:
        print(f"[analysis_generate] template backfill call failed: {exc}", file=sys.stderr)
        return

    with db_cursor() as cur:
        for item in (result.get("formulas") or []):
            fid = item.get("formula_id")
            if fid not in valid_ids_by_formula:
                continue
            template = _clean_template(item.get("template"))
            if not template:
                continue
            cur.execute("UPDATE formulas SET template = %s, updated_at = now() WHERE id = %s AND project_id = %s",
                        (template, fid, project["id"]))
            _upsert_evidence(cur, fid, _clean_evidence(item.get("evidence"), valid_ids_by_formula[fid]),
                             user_id, project["id"])


CONSOLIDATE_PROMPT_TEMPLATE = """You are tidying a library of content formulas for ONE category ("{category}"). The formulas were found by several separate passes over batches of reels, so the same SHAPE often appears more than once under different names. Your only job is to say which entries are the same shape and should be merged.
{project_context}
Same shape means: once the specifics are blanked out, the two templates are the same sentence skeleton - same moves in the same order, interchangeable blanks. Examples: "Here are [number] [things] that [outcome]" and "Here are [number] [tools] that [result]" are the same shape; "Here are [number] [things]" and "[number] [things] nobody tells you about [topic]" are NOT; "Never say [X]. Always say [Y]." and "Never tell AI [X]. Always tell AI [Y]." are the same shape. Same TOPIC is not a reason to merge. When in doubt, do not merge.

For each merge group: choose the clearest existing name (or write a better short one), write the single best template for the group (2-4 [blanks], concrete blank names), and list every formula id in the group with the one to keep first.

Formulas (id, name, template, evidence_count, example):
{formulas}

Respond with ONLY a strict JSON object: {{"merges": [{{"keep_id": <id>, "merge_ids": [<other ids>], "name": "...", "template": "..."}}], "note": "one line on how conservative you were"}}. An empty merges array is a valid answer.
"""


def consolidate_formulas(settings, user_id, project_id, project, category=None):
    """Merge same-shape formulas within each category. One call per category,
    conservative by instruction; applies merges by moving evidence onto the kept
    row, summing times_seen, then re-measuring lift/effect. Returns a summary."""
    with db_cursor() as cur:
        cur.execute("SELECT slug FROM formula_categories WHERE project_id = %s ORDER BY sort_order", (project_id,))
        slugs = [r["slug"] for r in cur.fetchall() if not category or r["slug"] == category]
    rows = _tiered_rows(user_id, project_id)
    summary = {"merged_groups": 0, "removed": 0, "by_category": {}}
    for slug in slugs:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.name, f.template, f.times_seen,
                       COALESCE(json_agg(json_build_object('reel_id', fe.reel_id, 'breakdown', fe.breakdown))
                                FILTER (WHERE fe.reel_id IS NOT NULL), '[]') AS evidence
                FROM formulas f LEFT JOIN formula_evidence fe ON fe.formula_id = f.id
                WHERE f.project_id = %s AND f.category = %s GROUP BY f.id ORDER BY f.id
                """, (project_id, slug))
            formulas = [_jsonify_row(r) for r in cur.fetchall()]
        if len(formulas) < 2:
            continue
        listing = []
        for f in formulas:
            ev = f["evidence"] or []
            example = ""
            for e in ev:
                if e.get("breakdown"):
                    example = "; ".join(f"{b.get('label')}={b.get('text')}" for b in e["breakdown"][:4])
                    break
            listing.append(f"- id={f['id']} | {f['name']} | \"{f['template'] or ''}\" | evidence={len(ev)} | e.g. {example[:160]}")
        prompt = CONSOLIDATE_PROMPT_TEMPLATE.format(category=slug, project_context=project_prompt_context(project),
                                                    formulas="\n".join(listing))
        try:
            result = call_ai(settings, prompt, max_tokens=3000)
        except Exception as exc:
            print(f"[consolidate] {slug} failed: {exc}", file=sys.stderr)
            continue
        valid_ids = {f["id"] for f in formulas}
        groups, seen = [], set()
        for m in (result.get("merges") or []):
            keep = m.get("keep_id")
            others = [i for i in (m.get("merge_ids") or []) if isinstance(i, int) and i in valid_ids and i != keep and i not in seen]
            if keep not in valid_ids or keep in seen or not others:
                continue
            seen.add(keep)
            seen.update(others)
            groups.append({"keep": keep, "others": others, "name": (m.get("name") or "").strip()[:80],
                           "template": _clean_template(m.get("template"))})
        if not groups:
            summary["by_category"][slug] = 0
            continue
        with db_cursor() as cur:
            for gkeep in groups:
                keep, others = gkeep["keep"], gkeep["others"]
                # Move evidence onto the kept row; a reel already cited there keeps its existing breakdown.
                cur.execute("DELETE FROM formula_evidence WHERE formula_id = ANY(%s) AND reel_id IN "
                            "(SELECT reel_id FROM formula_evidence WHERE formula_id = %s)", (others, keep))
                cur.execute("UPDATE formula_evidence SET formula_id = %s WHERE formula_id = ANY(%s)", (keep, others))
                cur.execute("SELECT COALESCE(SUM(times_seen), 0) AS s FROM formulas WHERE id = ANY(%s)", (others,))
                extra_seen = cur.fetchone()["s"]
                cur.execute("DELETE FROM formulas WHERE id = ANY(%s) AND project_id = %s", (others, project_id))
                cur.execute("SELECT reel_id FROM formula_evidence WHERE formula_id = %s", (keep,))
                ev_ids = [r["reel_id"] for r in cur.fetchall()]
                fx = levers.formula_effect(ev_ids, rows)
                status = ("not_working" if fx["effect"] == "negative" else "working") if fx["lift"] is not None else None
                cur.execute(
                    "UPDATE formulas SET name = COALESCE(NULLIF(%s, ''), name), template = COALESCE(%s, template), "
                    "times_seen = times_seen + %s, effect = %s, lift = %s, status = COALESCE(%s, status), updated_at = now() "
                    "WHERE id = %s AND project_id = %s",
                    (gkeep["name"], gkeep["template"], extra_seen, fx["effect"], fx["lift"], status, keep, project_id))
                summary["removed"] += len(others)
            summary["merged_groups"] += len(groups)
            summary["by_category"][slug] = len(groups)
    return summary


@app.route("/api/analysis/consolidate", methods=["POST"])
@require_login
@require_project
def analysis_consolidate():
    settings = with_platform_fallback(load_settings(g.user_id))
    if not settings.get("api_key"):
        return jsonify({"error": "No AI API key configured. Add one in Settings."}), 400
    category = (request.get_json(silent=True) or {}).get("category")
    try:
        summary = consolidate_formulas(settings, g.user_id, g.project_id, g.project, category=category)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, **summary})


PLAYBOOK_PROMPT_TEMPLATE = """You are writing the PLAYBOOK for one creator's content library - the short, number-backed summary of what works in it. An assistant reads this first in every conversation about the library, so it has to be dense, specific and honest about thin data.
{project_context}
Write 250-400 words of plain prose with short headed sections (Hooks / Structure & length / CTA / Format & delivery / Own account (only if own reels exist) / Watch out for). Rules:
- Every claim cites its number: lift, n, breakout rate, or a formula id in the form [F12]. No number, no claim.
- Lead with the biggest, best-supported lifts. Mark anything with n < 5 as "weak evidence".
- Lift is the value's median reach multiple over the library's median - say "reels with X reached 2.4x the library median (n=9)". Sends lift uses reshares per 1k views.
- "Watch out for" is for negative lifts and for things that look good but aren't supported (tiny n, one outlier).
- No generic advice. Nothing the numbers don't say.

Library baseline: {baseline}

Lever statistics (dimension=value: n, lift_reach, lift_sends, breakout_rate, confidence):
{levers}

Contrast pairs (same idea, very different reach):
{pairs}

Formulas in the library (id, category, name, template, effect, lift, times_seen):
{formulas}

Own account baseline (if any): {own}

Respond with ONLY a strict JSON object: {{"playbook": "<the prose, with headed sections>"}}
"""


def generate_playbook(settings, user_id, project_id, project, run_id=None):
    """Regenerated after every formula run and stored on the project. The agent
    reads it first; it's the cheap context that stops every chat re-deriving
    the library."""
    knowledge = project_knowledge(user_id, project_id)
    with db_cursor() as cur:
        cur.execute("SELECT id, category, name, template, effect, lift, times_seen FROM formulas "
                    "WHERE project_id = %s ORDER BY category, lift DESC NULLS LAST LIMIT 60", (project_id,))
        formulas = [_jsonify_row(r) for r in cur.fetchall()]
    lever_lines = [f"- {e['dimension']}={e['value']}: n={e['n']}, lift_reach={e['lift_reach']}, lift_sends={e['lift_sends']}, "
                   f"breakout_rate={e['breakout_rate']}, {e['confidence']}" for e in knowledge["levers"]]
    pair_lines = [f"- {p['ratio']}x apart, shared {p['shared']}: {p['high']['id']} ({p['high']['reach_multiple']}x) vs "
                  f"{p['low']['id']} ({p['low']['reach_multiple']}x); differs on "
                  + ", ".join(d['dimension'] for d in p['diff'][:5]) for p in knowledge["pairs"][:8]]
    prompt = PLAYBOOK_PROMPT_TEMPLATE.format(
        project_context=project_prompt_context(project),
        baseline=json.dumps(knowledge["baseline"]),
        levers="\n".join(lever_lines) or "(none with n >= 3 yet)",
        pairs="\n".join(pair_lines) or "(none)",
        formulas="\n".join(f"- [F{f['id']}] {f['category']}: {f['name']} - \"{f['template'] or ''}\" effect={f['effect']} "
                           f"lift={f['lift']} seen={f['times_seen']}" for f in formulas) or "(none)",
        own=json.dumps(knowledge["own"]) if knowledge["own"].get("n") else "(no own reels - project has no handle set or none ingested)",
    )
    result = call_ai(settings, prompt, max_tokens=1500)
    text = (result.get("playbook") or "").strip() if isinstance(result, dict) else ""
    if not text:
        return None
    playbook = {"text": text[:6000], "generated_at": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id, "model": settings.get("model"), "taxonomy_v": taxonomy.TAXONOMY_VERSION}
    with db_cursor() as cur:
        cur.execute("UPDATE projects SET playbook = %s, updated_at = now() WHERE id = %s AND user_id = %s",
                    (psycopg2.extras.Json(playbook), project_id, user_id))
    invalidate_project(project_id)
    return playbook


@app.route("/api/analysis/playbook", methods=["GET", "POST"])
@require_login
@require_project
def analysis_playbook():
    """GET the stored playbook; POST regenerates it on demand."""
    if request.method == "GET":
        return jsonify({"playbook": (g.project or {}).get("playbook")})
    settings = with_platform_fallback(load_settings(g.user_id))
    if not settings.get("api_key"):
        return jsonify({"error": "No AI API key configured. Add one in Settings."}), 400
    try:
        playbook = generate_playbook(settings, g.user_id, g.project_id, g.project)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"playbook": playbook})


@app.route("/api/analysis/generate", methods=["POST"])
@require_login
@require_project
def analysis_generate():
    user_id, project_id, project = g.user_id, g.project_id, g.project
    settings = with_platform_fallback(load_settings(user_id))
    if not settings.get("api_key"):
        return jsonify({"error": "No AI API key configured. Add one in Settings."}), 400

    rows = _tiered_rows(user_id, project_id)
    if len(rows) < MIN_BULK_REELS:
        return jsonify({
            "error": f"Need at least {MIN_BULK_REELS} reels with an AI breakdown to generate formulas "
                     f"(have {len(rows)})."
        }), 400

    amazing_count = sum(1 for r in rows if r["tier"] == "amazing")
    good_count = sum(1 for r in rows if r["tier"] == "good")
    low_count = len(rows) - amazing_count - good_count
    valid_reel_ids = {r["job"]["id"] for r in rows}  # every reel in the batch - nothing excluded

    # The deterministic layer goes in first: lever lifts and contrast pairs over
    # the whole library, so the model argues from counted evidence and phrases
    # formulas in the taxonomy's vocabulary. Computed once per run, sliced per
    # category below.
    lever_table = levers.lever_table(rows)
    contrast = levers.contrast_pairs(rows)
    LEVER_DIMS_BY_CATEGORY = {
        "hook": ["hook.type", "hook.devices", "emotion.primary", "audience.awareness"],
        "script": ["promise.type", "structure.type", "cta.type", "cta.position", "duration_band"],
        "talking": ["delivery.energy", "delivery.wpm_band", "format.type", "format.captions"],
    }

    batch_payload = [
        {
            "id": r["job"]["id"],
            "tier": r["tier"],
            "metrics": {k: v for k, v in r["metrics"].items() if k != "engagement_score"},
            "hook": r["job"]["analysis"].get("hook", ""),
            "promise": r["job"]["analysis"].get("promise", ""),
            "validation": r["job"]["analysis"].get("validation", ""),
            "cta": r["job"]["analysis"].get("cta", ""),
            "visual": visual_batch_payload(r["job"]),
            # Reel Card: the closed-vocabulary read of the reel, plus its diagnosis
            # one-liner. Tags are what make two reels comparable without re-reading
            # both transcripts; absent when the card hasn't run yet.
            "tags": ({k: v for k, v in (r["job"].get("tags") or {}).items()
                      if k not in ("other_notes",) and v not in (None, [], {})} or None),
            "diagnosis": ((r["job"].get("diagnosis") or {}).get("one_line") or None),
        }
        for r in rows
    ]

    with db_cursor() as cur:
        cur.execute("SELECT slug, display_name FROM formula_categories WHERE project_id = %s "
                    "ORDER BY sort_order", (project_id,))
        category_rows = cur.fetchall()
        category_slugs = [row["slug"] for row in category_rows]
        cur.execute("SELECT id, category, name, description, template, rating FROM formulas WHERE project_id = %s",
                    (project_id,))
        existing = cur.fetchall()

    if not category_slugs:
        return jsonify({"error": "This project has no formula categories. Add one on the Analysis page."}), 400

    existing_by_id = {row["id"]: row for row in existing}
    category_names = {row["slug"]: row["display_name"] for row in category_rows}

    # One call per category rather than one call for all of them. A single call
    # has to split a fixed output budget across every category and reliably
    # answers with two or three formulas total for a library of forty reels; a
    # focused call spends the whole budget on one question. It costs N calls
    # instead of one, which at these sizes is cents.
    # Chunked, because coverage is the thing that keeps breaking. Handed forty
    # reels at once the model skims and cites two; handed a dozen it can actually
    # work through them and account for each. Chunks are dealt round-robin off
    # the ranked list so every chunk holds a spread of strong and weak reels -
    # a chunk of only top performers has no contrast to explain.
    chunk_count = max(1, math.ceil(len(batch_payload) / FORMULA_CHUNK_SIZE))
    chunks = [batch_payload[i::chunk_count] for i in range(chunk_count)]

    def formula_targets(chunk_size):
        # A fixed "2-5" anchored the model low (three chunks of twelve reels came
        # back as three hook formulas total); "up to one per reel" anchored it
        # high (thirty-six formulas with one reel each - a catalogue, not
        # formulas). One per three-to-four reels is where shapes actually repeat;
        # the consolidation pass after the run merges what the chunks split.
        return max(2, math.ceil(chunk_size / 4)), max(4, math.ceil(chunk_size / 1.5))

    results, failures = [], []
    for slug in category_slugs:
        scoped = [{"id": row["id"], "name": row["name"], "description": row["description"],
                   "template": row["template"]}
                  for row in existing if row["category"] == slug]
        # Formulas this run has already found in this category are fed forward,
        # so a later chunk reuses a name rather than coining a near-duplicate.
        found_this_run = []
        for index, chunk in enumerate(chunks):
            min_formulas, max_formulas = formula_targets(len(chunk))
            prompt = FORMULA_ANALYSIS_PROMPT_TEMPLATE.format(
                project_context=project_prompt_context(project),
                algorithm_context=algorithm_prompt_block(),
                category=slug,
                category_description=category_names.get(slug, slug),
                category_guidance=category_guidance_block(slug, category_names.get(slug, slug)),
                lever_context=levers.lever_context_for_prompt(
                    lever_table, contrast, dimensions=LEVER_DIMS_BY_CATEGORY.get(slug)),
                good_views=GOOD_VIEW_THRESHOLD,
                amazing_views=AMAZING_VIEW_THRESHOLD,
                reel_count=len(chunk),
                chunk_note=(f" (batch {index + 1} of {chunk_count} from a "
                            f"{len(batch_payload)}-reel library)" if chunk_count > 1 else ""),
                min_formulas=min_formulas,
                max_formulas=max_formulas,
                existing_formulas=(json.dumps(scoped, ensure_ascii=False) if scoped
                                   else "(none yet in this category - this is the first run)"),
                found_this_run=(json.dumps(found_this_run, ensure_ascii=False) if found_this_run
                                else "(none yet - this is the first batch of the run)"),
                batch=json.dumps(chunk, ensure_ascii=False),
            )
            try:
                result = call_ai(settings, prompt, max_tokens=6000)
            except Exception as exc:
                print(f"[analysis_generate] {slug} chunk {index + 1} failed: {exc}", file=sys.stderr)
                failures.append(f"{category_names.get(slug, slug)} batch {index + 1}: {exc}")
                continue
            result["_category"] = slug
            results.append(result)
            for item in (result.get("new") or []):
                if item.get("name") and item.get("description"):
                    found_this_run.append({"name": item["name"],
                                           "description": item["description"],
                                           "template": item.get("template")})

    if not results:
        return jsonify({"error": "Formula generation failed for every category. "
                                 + (failures[0] if failures else "")}), 500

    # Validate the AI's response before writing anything: a hallucinated formula_id
    # would otherwise UPDATE 0 rows and then blow up the whole transaction on the
    # matching formula_evidence insert's FK constraint.
    def clean_rating(value, fallback):
        return value if isinstance(value, int) and 1 <= value <= 5 else fallback

    reel_text = _reel_text_index(rows)
    matched, cleaned_new = [], []
    cited_reels = set()
    summaries, caveats = [], []

    for result in results:
        slug = result["_category"]
        if isinstance(result.get("summary"), str) and result["summary"].strip():
            summaries.append(f"{category_names.get(slug, slug)}: {result['summary'].strip()}")
        if isinstance(result.get("caveats"), str) and result["caveats"].strip():
            caveats.append(result["caveats"].strip())

        new_items = list(result.get("new") or [])
        for m in (result.get("matched") or []):
            existing_row = existing_by_id.get(m.get("formula_id"))
            # The call was scoped to one category, so a formula_id from another
            # category is as wrong as one that doesn't exist.
            if not existing_row or existing_row["category"] != slug:
                if m.get("name") and m.get("description"):
                    new_items.append(m)   # unusable id - treat it as a new formula instead
                continue
            if m.get("status") not in ("working", "not_working"):
                continue
            evidence = _clean_evidence(m.get("evidence"), valid_reel_ids, reel_text)
            cited_reels.update(e["reel_id"] for e in evidence)
            matched.append({
                "formula_id": existing_row["id"],
                "rating": clean_rating(m.get("rating"), existing_row.get("rating") or 3),
                "status": m["status"],
                "reason": m.get("reason"),
                "template": _clean_template(m.get("template")) or existing_row.get("template"),
                "levers": levers.clean_levers(m.get("levers")),
                "evidence": evidence,
            })

        for n in new_items:
            if not n.get("name") or not n.get("description"):
                continue
            if n.get("status") not in ("working", "not_working"):
                continue
            evidence = _clean_evidence(n.get("evidence"), valid_reel_ids, reel_text)
            cited_reels.update(e["reel_id"] for e in evidence)
            cleaned_new.append({
                "category": slug,   # from the run, not from the model
                "name": n["name"].strip(),
                "description": n["description"],
                "reason": n.get("reason"),
                "rating": clean_rating(n.get("rating"), 3),
                "status": n["status"],
                "template": _clean_template(n.get("template")),
                "levers": levers.clean_levers(n.get("levers")),
                "evidence": evidence,
            })

    matched = _collapse_matched(matched)
    cleaned_new = _collapse_new(cleaned_new)

    uncited = len(valid_reel_ids - cited_reels)
    coverage_note = (f"{len(cited_reels)} of {len(valid_reel_ids)} reels are cited as evidence."
                     + (f" {uncited} matched no formula in any category." if uncited else ""))
    summary_text = " ".join(summaries) or None
    caveats_text = " ".join(dict.fromkeys(caveats + [coverage_note])) or None
    if failures:
        caveats_text = ((caveats_text or "") +
                        f" Some categories failed this run: {'; '.join(failures)}").strip()

    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_runs (user_id, project_id, amazing_count, good_count, low_count, summary, caveats) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, project_id, amazing_count, good_count, low_count,
             summary_text, caveats_text),
        )
        run_id = cur.fetchone()["id"]

        # effect / lift are MEASURED from the cited reels against the project,
        # not taken from the model. The model's working/not_working is kept only
        # when there are no numbers to measure with.
        def measured(item):
            fx = levers.formula_effect([e["reel_id"] for e in item["evidence"]], rows)
            if fx["lift"] is not None:
                item["status"] = "not_working" if fx["effect"] == "negative" else "working"
            return fx

        for m in matched:
            fx = measured(m)
            cur.execute(
                "UPDATE formulas SET rating = %s, status = %s, reason = %s, template = %s, "
                "levers = COALESCE(%s, levers), effect = %s, lift = %s, taxonomy_v = %s, "
                "times_seen = times_seen + 1, updated_at = now(), last_run_id = %s WHERE id = %s AND project_id = %s",
                (m["rating"], m["status"], m["reason"], m["template"],
                 psycopg2.extras.Json(m["levers"]) if m.get("levers") else None,
                 fx["effect"], fx["lift"], taxonomy.TAXONOMY_VERSION,
                 run_id, m["formula_id"], project_id),
            )
            _upsert_evidence(cur, m["formula_id"], m["evidence"], user_id, project_id)

        for n in cleaned_new:
            fx = measured(n)
            cur.execute(
                """
                INSERT INTO formulas (user_id, project_id, category, name, description, reason, rating, status, template,
                                      levers, effect, lift, taxonomy_v, last_run_id)
                VALUES (%(user_id)s, %(project_id)s, %(category)s, %(name)s, %(description)s, %(reason)s, %(rating)s, %(status)s, %(template)s,
                        %(levers)s, %(effect)s, %(lift)s, %(taxonomy_v)s, %(last_run_id)s)
                ON CONFLICT (project_id, category, lower(btrim(name))) DO UPDATE SET
                    reason = EXCLUDED.reason, rating = EXCLUDED.rating, status = EXCLUDED.status,
                    template = COALESCE(EXCLUDED.template, formulas.template),
                    levers = COALESCE(EXCLUDED.levers, formulas.levers),
                    effect = EXCLUDED.effect, lift = EXCLUDED.lift, taxonomy_v = EXCLUDED.taxonomy_v,
                    times_seen = formulas.times_seen + 1, updated_at = now(), last_run_id = EXCLUDED.last_run_id
                RETURNING id
                """,
                {**n, "levers": psycopg2.extras.Json(n["levers"]) if n.get("levers") else None,
                 "effect": fx["effect"], "lift": fx["lift"], "taxonomy_v": taxonomy.TAXONOMY_VERSION,
                 "user_id": user_id, "project_id": project_id, "last_run_id": run_id},
            )
            new_id = cur.fetchone()["id"]
            _upsert_evidence(cur, new_id, n["evidence"], user_id, project_id)

    try:
        _backfill_missing_templates(settings, user_id, project)
    except Exception as exc:
        print(f"[analysis_generate] template backfill pass failed: {exc}", file=sys.stderr)

    # Chunks coin their own names, so the same shape lands in the library two or
    # three times per run; one conservative merge pass per category fixes that.
    consolidation = None
    try:
        consolidation = consolidate_formulas(settings, user_id, project_id, project)
    except Exception as exc:
        print(f"[analysis_generate] consolidation failed: {exc}", file=sys.stderr)

    # The playbook is the library summarised with its numbers; it goes stale the
    # moment the formulas change, so it is rewritten at the end of every run.
    try:
        generate_playbook(settings, user_id, project_id, project, run_id=run_id)
    except Exception as exc:
        print(f"[analysis_generate] playbook failed: {exc}", file=sys.stderr)

    return jsonify({"ok": True, "run_id": run_id, "formulas": len(matched) + len(cleaned_new),
                    "cited_reels": len(cited_reels), "uncited_reels": uncited,
                    "failed_categories": failures, "consolidation": consolidation})


@app.route("/api/reels", methods=["DELETE"])
@require_login
@require_project
def delete_reels():
    data = request.get_json(force=True)
    requested_ids = {str(i) for i in (data.get("ids") or [])}
    if not requested_ids:
        return jsonify({"error": "No reel ids provided."}), 400

    # Only done/error reels are eligible - anything still downloading/transcribing
    # is owned by a worker thread mid-flight (update_job/persist_reel index into
    # `jobs` by id), so deleting it out from under that thread would KeyError it.
    with store_cache_lock:
        merged = dict(store_cache)
    with jobs_lock:
        merged.update(jobs)

    deletable = [
        rid for rid in requested_ids
        if (j := merged.get(rid)) and j.get("user_id") == g.user_id
        and j.get("project_id") == g.project_id and j.get("status") in ("done", "error")
    ]
    skipped = sorted(requested_ids - set(deletable))
    if not deletable:
        return jsonify({"deleted": [], "skipped": skipped, "deleted_formulas": 0})

    with db_cursor() as cur:
        # Formulas backed by these reels, captured before the delete. Their
        # formula_evidence rows go with the reel (FK cascade); any formula left
        # with no evidence at all was derived purely from what's being deleted,
        # so it goes too. Scoped to this set rather than "every evidence-less
        # formula in the project" so a formula the model happened to create
        # without evidence isn't swept up by an unrelated delete.
        cur.execute(
            "SELECT DISTINCT formula_id FROM formula_evidence "
            "WHERE reel_id = ANY(%s) AND project_id = %s",
            (deletable, g.project_id),
        )
        touched_formula_ids = [r["formula_id"] for r in cur.fetchall()]

        cur.execute("DELETE FROM reels WHERE id = ANY(%s) AND user_id = %s AND project_id = %s",
                    (deletable, g.user_id, g.project_id))

        # times_seen counts analysis runs, not reels, so there's nothing
        # meaningful to decrement on the formulas that survive - their evidence
        # list just shrinks, and the next Generate re-rates them against what's
        # left.
        deleted_formulas = []
        if touched_formula_ids:
            cur.execute(
                "DELETE FROM formulas f WHERE f.id = ANY(%s) AND f.project_id = %s "
                "AND NOT EXISTS (SELECT 1 FROM formula_evidence fe WHERE fe.formula_id = f.id) "
                "RETURNING f.id",
                (touched_formula_ids, g.project_id),
            )
            deleted_formulas = [r["id"] for r in cur.fetchall()]

    with store_cache_lock:
        for rid in deletable:
            store_cache.pop(rid, None)
    with jobs_lock:
        for rid in deletable:
            jobs.pop(rid, None)
    with _ai_attempted_lock:
        for rid in deletable:
            _ai_attempted.discard(rid)
            _stats_attempted.discard(rid)

    for rid in deletable:
        for path in VIDEOS_DIR.glob(f"{rid}.*"):
            path.unlink(missing_ok=True)

    return jsonify({
        "deleted": deletable,
        "skipped": skipped,
        "deleted_formulas": len(deleted_formulas),
    })


# ------------------------------------------------------- creative corner ----
# The per-project ideas board: cards move idea -> scripting -> ready -> posted.
# ord is a float so a drag lands between two neighbours without renumbering.

IDEA_STATUSES = ("idea", "scripting", "ready", "posted")
IDEA_COLORS = ("yellow", "orange", "pink", "green", "blue", "purple")
EMPTY_IDEA_SCRIPT = {"hook": "", "promise": "", "validation": "", "cta": "", "full": ""}
IDEA_SELECT = "id, title, notes, status, color, tags, script, refs, ord, created_at, updated_at"


def clean_idea_fields(data):
    """Pick the editable fields out of a request payload, normalizing types.
    Unknown statuses/colors are dropped rather than erroring - a stale frontend
    should never be able to wedge a card into an unreachable state."""
    fields = {}
    if "title" in data:
        fields["title"] = str(data["title"] or "")[:300]
    if "notes" in data:
        fields["notes"] = str(data["notes"] or "")
    if data.get("status") in IDEA_STATUSES:
        fields["status"] = data["status"]
    if data.get("color") in IDEA_COLORS:
        fields["color"] = data["color"]
    if isinstance(data.get("tags"), list):
        fields["tags"] = [str(t)[:40] for t in data["tags"] if str(t).strip()][:12]
    if isinstance(data.get("refs"), list):
        fields["refs"] = [str(r) for r in data["refs"]][:30]
    if isinstance(data.get("script"), dict):
        fields["script"] = {k: str(data["script"].get(k) or "") for k in EMPTY_IDEA_SCRIPT}
    return fields


def _idea_row(row):
    d = dict(row)
    for key in ("created_at", "updated_at"):
        if d.get(key):
            d[key] = d[key].isoformat()
    return d


@app.route("/api/ideas")
@require_login
@require_project
def list_ideas():
    with db_cursor() as cur:
        cur.execute(f"SELECT {IDEA_SELECT} FROM ideas WHERE project_id = %s ORDER BY status, ord",
                    (g.project_id,))
        return jsonify([_idea_row(r) for r in cur.fetchall()])


@app.route("/api/ideas", methods=["POST"])
@require_login
@require_project
def create_idea():
    fields = clean_idea_fields(request.get_json(force=True))
    status = fields.get("status", "idea")
    with db_cursor() as cur:
        # New cards land at the top of their column.
        cur.execute("SELECT coalesce(min(ord), 1) - 1 AS ord FROM ideas WHERE project_id = %s AND status = %s",
                    (g.project_id, status))
        ord_ = cur.fetchone()["ord"]
        cur.execute(
            f"""INSERT INTO ideas (title, notes, status, color, tags, script, refs, ord, user_id, project_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {IDEA_SELECT}""",
            (fields.get("title", ""), fields.get("notes", ""), status, fields.get("color", "yellow"),
             psycopg2.extras.Json(fields.get("tags", [])),
             psycopg2.extras.Json(fields.get("script") or dict(EMPTY_IDEA_SCRIPT)),
             psycopg2.extras.Json(fields.get("refs", [])),
             ord_, g.user_id, g.project_id),
        )
        return jsonify(_idea_row(cur.fetchone())), 201


@app.route("/api/ideas/<int:idea_id>", methods=["PATCH"])
@require_login
@require_project
def update_idea(idea_id):
    fields = clean_idea_fields(request.get_json(force=True))
    if not fields:
        return jsonify({"error": "Nothing to update."}), 400
    sets, params = [], []
    for key, value in fields.items():
        sets.append(f"{key} = %s")
        params.append(psycopg2.extras.Json(value) if key in ("tags", "script", "refs") else value)
    params += [idea_id, g.project_id]
    with db_cursor() as cur:
        cur.execute(
            f"UPDATE ideas SET {', '.join(sets)}, updated_at = now() "
            f"WHERE id = %s AND project_id = %s RETURNING {IDEA_SELECT}",
            params,
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "Idea not found."}), 404
    return jsonify(_idea_row(row))


@app.route("/api/ideas/<int:idea_id>", methods=["DELETE"])
@require_login
@require_project
def delete_idea(idea_id):
    with db_cursor() as cur:
        cur.execute("DELETE FROM ideas WHERE id = %s AND project_id = %s RETURNING id",
                    (idea_id, g.project_id))
        if not cur.fetchone():
            return jsonify({"error": "Idea not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/ideas/positions", methods=["POST"])
@require_login
@require_project
def reorder_ideas():
    """Persist drag-and-drop moves: a list of {id, status, ord} updates."""
    positions = request.get_json(force=True).get("positions", [])
    with db_cursor() as cur:
        for p in positions:
            if not isinstance(p, dict) or p.get("status") not in IDEA_STATUSES:
                continue
            try:
                idea_id, ord_ = int(p["id"]), float(p["ord"])
            except (KeyError, TypeError, ValueError):
                continue
            cur.execute(
                "UPDATE ideas SET status = %s, ord = %s, updated_at = now() "
                "WHERE id = %s AND project_id = %s",
                (p["status"], ord_, idea_id, g.project_id),
            )
    return jsonify({"ok": True})


# ---------------------------------------------------------------- agent ----
# The in-app door to the Reel agent. Tools, schemas and the system prompt are
# shared with the MCP server (agent/); this is threads + an SSE turn endpoint.
from agent import prompts as agent_prompts, runtime as agent_runtime, tools as agent_tools  # noqa: E402


@app.route("/api/agent/threads", methods=["GET"])
@require_login
@require_project
def agent_threads():
    return jsonify({"threads": agent_runtime.list_threads(g.user_id, g.project_id),
                    "starters": agent_prompts.STARTER_PROMPTS})


@app.route("/api/agent/threads", methods=["POST"])
@require_login
@require_project
def agent_thread_create():
    data = request.get_json(silent=True) or {}
    return jsonify(agent_runtime.create_thread(g.user_id, g.project_id, data.get("title"))), 201


@app.route("/api/agent/threads/<int:thread_id>", methods=["GET"])
@require_login
@require_project
def agent_thread_get(thread_id):
    thread = agent_runtime.get_thread(g.user_id, g.project_id, thread_id)
    if not thread:
        return jsonify({"error": "Thread not found."}), 404
    messages = agent_runtime.load_messages(thread_id)
    # Tool results are kept for replay/audit but not shipped to the UI in full -
    # the chips only need name + one-line summary.
    for m in messages:
        if m.get("tool_results"):
            m["tool_results"] = [{"id": r["id"], "name": r["name"], "chars": len(r.get("result") or "")}
                                 for r in m["tool_results"]]
    return jsonify({"thread": thread, "messages": messages})


@app.route("/api/agent/threads/<int:thread_id>", methods=["DELETE"])
@require_login
@require_project
def agent_thread_delete(thread_id):
    if not agent_runtime.delete_thread(g.user_id, g.project_id, thread_id):
        return jsonify({"error": "Thread not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/agent/threads/<int:thread_id>/messages", methods=["POST"])
@require_login
@require_project
def agent_thread_message(thread_id):
    """One turn, streamed as server-sent events (see agent/runtime.py for the
    event shapes). The turn runs inside the request; the generator yields as
    each tool call lands, so the UI shows progress long before the answer."""
    thread = agent_runtime.get_thread(g.user_id, g.project_id, thread_id)
    if not thread:
        return jsonify({"error": "Thread not found."}), 404
    settings = with_platform_fallback(load_settings(g.user_id))
    if not settings.get("api_key"):
        return jsonify({"error": "No AI API key configured. Add one in Settings."}), 400
    body = request.get_json(force=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Say something."}), 400
    # What the user has on screen (e.g. the open reel) - resolved in the system
    # prompt so "this reel" works without pasting an id.
    context = body.get("context") if isinstance(body.get("context"), dict) else None
    user_id, project_id, project = g.user_id, g.project_id, g.project

    def stream():
        try:
            for event in agent_runtime.run_turn(user_id, project_id, thread_id, text, settings, project, context=context):
                yield "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"
        except Exception as exc:
            print(f"[agent] turn failed: {exc}", file=sys.stderr)
            yield "data: " + json.dumps({"type": "error", "text": str(exc)}) + "\n\n"
        yield "data: " + json.dumps({"type": "end"}) + "\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/agent/drafts", methods=["GET"])
@require_login
@require_project
def agent_drafts():
    return jsonify(agent_tools.list_drafts(g.user_id, g.project_id, limit=50))


@app.route("/api/agent/drafts/<int:draft_id>", methods=["GET"])
@require_login
@require_project
def agent_draft_get(draft_id):
    draft = agent_tools.get_draft(g.user_id, g.project_id, draft_id)
    return (jsonify(draft), 404) if draft.get("error") else jsonify(draft)


@app.route("/api/agent/drafts/<int:draft_id>", methods=["DELETE"])
@require_login
@require_project
def agent_draft_delete(draft_id):
    return jsonify(agent_tools.delete_draft(g.user_id, g.project_id, draft_id))


@app.route("/api/agent/notes", methods=["GET", "POST"])
@require_login
@require_project
def agent_notes():
    if request.method == "GET":
        return jsonify(agent_tools.list_notes(g.user_id, g.project_id))
    text = ((request.get_json(force=True) or {}).get("text") or "").strip()
    note = agent_tools.save_note(g.user_id, g.project_id, text, created_by="user")
    return (jsonify(note), 400) if note.get("error") else (jsonify(note), 201)


@app.route("/api/agent/notes/<int:note_id>", methods=["DELETE"])
@require_login
@require_project
def agent_note_delete(note_id):
    return jsonify(agent_tools.delete_note(g.user_id, g.project_id, note_id))


# ------------------------------------------------------- rate my script ----
# "Rate my script": the user pastes a draft and gets, in three tabs, a rating,
# targeted improvements, and a full rewrite. Every call is grounded in the same
# project context the agent reads - playbook, lever stats, top reels by reach,
# the creator's own baseline and the algorithm brief - so the feedback cites
# what travels in THIS library instead of generic creator advice. Stateless on
# the server: the page keeps the script and results locally.

SCRIPT_RUBRIC = """SCORING RUBRIC (each 0-10; be calibrated - 5 is "fine, forgettable", 8+ is "would travel in this library")
- hook: first 1-2 lines + first 3 seconds. Does it stop the scroll for THIS audience? Is it a known shape that travels here? Specific, curiosity/tension, no throat-clearing. Its LENGTH is judged against the SHAPE block: a hook outside the top quartile's middle half (words / seconds) costs points and the note must say the numbers ("21 words, ~7s; top quartile here opens in 11-18 words").
- promise: does the viewer know within ~5s what they'll get and why to stay? Is it concrete and worth the watch?
- body: validation / proof / beats. Order, escalation, payoff delivery, no sag in the middle, claims backed by something (demo, number, story, example).
- cta: is there a clear ask that matches the platform signal the script needs (share/send, save, comment, follow)? Verbatim wording, placement (compare its position and length to the SHAPE block and the cta.type / cta.position levers - if no-CTA reels out-reach CTA reels here, say so), does it earn the ask?
- clarity: one idea, plain words, no jargon the audience doesn't use, nothing the viewer has to re-read.
- retention: pacing - sentence length, open loops, pattern breaks, no dead seconds, ends before it drags. Judge total words / duration / sentence length against the SHAPE block, not against a generic 30-60s rule: if the top quartile here runs 55-90s, a 40s draft is short for THIS library, and vice versa. Quote the numbers.
- voice: fit with the creator's own baseline / project context when present (wpm, energy, CTA habits, vocabulary); otherwise fit with the top reels' register. If there is no voice data, judge consistency of register and say the voice fit is unknown."""

SCRIPT_CONTEXT_RULES = """HOW TO USE THE LIBRARY CONTEXT
- Prefer specific, library-backed claims: name the lever (e.g. hook.type=contrarian), its n and lift from the lever table, or a reel id that did the thing. Never invent a lift; if a lever has n < 5 or the library has < 15 carded reels, say the evidence is thin in the same sentence.
- Reel ids look like 7_<shortcode>; use them exactly as given so the UI can link them. Formula ids look like [F42].
- Banned generic advice: "post consistently", "use trending audio", "make the hook more engaging", "know your audience", "add value", "be authentic", "keep it short". Say WHICH line, WHICH word, WHICH second.
- The external algorithm brief is platform background, not data about these reels. Use it to explain WHY a change should help (watch time / likes / sends), never to override what the library shows.
- The SHAPE block is measured, not guessed: the draft's hook / script / CTA in words and seconds against the medians and middle half of this library, its top quartile by reach, and the creator's own reels. Every ABOVE / BELOW row is a finding you must either act on (name it in the relevant score note, a weakness, or the one change, with the numbers) or explicitly dismiss with a reason. Prefer the top quartile's range over the library's, and the creator's own numbers for voice."""


def _script_studio_context(user_id, project_id, project):
    """Shared prompt context for the three script calls. Kept compact: the
    playbook, a trimmed lever table, top reels' hooks/CTAs, own baseline."""
    overview = agent_tools.get_project_overview(user_id, project_id)
    knowledge = project_knowledge(user_id, project_id)
    lever_rows = sorted(knowledge.get("levers") or [], key=lambda e: -(e.get("n") or 0))[:40]
    lever_lines = [f"- {e['dimension']}={e['value']}: n={e['n']}, lift_reach={e['lift_reach']}, lift_sends={e['lift_sends']}, "
                   f"breakout_rate={e['breakout_rate']}, {e['confidence']}" for e in lever_rows]
    top_lines = []
    for r in (overview.get("top_by_reach") or [])[:6]:
        if not isinstance(r, dict):
            continue
        tags = r.get("tags") or {}
        top_lines.append(f"- {r.get('id')}: reach {r.get('reach_multiple')}x, sends/1k {r.get('sends_per_1k')}, "
                         f"{r.get('duration_sec')}s; hook: \"{str(r.get('hook') or '')[:160]}\"; "
                         f"hook.type={tags.get('hook.type')}, cta.type={tags.get('cta.type')}, structure={tags.get('structure.type')}"
                         + (f"; read: {str(r.get('one_line'))[:140]}" if r.get('one_line') else ""))
    own = overview.get("own_baseline") or {}
    own_full = knowledge.get("own") or {}
    voice = {k: own_full.get(k) for k in ("n", "median_reach", "median_sends_1k", "median_er", "voice")
             if own_full.get(k) is not None}
    counts = overview.get("counts") or {}
    parts = [project_prompt_context(project)]
    parts.append(f"\nLIBRARY: {counts.get('reels_done', 0)} reels, {counts.get('carded') or 0} carded, "
                 f"{counts.get('own', 0)} own, {counts.get('formulas', 0)} formulas. "
                 + (overview.get("data_caveat") or ""))
    if overview.get("playbook"):
        parts.append("\nPLAYBOOK (what works in this library, with numbers):\n" + str(overview["playbook"])[:4000])
    parts.append("\nBASELINE: " + json.dumps(overview.get("baseline") or {}, default=str)[:800])
    parts.append("\nLEVER TABLE (tag -> n, lift vs baseline):\n" + ("\n".join(lever_lines) or "(no levers with enough data yet)"))
    parts.append("\nTOP REELS BY REACH (hook / cta verbatim):\n" + ("\n".join(top_lines) or "(none)"))
    parts.append("\nCREATOR'S OWN BASELINE / VOICE: " + (json.dumps(voice or own, default=str)[:1200] if (voice or own) else "(no own reels - voice unknown)"))
    norms = levers.shape_norms_block(_shape_profile(user_id, project_id))
    if norms:
        parts.append("\n" + norms)
    parts.append(algorithm_prompt_block())
    return "\n".join(p for p in parts if p)


def _shape_profile(user_id, project_id):
    """Shape norms (hook words/seconds, length, pace, CTA position...) measured
    over this project's cards - library, top quartile by reach, own reels."""
    return levers.shape_profile(_rank_rows(_done_jobs(user_id, project_id), project_id))


def _script_shape_bundle(user_id, project_id, script):
    """Measure a pasted draft and line it up against the norms. Returns
    (prompt_block, payload_for_ui, wpm_used). The wpm the seconds assume is the
    creator's own median when they have reels, else the top quartile's, else 150."""
    profile = _shape_profile(user_id, project_id)
    groups = profile.get("groups") or {}
    wpm = None
    for grp in ("own", "top", "all"):
        q = (groups.get(grp) or {}).get("wpm")
        if q and q.get("median"):
            wpm = int(round(q["median"]))
            break
    wpm = wpm or 150
    draft = levers.script_shape(script, wpm)
    checks = levers.shape_check(draft, profile)
    block = levers.shape_prompt_block(draft, checks, profile)
    payload = None
    if draft:
        slim = {k: v for k, v in draft.items() if k != "spoken_text"}
        payload = {"draft": slim, "checks": checks, "wpm_assumed": wpm,
                   "norms_n": {g: (groups.get(g) or {}).get("n", 0) for g in ("all", "top", "own")},
                   "top_basis": profile.get("top_basis")}
    return block, payload, wpm


RATE_SCRIPT_PROMPT = """You are a ruthless but useful short-form video script editor, rating ONE draft Instagram Reel script for a creator whose library of reference reels you have stats on. Rate the draft as written - do not rewrite it here.
{context}

{rubric}

{rules}

{shape_block}

{taxonomy}

Respond with ONLY a strict JSON object:
{{
  "overall": 0-100 integer (weighted: hook 25, promise 10, body 20, cta 15, clarity 10, retention 15, voice 5),
  "verdict": "one sentence, the honest headline - would this travel in this library or not, and the single biggest reason",
  "band": "weak" | "ok" | "good" | "strong",
  "scores": {{
    "hook": {{"score": 0-10, "note": "1-2 sentences, quote the line, name the shape, cite a lever/reel"}},
    "promise": {{"score": 0-10, "note": "..."}},
    "body": {{"score": 0-10, "note": "..."}},
    "cta": {{"score": 0-10, "note": "..."}},
    "clarity": {{"score": 0-10, "note": "..."}},
    "retention": {{"score": 0-10, "note": "..."}},
    "voice": {{"score": 0-10, "note": "..."}}
  }},
  "first_seconds": "what a viewer sees/hears in seconds 0-3 and whether it stops the scroll - 1-2 sentences",
  "strengths": ["2-4 items, each specific, quoting the script"],
  "weaknesses": ["2-5 items, each specific, quoting the script, each naming the fix direction"],
  "predicted_signals": {{"watch_time": "strong|ok|weak - why", "likes": "strong|ok|weak - why", "sends": "strong|ok|weak - why"}},
  "one_change": "the single edit with the biggest expected lift, concrete enough to paste",
  "estimated_duration_sec": integer (at ~{wpm} wpm spoken),
  "tags": {{"hook.type": "...", "hook.devices": [...], "promise.type": "...", "structure.type": "...", "cta.type": "...", "cta.position": "...", "format.type": "...", "delivery.energy": "...", "emotion.primary": "...", "topic.niche": "...", "topic.tags": [...]}}
}}
Use the dotted tag keys exactly as written (e.g. "hook.type", not a nested object); pick values from the vocabulary above and "other" only when nothing fits.

{title_line}{goal_line}SCRIPT:
\"\"\"
{script}
\"\"\"
"""

IMPROVE_SCRIPT_PROMPT = """You are editing ONE draft Instagram Reel script for a creator whose library of reference reels you have stats on. Keep the creator's idea, structure and voice - this is an EDIT pass, not a rewrite. Make the fewest changes with the biggest lift: tighten the hook, sharpen the promise, cut dead lines, fix the CTA wording/placement, fix pacing. Every change must be justified with a lever stat, a reel that did it, or a platform signal.
{context}

{rules}

{shape_block}
{rating_block}
Respond with ONLY a strict JSON object:
{{
  "summary": "2-3 sentences: what you changed and the expected effect, with numbers where the library has them",
  "changes": [
    {{"where": "hook | promise | body | cta | pacing | on-screen", "from": "the original line(s), verbatim or closely quoted", "to": "the replacement, verbatim", "why": "one sentence naming the lever / reel / signal"}}
  ],
  "kept": ["1-3 things that already work and were left alone, quoting the script"],
  "improved_script": "the FULL script with the edits applied, as plain text the creator can read aloud. Keep their line breaks and labels if they used any (HOOK:, CTA:, etc). No markdown.",
  "still_weak": ["0-2 things an edit pass can't fix - only if true; otherwise empty"]
}}
Aim for 3-7 changes. Do not pad. Do not add sections the script didn't have unless it's a missing CTA.

{title_line}{goal_line}SCRIPT:
\"\"\"
{script}
\"\"\"
"""

REWRITE_SCRIPT_PROMPT = """You are rewriting ONE draft Instagram Reel script for a creator whose library of reference reels you have stats on. Keep the core idea and the facts the creator put in; everything else - hook shape, order of beats, CTA, on-screen text - should be rebuilt around what travels in this library. Match the creator's voice when there is a voice profile; otherwise match the register of the top reels.
{context}

{rules}

{shape_block}
{rating_block}
ANGLE FOR THIS REWRITE: {angle}
TARGET DURATION: {duration}

Respond with ONLY a strict JSON object:
{{
  "title": "short working title",
  "script": {{
    "hook": "first line(s), verbatim, <= 2 sentences",
    "promise": "one line",
    "beats": ["1. Said: <what is said> | Shown: <what is on screen>", "2. ...", "..."],
    "cta": "verbatim, or 'none - ends on <line>'",
    "on_screen_text": ["in order, one per item"],
    "caption": "with the text hook",
    "levers": {{"hook.type": "...", "cta.type": "...", "structure.type": "...", "promise.type": "..."}},
    "why": "2-4 short lines, each tying a lever to a lever stat (lift, n), a formula id or a reel id, and to a platform signal",
    "expected_signals": {{"watch_time": "strong|ok|weak - why", "likes": "...", "sends": "..."}},
    "duration_target_sec": integer,
    "format": "talking head | voiceover b-roll | screen recording | text on screen | ..."
  }},
  "script_text": "the same script as plain read-aloud text: HOOK line, then each beat's spoken part on its own line, then the CTA. No markdown, no 'Said:' labels.",
  "what_changed": ["3-6 items: the structural differences from the original and why, each citing a lever/reel/signal"],
  "kept_from_original": ["1-3 items: the ideas/facts/lines carried over"]
}}
Short lines. Never pad. The hook must be a shape that has data in the lever table or the top reels, and you must say which.

{title_line}{goal_line}ORIGINAL SCRIPT:
\"\"\"
{script}
\"\"\"
"""

SCRIPT_ANGLES = {
    "default": "Best shot at travelling in this library - use the strongest-evidence hook shape and CTA.",
    "shorter": "Cut to the bone: a 20-30 second version. One idea, one proof, one ask.",
    "contrarian": "Lead with the counter-intuitive take / the thing the audience gets wrong, then earn it.",
    "story": "Story-led: a specific moment or person first, the lesson second. Present tense, concrete details.",
    "listicle": "Numbered beats ('3 things...'), each beat a pattern break, payoff saved for the last one.",
    "sends": "Optimise for sends/shares: make it the reel someone forwards to a specific friend, with a send CTA worded for that.",
}


def _script_payload():
    body = request.get_json(force=True, silent=True) or {}
    script = (body.get("script") or "").strip()
    if len(script) < 20:
        return None, None, (jsonify({"error": "Paste a script first - at least a couple of sentences."}), 400)
    if len(script) > 12000:
        script = script[:12000]
    settings = with_platform_fallback(load_settings(g.user_id))
    if not settings.get("api_key"):
        return None, None, (jsonify({"error": "No AI API key configured. Add one in Settings."}), 400)
    return body, settings, None


def _script_title_goal(body):
    title = (body.get("title") or "").strip()[:160]
    goal = (body.get("goal") or "").strip()[:400]
    return (f"TITLE / TOPIC: {title}\n" if title else ""), (f"WHAT THE CREATOR WANTS FROM THIS REEL: {goal}\n" if goal else "")


def _rating_block(body):
    rating = body.get("rating")
    if not isinstance(rating, dict) or not rating.get("scores"):
        return ""
    slim = {k: rating.get(k) for k in ("overall", "verdict", "scores", "weaknesses", "one_change", "predicted_signals") if rating.get(k) is not None}
    return ("\nTHE RATING THIS DRAFT ALREADY GOT (address the weaknesses; don't repeat the diagnosis):\n"
            + json.dumps(slim, ensure_ascii=False, default=str)[:3500] + "\n")


def _clamp_score(v, hi):
    try:
        return max(0, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


@app.route("/api/script/rate", methods=["POST"])
@require_login
@require_project
def script_rate():
    body, settings, err = _script_payload()
    if err:
        return err
    script = body["script"].strip()[:12000]
    title_line, goal_line = _script_title_goal(body)
    try:
        context = _script_studio_context(g.user_id, g.project_id, g.project)
        shape_block, shape, wpm = _script_shape_bundle(g.user_id, g.project_id, script)
        prompt = RATE_SCRIPT_PROMPT.format(context=context, rubric=SCRIPT_RUBRIC, rules=SCRIPT_CONTEXT_RULES,
                                           shape_block=shape_block, taxonomy=taxonomy.prompt_block(), wpm=wpm,
                                           title_line=title_line, goal_line=goal_line, script=script)
        result = call_ai(settings, prompt, max_tokens=3000)
    except Exception as exc:
        print(f"[script] rate failed: {exc}", file=sys.stderr)
        return jsonify({"error": f"Rating failed: {exc}"}), 500
    if not isinstance(result, dict):
        return jsonify({"error": "The model returned something that wasn't a rating. Try again."}), 502

    scores = result.get("scores") if isinstance(result.get("scores"), dict) else {}
    clean_scores = {}
    for key in ("hook", "promise", "body", "cta", "clarity", "retention", "voice"):
        entry = scores.get(key)
        if isinstance(entry, dict):
            clean_scores[key] = {"score": _clamp_score(entry.get("score"), 10), "note": str(entry.get("note") or "")[:600]}
        elif entry is not None:
            clean_scores[key] = {"score": _clamp_score(entry, 10), "note": ""}
    overall = _clamp_score(result.get("overall"), 100)
    if overall is None and clean_scores:
        weights = {"hook": 25, "promise": 10, "body": 20, "cta": 15, "clarity": 10, "retention": 15, "voice": 5}
        num = sum(weights[k] * (v["score"] or 0) for k, v in clean_scores.items() if v.get("score") is not None)
        den = sum(weights[k] for k, v in clean_scores.items() if v.get("score") is not None)
        overall = int(round(num * 10 / den)) if den else None
    band = result.get("band") if result.get("band") in ("weak", "ok", "good", "strong") else (
        None if overall is None else ("weak" if overall < 45 else "ok" if overall < 65 else "good" if overall < 80 else "strong"))

    # Same deterministic lever matching the agent's tag_text does: the model's
    # tags -> this library's lever table -> n / lift per tag. Numbers, not vibes.
    lever_matches, sketch = [], None
    try:
        tags, _problems = taxonomy.clean_tags(result.get("tags"), {"wpm": None, "duration_sec": None})
        table = levers.lever_table(_rank_rows(_done_jobs(g.user_id, g.project_id), g.project_id))
        by_key = {(e["dimension"], e["value"]): e for e in table.get("levers") or []}
        for dim, val in taxonomy.flat_tag_pairs(tags):
            e = by_key.get((dim, val))
            lever_matches.append({"lever": f"{dim}={val}", "n": e["n"] if e else None,
                                  "lift_reach": e["lift_reach"] if e else None, "lift_sends": e["lift_sends"] if e else None,
                                  "confidence": e["confidence"] if e else None})
        lifts = [m["lift_reach"] for m in lever_matches if m["lift_reach"]]
        if lifts:
            sketch = round(math.exp(sum(math.log(x) for x in lifts) / len(lifts)), 2)
    except Exception as exc:
        print(f"[script] lever match skipped: {exc}", file=sys.stderr)

    words = len(script.split())
    return jsonify({
        "overall": overall, "band": band,
        "verdict": str(result.get("verdict") or "")[:500],
        "scores": clean_scores,
        "first_seconds": str(result.get("first_seconds") or "")[:600],
        "strengths": [str(x)[:400] for x in (result.get("strengths") or []) if x][:6],
        "weaknesses": [str(x)[:400] for x in (result.get("weaknesses") or []) if x][:8],
        "predicted_signals": result.get("predicted_signals") if isinstance(result.get("predicted_signals"), dict) else {},
        "one_change": str(result.get("one_change") or "")[:800],
        "estimated_duration_sec": _clamp_score(result.get("estimated_duration_sec"), 600) or int(round(words / wpm * 60)),
        "word_count": words,
        "lever_matches": lever_matches,
        "predicted_lift_sketch": sketch,
        "shape": shape,
        "model": settings.get("model"),
        "rated_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/script/improve", methods=["POST"])
@require_login
@require_project
def script_improve():
    body, settings, err = _script_payload()
    if err:
        return err
    script = body["script"].strip()[:12000]
    title_line, goal_line = _script_title_goal(body)
    try:
        context = _script_studio_context(g.user_id, g.project_id, g.project)
        shape_block, _shape, _wpm = _script_shape_bundle(g.user_id, g.project_id, script)
        prompt = IMPROVE_SCRIPT_PROMPT.format(context=context, rules=SCRIPT_CONTEXT_RULES, shape_block=shape_block, rating_block=_rating_block(body),
                                              title_line=title_line, goal_line=goal_line, script=script)
        result = call_ai(settings, prompt, max_tokens=3500)
    except Exception as exc:
        print(f"[script] improve failed: {exc}", file=sys.stderr)
        return jsonify({"error": f"Improve failed: {exc}"}), 500
    if not isinstance(result, dict) or not result.get("improved_script"):
        return jsonify({"error": "The model didn't return an improved script. Try again."}), 502
    changes = []
    for c in result.get("changes") or []:
        if isinstance(c, dict):
            changes.append({k: str(c.get(k) or "")[:900] for k in ("where", "from", "to", "why")})
        elif c:
            changes.append({"where": "", "from": "", "to": str(c)[:900], "why": ""})
    return jsonify({
        "summary": str(result.get("summary") or "")[:1200],
        "changes": changes[:12],
        "kept": [str(x)[:400] for x in (result.get("kept") or []) if x][:5],
        "improved_script": str(result.get("improved_script"))[:14000],
        "still_weak": [str(x)[:400] for x in (result.get("still_weak") or []) if x][:4],
        "model": settings.get("model"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/script/rewrite", methods=["POST"])
@require_login
@require_project
def script_rewrite():
    body, settings, err = _script_payload()
    if err:
        return err
    script = body["script"].strip()[:12000]
    title_line, goal_line = _script_title_goal(body)
    angle_key = body.get("angle") if body.get("angle") in SCRIPT_ANGLES else "default"
    try:
        duration = int(body.get("duration_target_sec") or 0)
    except (TypeError, ValueError):
        duration = 0
    duration_text = f"about {duration} seconds" if 10 <= duration <= 180 else "whatever the idea needs; the library's median is a guide"
    try:
        context = _script_studio_context(g.user_id, g.project_id, g.project)
        shape_block, _shape, _wpm = _script_shape_bundle(g.user_id, g.project_id, script)
        prompt = REWRITE_SCRIPT_PROMPT.format(context=context, rules=SCRIPT_CONTEXT_RULES, shape_block=shape_block, rating_block=_rating_block(body),
                                              angle=SCRIPT_ANGLES[angle_key], duration=duration_text,
                                              title_line=title_line, goal_line=goal_line, script=script)
        result = call_ai(settings, prompt, max_tokens=3500)
    except Exception as exc:
        print(f"[script] rewrite failed: {exc}", file=sys.stderr)
        return jsonify({"error": f"Rewrite failed: {exc}"}), 500
    new_script = result.get("script") if isinstance(result, dict) else None
    if not isinstance(new_script, dict) or not new_script.get("hook"):
        return jsonify({"error": "The model didn't return a rewrite. Try again."}), 502
    script_text = str(result.get("script_text") or "").strip()
    if not script_text:
        spoken = []
        for b in new_script.get("beats") or []:
            s = str(b)
            s = s.split("| Shown:")[0]
            s = re.sub(r"^\s*\d+[.)]\s*", "", s)
            s = re.sub(r"^\s*Said:\s*", "", s, flags=re.I)
            spoken.append(s.strip())
        script_text = "\n".join([str(new_script.get("hook") or "")] + spoken + ([str(new_script["cta"])] if new_script.get("cta") else []))
    return jsonify({
        "title": str(result.get("title") or new_script.get("hook") or "Rewrite")[:120],
        "script": new_script,
        "script_text": script_text[:14000],
        "what_changed": [str(x)[:400] for x in (result.get("what_changed") or []) if x][:8],
        "kept_from_original": [str(x)[:400] for x in (result.get("kept_from_original") or []) if x][:5],
        "angle": angle_key,
        "model": settings.get("model"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/agent/drafts", methods=["POST"])
@require_login
@require_project
def agent_draft_create():
    """Save a script the user already has (e.g. a Rate-my-script rewrite) into the
    same drafts list the agent writes to."""
    body = request.get_json(force=True, silent=True) or {}
    script = body.get("script")
    if not isinstance(script, dict):
        return jsonify({"error": "script must be an object with at least a 'hook'"}), 400
    draft = agent_tools.save_draft(g.user_id, g.project_id, body.get("title"), script, predicted_tags=body.get("predicted_tags"))
    return (jsonify(draft), 400) if draft.get("error") else (jsonify(draft), 201)


@app.route("/api/submit", methods=["POST"])
@require_login
@require_project
def submit():
    data = request.get_json(force=True)
    raw_lines = data.get("urls", [])
    lines = [u.strip() for u in raw_lines if u.strip()]

    accepted_ids = []
    skipped = []

    for line in lines:
        if not INSTAGRAM_URL_RE.search(line):
            skipped.append(line)
            continue
        rid = reel_id_for(line, g.project_id)
        with jobs_lock:
            already_running = rid in jobs and jobs[rid].get("status") not in ("done", "error")
        if rid not in jobs or not already_running:
            new_job(rid, line, g.user_id, g.project_id, status="queued")
            download_queue.put((rid, line))
        accepted_ids.append(rid)

    return jsonify({"accepted": accepted_ids, "skipped": skipped})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5151)), debug=False, threaded=True)
