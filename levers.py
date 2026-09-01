"""Deterministic group analysis over Reel Cards - the part of the knowledge layer
that is just counting.

Everything here takes `rows` - the output of app._rank_rows(): a list of
{"job": <reel dict>, "metrics": <compute_metrics + project annotations>} - and
returns JSON-able dicts. No LLM, no DB, no request context, so the same
functions serve the Analysis page, the formula run, the agent tools and the MCP
server, and they can be unit-tested with hand-made rows.

Why medians and lifts: a lever's value is "how did reels with this tag do,
relative to the project". Medians survive the one 500x outlier every library
has; lift = value median / project median reads the same in a 40k-follower
library and a 4M one. Confidence is purely a function of n - a lift built on
three reels is a hint, on twelve it is a finding.
"""

from statistics import median

import taxonomy

MIN_N = 3
LIFT_POSITIVE = 1.25
LIFT_NEGATIVE = 0.8
CONTRAST_RATIO = 3.0


def _num(value):
    return value if isinstance(value, (int, float)) and value == value else None  # NaN-safe


def _median(values):
    values = [v for v in values if v is not None]
    return round(median(values), 3) if values else None


def _lift(value, base):
    if value is None or not base:
        return None
    return round(value / base, 2)


def confidence_for_n(n):
    if n >= 10:
        return "high"
    if n >= 5:
        return "medium"
    return "low"


def carded(rows):
    return [r for r in rows if (r["job"].get("tags") or {}).get("taxonomy_v")]


def project_baseline(rows):
    """Project-wide medians the lifts are measured against."""
    ms = [r["metrics"] for r in rows]
    return {
        "n": len(rows),
        "n_carded": len(carded(rows)),
        "median_reach": _median(_num(m.get("reach_multiple")) for m in ms),
        "median_views": _median(_num(m.get("view_count")) for m in ms),
        "median_sends_1k": _median(_num(m.get("reshares_per_1k_views")) for m in ms),
        "median_er": _median(_num(m.get("engagement_rate_pct")) for m in ms),
        "breakout_rate": (round(sum(1 for m in ms if m.get("outcome_band") in ("breakout", "above")) / len(ms), 2)
                          if ms else None),
    }


def _stats_for(group, base):
    ms = [r["metrics"] for r in group]
    med_reach = _median(_num(m.get("reach_multiple")) for m in ms)
    med_views = _median(_num(m.get("view_count")) for m in ms)
    med_sends = _median(_num(m.get("reshares_per_1k_views")) for m in ms)
    med_er = _median(_num(m.get("engagement_rate_pct")) for m in ms)
    return {
        "n": len(group),
        "median_reach": med_reach,
        "median_views": med_views,
        "median_sends_1k": med_sends,
        "median_er": med_er,
        "lift_reach": _lift(med_reach, base.get("median_reach")),
        "lift_views": _lift(med_views, base.get("median_views")),
        "lift_sends": _lift(med_sends, base.get("median_sends_1k")),
        "lift_er": _lift(med_er, base.get("median_er")),
        "breakout_rate": round(sum(1 for m in ms if m.get("outcome_band") in ("breakout", "above")) / len(group), 2),
        "confidence": confidence_for_n(len(group)),
        "examples": [r["job"]["id"] for r in sorted(
            group, key=lambda r: -(_num(r["metrics"].get("reach_multiple")) or _num(r["metrics"].get("view_count")) or 0))[:3]],
    }


