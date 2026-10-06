"""Claude picks the moments worth clipping and scores them for virality."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from . import toolio
from .config import ANTHROPIC_API_KEY, CLAUDE_MODEL

BLOCK_SECONDS = 1500          # ~25 min of transcript per Claude call

# What kind of moment a clip is decides how it is built and edited:
#   funny, hype      self-contained: start on the setup, keep it tight, punchy motion
#   reaction, reveal need the line that sets them up inside the clip
#   story, emotional need the "before" — often stitched from another part
#   info, hot_take   need the claim stated plainly up front, calm motion
CLIP_TYPES = ["funny", "hype", "reaction", "reveal", "story", "emotional", "info", "hot_take"]
PUNCHY_TYPES = {"funny", "hype", "reaction"}
MIN_LEN, MAX_LEN = 8.0, 90.0  # a short-form clip lives inside this range

CLIP_TOOL = {
    "name": "submit_clips",
    "description": "Return the moments from this video that should become short-form clips.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number", "description": "Start in seconds from the beginning of the source video. Begin one beat BEFORE the setup line so the moment lands in context."},
                        "end": {"type": "number", "description": "End in seconds. Cut right after the payoff — never trail into dead air."},
                        "title": {"type": "string", "description": "Short internal label, 3-6 words."},
                        "hook": {"type": "string", "description": "On-screen hook text for the first 2 seconds. Max 8 words, no hashtags, no clickbait punctuation spam. It must make sense to someone who has NEVER seen this video: say who or what (\"MrBeast built him a house — he hasn't seen it\"), not just \"He has no idea...\"."},
                        "score": {"type": "integer", "description": "Virality score 0-100. Reserve 85+ for moments you would bet on."},
                        "reason": {"type": "string", "description": "One sentence: why this works as a short, or why it is weak."},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "2-4 lowercase labels, e.g. reaction, clutch, rant, story, tutorial, fail, funny, hot-take."},
                        "type": {"type": "string", "enum": CLIP_TYPES, "description": "What kind of moment this is. It decides how the clip is built and edited."},
                        "needs_context": {"type": "boolean", "description": "True if a viewer who never saw the video would be confused, or feel nothing, without something that happened earlier or later in it."},
                        "context_note": {"type": "string", "description": "If needs_context: what that viewer is missing, in one line. Else empty."},
                        "speaker": {"type": "string", "enum": ["creator", "other", "unclear"], "description": "Who says this moment's key lines (the ones the hook is about): creator = the creator named above (or the channel's own host), other = anyone else (a guest, a friend, a caller, a clip being reacted to), unclear = the words and context don't settle it."},
                        "speaker_name": {"type": "string", "description": "When speaker is other: their name if it is said in the video or the title, else a short neutral description ('his friend', 'the guest'). Else empty."},
                    },
                    "required": ["start", "end", "title", "hook", "score", "reason", "tags", "type", "needs_context", "speaker"],
                },
            }
        },
        "required": ["clips"],
    },
}

SYSTEM = """You are the highlight engine inside a short-form clipping tool. Creators \
feed you long videos — streams, podcasts, gameplay, talking-head uploads — and you find \
the segments that would perform as standalone TikToks, Reels and YouTube Shorts.

What makes a clip work:
- It is self-contained. A viewer with zero context understands it in the first 2 seconds.
- It has a hook, a build and a payoff inside 15-60 seconds.
- It carries one idea, not three. Ending early beats trailing off.
- Strong candidates: a hot take stated plainly, a story with a turn, a genuinely funny \
moment, a clutch or a fail, a surprising number or fact, a myth being corrected, \
an emotional beat, a crisp how-to answer.
- Weak candidates: throat-clearing, greetings, rambles without a point, inside jokes that \
need the stream, technical setup talk, anything that only lands with what came before.

Scoring must be honest and spread out. Most segments of most videos are not viral. Use the \
full range: 90+ is rare, 70-85 is a good clip, 50-69 is usable filler, below 50 means do not \
post it. Do not give everything 80.

