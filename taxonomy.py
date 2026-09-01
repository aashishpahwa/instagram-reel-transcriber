"""The closed vocabulary every reel is tagged against - the "levers".

Why a closed list: formulas, lever statistics and the agent all need to compare
reels across runs, projects and niches. Free text can't be counted or filtered;
a fixed slug can. Every enum dimension ends in `other`, and the model must give
an `other_note` when it uses it - that is how the taxonomy grows deliberately
(someone reads the notes and promotes a recurring one to a real value) instead
of drifting.

The values are the things a creator can CHANGE next time, chosen so they map
onto what the platform says it ranks on: watch time (hook, structure, pacing),
sends (emotion, utility), likes (relatability, clarity).

Bump TAXONOMY_VERSION whenever a value is added, renamed or removed; reels carry
the version they were tagged under so a later stats pass knows what it is
comparing.
"""

TAXONOMY_VERSION = 1

# key -> {label, multi, values: [(slug, one-line meaning)]}
DIMENSIONS = {
    "hook.type": {
        "label": "Hook type", "multi": False,
        "values": [
            ("question", "opens with a question the viewer wants answered"),
            ("bold_claim", "a strong, surprising or absolute statement"),
            ("contrarian", "pushes against a common belief or popular advice"),
            ("curiosity_gap", "withholds the key detail to make you wait for it"),
            ("story_open", "drops into a scene, moment or anecdote"),
            ("stat_or_number", "leads with a specific number, stat or figure"),
            ("listicle_promise", "promises N things / tips / tools"),
            ("callout", "addresses a specific viewer: 'if you...', 'you...'"),
            ("result_first", "shows or states the payoff/result before explaining"),
            ("pain_point", "names a frustration or problem the viewer has"),
            ("authority_credential", "leads with who the creator is or what they've done"),
            ("trend_or_news", "hooks on something current - news, a launch, a trend"),
            ("challenge_or_dare", "sets up a test, bet, experiment or dare"),
            ("other", "none of the above - say what in other_note"),
        ],
    },
    "hook.devices": {
        "label": "Hook devices", "multi": True,
        "values": [
            ("open_loop", "starts something it doesn't finish until later"),
            ("specificity", "concrete names, numbers, details rather than generalities"),
            ("negativity_or_fear", "warning, loss, mistake, 'stop doing X'"),
            ("novelty", "something new, unseen, or unusual"),
            ("social_proof", "others did it / it worked for many / it went viral"),
            ("direct_address", "'you', 'your', talking straight at the viewer"),
            ("pattern_interrupt_visual", "the first frames look unlike the usual feed"),
            ("text_hook_on_screen", "a text card / caption carries the hook"),
            ("humor", "a joke, irony or absurdity in the opening"),
            ("other", "none of the above - say what in other_note"),
        ],
    },
    "promise.type": {
        "label": "Promise", "multi": False,
        "values": [
            ("how_to", "you'll learn how to do something"),
            ("list", "you'll get N items"),
            ("story_payoff", "you'll find out how the story ends"),
            ("reveal_or_secret", "you'll learn something hidden or insider"),
            ("comparison", "X vs Y, which is better"),
            ("warning", "what to avoid and why"),
            ("opinion", "the creator's take on something"),
            ("none", "no explicit promise - goes straight into content"),
            ("other", "none of the above - say what in other_note"),
        ],
    },
    "structure.type": {
        "label": "Structure", "multi": False,
        "values": [
            ("listicle", "numbered or implicit list of items"),
            ("story", "setup -> turn -> payoff"),
            ("tutorial_steps", "ordered steps to do something"),
            ("rant", "a run of grievances or opinions on one theme"),
            ("review", "verdicts on a product/tool/thing"),
            ("myth_bust", "claim then debunk"),
            ("reaction", "a thing, then the creator's reaction to it"),
            ("demo", "showing the thing working"),
            ("comparison", "side by side of two or more options"),
            ("commentary", "explaining or analysing something happening elsewhere"),
            ("skit", "acted scenario / characters"),
            ("other", "none of the above - say what in other_note"),
        ],
    },
    "cta.type": {
        "label": "CTA", "multi": False,
        "values": [
            ("none", "no ask at all"),
            ("follow", "follow for more"),
            ("comment_keyword", "comment a word to get something"),
            ("save", "save this for later"),
            ("share_send", "send this to someone / share"),
            ("link_in_bio", "go to the link in bio"),
            ("dm", "DM me"),
            ("watch_next", "watch another video / part 2"),
            ("other", "none of the above - say what in other_note"),
        ],
    },
    "cta.position": {
        "label": "CTA position", "multi": False,
        "values": [
            ("none", "no CTA"),
            ("early", "within the first third"),
            ("mid", "somewhere in the middle"),
            ("end", "in the last third"),
        ],
    },
    "format.type": {
        "label": "Format", "multi": False,
        "values": [
            ("talking_head", "person speaking to camera"),
            ("screen_recording", "screen capture / software demo"),
            ("voiceover_broll", "narration over footage or images"),
            ("text_only", "text cards / captions carry it, no speaker on screen"),
            ("vlog", "handheld, in-the-moment footage"),
            ("interview", "two or more people talking"),
            ("skit", "acted scenario"),
            ("ugc_testimonial", "first-person product experience / review style"),
            ("other", "none of the above - say what in other_note"),
        ],
    },
    "format.captions": {
        "label": "Captions", "multi": False,
        "values": [
            ("none", "no on-screen captions"),
            ("auto_style", "plain / auto-generated style captions"),
            ("designed", "styled, emphasised, animated captions"),
        ],
    },
    "delivery.energy": {
        "label": "Energy", "multi": False,
        "values": [("low", "calm, measured"), ("medium", "conversational"), ("high", "fast, loud, excited")],
    },
    "audience.awareness": {
        "label": "Audience awareness", "multi": False,
        "values": [
            ("unaware", "doesn't know they have the problem"),
            ("problem_aware", "knows the problem, not the solutions"),
            ("solution_aware", "knows solutions exist, comparing"),
            ("product_aware", "knows the specific thing, deciding"),
        ],
    },
    "emotion.primary": {
        "label": "Primary emotion", "multi": False,
        "values": [
            ("curiosity", "want to know"), ("fear_fomo", "fear of missing out / losing"),
            ("aspiration", "want to be / have that"), ("humor", "funny"),
            ("outrage", "that's wrong / unfair"), ("relatability", "that's so me"),
            ("utility", "useful, I'll need this"), ("awe", "wow"),
            ("other", "none of the above - say what in other_note"),
        ],
    },
}