def lever_table(rows, min_n=MIN_N, dimension=None):
    """One entry per (dimension, value) with n >= min_n."""
    rows = carded(rows)
    base = project_baseline(rows)
    groups = {}
    for r in rows:
        for key, value in taxonomy.flat_tag_pairs(r["job"]["tags"]):
            if dimension and key != dimension:
                continue
            groups.setdefault((key, value), []).append(r)

    labels = {key: dim["label"] for key, dim in taxonomy.DIMENSIONS.items()}
    labels.update({"delivery.wpm_band": "Words per minute", "duration_band": "Duration"})
    meanings = {(key, slug): meaning for key, dim in taxonomy.DIMENSIONS.items() for slug, meaning in dim["values"]}

    table = []
    for (key, value), group in groups.items():
        if len(group) < min_n:
            continue
        entry = {"dimension": key, "dimension_label": labels.get(key, key), "value": value,
                 "meaning": meanings.get((key, value)), **_stats_for(group, base)}
        # The one number to sort by: reach lift when we have it, views lift otherwise.
        entry["lift"] = entry["lift_reach"] if entry["lift_reach"] is not None else entry["lift_views"]
        entry["effect"] = effect_for_lift(entry["lift"], entry["n"])
        table.append(entry)
    table.sort(key=lambda e: (e["dimension"], -(e["lift"] or 0), -e["n"]))
    dropped = sum(1 for g in groups.values() if len(g) < min_n)
    return {"baseline": base, "levers": table, "min_n": min_n, "dropped_under_min_n": dropped}


def effect_for_lift(lift, n):
    if lift is None or n < 2:
        return "neutral"
    if lift >= LIFT_POSITIVE:
        return "positive"
    if lift <= LIFT_NEGATIVE:
        return "negative"
    return "neutral"


_STOPWORDS = {"and", "the", "for", "with", "from", "about", "your", "you", "how", "tips", "tip",
              "content", "video", "reel", "reels", "into", "that", "this", "what", "why", "who"}


def _niche_tokens(tags):
    niche = ((tags or {}).get("topic.niche") or "").lower()
    toks = {t for t in "".join(c if c.isalnum() else " " for c in niche).split()
            if len(t) > 2 and t not in _STOPWORDS}
    toks |= {str(t).lower() for t in (tags or {}).get("topic.tags") or [] if str(t).lower() not in _STOPWORDS}
    return toks


def _tag_diff(a, b):
    """Single-value dimensions where the two reels differ, plus device/multi diffs."""
    diff = []
    for key, dim in taxonomy.DIMENSIONS.items():
        va, vb = (a or {}).get(key), (b or {}).get(key)
        if dim["multi"]:
            sa, sb = set(va or []), set(vb or [])
            if sa != sb:
                diff.append({"dimension": key, "a": sorted(sa), "b": sorted(sb)})
        elif va != vb and (va or vb):
            diff.append({"dimension": key, "a": va, "b": vb})
    for key in taxonomy.DERIVED:
        va, vb = (a or {}).get(key), (b or {}).get(key)
        if va != vb and (va or vb):
            diff.append({"dimension": key, "a": va, "b": vb})
    return diff


def contrast_pairs(rows, limit=20, ratio=CONTRAST_RATIO, dimension=None):
    """Pairs of carded reels that share a hook type or a topic but landed far
    apart on reach. Where formulas come from: same idea, different outcome, and
    a tag diff that says what else was different."""
    rows = [r for r in carded(rows) if _num(r["metrics"].get("reach_multiple"))]
    pairs = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            ta, tb = a["job"]["tags"], b["job"]["tags"]
            shared = []
            if ta.get("hook.type") and ta.get("hook.type") == tb.get("hook.type"):
                shared.append(f"hook.type={ta['hook.type']}")
            if ta.get("structure.type") and ta.get("structure.type") == tb.get("structure.type"):
                shared.append(f"structure.type={ta['structure.type']}")
            overlap = _niche_tokens(ta) & _niche_tokens(tb)
            if len(overlap) >= 2:
                shared.append("topic: " + ", ".join(sorted(overlap)[:3]))
            if not shared:
                continue
            if dimension and not any(s.startswith(dimension) for s in shared):
                continue
            ra, rb = a["metrics"]["reach_multiple"], b["metrics"]["reach_multiple"]
            hi, lo = (a, b) if ra >= rb else (b, a)
            r = (max(ra, rb) / min(ra, rb)) if min(ra, rb) > 0 else None
            if not r or r < ratio:
                continue
            pairs.append({
                "shared": shared,
                "ratio": round(r, 1),
                "high": _compact(hi),
                "low": _compact(lo),
                "diff": _tag_diff(hi["job"]["tags"], lo["job"]["tags"]),
            })
    pairs.sort(key=lambda p: -p["ratio"])
    return pairs[:limit]


