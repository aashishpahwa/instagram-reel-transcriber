"""Jev - TypeSafe AI's System One model - as this app's judgement layer.

Jev does not write. It reads one `state` (text or JSON) and answers typed
questions about it - `choice` (one label out of a closed set), `score` (a
position on an ordered rubric) and `noul` (a yes/no probability) - and every
answer comes back with calibrated probabilities. That is exactly the shape of
this app's closed taxonomy (see taxonomy.py), so the division of labour is:

  Jev       tags reels against the closed vocabulary, scores rewrite candidates
            against the creator's measured style. Cheap, fast, and the same
            question always means the same thing, which is what lever
            statistics need.
  the LLM   everything that has to be written: diagnoses, the account study,
            the rewrites themselves.
  code      all arithmetic (lifts, medians, ranking). Jev is documented as
            unreliable at counting, arithmetic and date ordering, so it is never
            asked to do any.

Wire format read from the official SDK (@typesafe-ai/sdk 0.6.0), called here
over plain `requests` like every other provider in this app:

  POST {base}/v1/systemone   Authorization: Bearer <key>
  {"model": "jev-latest", "state": <text|json>, "questions": {name: question}}
  question = {"type": "choice", "instructions": str, "criteria": {label: description}}
           | {"type": "score",  "instructions": str, "criteria": [level0, level1, ...]}
           | {"type": "noul",   "instructions": str}
  -> {"model": str, "usage": {...}, "answers": {name:
        {"type": "choice", "choice": label, "confidence": f, "probabilities": {label: f}}
      | {"type": "score",  "score": f, "confidence": f, "probabilities": {...}}
      | {"type": "noul",   "noul": f}}}

Limits: 64k tokens per request, 32k for state + the longest question; text only.
"""

import os
import sys
import time

import requests

import taxonomy

BASE_URL = (os.environ.get("TYPESAFE_BASE_URL", "").strip() or "https://api.typesafe.ai").rstrip("/")
DEFAULT_MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "").strip() or "jev-latest"
PLATFORM_KEY = os.environ.get("TYPESAFE_API_KEY", "").strip()

TIMEOUT_SEC = 20
MAX_RETRIES = 2
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

# State is capped well inside the 32k-token ceiling: Jev's accuracy degrades
# with context it doesn't need, so a reel is sent as its transcript plus a
# little structure, never the whole job.
STATE_TRANSCRIPT_CHARS = 6000
STATE_VISUAL_CHARS = 1200

# A closed-vocabulary answer replaces the LLM's only when Jev is at least this
# sure; below it the LLM's tag stands and the disagreement is recorded.
TAG_CONFIDENCE_FLOOR = 0.5
# hook.devices is "all that apply": one noul per device, kept above this.
DEVICE_PROBABILITY_FLOOR = 0.6


class JevError(Exception):
    pass


def system_one(api_key, state, questions, model=None, timeout=TIMEOUT_SEC):
    """One Jev call. Returns the parsed response dict; raises JevError."""
    if not api_key:
        raise JevError("No Jev (TypeSafe) API key configured.")
    if not questions:
        raise JevError("Jev needs at least one question.")
    body = {"model": model or DEFAULT_MODEL, "state": state, "questions": questions}
    last = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(f"{BASE_URL}/v1/systemone", json=body, timeout=timeout,
                                 headers={"Authorization": f"Bearer {api_key}",
                                          "Accept": "application/json"})
        except requests.RequestException as exc:
            last = JevError(f"Jev request failed: {exc}")
        else:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    raise JevError("Jev returned a non-JSON response.")
                if not isinstance(data.get("answers"), dict):
                    raise JevError("Jev response had no answers.")
                return data
            detail = (resp.text or "")[:300]
            last = JevError(f"Jev HTTP {resp.status_code}: {detail}")
            if resp.status_code not in RETRY_STATUSES:
                raise last
        if attempt < MAX_RETRIES:
            time.sleep(0.5 * (2 ** attempt))
    raise last


def choice(instructions, criteria):
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions, levels):
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def noul(instructions):
    return {"type": "noul", "instructions": instructions}


# ---------- reel tagging against the closed taxonomy ----------

_SCORE_LEVELS = {
    "specificity": ["Entirely vague - no names, numbers or concrete details",
                    "Mostly general, one or two concrete details",
                    "A mix of general statements and concrete details",
                    "Mostly concrete - named things, real numbers",
                    "Razor specific throughout - every claim has a name or a number"],
    "novelty": ["An idea or angle seen everywhere",
                "A familiar idea with a slightly fresh framing",
                "A reasonably fresh angle on a known topic",
                "A new angle most of the audience has not seen",
                "Something the audience has almost certainly never seen"],
    "clarity": ["Muddled - hard to say what the point is",
                "The point is there but takes effort to follow",
                "Followable on one viewing with some attention",
                "Clear on one viewing",
                "Crystal clear - one idea, plainly said"],
    "hook_strength": ["The opening gives no reason to stop scrolling",
                      "A weak opening - generic or slow to start",
                      "A serviceable opening that some viewers would stay for",
                      "A strong opening that most of the target audience would stay for",
                      "A scroll-stopping opening - immediate tension, curiosity or stakes"],
}