Different kinds of moment work differently — label each with its type:
- funny / hype: self-contained. Start right on the setup line, 15-30s, end on the laugh or peak.
- reaction / reveal: include the line that sets it up ("Are you ready to see your new home?") \
inside the clip, then the reaction. A face reacting needs no explanation; the reason for it does.
- story / emotional: these need the "before" — the problem, the stakes. If that sits elsewhere \
in the video, set needs_context and say what is missing; it can be stitched in later.
- info / hot_take: the claim stated plainly in the first seconds, then the reason.

Every hook is read by someone who has never seen this video, on a feed, with the sound off. \
Name who or what is happening. "He has no idea what's behind this door" makes them guess; \
"MrBeast built him a house — he hasn't seen it yet" tells them why to stay.

Boundaries matter more than anything. Start on the first word of a sentence — never on the \
last words of the one before — and end on the last word of the payoff. Loud moments in the \
energy hints are a signal that something happened there — check them, but only clip them if \
the words hold up.

Say who speaks. The transcript has no speaker labels, so work it out from the questions and \
answers, names said aloud, who is being addressed, and the video's title — and say unclear when \
you can't. Never put words in the creator's mouth: a hook or caption may say the creator said, \
explained or thinks something ONLY when speaker is creator. When someone else says it, name them \
("His friend Timmy asks why…") or name no one — never "<creator> says…" for a line they didn't say."""

PROMPT = """Video: {title}{creator_line}
Segment of the source covered by this transcript: {block_start} to {block_end} (seconds).
All timestamps below are absolute seconds in the full video — use that same scale.

{energy}

TRANSCRIPT
{transcript}

Find up to {want} clips in this segment. Each between {min_len:.0f} and {max_len:.0f} seconds, \
ideally {ideal} — that is where winning clips sit right now on the platforms these are for. Go past \
that only when the moment truly can't land shorter; cut the build-up instead. Do not overlap them. If this segment honestly has nothing worth posting, \
return fewer clips, or none."""

CAMPAIGN_NOTE = "\n\nCAMPAIGN RULES — these come from a paid campaign's brief and override anything above:\n{guidance}"


def _winning_hooks() -> str:
    """Real hooks from the Clip Style Database, so first drafts read like winners."""
    try:
        from . import styles
        return styles.hook_examples(10)
    except Exception:
        return ""


# Where winning clips sit, by platform (Clip Style Database: clip pages on
# YouTube Shorts mostly 16-33 s, on TikTok 25-68 s).
PLATFORM_IDEAL = {"youtube": (16.0, 35.0), "tiktok": (25.0, 60.0), "instagram": (15.0, 45.0)}


def length_window(min_len: float | None = None, max_len: float | None = None,
                  platforms: List[str] | None = None) -> Tuple[float, float, str]:
    """The clip-length limits for this run: ClipAgent's own, narrowed by a
    campaign's brief when there is one. Returns (lo, hi, the ideal range in words)."""
    lo = max(MIN_LEN, float(min_len or 0))
    hi = min(MAX_LEN, float(max_len)) if max_len else MAX_LEN
    if hi - lo < 2:
        lo = max(1.0, hi - 2) if max_len else lo
        hi = max(hi, lo + 2)
    targets = [PLATFORM_IDEAL[p] for p in (platforms or []) if p in PLATFORM_IDEAL]
    base_lo, base_hi = ((min(t[0] for t in targets), max(t[1] for t in targets)) if len(targets) == 1
                        else (20.0, 45.0))
    if len(targets) > 1:          # several platforms: the overlap, so one cut serves them all
        base_lo, base_hi = max(t[0] for t in targets), min(t[1] for t in targets)
        if base_hi - base_lo < 8:
            base_lo, base_hi = 20.0, 45.0
    if targets and not max_len:
        # You said where it's going: past half again the winning length is too
        # long for that feed (a 90 s cut is wasted on Shorts). A brief's own
        # maximum, when there is one, decides instead.
        hi = min(hi, max(lo + 10, round(base_hi * 1.5)))
    ideal_lo, ideal_hi = max(lo, base_lo), min(hi, base_hi)
    ideal = f"{ideal_lo:.0f}-{ideal_hi:.0f}" if ideal_hi - ideal_lo >= 3 else f"{lo:.0f}-{hi:.0f}"
    return lo, hi, ideal


