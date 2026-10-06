"""Two ways to build every clip, so they can be judged against each other.

continuous  one unbroken stretch of the video, allowed to open earlier on the
            line that tells a cold viewer what is going on ("This is Isaac from
            earlier. Are you ready to see your new home?").
stitched    the payoff plus whatever context a cold viewer needs, pulled from
            wherever it sits in the video — the premise, the "before", the
            problem — with each jump in time marked on screen.

This is how the best-performing Shorts cut from long videos are built: the
most-viewed Short of a MrBeast project video opens on the premise in its first
three seconds and jumps from the empty field (0:00) to the progress (7:08) to
the finished village (17:09). A funny moment, though, carries itself; nothing
is gained by stitching it, so the model is told to leave those alone.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import highlights, smartstitch, toolio
from .config import CLAUDE_MODEL

ROLES = ["premise", "before", "setup", "callback", "progress", "payoff", "after"]
FUNNY_MAX = 35.0            # a stitched funny/hype moment stays this tight

_QUOTE = {"type": "string", "description": "Its exact words, copied from the transcript. For a part longer "
                                          "than ~12 s: its first 8-12 words … its last 8-12 words."}
TEASER_SCHEMA = {
    "type": "object",
    "description": "Optional flash-forward: the single strongest 1.5-3 s inside the payoff (the reaction, the "
                   "number, the punchline's first words), shown first, before the story. Omit it, or set "
                   "spoils_surprise, when knowing it up front would spoil a surprise.",
    "properties": {
        "start": {"type": "number"}, "end": {"type": "number"},
        "quote": _QUOTE,
        "label": {"type": "string", "enum": ["HOW IT STARTED", "BUT FIRST", ""],
                  "description": "Shown when the story starts after the teaser."},
        "spoils_surprise": {"type": "boolean"},
        "why": {"type": "string", "description": "One line: why this is the moment to tease."},
    },
    "required": ["start", "end", "quote", "why"],
}
INSERTS_SCHEMA = {
    "type": "array",
    "description": "Optional, max 3. callback: an EARLIER line (1.5-4 s) that a line in this clip refers back to "
                   "('remember I said I'd never…'). reaction: a real laugh or shout (0.7-2 s) that came right "
                   "after a line in this clip but isn't in it.",
    "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["callback", "reaction"]},
            "at": {"type": "number", "description": "Absolute seconds: when the line it goes with is said in the clip."},
            "refers_to": {"type": "string", "description": "callback: the exact words of the line in the clip that refers back."},
            "reacts_to": {"type": "string", "description": "reaction: the exact words of the line it reacts to."},
            "start": {"type": "number"}, "end": {"type": "number"},
            "quote": {"type": "string", "description": "Its exact words ('' for a laugh with no words)."},
            "sound": {"type": "string", "enum": ["laugh", "shout", "silent"]},
            "why": {"type": "string"},
        },
        "required": ["kind", "at", "start", "end", "quote", "why"],
    },
}

STRUCTURE_TOOL = {
    "name": "submit_structures",
    "description": "For each candidate: the best continuous version and, where it genuinely helps, a stitched version.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "continuous": {
                            "type": "object",
                            "properties": {
                                "start": {"type": "number", "description": "Absolute seconds. Open on the line that sets the moment up for a cold viewer."},
                                "end": {"type": "number", "description": "Absolute seconds, just after the payoff."},
                                "hook": {"type": "string", "description": "Max 8 words, understandable with zero context."},
                            },
                            "required": ["start", "end", "hook"],
                        },
                        "stitched": {
                            "type": "object",
                            "description": "Omit when nothing elsewhere in the video adds to this one — and for funny and hype moments unless a setup or earlier mention elsewhere makes the punchline land harder.",
                            "properties": {
                                "parts": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "start": {"type": "number"},
                                            "end": {"type": "number"},
                                            "role": {"type": "string", "enum": ROLES},
                                            "label": {"type": "string", "description": "1-3 words on screen when time jumps into this part ('BEFORE', 'AFTER', 'LATER'). A number only when it is said in the video. Empty when no jump needs marking."},
                                            "quote": _QUOTE,
                                        },
                                        "required": ["start", "end", "role", "label", "quote"],
                                    },
                                },
                                "hook": {"type": "string", "description": "Max 8 words, understandable with zero context."},
                            },
                            "required": ["parts", "hook"],
                        },
                        "why": {"type": "string", "description": "One line: what the stitched version adds, or why there is none."},
                        "teaser": TEASER_SCHEMA,
                        "inserts": INSERTS_SCHEMA,
                    },
                    "required": ["id", "continuous", "why"],
                },
            }
        },
        "required": ["clips"],
    },
}

SYSTEM = """You build short-form clips from a long video, two ways, so the two can be \
judged against each other and tested on real viewers.