def _dim_question_name(key):
    return key.replace(".", "__")


def reel_state(job, visual_text=None):
    """The reel as Jev sees it: what was said, in what order, plus the caption
    and - when the visual pass has run - what was on screen."""
    analysis = job.get("analysis") or {}
    state = {
        "transcript": (job.get("transcript") or "")[:STATE_TRANSCRIPT_CHARS],
        "opening_lines": str(analysis.get("hook") or "")[:500] or None,
        "call_to_action": str(analysis.get("cta") or "")[:400] or None,
        "caption": (job.get("caption") or "")[:500] or None,
        "on_screen": (visual_text or "")[:STATE_VISUAL_CHARS] or None,
    }
    return {k: v for k, v in state.items() if v}


def script_state(script_text, caption=None):
    return {k: v for k, v in {"transcript": (script_text or "")[:STATE_TRANSCRIPT_CHARS],
                              "caption": (caption or "")[:500] or None}.items() if v}


def taxonomy_questions():
    """Every closed dimension as a Jev question. Single-value dimensions become
    one `choice`; the multi-value one becomes a `noul` per value; the 1-5 scores
    become five-level rubrics."""
    questions = {}
    for key, dim in taxonomy.DIMENSIONS.items():
        if dim["multi"]:
            for slug, meaning in dim["values"]:
                if slug == "other":
                    continue
                questions[f"{_dim_question_name(key)}__{slug}"] = noul(
                    f"The opening of this short-form video uses this device: {meaning}.")
        else:
            questions[_dim_question_name(key)] = choice(
                f"{dim['label']} of this short-form video.",
                {slug: meaning for slug, meaning in dim["values"]})
    for key, meaning in taxonomy.SCORES.items():
        questions[f"score__{key}"] = score(f"Rate {meaning.split(' (')[0]}.", _SCORE_LEVELS[key])
    return questions


def tags_from_answers(answers):
    """Jev's answers as (raw_tags, confidence): raw_tags is in the shape
    taxonomy.clean_tags() accepts, holding only what Jev was sure enough about;
    confidence maps every dimension Jev answered to how sure it was."""
    raw, confidence = {"scores": {}}, {}
    for key, dim in taxonomy.DIMENSIONS.items():
        name = _dim_question_name(key)
        if dim["multi"]:
            picked = []
            for slug, _ in dim["values"]:
                answer = answers.get(f"{name}__{slug}")
                if isinstance(answer, dict) and isinstance(answer.get("noul"), (int, float)) \
                        and answer["noul"] >= DEVICE_PROBABILITY_FLOOR:
                    picked.append(slug)
            if any(f"{name}__{slug}" in answers for slug, _ in dim["values"]):
                raw[key] = picked
            continue
        answer = answers.get(name)
        if not isinstance(answer, dict) or not answer.get("choice"):
            continue
        conf = answer.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else 0.0
        confidence[key] = round(conf, 3)
        # `other` needs a written note Jev cannot give - leave that to the LLM.
        if conf >= TAG_CONFIDENCE_FLOOR and answer["choice"] != "other":
            raw[key] = answer["choice"]
    for key in taxonomy.SCORES:
        answer = answers.get(f"score__{key}")
        if isinstance(answer, dict) and isinstance(answer.get("score"), (int, float)):
            raw["scores"][key] = int(round(answer["score"])) + 1   # rubric is 0-4, the card is 1-5
            if isinstance(answer.get("confidence"), (int, float)):
                confidence[f"scores.{key}"] = round(float(answer["confidence"]), 3)
    return raw, confidence


def tag_reel(api_key, job, visual_text=None, model=None):
    """Tag one reel. Returns (raw_tags, info) - info is what gets stored under
    tags['jev'] so the card can show where each tag came from."""
    response = system_one(api_key, reel_state(job, visual_text), taxonomy_questions(), model=model)
    raw, confidence = tags_from_answers(response["answers"])
    return raw, {"model": response.get("model"), "confidence": confidence,
                 "input_tokens": (response.get("usage") or {}).get("input_tokens")}