def _compact(r):
    job, m = r["job"], r["metrics"]
    return {
        "id": job["id"],
        "creator": m.get("creator"),
        "views": m.get("view_count"),
        "reach_multiple": m.get("reach_multiple"),
        "sends_1k": m.get("reshares_per_1k_views"),
        "outcome_band": m.get("outcome_band"),
        "hook": ((job.get("analysis") or {}).get("hook") or "")[:160],
        "one_line": ((job.get("diagnosis") or {}).get("one_line") or None),
    }


def own_baseline(rows):
    """The user's own reels: medians, best/worst, and a voice profile derived
    from their cards. Empty when the project has no own reels."""
    own = [r for r in rows if (r["job"].get("source") == "own")]
    if not own:
        return {"n": 0}
    ms = [r["metrics"] for r in own]
    by_reach = sorted(own, key=lambda r: -(_num(r["metrics"].get("reach_multiple")) or _num(r["metrics"].get("view_count")) or 0))
    tagged = [r["job"]["tags"] for r in own if (r["job"].get("tags") or {}).get("taxonomy_v")]

    def mode(key, multi=False):
        counts = {}
        for t in tagged:
            vals = t.get(key) or []
            vals = vals if multi else ([vals] if vals else [])
            for v in vals:
                counts[v] = counts.get(v, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])[:4]

    return {
        "n": len(own),
        "n_carded": len(tagged),
        "median_reach": _median(_num(m.get("reach_multiple")) for m in ms),
        "median_views": _median(_num(m.get("view_count")) for m in ms),
        "median_sends_1k": _median(_num(m.get("reshares_per_1k_views")) for m in ms),
        "median_er": _median(_num(m.get("engagement_rate_pct")) for m in ms),
        "best": [_compact(r) for r in by_reach[:3]],
        "worst": [_compact(r) for r in by_reach[-3:][::-1]] if len(by_reach) > 3 else [],
        "voice": {
            "median_wpm": _median(_num(m.get("wpm")) for m in ms),
            "median_duration_sec": _median(_num(m.get("duration_sec")) for m in ms),
            "median_you_density": _median(_num(m.get("you_density")) for m in ms),
            "hook_types": mode("hook.type"),
            "hook_devices": mode("hook.devices", multi=True),
            "structures": mode("structure.type"),
            "cta_types": mode("cta.type"),
            "energy": mode("delivery.energy"),
            "formats": mode("format.type"),
            "captions": mode("format.captions"),
        },
    }


def formula_effect(evidence_reel_ids, rows):
    """lift + effect for a formula from the reels cited as its evidence, measured
    against the project. Reach when available, views otherwise; None when the
    evidence has no numbers."""
    by_id = {r["job"]["id"]: r["metrics"] for r in rows}
    ms = [by_id[i] for i in evidence_reel_ids if i in by_id]
    base = project_baseline(rows)
    med_reach = _median(_num(m.get("reach_multiple")) for m in ms)
    med_views = _median(_num(m.get("view_count")) for m in ms)
    lift = _lift(med_reach, base.get("median_reach"))
    basis = "reach_multiple"
    if lift is None:
        lift = _lift(med_views, base.get("median_views"))
        basis = "views"
    n = sum(1 for m in ms if (m.get("reach_multiple") if basis == "reach_multiple" else m.get("view_count")) is not None)
    return {"lift": lift, "effect": effect_for_lift(lift, n), "basis": basis if lift is not None else None, "n": n}