LAST_ERROR = ""   # the last Claude failure, so the job can say why it fell back


def _client():
    """The Claude client, or RuntimeError — never an import crash.

    A missing key or a missing package both mean the same thing to the caller:
    fall back, do not take the run down with you.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(f"anthropic package not installed: {exc}") from exc

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _transcript_text(segments: List[Dict[str, Any]], start: float, end: float) -> str:
    lines = []
    for seg in segments:
        if seg["end"] < start or seg["start"] > end:
            continue
        text = seg["text"].strip()
        if text:
            lines.append(f"[{seg['start']:.1f}s {_ts(seg['start'])}] {text}")
    return "\n".join(lines)


def _energy_text(peaks: List[Dict[str, float]], start: float, end: float) -> str:
    hits = [p for p in peaks if start <= p["t"] <= end]
    if not hits:
        return ""
    listed = ", ".join(f"{p['t']:.0f}s ({p['energy']:.2f})" for p in hits[:15])
    return ("AUDIO ENERGY HINTS — loudest moments in this segment "
            f"(seconds, 0-1 scale): {listed}\n")


def find_highlights(
    title: str,
    segments: List[Dict[str, Any]],
    duration: float,
    peaks: List[Dict[str, float]] | None = None,
    want: int = 12,
    progress=None,
    min_len: float | None = None,
    max_len: float | None = None,
    guidance: str = "",
    platforms: List[str] | None = None,
    creator: str = "",
) -> List[Dict[str, Any]]:
    """Run Claude over the transcript in blocks and return merged, ranked clips.

    `min_len`/`max_len` narrow the usual length limits and `guidance` adds a
    campaign brief's rules to what Claude is told — both only for campaign runs.
    `creator` (a campaign's person) is who a hook may never credit with someone else's words."""
    global LAST_ERROR
    LAST_ERROR = ""
    peaks = peaks or []
    lo, hi, ideal = length_window(min_len, max_len, platforms)
    extra = CAMPAIGN_NOTE.format(guidance=guidance.strip()) if guidance.strip() else ""
    creator = (creator or "").strip()
    creator_line = (f"\nThe creator (the person these clips are for): {creator}. Other people in it are "
                    "guests, friends or callers." if creator else "")
    if not segments:
        return _fallback(peaks, duration, want, lo, hi)

    try:
        client = _client()
    except RuntimeError:
        return _fallback(peaks, duration, want, lo, hi)

    blocks: List[tuple[float, float]] = []
    cursor = 0.0
    while cursor < duration:
        blocks.append((cursor, min(duration, cursor + BLOCK_SECONDS)))
        cursor += BLOCK_SECONDS
    per_block = max(3, min(10, round(want / max(1, len(blocks)) * 2)))

    found: List[Dict[str, Any]] = []
    for i, (b_start, b_end) in enumerate(blocks):
        transcript = _transcript_text(segments, b_start, b_end)
        if len(transcript) < 80:
            continue
        try:
            got = toolio.ask(
                client, "clips",
                model=CLAUDE_MODEL,
                max_tokens=6000,
                system=SYSTEM + _winning_hooks(),
                tools=[CLIP_TOOL],
                tool_choice={"type": "tool", "name": "submit_clips"},
                messages=[{"role": "user", "content": PROMPT.format(
                    title=title or "Untitled", creator_line=creator_line,
                    block_start=f"{b_start:.0f}", block_end=f"{b_end:.0f}",
                    energy=_energy_text(peaks, b_start, b_end),
                    transcript=transcript, want=per_block,
                    min_len=lo, max_len=hi, ideal=ideal,
                ) + extra}],
            )
        except Exception as exc:
            # Out of credit, rate limited, network: remember it and carry on.
            # If no block got through at all, fall back to loudness below.
            LAST_ERROR = str(exc)[:300]
            continue
        found.extend(got)
        if progress:
            progress(int((i + 1) / len(blocks) * 80))

    if not found and LAST_ERROR:
        return _fallback(peaks, duration, want, lo, hi)

    # Each block scored blind to the others, so put them all on one scale
    # before ranking — and pick up the post copy while we are there.
    if len(blocks) > 1 or len(found) > 1:
        found = rerank(title or "Untitled", found, segments, guidance=guidance, creator=creator)
    if progress:
        progress(100)

    return rank(found, peaks, duration, want, lo, hi, creator=creator)


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def rank(
    clips: List[Dict[str, Any]],
    peaks: List[Dict[str, float]],
    duration: float,
    want: int,
    min_len: float = MIN_LEN,
    max_len: float = MAX_LEN,
    creator: str = "",
) -> List[Dict[str, Any]]:
    """Clean up boundaries, blend in audio energy, drop overlaps, sort."""
    cleaned: List[Dict[str, Any]] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        try:
            start = max(0.0, float(clip["start"]))
            end = min(duration, float(clip["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end - start < min_len:
            end = min(duration, start + min_len)
        if end - start > max_len:
            end = start + max_len
        if end - start < min_len:
            continue

        score = int(max(0, min(100, _num(clip.get("score"), 50))))
        near = [p["energy"] for p in peaks if start - 2 <= p["t"] <= end]
        if near:
            # A loud moment inside the window nudges the score, never dominates it.
            score = int(round(score * 0.85 + max(near) * 100 * 0.15))

        cleaned.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "score": min(100, score),
            "title": (_text(clip.get("title")) or "Untitled clip")[:80],
            "hook": _text(clip.get("hook"))[:70],
            "reason": _text(clip.get("reason"))[:300],
            "tags": [str(t).lower()[:20] for t in toolio.as_list(clip.get("tags"))][:4],
            "caption": _text(clip.get("caption")),
            "hashtags": [str(t) for t in toolio.as_list(clip.get("hashtags"))],
            "verdict": _text(clip.get("verdict")),
            "type": clip.get("type") if clip.get("type") in CLIP_TYPES else "story",
            "needs_context": clip.get("needs_context") in (True, "true", "True", 1),
            "context_note": _text(clip.get("context_note"))[:200],
            "headline": _text(clip.get("headline"))[:60],
            "speaker": clip.get("speaker") if clip.get("speaker") in SPEAKERS else "unclear",
            "speaker_name": _text(clip.get("speaker_name"))[:60],
        })
        if creator:
            credit_clip(cleaned[-1], None, creator)

    cleaned.sort(key=lambda c: c["score"], reverse=True)
    kept: List[Dict[str, Any]] = []
    for clip in cleaned:
        if any(clip["start"] < k["end"] - 1 and clip["end"] > k["start"] + 1 for k in kept):
            continue
        kept.append(clip)
        if len(kept) >= want:
            break

    for n, clip in enumerate(kept, 1):
        clip["rank"] = n
    return kept


# --- who said it -------------------------------------------------------------------
# A hook that says "TJR explains…" over his friend's words gets the post rejected
# and misleads people. Claude is told the rule; this enforces it in code.

SPEAKERS = ("creator", "other", "unclear")
_SAY = (r"(?:just\s+|really\s+|literally\s+|finally\s+)?(?:says?|said|saying|explains?|explained|reveals?|revealed|"
        r"admits?|admitted|claims?|claimed|tells?|told|thinks?|believes?|warns?|warned|asks?|asked|answers?|"
        r"answered|swears?|insists?|breaks?\s+down|on)")
_OWN = (r"(?:advice|take|rule|rules|tip|tips|secret|secrets|answer|words|lesson|lessons|strategy|warning|quote|"
        r"opinion|theory|story|method)")
_NOT_A_NAME = {"his", "her", "their", "the", "a", "an", "my", "our", "your", "some", "someone", "somebody",
               "friend", "guest", "caller", "host", "guy", "girl", "man", "woman", "person", "brother", "sister",
               "co-host", "cohost", "chat", "viewer", "student", "other"}


def _names(creator: str) -> str:
    parts = creator.split()
    names = {creator.strip()}
    if len(parts) > 1 and len(parts[0]) >= 3:
        names.add(parts[0])                        # "Kevin says…" for Kevin Langue
    alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True) if n)
    return rf"(?<![\w@#])\[?(?:{alts})\]?(?![\w])"


def _credit_patterns(name: str) -> List[str]:
    return [rf"^\W*{name}\s*[:—–-]\s*\S", rf"{name}\s+{_SAY}\b",
            rf"{name}['’]s\s+(?:\#?\d+\s+|top\s+|best\s+|real\s+|golden\s+|biggest\s+)?{_OWN}\b",
            rf"according\s+to\s+{name}", rf"[\"”'’]\s*[—–-]+\s*{name}\W*$"]


def credits_creator(text: str, creator: str) -> bool:
    """Does this text put words in the creator's mouth ("TJR says…", "TJR: …", "TJR's #1 rule")?"""
    if not (text or "").strip() or not (creator or "").strip():
        return False
    return any(re.search(p, text, re.I) for p in _credit_patterns(_names(creator)))


def _proper_name(other: str) -> str:
    """'his friend Timmy' -> 'Timmy'; 'the guest' -> ''."""
    words = [w.strip(".,;:!?\"'()") for w in (other or "").split()]
    run: List[str] = []
    for w in words:
        if w and w[0].isupper() and w.lower() not in _NOT_A_NAME:
            run.append(w)
        elif run:
            break
    return " ".join(run[:3])


def fix_credit(text: str, creator: str, speaker: str = "unclear", other: str = "") -> str:
    """The text with any credit to the creator for words they didn't say taken out —
    put on the real speaker when their name is known, otherwise on no one."""
    if not text or not creator or speaker == "creator" or not credits_creator(text, creator):
        return text
    name = _names(creator)
    who = _proper_name(other) if speaker == "other" else ""
    out = text
    if who:                                   # only where the words are credited, not "Timmy asks TJR…"
        for pat in _credit_patterns(name):
            out = re.sub(pat, lambda m: re.sub(name, who, m.group(0), flags=re.I), out, flags=re.I)
    else:
        out = re.sub(rf"^\W*{name}\s*[:—–-]\s*", "", out, flags=re.I)
        out = re.sub(rf"^\W*{name}\s+{_SAY}\s+(?:that\s+)?", "", out, flags=re.I)
        out = re.sub(rf"\b{name}\s+{_SAY}\s+(?:that\s+)?", "", out, flags=re.I)
        out = re.sub(rf"^\W*{name}['’]s\s+", "The ", out, flags=re.I)
        out = re.sub(rf"{name}['’]s\s+", "the ", out, flags=re.I)
        out = re.sub(rf",?\s*according\s+to\s+{name},?", "", out, flags=re.I)
        out = re.sub(rf"\s*[—–-]+\s*{name}\W*$", "", out, flags=re.I)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,;:—–-")
    if out and out[0].islower() and not out.startswith("["):
        out = out[0].upper() + out[1:]
    return out or text


def credit_clip(clip: Dict[str, Any], style_edits: Dict[str, Any] | None, creator: str) -> List[str]:
    """Hold a clip's hook, caption and on-screen cards to the rule: words go to whoever said them.
    Returns a note for each change (also kept on the clip as 'credit_note')."""
    if not creator:
        return []
    speaker = clip.get("speaker") if clip.get("speaker") in SPEAKERS else "unclear"
    other = clip.get("speaker_name") or ""
    notes = []
    for key in ("hook", "caption"):
        before = clip.get(key) or ""
        after = fix_credit(before, creator, speaker, other)
        if after != before:
            clip[key] = after
            notes.append(f"{key}: “{before}” → “{after}”")
    if style_edits:
        if style_edits.get("hook"):
            style_edits["hook"] = fix_credit(style_edits["hook"], creator, speaker, other)
        for card in style_edits.get("cards") or []:
            if card.get("text"):
                fixed = fix_credit(card["text"], creator, speaker, other)
                if fixed != card["text"]:
                    notes.append(f"card: “{card['text']}” → “{fixed}”")
                    card["text"] = fixed
    if notes:
        clip["credit_note"] = ((f"Didn't credit {creator} with words {_proper_name(other) or 'someone else'} says"
                                if speaker == "other" else
                                f"Took {creator}'s name off words nobody could confirm {creator} says")
                               + ": " + "; ".join(notes))[:300]
    return notes


SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*$")
PAUSE = 0.45            # a gap this long counts as a sentence break


def _capital(word: str) -> bool:
    w = word.lstrip("\"'“‘(¿¡")
    return bool(w) and (w[0].isupper() or w[0].isdigit())


def _sentence_edges(ws: List[Dict[str, Any]]) -> Tuple[set, set]:
    """Indices of words that open / close a sentence.

    A full stop only counts when the next word is capitalised: the
    transcriber sometimes drops one mid-sentence ("This part of it. is gonna
    be special") and a clip opening on "is" is exactly the problem we are
    fixing. A real pause counts either way.
    """
    opens, closes = {0}, {len(ws) - 1}
    for i in range(1, len(ws)):
        pause = ws[i]["start"] - ws[i - 1]["end"] >= PAUSE
        stop = SENTENCE_END.search(ws[i - 1]["w"]) and _capital(ws[i]["w"])
        if pause or stop:
            opens.add(i)
            closes.add(i - 1)
    return opens, closes


def clean_bounds(start: float, end: float, words: List[Dict[str, Any]],
                 lead: float = 0.12, tail: float = 0.25, min_len: float = 3.0) -> Tuple[float, float]:
    """Move a span's edges onto sentence boundaries.

    A clip must open on the first word of a sentence — never on the tail of
    the one before ("…lives. How's it going?") — and close on the end of one.
    Moving the start later costs a little more than moving it earlier (it
    drops setup), moving the end earlier costs more than later (it could cut
    the punchline). Nothing moves more than a couple of seconds.
    """
    from .captions import sanitize_words      # overlapping timings (crosstalk) resolved first
    near = [w for w in words if start - 30 <= w["start"] <= end + 30]
    ws = sanitize_words(near)
    if len(ws) < 2:
        return start, end
    opens, closes = _sentence_edges(ws)

    s_cands = [(abs(ws[i]["start"] - start), i)
               for i in opens if start - 2.0 <= ws[i]["start"] <= start + 2.5]
    if s_cands:
        i = min(s_cands)[1]
    else:                                   # no sentence edge nearby: at least skip the partial word
        after = [i for i, w in enumerate(ws) if w["start"] >= start - 0.05]
        i = after[0] if after else 0
    new_start = ws[i]["start"] - lead
    if i > 0:
        new_start = max(new_start, ws[i - 1]["end"] + 0.02)   # no tail of the previous word

    e_cands = [(abs(ws[j]["end"] - end) * (1.5 if ws[j]["end"] < end else 1.0), j)
               for j in closes if end - 2.5 <= ws[j]["end"] <= end + 2.0 and j >= i]
    if e_cands:
        j = min(e_cands)[1]
    else:
        before = [j for j, w in enumerate(ws) if w["end"] <= end + 0.05 and j >= i]
        j = before[-1] if before else len(ws) - 1
    new_end = ws[j]["end"] + tail
    if j + 1 < len(ws):
        new_end = min(new_end, ws[j + 1]["start"] - 0.02)     # no head of the next word

    if new_end - new_start < min_len:
        return start, end
    return round(max(0.0, new_start), 2), round(new_end, 2)


def snap_to_words(clip: Dict[str, Any], words: List[Dict[str, Any]], pad: float = 0.15) -> Dict[str, Any]:
    """Put the clip's edges on sentence boundaries (see clean_bounds).

    Claude works from segment timestamps, which land a beat off the actual
    speech; without this, clips open on the last word of the previous
    sentence and a stray "LIVES." is the first caption anyone sees.
    """
    if not words:
        return clip
    clip["start"], clip["end"] = clean_bounds(clip["start"], clip["end"], words,
                                              min_len=min(MIN_LEN, clip["end"] - clip["start"]))
    if clip["end"] - clip["start"] < MIN_LEN:
        clip["end"] = round(clip["start"] + MIN_LEN, 2)
    return clip


KEYWORDS = re.compile(
    r"\b(insane|crazy|no way|what the|oh my|clutch|actually|listen|secret|nobody|"
    r"never|biggest|mistake|truth|wild|unreal|let me tell you|here's why|the thing is)\b",
    re.I,
)


def _fallback(peaks: List[Dict[str, float]], duration: float, want: int,
              min_len: float = MIN_LEN, max_len: float = MAX_LEN) -> List[Dict[str, Any]]:
    """No Claude key, or no speech at all: fall back to loudness alone."""
    clips = []
    span = max(min_len, min(34.0, max_len))
    for peak in peaks[: want * 2]:
        start = max(0.0, peak["t"] - min(12.0, span / 3))
        clips.append({
            "start": start,
            "end": min(duration, start + span),
            "score": int(40 + peak["energy"] * 45),
            "title": f"Loud moment at {_ts(peak['t'])}",
            "hook": "",
            "reason": "Picked from audio energy only — no transcript analysis ran.",
            "tags": ["energy"],
        })
    return rank(clips, peaks, duration, want, min_len, max_len)


# --- second pass: one scale for the whole video ---------------------------

RERANK_TOOL = {
    "name": "rank_clips",
    "description": "Re-score every candidate against the others and write the post copy.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "The candidate's id from the list you were given."},
                        "score": {"type": "integer", "description": "Virality score 0-100 on ONE scale across the whole video. Spread these out."},
                        "verdict": {"type": "string", "enum": ["post", "maybe", "skip"], "description": "post = worth publishing, maybe = usable filler, skip = do not publish."},
                        "title": {"type": "string", "description": "Short internal label, 3-6 words."},
                        "hook": {"type": "string", "description": "On-screen hook text for the first 2 seconds. Max 8 words. Must make sense to someone who never saw the video: say who or what."},
                        "type": {"type": "string", "enum": CLIP_TYPES, "description": "What kind of moment this is."},
                        "headline": {"type": "string", "description": "Max 7 words: the premise of the whole video in plain words, e.g. 'MrBeast built a city in Ghana'. Shown small at the top for the whole clip so a cold viewer always knows what this is."},
                        "caption": {"type": "string", "description": "The post caption, written the way a creator writes one. One or two lines, no hashtags in here, no emoji spam."},
                        "hashtags": {"type": "array", "items": {"type": "string"}, "description": "3-6 hashtags without the # sign, lowercase, specific to the content rather than generic reach-bait."},
                        "reason": {"type": "string", "description": "One sentence on why this ranks where it does."},
                    },
                    "required": ["id", "score", "verdict", "title", "hook", "type", "headline", "caption", "hashtags", "reason"],
                },
            }
        },
        "required": ["clips"],
    },
}