def merge_tags(llm_raw, jev_raw):
    """Jev's confident answers win over the LLM's on the closed dimensions and
    the scores; everything Jev can't produce (topic, numbers, other_notes) and
    everything it wasn't sure about stays the LLM's. Returns (merged_raw,
    disagreements) with disagreements as {dimension: {"llm": x, "jev": y}}."""
    llm_flat = taxonomy._flatten(llm_raw if isinstance(llm_raw, dict) else {})
    merged = dict(llm_flat)
    disagreements = {}
    for key in taxonomy.DIMENSIONS:
        if key not in jev_raw:
            continue
        before = llm_flat.get(key)
        if before and before != jev_raw[key] and not taxonomy.DIMENSIONS[key]["multi"]:
            disagreements[key] = {"llm": before, "jev": jev_raw[key]}
        merged[key] = jev_raw[key]
    scores = dict(llm_flat.get("scores") if isinstance(llm_flat.get("scores"), dict) else {})
    scores.update(jev_raw.get("scores") or {})
    merged["scores"] = scores
    return merged, disagreements


# ---------- style match: judging a rewrite against the creator's own style ----------

def style_questions(style):
    """Questions that judge a candidate script against a measured style profile.
    `style` is the creator_profiles.style object the account study writes."""
    style = style if isinstance(style, dict) else {}
    voice = str(style.get("voice_summary") or "a direct, conversational short-form creator")[:600]
    questions = {
        "voice_match": score(
            {"task": "How closely this script sounds like the creator described here.", "creator_voice": voice,
             "signature_moves": (style.get("signature_moves") or [])[:6],
             "words_they_use": (style.get("vocabulary") or [])[:20],
             "words_they_avoid": (style.get("avoid") or [])[:12]},
            ["Sounds like a different person entirely - wrong register and vocabulary",
             "Mostly generic, with one or two touches of the creator's voice",
             "Recognisably in the creator's register, with some lines that are not theirs",
             "Sounds like the creator throughout, a line or two slightly off",
             "Indistinguishable from the creator's own scripts"]),
        "hook_strength": score("Rate how likely the first two lines are to stop a scroll.",
                               _SCORE_LEVELS["hook_strength"]),
        "clarity": score("Rate how easy the point is to follow on one viewing.", _SCORE_LEVELS["clarity"]),
        "specificity": score("Rate how concrete the claims, names and numbers are.", _SCORE_LEVELS["specificity"]),
        "generic_ai_tone": noul("This script reads like generic AI-written marketing copy rather than a real "
                                "person talking to camera."),
        "padding": noul("This script contains filler lines that could be cut without losing anything."),
    }
    for key in ("hook.type", "structure.type", "cta.type"):
        dim = taxonomy.DIMENSIONS[key]
        questions[_dim_question_name(key)] = choice(f"{dim['label']} of this script.",
                                                    {slug: meaning for slug, meaning in dim["values"]})
    return questions


def judge_script(api_key, script_text, style, winning=None, caption=None, model=None):
    """Score one candidate script. `winning` is {dimension: [slugs that outperform
    on this account]}; a candidate earns credit for landing on one. Returns a
    flat verdict dict with a 0-100 `total` computed here, never by the model."""
    response = system_one(api_key, script_state(script_text, caption), style_questions(style), model=model)
    answers = response["answers"]

    def level(name):   # 0-4 rubric -> 0-1
        answer = answers.get(name) or {}
        return max(0.0, min(1.0, float(answer.get("score") or 0) / 4.0))

    def prob(name):
        answer = answers.get(name) or {}
        return max(0.0, min(1.0, float(answer.get("noul") or 0)))

    levers = {}
    on_winning = 0
    winning = winning if isinstance(winning, dict) else {}
    for key in ("hook.type", "structure.type", "cta.type"):
        answer = answers.get(_dim_question_name(key)) or {}
        if answer.get("choice"):
            levers[key] = answer["choice"]
            if answer["choice"] in (winning.get(key) or []):
                on_winning += 1
    lever_fit = on_winning / 3.0 if winning else 0.5

    verdict = {
        "voice_match": round(level("voice_match"), 3),
        "hook_strength": round(level("hook_strength"), 3),
        "clarity": round(level("clarity"), 3),
        "specificity": round(level("specificity"), 3),
        "generic_ai_tone": round(prob("generic_ai_tone"), 3),
        "padding": round(prob("padding"), 3),
        "levers": levers,
        "lever_fit": round(lever_fit, 3),
        "model": response.get("model"),
    }
    # Voice first - "in my style" is the whole point - then the hook, then the
    # rest. The two nouls are penalties.
    total = (0.34 * verdict["voice_match"] + 0.24 * verdict["hook_strength"] + 0.12 * verdict["clarity"]
             + 0.10 * verdict["specificity"] + 0.20 * lever_fit
             - 0.15 * verdict["generic_ai_tone"] - 0.08 * verdict["padding"])
    verdict["total"] = int(round(max(0.0, min(1.0, total)) * 100))
    return verdict


def log(message):
    print(f"[jev] {message}", file=sys.stderr)