# 1-5 ratings the model gives. Use with care - they are judgements, not measurements.
SCORES = {
    "specificity": "how concrete the claims, names and numbers are (1 vague - 5 razor specific)",
    "novelty": "how new the idea/angle is to this audience (1 seen everywhere - 5 never seen)",
    "clarity": "how easy the point is to follow on one viewing (1 muddled - 5 crystal)",
    "hook_strength": "how likely the first 3 seconds stop a scroll, judged on the opening alone",
}

# Integers the model estimates from the transcript timing / scenes.
NUMBERS = {
    "hook.seconds_to_first_payoff": "seconds until the viewer first gets something (an answer, a reveal, a laugh, a result)",
    "structure.rehook_count": "how many times the video re-opens a loop mid-way ('but here's the thing', 'and the last one is the best')",
}

# Derived in code, never asked of the model.
DERIVED = ("delivery.wpm_band", "duration_band")


def wpm_band(wpm):
    if wpm is None:
        return None
    if wpm < 120:
        return "<120"
    if wpm < 160:
        return "120-160"
    if wpm < 200:
        return "160-200"
    return "200+"


def duration_band(seconds):
    if seconds is None:
        return None
    if seconds < 15:
        return "<15"
    if seconds < 30:
        return "15-30"
    if seconds < 60:
        return "30-60"
    if seconds < 90:
        return "60-90"
    return "90+"


def prompt_block():
    """The vocabulary as the model sees it."""
    lines = ["TAXONOMY (closed lists - use ONLY these slugs; `other` needs an other_note):"]
    for key, dim in DIMENSIONS.items():
        kind = "choose ALL that apply (array)" if dim["multi"] else "choose ONE"
        lines.append(f"- {key} ({kind}):")
        for slug, meaning in dim["values"]:
            lines.append(f"    {slug}: {meaning}")
    lines.append("- topic.niche: 2-5 word free text for the niche this reel sits in (e.g. 'AI tools for marketers')")
    lines.append("- topic.tags: array of 3-6 short topic labels")
    lines.append("- scores (integers 1-5):")
    for key, meaning in SCORES.items():
        lines.append(f"    {key}: {meaning}")
    lines.append("- numbers (integers):")
    for key, meaning in NUMBERS.items():
        lines.append(f"    {key}: {meaning}")
    return "\n".join(lines)