def formula_confidence(times_seen, evidence_count, lift=None):
    """Re-confirmation across runs, citation across reels, and - when the
    effect is measured - how far from neutral it sits. Data-derived, never
    self-reported."""
    score = min(times_seen or 0, 4) + min(evidence_count or 0, 4)
    if lift is not None and evidence_count >= 3 and (lift >= 1.5 or lift <= 0.67):
        score += 1
    if score >= 6:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def clean_levers(raw):
    """Validate a model-declared lever set against the taxonomy: unknown
    dimensions/slugs dropped, multi dims kept as lists."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, value in raw.items():
        if key in taxonomy.DIMENSIONS:
            valid = {slug for slug, _ in taxonomy.DIMENSIONS[key]["values"]}
            if taxonomy.DIMENSIONS[key]["multi"]:
                vals = value if isinstance(value, list) else [value]
                vals = [str(v).strip().lower() for v in vals if isinstance(v, (str, int))]
                vals = [v for v in vals if v in valid]
                if vals:
                    out[key] = vals
            elif isinstance(value, str) and value.strip().lower() in valid:
                out[key] = value.strip().lower()
        elif key in taxonomy.DERIVED and isinstance(value, str):
            out[key] = value.strip()
    return out or None


def lever_context_for_prompt(table, pairs, dimensions=None, max_levers=24, max_pairs=6):
    """Compact text for the formula prompt: the strongest lever stats (optionally
    restricted to some dimensions) and a few contrast pairs."""
    levers = table.get("levers") or []
    if dimensions:
        levers = [e for e in levers if e["dimension"] in dimensions]
    levers = sorted(levers, key=lambda e: -abs((e["lift"] or 1) - 1))[:max_levers]
    base = table.get("baseline") or {}
    lines = [f"Project baseline over {base.get('n_carded')} tagged reels: median reach {base.get('median_reach')}x, "
             f"median views {base.get('median_views')}, median sends/1k {base.get('median_sends_1k')}, "
             f"median engagement {base.get('median_er')}%."]
    for e in levers:
        lines.append(f"- {e['dimension']}={e['value']}: n={e['n']}, lift(reach)={e['lift_reach']}, lift(sends)={e['lift_sends']}, "
                     f"breakout_rate={e['breakout_rate']}, confidence={e['confidence']}, examples={e['examples']}")
    if pairs:
        lines.append("Contrast pairs (same idea, very different reach - the tag differences are the leads):")
        for p in pairs[:max_pairs]:
            diffs = "; ".join(f"{d['dimension']}: {d['a']} vs {d['b']}" for d in p["diff"][:5])
            lines.append(f"- {p['ratio']}x apart, shared {p['shared']}: {p['high']['id']} ({p['high']['reach_multiple']}x) "
                         f"vs {p['low']['id']} ({p['low']['reach_multiple']}x). Differences: {diffs or 'none in tags'}")
    return "\n".join(lines)


# ------------------------------------------------------------ shape norms ----
# "How long is a hook here?" is a question the lever table can't answer - it
# counts tags, not words and seconds. shape_profile measures the cards'
# hook / promise / body / CTA in words and seconds and reports medians and the
# middle half (p25-p75) for the whole library, the top quartile by reach and the
# creator's own reels. script_shape measures a pasted draft the same way, and
# shape_check lines the two up so a rating can say "your hook is 31 words; the
# top quartile here opens in 9-14". Pure functions over rows / text.

import re as _re

_SENT_RE = _re.compile(r"[.!?]+")
_YOU_RE = _re.compile(r"\b(you|your|you're|yours|yourself)\b", _re.I)
_NUM_RE = _re.compile(r"\b\d[\d,.]*\b")
_CTA_WORDS_RE = _re.compile(r"\b(share|send|follow|comment|save|link|bio|dm|tag|subscribe|drop|let me know|tell me)\b", _re.I)
_LABEL_RE = _re.compile(r"^\s*(hook|promise|validation|body|beats?|cta|call to action|caption|on[- ]screen text|levers|why|expected signals|duration target|sources|formulas?)\s*[:\-]\s*(.*)$", _re.I)
_SHOWN_RE = _re.compile(r"\s*\|\s*shown\s*:.*$", _re.I)
_SAID_RE = _re.compile(r"^\s*(?:\d+[.)]\s*)?said\s*:\s*", _re.I)
_SPOKEN_LABELS = ("hook", "promise", "cta", "call to action")
_BODY_LABELS = ("validation", "body", "beat", "beats")

SHAPE_METRICS = [
    # key, label, unit
    ("hook_words", "hook length", "words"),
    ("hook_seconds", "hook length", "sec"),
    ("promise_end_seconds", "hook+promise end", "sec"),
    ("total_words", "script length", "words"),
    ("duration_sec", "duration", "sec"),
    ("wpm", "pace", "wpm"),
    ("avg_sentence_len", "sentence length", "words"),
    ("sentence_count", "sentences", "n"),
    ("cta_words", "CTA length", "words"),
    ("cta_position_pct", "CTA position", "% in"),
    ("you_density", "you-density", "% of words"),
    ("question_count", "questions", "n"),
    ("number_mentions", "numbers said", "n"),
    ("seconds_to_first_payoff", "first payoff", "sec"),
    ("rehook_count", "re-hooks", "n"),
]


def _quartiles(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    n = len(values)

    def q(p):
        if n == 1:
            return values[0]
        pos = (n - 1) * p
        lo, hi = int(pos), min(int(pos) + 1, n - 1)
        return values[lo] + (values[hi] - values[lo]) * (pos - lo)
    return {"n": n, "median": round(median(values), 1), "p25": round(q(0.25), 1), "p75": round(q(0.75), 1)}


def _seconds_at_word(segments, word_index, wpm=None):
    """Time at which the word_index-th spoken word ends, from transcript
    segments; falls back to words / wpm when there are no segments."""
    seen = 0
    for seg in segments or []:
        n = len((seg.get("text") or "").split())
        if not n:
            continue
        if seen + n >= word_index:
            start, end = seg.get("start") or 0, seg.get("end") or 0
            return round(start + (end - start) * ((word_index - seen) / n), 1)
        seen += n
    if wpm:
        return round(word_index / wpm * 60, 1)
    return None


def reel_shape(job, metrics):
    """One reel's shape in words and seconds. None when the card has no hook."""
    analysis = job.get("analysis") or {}
    hook = (analysis.get("hook") or "").strip()
    if not hook:
        return None
    promise = (analysis.get("promise") or "").strip()
    cta = (analysis.get("cta") or "").strip()
    segments = job.get("segments") or []
    wpm = _num(metrics.get("wpm"))
    hook_words = len(hook.split())
    promise_words = len(promise.split())
    numbers = (job.get("tags") or {}).get("numbers") or {}
    return {
        "hook_words": hook_words,
        "hook_seconds": _seconds_at_word(segments, hook_words, wpm),
        "promise_words": promise_words or None,
        "promise_end_seconds": _seconds_at_word(segments, hook_words + promise_words, wpm) if promise_words else None,
        "total_words": _num(metrics.get("word_count")),
        "duration_sec": _num(metrics.get("duration_sec")),
        "wpm": wpm,
        "avg_sentence_len": _num(metrics.get("avg_sentence_len")),
        "sentence_count": _num(metrics.get("sentence_count")),
        "cta_words": len(cta.split()) if cta else 0,
        "has_cta": bool(cta),
        "cta_position_pct": _num(metrics.get("cta_position_pct")) if cta else None,
        "you_density": _num(metrics.get("you_density")),
        "question_count": _num(metrics.get("question_count")),
        "number_mentions": _num(metrics.get("number_mentions")),
        "seconds_to_first_payoff": _num(numbers.get("hook.seconds_to_first_payoff")),
        "rehook_count": _num(numbers.get("structure.rehook_count")),
    }