CONTINUOUS — one unbroken stretch of the video. You may move its start earlier (up to \
25 seconds) so it opens on the line that tells a cold viewer what is happening \
("This is Isaac from earlier. Are you ready to see your new home?"), and you may tighten its \
end onto the payoff. 12-75 seconds. Funny, hype and reaction moments stay tight: 15-45 seconds, \
opening on the setup line closest to the punchline and ending on the laugh.

STITCHED — the payoff plus the context a cold viewer needs, taken from wherever it is in the \
video: the premise ("This field will be a city by the end of this video"), the before (the \
family sleeping in one room), the problem ("they need water"), who this person is — then the payoff.
- Build one whenever anything elsewhere in the video could make the payoff hit harder. A judge compares the two versions afterwards, so \
you do not need to be sure it wins; skip only when nothing elsewhere adds to this moment.
- 2-4 parts in story order. Each part is a complete thought: it starts on the first word of a \
sentence and ends on the last word of one. No part shorter than 3 seconds.
- Context parts are short, 3-12 seconds each. The payoff part carries the moment itself: trim \
it to its strongest stretch rather than keeping all of it.
- 20-60 seconds in total. Never pad: a stitched version that only adds length is worse.
- label: 1-3 words shown on screen when time jumps into a part ("BEFORE", "5 MONTHS LATER", \
"AFTER", "THE REVEAL"). Leave it empty when no jump needs marking.
- quote: every part's exact words, copied from the transcript (a long part: its first words … its \
last words). Parts whose quote doesn't match what is said there are thrown out.
- Funny and hype moments usually carry themselves. Stitch one ONLY when its setup, an earlier \
mention or a running joke sits elsewhere in the video and makes the punchline land harder: include \
that part with role "setup" or "callback", keep the whole thing under 35 seconds, leave the \
punchline part untouched and end on the laugh. Otherwise give no stitched version.