def _valid_slugs(key):
    return {slug for slug, _ in DIMENSIONS[key]["values"]}


_PREFIXES = {key.split(".")[0] for key in DIMENSIONS} | {"topic"}
_NON_FLATTEN = {"scores", "numbers", "other_notes"}


def _flatten(raw):
    """Accept {"hook": {"type": "x", "devices": [...]}} as well as {"hook.type": "x"}.
    Models produce both shapes; dotted keys are the canonical one."""
    out = {}
    for key, value in raw.items():
        if key in _PREFIXES and isinstance(value, dict) and key not in _NON_FLATTEN:
            for sub, val in value.items():
                out.setdefault(f"{key}.{sub}", val)
        else:
            out.setdefault(key, value)
    return out


def clean_tags(raw, metrics=None):
    """Validate a model's tag object against the taxonomy. Unknown slugs are
    dropped, `other` without a note is kept but flagged, scores/numbers are
    coerced to ints in range. Returns (tags, problems) where problems is a list
    of strings for the log - the caller stores tags regardless, because a
    partially valid card is more useful than none."""
    raw = _flatten(raw if isinstance(raw, dict) else {})
    tags = {"taxonomy_v": TAXONOMY_VERSION}
    problems = []
    notes = raw.get("other_notes") if isinstance(raw.get("other_notes"), dict) else {}

    for key, dim in DIMENSIONS.items():
        valid = _valid_slugs(key)
        value = raw.get(key)
        if dim["multi"]:
            items = value if isinstance(value, list) else ([value] if isinstance(value, str) else [])
            cleaned = []
            for item in items:
                slug = str(item).strip().lower()
                if slug in valid and slug not in cleaned:
                    cleaned.append(slug)
                elif slug:
                    problems.append(f"{key}: unknown slug {slug!r} dropped")
            tags[key] = cleaned
            if "other" in cleaned and not notes.get(key):
                problems.append(f"{key}: 'other' without other_note")
        else:
            slug = str(value).strip().lower() if isinstance(value, str) else None
            if slug in valid:
                tags[key] = slug
                if slug == "other" and not notes.get(key):
                    problems.append(f"{key}: 'other' without other_note")
            else:
                tags[key] = None
                if slug:
                    problems.append(f"{key}: unknown slug {slug!r} dropped")

    clean_notes = {k: str(v).strip()[:160] for k, v in notes.items()
                   if k in DIMENSIONS and isinstance(v, (str, int, float)) and str(v).strip()}
    if clean_notes:
        tags["other_notes"] = clean_notes

    niche = raw.get("topic.niche")
    tags["topic.niche"] = str(niche).strip()[:80] if isinstance(niche, str) and niche.strip() else None
    topic_tags = raw.get("topic.tags")
    tags["topic.tags"] = ([str(t).strip()[:40] for t in topic_tags if isinstance(t, (str, int, float)) and str(t).strip()][:8]
                          if isinstance(topic_tags, list) else [])

    scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
    tags["scores"] = {}
    for key in SCORES:
        value = scores.get(key, raw.get(key))
        try:
            value = int(round(float(value)))
        except (TypeError, ValueError):
            value = None
        tags["scores"][key] = min(5, max(1, value)) if value is not None else None

    numbers = raw.get("numbers") if isinstance(raw.get("numbers"), dict) else {}
    tags["numbers"] = {}
    for key in NUMBERS:
        value = numbers.get(key, raw.get(key))
        try:
            value = int(round(float(value)))
        except (TypeError, ValueError):
            value = None
        tags["numbers"][key] = max(0, value) if value is not None else None

    metrics = metrics or {}
    tags["delivery.wpm_band"] = wpm_band(metrics.get("wpm"))
    tags["duration_band"] = duration_band(metrics.get("duration_sec"))
    return tags, problems


def flat_tag_pairs(tags):
    """[(dimension, value), ...] for stats: multi dimensions expand to one pair
    per value, scores/numbers/topic are left out (they are not levers)."""
    pairs = []
    for key, dim in DIMENSIONS.items():
        value = (tags or {}).get(key)
        if dim["multi"]:
            pairs.extend((key, v) for v in (value or []))
        elif value:
            pairs.append((key, value))
    for key in DERIVED:
        if (tags or {}).get(key):
            pairs.append((key, tags[key]))
    return pairs