def _group_profile(shapes):
    if not shapes:
        return {"n": 0}
    out = {"n": len(shapes)}
    for key, _label, _unit in SHAPE_METRICS:
        q = _quartiles([s.get(key) for s in shapes])
        if q:
            out[key] = q
    out["cta_share"] = round(sum(1 for s in shapes if s.get("has_cta")) / len(shapes), 2)
    return out


def shape_profile(rows, top_fraction=0.25):
    """Shape norms for a library: {"all", "top", "own"} groups, each with n and
    per-metric {median, p25, p75}. "top" is the top quartile by reach multiple
    (views when no reach), the reels a draft should be measured against."""
    measured = []
    for r in rows:
        s = reel_shape(r["job"], r["metrics"])
        if s:
            s["_reach"] = _num(r["metrics"].get("reach_multiple"))
            s["_views"] = _num(r["metrics"].get("view_count"))
            s["_own"] = r["job"].get("source") == "own"
            measured.append(s)
    ranked = sorted([s for s in measured if s["_reach"] is not None], key=lambda s: -s["_reach"])
    basis = "reach_multiple"
    if len(ranked) < 4:
        ranked = sorted([s for s in measured if s["_views"] is not None], key=lambda s: -s["_views"])
        basis = "views"
    k = max(3, int(round(len(ranked) * top_fraction))) if ranked else 0
    top = ranked[:k] if len(ranked) >= 4 else []
    return {
        "n_measured": len(measured),
        "top_basis": basis if top else None,
        "groups": {
            "all": _group_profile(measured),
            "top": _group_profile(top),
            "own": _group_profile([s for s in measured if s["_own"]]),
        },
        "note": ("Words and seconds measured from the cards' hook/promise/cta and the transcript timestamps. "
                 "'top' = top quartile by " + basis + ". Medians with the middle half (p25-p75)."),
    }