TEASER (optional, for either version) — the single strongest 1.5-3 seconds inside the payoff (the \
reaction, the number, the punchline's first words), shown first so the viewer knows a payoff is \
coming; the story then plays in full. Whole sentences only. Leave it out (or set spoils_surprise) \
when knowing it up front would spoil a surprise.

INSERTS (optional, max 3) — callback: an earlier line, 1.5-4 seconds, that a line in the clip refers \
back to ("remember I said I'd never…"). reaction: a real laugh or shout, 0.7-2 seconds, that came \
right after a line in the clip but isn't in it. "at" is when the line it goes with is said. Only \
what is really there; none is fine.

Hooks: each version gets its own, max 8 words, understandable by someone who has never seen \
the video — who or what, not a riddle.

The best-performing Shorts of this kind open on the premise in their first three seconds, \
jump across the video from the before to the after, and mark each jump on screen."""


def _condensed(segments: List[Dict[str, Any]], max_chars: int = 90000) -> str:
    """The whole transcript with timestamps, merged into bigger chunks if it is long."""
    def render(chunk_seconds: float) -> str:
        lines, buf, t0 = [], [], None
        for seg in segments:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            if t0 is None:
                t0 = seg["start"]
            buf.append(text)
            if seg["end"] - t0 >= chunk_seconds:
                lines.append(f"[{t0:.1f}] {' '.join(buf)}")
                buf, t0 = [], None
        if buf:
            lines.append(f"[{t0:.1f}] {' '.join(buf)}")
        return "\n".join(lines)

    for chunk in (0.0, 8.0, 15.0, 30.0, 60.0):
        text = render(chunk)
        if len(text) <= max_chars:
            return text
    return text[:max_chars]


def _span_text(words: List[Dict[str, Any]], start: float, end: float) -> str:
    return " ".join(w["w"] for w in words if start - 0.05 <= w["start"] < end - 0.05)


def _clean_part(part: Dict[str, Any], words, duration: float) -> Optional[Dict[str, Any]]:
    try:
        s, e = float(part["start"]), float(part["end"])
    except (KeyError, TypeError, ValueError):
        return None
    s, e = max(0.0, s), min(duration, e)
    if e - s < 2.0:
        return None
    s, e = highlights.clean_bounds(s, e, words, min_len=2.0)
    role = part.get("role") if part.get("role") in ROLES else "setup"
    label = " ".join((part.get("label") or "").upper().split()[:3])[:24]
    out = {"start": round(s, 2), "end": round(e, 2), "role": role, "label": label}
    if "quote" in part:
        out["quote"] = highlights._text(part.get("quote"))[:400]
    return out


def stitch_problem(parts: List[Dict[str, Any]], continuous: Dict[str, Any], max_len: float = 90.0) -> str:
    """Why a proposed stitch can't be used, or "" when it can."""
    if len(parts) < 2:
        return f"needs 2+ usable parts, has {len(parts)}"
    total = sum(p["end"] - p["start"] for p in parts)
    if not 12.0 <= total <= max_len:
        return f"{total:.0f}s long (12-{max_len:.0f}s allowed)"
    ordered = sorted(parts, key=lambda p: p["start"])
    if any(b["start"] < a["end"] - 0.2 for a, b in zip(ordered, ordered[1:])):
        return "parts overlap each other"
    # Inside the continuous span it is only a stitch if it skips a real chunk.
    inside = all(continuous["start"] - 1 <= p["start"] and p["end"] <= continuous["end"] + 1 for p in parts)
    if inside:
        skipped = (continuous["end"] - continuous["start"]) - total
        if skipped < 8.0:
            return f"stays inside the continuous span and only skips {skipped:.0f}s"
    return ""


FIT_SECONDS = 75.0      # a stitched version longer than this loses its middle context first


def fit(parts: List[Dict[str, Any]], limit: float = FIT_SECONDS) -> List[Dict[str, Any]]:
    """Claude sometimes keeps every beat and overshoots. Drop middle context
    parts — the longest first, never the opening or the payoff — until the
    whole thing fits; a dropped part's label moves to the part after it, so
    the jump in time is still marked."""
    parts = [dict(p) for p in parts]
    while sum(p["end"] - p["start"] for p in parts) > limit and len(parts) > 2:
        middle = [i for i in range(1, len(parts) - 1) if parts[i].get("role") != "payoff"]
        if not middle:
            break
        i = max(middle, key=lambda k: parts[k]["end"] - parts[k]["start"])
        gone = parts.pop(i)
        if gone.get("label") and not parts[i].get("label"):
            parts[i]["label"] = gone["label"]
    return parts


def _valid_stitch(parts: List[Dict[str, Any]], continuous: Dict[str, Any]) -> bool:
    return not stitch_problem(parts, continuous)


def plan(title: str, clips: List[Dict[str, Any]], segments: List[Dict[str, Any]],
         words: List[Dict[str, Any]], duration: float, headline: str = "",
         max_len: float = 90.0, loud: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    """Attach clip["variants"] = {"continuous": {...}, "stitched": {...} | None} to each clip.

    `max_len` is this run's ceiling (where the clips are going): neither
    version may grow past it while pulling in setup. `loud` = stretches that
    are loud with nobody talking (smartstitch.loud_gaps), hints for reactions.
    Claude's teaser and insert ideas are kept raw on the clip ("teaser_raw",
    "inserts_raw") for smartstitch to check."""
    for clip in clips:                                   # a safe default, whatever happens below
        clip["variants"] = {"continuous": _as_variant(clip["start"], clip["end"], clip.get("hook", "")),
                            "stitched": None}
    if not clips:
        return clips
    try:
        client = highlights._client()
    except RuntimeError:
        return clips

    lines = []
    for i, c in enumerate(clips):
        lines.append(
            f"[{i}] type={c.get('type', 'story')} {c['start']:.1f}-{c['end']:.1f}s "
            f"({c['end'] - c['start']:.0f}s) \"{c.get('title', '')}\" hook: \"{c.get('hook', '')}\""
            + (f" | a cold viewer is missing: {c['context_note']}" if c.get("context_note") else "")
            + (f" | {smartstitch.loud_text(loud, c['start'], c['end'])}"
               if loud and smartstitch.loud_text(loud, c["start"], c["end"]) else "")
        )
    prompt = (f"Video: {title or 'Untitled'}\n"
              + (f"Premise, as one line: {headline}\n" if headline else "")
              + f"Video length: {duration:.0f}s\n\nFULL TRANSCRIPT (absolute seconds):\n"
              + _condensed(segments)
              + "\n\nCANDIDATES:\n" + "\n".join(lines)
              + "\n\nFor every candidate give the continuous version, and the stitched version where it helps."
              + (f" Neither version may run longer than {max_len:.0f} seconds in total — that is the most "
                 "the platforms these clips are for will hold." if max_len < 90 else ""))
    try:
        replies = toolio.ask(
            client, "clips",
            model=CLAUDE_MODEL, max_tokens=8000, system=SYSTEM,
            tools=[STRUCTURE_TOOL], tool_choice={"type": "tool", "name": "submit_structures"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        highlights.LAST_ERROR = str(exc)[:300]
        for clip in clips:
            clip["stitch_problem"] = f"structure call failed: {exc}"[:200]
        return clips
    if not replies:
        for clip in clips:
            clip["stitch_problem"] = "structure reply had nothing usable"

    for item in replies:
        idx = highlights._int(item.get("id"), -1)
        if not 0 <= idx < len(clips):
            continue
        try:
            _apply(clips[idx], item, words, duration, max_len)
        except Exception as exc:        # one odd reply must not cost the other clips
            clips[idx]["stitch_problem"] = f"unreadable reply: {exc}"[:200]
    return clips


def _apply(clip: Dict[str, Any], item: Dict[str, Any], words: List[Dict[str, Any]], duration: float,
           max_len: float = 90.0) -> None:
    cont = toolio.as_dict(item.get("continuous"))
    cs = highlights._num(cont.get("start"), clip["start"])
    ce = highlights._num(cont.get("end"), clip["end"])
    cs, ce = max(0.0, cs), min(duration, ce)
    # never let the "continuous" rewrite wander off to a different moment
    longest = max(max_len, clip["end"] - clip["start"])          # never shorter than the pick itself
    if ce - cs < highlights.MIN_LEN or ce < clip["start"] or cs > clip["end"] or ce - cs > min(90, longest):
        cs, ce = clip["start"], clip["end"]
    cs, ce = highlights.clean_bounds(cs, ce, words, min_len=min(highlights.MIN_LEN, ce - cs))
    hook = highlights._text(cont.get("hook")) or clip.get("hook", "")
    clip["variants"]["continuous"] = _as_variant(cs, ce, hook[:70])
    st = toolio.as_dict(item.get("stitched"))
    raw_parts = [p for p in toolio.as_list(st.get("parts")) if isinstance(p, dict)]
    clip["structure_raw"] = item
    clip["teaser_raw"] = toolio.as_dict(item.get("teaser")) or None
    clip["inserts_raw"] = [i for i in toolio.as_list(item.get("inserts")) if isinstance(i, dict)][:6]
    if raw_parts:
        parts = clean_stitch(raw_parts, words, duration, limit=min(FIT_SECONDS, max_len))
        problem = stitch_problem(parts, clip["variants"]["continuous"], max_len=min(90.0, max_len))
        if not problem:
            # The honesty rules (smart stitch section 0): quotes verified, whole
            # sentences, true time labels — a part that fails is dropped.
            parts, dropped = honest_parts(parts, words)
            problem = stitch_problem(parts, clip["variants"]["continuous"], max_len=min(90.0, max_len))
            if dropped and problem:
                problem = f"{problem} after dropping parts: {'; '.join(dropped)}"[:200]
        if not problem and clip.get("type") in ("funny", "hype"):
            problem = funny_problem(parts, clip["variants"]["continuous"])
        clip["stitch_problem"] = problem
        if not problem:
            clip["variants"]["stitched"] = {"parts": parts, "hook": highlights._text(st.get("hook"))[:70]}
    else:
        clip["stitch_problem"] = "none proposed"
    clip["structure_note"] = highlights._text(item.get("why"))[:200]


def honest_parts(parts: List[Dict[str, Any]], words: List[Dict[str, Any]]) -> tuple:
    """Rule 0 on a proposed stitch: each part's quote must match what is said
    there, each part is whole sentences, and a time label is only kept when
    it is true. Returns (parts that pass, why the others didn't)."""
    from . import stitchrules
    kept: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for p in parts:
        ok, why = stitchrules.check_quote(p.get("quote", ""), words, p["start"], p["end"])
        if ok:
            ok, why = stitchrules.check_sentences(p["start"], p["end"], words)
        if not ok:
            dropped.append(f"{p['start']:.0f}-{p['end']:.0f}s: {why}")
            continue
        kept.append(dict(p))
    for i, p in enumerate(kept):
        if not p.get("label"):
            continue
        if i > 0:
            prev = kept[i - 1]
            said = _span_text(words, prev["start"], prev["end"]) + " " + _span_text(words, p["start"], p["end"])
            p["label"] = stitchrules.true_label(p["label"], p["start"] - prev["end"], said)
        else:
            p["label"] = stitchrules.true_label(p["label"], None, _span_text(words, p["start"], p["end"]))
    return kept, dropped


def funny_problem(parts: List[Dict[str, Any]], continuous: Dict[str, Any]) -> str:
    """A funny or hype moment may only be stitched when a setup or callback
    elsewhere makes the punchline land harder: tight, the punchline left
    whole, ending on the laugh. Why not, or ""."""
    payoff = parts[-1]
    if payoff.get("role") != "payoff":
        return "a funny moment has to end on its punchline"
    if not any(p.get("role") in ("setup", "callback") and p["end"] <= payoff["start"] + 0.5 for p in parts[:-1]):
        return "a funny moment is only stitched when a setup or callback from elsewhere comes first"
    total = sum(p["end"] - p["start"] for p in parts)
    if total > FUNNY_MAX:
        return f"{total:.0f}s — a stitched funny moment stays under {FUNNY_MAX:.0f}s"
    if payoff["end"] < continuous["end"] - 1.0:
        return "the punchline part stops before the laugh"
    return ""


def clean_stitch(raw_parts: List[Dict[str, Any]], words: List[Dict[str, Any]],
                 duration: float, limit: float = FIT_SECONDS) -> List[Dict[str, Any]]:
    """Proposed parts onto sentence edges, too-short ones dropped (their label
    moving on to the next jump), then fitted to length."""
    parts: List[Dict[str, Any]] = []
    carry = ""
    for raw_part in raw_parts:
        if not isinstance(raw_part, dict):
            continue
        part = _clean_part(raw_part, words, duration)
        if part is None:
            carry = carry or " ".join((raw_part.get("label") or "").upper().split()[:3])[:24]
            continue
        if carry and not part["label"]:
            part["label"] = carry
        carry = ""
        parts.append(part)
    return fit(parts, limit)


def _as_variant(start: float, end: float, hook: str) -> Dict[str, Any]:
    return {"parts": [{"start": round(start, 2), "end": round(end, 2), "role": "payoff", "label": ""}],
            "hook": hook, "start": round(start, 2), "end": round(end, 2)}


def viewer_view(variant: Dict[str, Any], words: List[Dict[str, Any]], headline: str) -> str:
    """What a viewer actually gets from a version: on-screen text and what is said, in order."""
    lines = []
    total = sum(p["end"] - p["start"] for p in variant["parts"])
    lines.append(f"Length: {total:.0f}s" + (f". Headline bar on screen the whole time: \"{headline}\"" if headline else ""))
    if variant.get("hook"):
        lines.append(f"Hook text on screen, first 2 seconds: \"{variant['hook']}\"")
    t = 0.0
    for n, p in enumerate(variant["parts"], 1):
        span = p["end"] - p["start"]
        tag = f" — on-screen label \"{p['label']}\"" if p.get("label") else ""
        if p.get("role") == "teaser":
            tag = " — a flash-forward: this exact moment plays again later in the clip, then a quick rewind"
        lines.append(f"Part {n} [{t:.0f}-{t + span:.0f}s]{tag}: \"{_span_text(words, p['start'], p['end'])}\"")
        t += span
    return "\n".join(lines)


def choose(clip: Dict[str, Any], variant: str) -> Dict[str, Any]:
    """Make one variant the clip: parts, hook, and a display span."""
    v = clip["variants"].get(variant) or clip["variants"]["continuous"]
    clip["variant"] = variant if clip["variants"].get(variant) else "continuous"
    clip["parts"] = v["parts"]
    clip["hook"] = v.get("hook") or clip.get("hook", "")
    clip["start"] = min(p["start"] for p in v["parts"])
    clip["end"] = max(p["end"] for p in v["parts"])
    return clip


def to_json(clip: Dict[str, Any]) -> str:
    return json.dumps(clip.get("parts", []))