RERANK_SYSTEM = """You are ranking clip candidates that were picked from one video, \
in separate passes that could not see each other. Each arrived with a score from its own \
pass, so those scores are not comparable — a 78 from one stretch of the video may be \
stronger or weaker than an 85 from another. Your job is to put them all on one scale.

Judge them against each other, not against an abstract standard. In any video a few \
moments genuinely carry it and the rest are ordinary; say so with the numbers. If ten \
candidates come back and you score them all 70-85, you have not done the job.

Also write the post copy for each: a caption a creator would actually type, and hashtags \
that describe what is in the clip rather than whatever is trending. Rewrite each hook for a \
viewer who has never seen this video — who or what, in the first two seconds — and give one \
plain headline for the video's premise. No emoji walls, no \
"wait for it 😱", no ten generic tags.

Each candidate says who speaks its key lines. A hook or caption may credit the creator with \
words ("<creator> says…", "<creator>: …", "<creator>'s rule") ONLY when the speaker is the \
creator; otherwise name the real speaker, or no one."""

RERANK_PROMPT = """Video: {title}{creator_line}

{candidates}

Put all {count} candidates on one scale, write the post copy, and mark each post, maybe or skip."""


def rerank(
    title: str,
    clips: List[Dict[str, Any]],
    segments: List[Dict[str, Any]],
    progress=None,
    guidance: str = "",
    creator: str = "",
) -> List[Dict[str, Any]]:
    """Score every candidate against every other one, and write the post copy.

    Without this each block's scores mean something slightly different, and
    ranking them together quietly compares numbers that were never on the same
    scale.
    """
    if len(clips) < 2:
        return clips
    try:
        client = _client()
    except RuntimeError:
        return clips

    lines = []
    for i, clip in enumerate(clips):
        excerpt = _transcript_text(segments, clip["start"], clip["end"])
        excerpt = re.sub(r"\[[\d.]+s [\d:]+\] ", "", excerpt).replace("\n", " ")[:420]
        lines.append(
            f"[{i}] {_ts(clip['start'])}-{_ts(clip['end'])} "
            f"({clip['end'] - clip['start']:.0f}s) first-pass score {clip.get('score', 50)}\n"
            f"    label: {clip.get('title', '')}\n"
            f"    first-pass note: {clip.get('reason', '')}\n"
            f"    who speaks the key lines: {clip.get('speaker') or 'unclear'}"
            + (f" ({clip['speaker_name']})" if clip.get("speaker_name") else "") + "\n"
            f"    transcript: {excerpt}"
        )

    try:
        replies = toolio.ask(
            client, "clips",
            model=CLAUDE_MODEL,
            max_tokens=8000,
            system=RERANK_SYSTEM,
            tools=[RERANK_TOOL],
            tool_choice={"type": "tool", "name": "rank_clips"},
            messages=[{"role": "user", "content": RERANK_PROMPT.format(
                title=title or "Untitled", candidates="\n\n".join(lines), count=len(clips),
                creator_line=f"\nThe creator: {creator.strip()}" if (creator or "").strip() else "",
            ) + (CAMPAIGN_NOTE.format(guidance=guidance.strip()) if guidance.strip() else "")}],
        )
    except Exception:
        return clips                      # a failed second pass must not lose the clips

    ranked: List[Dict[str, Any]] = []
    seen = set()
    for item in replies:
        idx = _int(item.get("id"), -1)
        if not 0 <= idx < len(clips) or idx in seen:
            continue
        seen.add(idx)
        base = dict(clips[idx])
        base.update({
            "score": int(max(0, min(100, _num(item.get("score"), _num(base.get("score"), 50))))),
            "verdict": item.get("verdict") if item.get("verdict") in ("post", "maybe", "skip") else "maybe",
            "title": _text(item.get("title")) or _text(base.get("title")),
            "hook": (_text(item.get("hook")) or _text(base.get("hook")))[:70],
            "type": item.get("type") if item.get("type") in CLIP_TYPES else base.get("type", "story"),
            "headline": _text(item.get("headline"))[:60],
            "caption": _text(item.get("caption"))[:400],
            "hashtags": [str(t).lstrip("#").lower()[:24] for t in toolio.as_list(item.get("hashtags"))][:6],
            "reason": (_text(item.get("reason")) or _text(base.get("reason")))[:300],
        })
        base["title"] = base["title"][:80]
        ranked.append(base)

    if progress:
        progress(100)
    return ranked or clips