def script_shape(text, wpm=150):
    """Measure a pasted draft the way reel_shape measures a card. Understands
    labelled scripts (Hook: / CTA: / Beats with 'Said: ... | Shown: ...') and
    plain prose (hook = first paragraph if short, else first two sentences;
    CTA = a last line with an ask in it)."""
    text = (text or "").strip()
    if not text:
        return None
    labelled = {}
    spoken_lines = []
    current = None
    for raw in text.split("\n"):
        line = raw.rstrip()
        m = _LABEL_RE.match(line)
        if m:
            current = m.group(1).lower()
            rest = m.group(2).strip()
            if current in _SPOKEN_LABELS:
                key = "cta" if current.startswith("c") else current
                if key == "cta" and rest.lower().startswith("none"):
                    labelled["cta"] = ""          # "CTA: none - ends on ..." is a note, not spoken
                    current = "cta_none"
                elif rest:
                    labelled[key] = (labelled.get(key, "") + " " + rest).strip()
                    spoken_lines.append(rest)
            elif current in _BODY_LABELS and rest:
                spoken_lines.append(rest)
            continue   # caption / on-screen / levers / why / ... are not spoken
        if not line.strip():
            continue
        if current in _SPOKEN_LABELS:
            key = "cta" if current.startswith("c") else current
            labelled[key] = (labelled.get(key, "") + " " + line.strip()).strip()
            spoken_lines.append(line.strip())
        elif current is None or current in _BODY_LABELS:
            spoken_lines.append(line.strip())
    spoken = []
    for l in spoken_lines:
        l = _SHOWN_RE.sub("", l)
        l = _SAID_RE.sub("", l)
        l = _re.sub(r"^\s*\d+[.)]\s+", "", l)
        if l.strip():
            spoken.append(l.strip())
    spoken_text = " ".join(spoken)
    total_words = len(spoken_text.split())
    if not total_words:
        return None
    wpm = float(wpm or 150)

    sents = [x.strip() for x in _re.split(r"(?<=[.!?])\s+", spoken_text) if x.strip()]
    hook = labelled.get("hook")
    hook_source = "Hook: label"
    if not hook:
        first_para = next((p.strip() for p in _re.split(r"\n\s*\n", text) if p.strip()), "")
        first_para = _SHOWN_RE.sub("", first_para)
        para_words = len(first_para.split())
        # A short opening paragraph that is NOT the whole script is the hook;
        # otherwise the first sentence (two when the first is very short).
        if first_para and "\n" not in first_para and para_words <= 30 and para_words < total_words:
            hook, hook_source = first_para, "first paragraph"
        elif sents:
            take = 2 if len(sents[0].split()) < 6 and len(sents) > 2 else 1
            hook, hook_source = " ".join(sents[:take]), ("first sentence" if take == 1 else "first two sentences")
        else:
            hook, hook_source = spoken_text, "whole text"
    hook_words = len(hook.split())
    promise = labelled.get("promise") or ""
    promise_words = len(promise.split())

    cta = labelled.get("cta")
    cta_source = "CTA: label"
    if cta is None:
        # The closing ask: the last sentence when it contains an ask word and it
        # is not the hook itself (a one-liner has no separate CTA).
        last = sents[-1] if sents else ""
        if last and len(sents) > 1 and last not in hook and _CTA_WORDS_RE.search(last):
            cta, cta_source = last, "last sentence (has an ask)"
        else:
            cta, cta_source = "", "none found"
    if cta.strip().lower().startswith("none"):
        cta = ""
    cta_words = len(cta.split())
    cta_position_pct = None
    if cta:
        idx = spoken_text.lower().rfind(cta.lower()[:40])
        if idx >= 0:
            cta_position_pct = round(idx / max(len(spoken_text), 1) * 100, 1)

    sentence_count = len([s for s in _SENT_RE.split(spoken_text) if s.strip()])
    return {
        "hook": hook[:300], "hook_source": hook_source,
        "hook_words": hook_words,
        "hook_seconds": round(hook_words / wpm * 60, 1),
        "promise_words": promise_words or None,
        "promise_end_seconds": round((hook_words + promise_words) / wpm * 60, 1) if promise_words else None,
        "total_words": total_words,
        "duration_sec": round(total_words / wpm * 60, 1),
        "wpm": wpm,
        "wpm_assumed": True,
        "avg_sentence_len": round(total_words / sentence_count, 1) if sentence_count else None,
        "sentence_count": sentence_count,
        "cta": cta[:300], "cta_source": cta_source,
        "cta_words": cta_words,
        "has_cta": bool(cta),
        "cta_position_pct": cta_position_pct,
        "you_density": round(len(_YOU_RE.findall(spoken_text)) / total_words * 100, 1),
        "question_count": spoken_text.count("?"),
        "number_mentions": len(_NUM_RE.findall(spoken_text)),
        "spoken_text": spoken_text[:6000],
    }


def shape_check(draft, profile):
    """Line the draft's shape up against the norms. One row per metric with the
    draft value, library / top / own medians, the reference group's middle half
    and a verdict: 'in range' (inside p25-p75 of the top quartile, or of the
    library when top is thin), 'above' / 'below' with % off the median."""
    if not draft or not profile:
        return []
    groups = profile.get("groups") or {}
    allg, topg, owng = groups.get("all") or {}, groups.get("top") or {}, groups.get("own") or {}
    rows = []
    for key, label, unit in SHAPE_METRICS:
        dv = draft.get(key)
        if dv is None or key in ("seconds_to_first_payoff", "rehook_count"):
            continue
        if key == "wpm" and draft.get("wpm_assumed"):
            continue
        use_top = (topg.get("n") or 0) >= 4 and bool(topg.get(key))
        ref = topg.get(key) if use_top else allg.get(key)
        if not ref:
            continue
        lo, hi, med = ref["p25"], ref["p75"], ref["median"]
        if lo <= dv <= hi:
            verdict, delta = "in range", 0
        else:
            verdict = "above" if dv > hi else "below"
            delta = round((dv / med - 1) * 100) if med else None
        rows.append({
            "metric": key, "label": label, "unit": unit, "draft": dv,
            "library_median": (allg.get(key) or {}).get("median"),
            "top_median": (topg.get(key) or {}).get("median") if topg.get(key) else None,
            "own_median": (owng.get(key) or {}).get("median") if owng.get(key) else None,
            "range": [lo, hi], "range_of": "top" if use_top else "all", "n": ref.get("n"),
            "verdict": verdict, "delta_pct_vs_median": delta,
        })
    return rows


def shape_prompt_block(draft, checks, profile, max_rows=12):
    """Plain-text block for a rating / critique prompt: the draft measured, then
    each metric against the norms. Short, numeric, cite-able."""
    if not draft:
        return ""
    groups = (profile or {}).get("groups") or {}
    n_all = (groups.get("all") or {}).get("n", 0)
    n_top = (groups.get("top") or {}).get("n", 0)
    n_own = (groups.get("own") or {}).get("n", 0)
    cta_line = (f"\"{draft.get('cta')[:120]}\" ({draft.get('cta_words')} words, at {draft.get('cta_position_pct')}% in)"
                if draft.get("has_cta") else "none")
    lines = [f"THIS DRAFT, MEASURED (hook = {draft.get('hook_source')}; CTA = {draft.get('cta_source')}; "
             f"seconds assume {int(draft.get('wpm') or 150)} wpm):",
             f"- hook ({draft.get('hook_words')} words, ~{draft.get('hook_seconds')}s): \"{(draft.get('hook') or '')[:140]}\"",
             f"- {draft.get('total_words')} spoken words, ~{draft.get('duration_sec')}s, {draft.get('sentence_count')} sentences, "
             f"avg {draft.get('avg_sentence_len')} words/sentence, you-density {draft.get('you_density')}%, "
             f"{draft.get('question_count')} questions, {draft.get('number_mentions')} numbers",
             f"- CTA: {cta_line}"]
    if checks:
        lines.append(f"SHAPE vs THIS LIBRARY (n={n_all} measured reels; 'top' = top quartile by reach, n={n_top}; "
                     f"own reels n={n_own}). Verdict is against the top group's middle half when it has >=4 reels, "
                     "else the library's:")
        for c in checks[:max_rows]:
            own = f", own {c['own_median']}" if c.get("own_median") is not None else ""
            top = f", top {c['top_median']}" if c.get("top_median") is not None else ""
            if c["verdict"] == "in range":
                tail = ""
            else:
                d = c["delta_pct_vs_median"] or 0
                tail = f" ({'+' if d > 0 else ''}{d}% vs {c['range_of']} median)"
            lines.append(f"- {c['label']}: draft {c['draft']} {c['unit']} | library {c['library_median']}{top}{own} | "
                         f"{c['range_of']} middle half {c['range'][0]}-{c['range'][1]} -> {c['verdict'].upper()}{tail}")
    else:
        lines.append("SHAPE NORMS: not enough measured reels in this library to compare against yet.")
    return "\n".join(lines)


def shape_norms_block(profile, max_metrics=10):
    """The norms alone (no draft) - for drafting prompts and the studio context."""
    groups = (profile or {}).get("groups") or {}
    allg, topg, owng = groups.get("all") or {}, groups.get("top") or {}, groups.get("own") or {}
    if not allg.get("n"):
        return ""
    lines = [f"SHAPE NORMS (measured from the cards; median [p25-p75]; library n={allg.get('n')}, "
             f"top quartile by reach n={topg.get('n', 0)}, own n={owng.get('n', 0)}):"]
    for key, label, unit in SHAPE_METRICS[:max_metrics]:
        a = allg.get(key)
        if not a:
            continue
        t, o = topg.get(key), owng.get(key)
        line = f"- {label} ({unit}): library {a['median']} [{a['p25']}-{a['p75']}]"
        if t:
            line += f"; top {t['median']} [{t['p25']}-{t['p75']}]"
        if o:
            line += f"; own {o['median']} [{o['p25']}-{o['p75']}]"
        lines.append(line)
    if allg.get("cta_share") is not None:
        line = f"- reels with a CTA: library {int(allg['cta_share'] * 100)}%"
        if topg.get("cta_share") is not None:
            line += f"; top {int(topg['cta_share'] * 100)}%"
        if owng.get("cta_share") is not None:
            line += f"; own {int(owng['cta_share'] * 100)}%"
        lines.append(line)
    return "\n".join(lines)
