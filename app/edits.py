"""The edit maker: short, music-driven edits cut from your videos.

A clip is one moment cut out of a long video. An edit is built: several
moments, chosen by Claude for a theme ("his best trading advice", "the
funniest bits"), laid onto the beats of a song you added, with the strongest
one landing on the drop — and finished with the things viral edits are made
of: speed ramps, slow-mo, flashes on the cut, zoom punches on the beat, shake
and an RGB glitch on the drop, one colour grade over everything, grain,
cinema bars, a text hook in the first seconds and an ending that loops.

This module decides WHAT goes WHERE (the styles, the moments, the timeline);
editrender.py draws it. `build_timeline` is pure — no files, no database —
so the timing rules can be tested on their own.
"""
from __future__ import annotations

import bisect
import json
import math
import re
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import beats, highlights, motionmatch, store, toolio, transcribe
from .config import CLAUDE_MODEL, DATA_DIR

SOUND_DIR = DATA_DIR / "sounds"
EDIT_DIR = DATA_DIR / "edits"
_lock = threading.Lock()                  # one edit renders at a time: the PC has no cores to spare

FPS = 30
HOOK_SECONDS = 2.8                        # the hook stays this long
LOOP_SECONDS = 0.4                        # the ending dissolves into the first frame over this long
EXTEND = 1.2                              # a moment's picture may run this far past its words

EFFECTS = {
    "ramp": "Speed ramps",
    "slowmo": "Slow-mo on the drop",
    "flash": "Flash on the cut",
    "pulse": "Zoom punches",
    "shake": "Shake on the hits",
    "glitch": "RGB glitch on the drop",
    "blur": "Motion blur on the cut",
    "push": "Slow push-in",
    "grain": "Film grain",
    "vignette": "Dark corners",
    "letterbox": "Cinema bars",
    "text": "Words on screen",
    "loop": "Loop the ending",
    "smooth": "Smooth slow-mo (slower to make)",
}

GRADES = {
    "punchy": "Punchy",
    "film": "Film",
    "tealorange": "Teal & orange",
    "teal": "Dark teal",
    "mono": "Black & white",
    "gold": "Warm gold",
    "none": "Natural",
}


def _fx(on: str) -> Dict[str, bool]:
    names = set(on.split())
    return {k: k in names for k in EFFECTS}


# `window` is how long ONE moment plays in that style (seconds, shortest–longest) — enforced in code
# (fit_moments), not only asked of Claude. `ideal` is the length a moment is cut to when nothing else
# decides it. Funny, Velocity and Motivation are gs's own numbers; the others follow each style's cut
# pattern (see _base_patterns): the window's shortest is the style's shortest normal shot, its longest
# what a shot may hold before the edit stalls.
STYLES: Dict[str, Dict[str, Any]] = {
    "velocity": {
        "name": "Velocity", "pace": "beat", "voice": 0.0, "music": 1.0, "grade": "punchy", "text": "punch",
        "length": 20, "needs_music": True, "pre_beats": 8, "count": (8, 12), "window": (1.5, 4.0), "ideal": 2.5,
        "what": "Cut on every beat: speed ramps, slow-mo and a glitch on the drop, flashes and zoom punches.",
        "effects": _fx("ramp slowmo flash pulse shake glitch vignette text loop"),
        "brief": "Short moments, {w} each, with the most energy: a big claim, a number, a hard "
                 "line, a laugh, a reaction. `text`: 1 to 4 punch words he actually says in that moment (\"SEVEN "
                 "YEARS\", \"STICK TO IT\"), no emoji. Mark the single hardest moment as the drop.",
    },
    "aura": {
        "name": "Aura", "pace": "beat", "voice": 0.0, "music": 1.0, "grade": "teal", "text": "hook",
        "length": 15, "needs_music": True, "pre_beats": 4, "count": (4, 7), "window": (1.5, 5.0), "ideal": 3.5,
        "what": "Slow and cold: long slow-mo shots, one cut every two bars, a lore hook on top.",
        "effects": _fx("slowmo flash push grain vignette text loop"),
        "brief": "Moments of {w} where he looks most in control: a calm flex, a knowing look "
                 "after a big line, a win. The words don't play (music only), so pick moments that look strong. "
                 "Mark the strongest as the drop. `text` can be empty: the hook carries the edit.",
    },
    "flow": {
        "name": "Flow", "pace": "beat", "voice": 0.0, "music": 1.0, "grade": "tealorange", "text": "hook",
        "length": 25, "needs_music": True, "pre_beats": 8, "count": (15, 30), "window": (0.75, 3.0), "ideal": 3.0,
        "what": "Smooth match cuts about once a second: every cut carries the movement into the next shot.",
        "effects": _fx("ramp slowmo flash pulse shake blur vignette text loop"),
        "brief": "Short moments, {w} each, where he MOVES: gestures, turns, leans in, laughs, "
                 "stands up, points, reacts. Movement matters more than words here (the words don't play). Mark the "
                 "most energetic as the drop. `text` can be empty.",
    },
    "cinematic": {
        "name": "Cinematic", "pace": "speech", "voice": 1.0, "music": 0.30, "grade": "film", "text": "subtitle",
        "length": 30, "music_optional": True, "dip": True, "count": (3, 5), "window": (4.0, 12.0), "ideal": 8.0,
        "what": "Film look with cinema bars and grain; his words with the music underneath.",
        "effects": _fx("push grain vignette letterbox text loop"),
        "brief": "Moments of {w} that each say something complete and quotable — a lesson, "
                 "a turning point, a truth. They should flow as one short story. `text`: the line itself.",
    },
    "motivation": {
        "name": "Motivation", "pace": "speech", "voice": 1.0, "music": 0.36, "grade": "mono", "text": "build",
        "length": 20, "music_optional": True, "dip": True, "count": (2, 4), "window": (4.0, 10.0), "ideal": 7.0,
        "what": "Black and white, his strongest lines building up word by word, music swelling behind.",
        "effects": _fx("flash push grain vignette text loop"),
        "brief": "Moments of {w}: his most powerful lines about discipline, mindset, money or "
                 "winning — each a complete thought on sentence edges. `text`: the line itself; `key`: the one word "
                 "that hits hardest. Mark the strongest as the drop.",
    },
    "funny": {
        "name": "Funny", "pace": "speech", "voice": 1.0, "music": 0.12, "grade": "none", "text": "meme",
        "length": 30, "music_optional": True, "count": (4, 7), "window": (3.0, 8.0), "ideal": 5.5,
        "what": "The funniest bits back to back: hard cuts, a zoom punch and shake on every punchline, meme text.",
        "effects": _fx("pulse shake text loop"),
        "brief": "Moments of {w}: the funniest bits — jokes, reactions, chaos, awkward moments. "
                 "Each must land its punchline inside it; `hit` is the punchline. `text`: a short meme caption in a "
                 "viewer's voice (max 8 words), or empty.",
    },
    "money": {
        "name": "Money", "pace": "beat", "voice": 0.0, "music": 1.0, "grade": "gold", "text": "quote",
        "length": 20, "needs_music": True, "pre_beats": 4, "dip": True, "count": (6, 9), "window": (1.5, 5.0),
        "ideal": 3.0,
        "what": "Warm gold grade, smooth slow-mo and push-ins, a cut every four beats, the money lines on screen.",
        "effects": _fx("slowmo push grain vignette text loop"),
        "brief": "Moments of {w} about money, wins, the lifestyle and big numbers. `text`: a "
                 "short money line he actually says there (max 6 words).",
    },
}
ALIASES = {"hype": "velocity", "luxury": "money"}      # names used by the first draft
LENGTHS = (15, 20, 25, 30, 40, 60)


def _a(name: str, low: bool = False) -> str:
    art = "An " if name[:1].lower() in "aeiou" else "A "
    return (art.lower() if low else art) + name


def style_key(name: Any) -> str:
    key = str(name or "").strip().lower()
    key = ALIASES.get(key, key)
    return key if key in STYLES else "velocity"


def window_text(style: str) -> str:
    """A style's moment length in plain words: "3–8 s"."""
    lo, hi = STYLES[style_key(style)]["window"]
    return f"{lo:g}–{hi:g} s"


def count_for(style: str, length: float) -> Tuple[int, int]:
    """How many moments to ask Claude for: the style's usual count scaled to the length, and never fewer
    than it takes to fill the length at the style's longest moment."""
    st = STYLES[style_key(style)]
    lo_w, hi_w = st["window"]
    c_lo, c_hi = st["count"]
    k = float(length or st["length"]) / st["length"]
    lo_n = max(1, int(round(c_lo * k)), int(math.ceil(0.9 * float(length or st["length"]) / hi_w)))
    hi_n = max(lo_n + 1, int(round(c_hi * k)))
    return lo_n, min(40, hi_n)


# --- sounds ----------------------------------------------------------------------------

AUDIO_TYPES = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac")
VIDEO_TYPES = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")


def add_sound(src: Path, name: str) -> Dict[str, Any]:
    """Keep a song you added (from a video file, only its sound), read its beats once, and remember them."""
    suffix = Path(name).suffix.lower()
    if suffix not in AUDIO_TYPES + VIDEO_TYPES:
        raise ValueError("That file type isn't a song ClipAgent can read — use MP3, M4A, WAV, or a video file")
    SOUND_DIR.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w.-]+", "_", Path(name).stem)[:60] or "sound"
    if suffix in VIDEO_TYPES:
        dest = SOUND_DIR / f"{int(time.time())}_{stem}.m4a"
        proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-c:a", "aac", "-b:a", "192k",
                               str(dest)], capture_output=True, text=True)
        src.unlink(missing_ok=True)
        if proc.returncode != 0 or not dest.is_file():
            dest.unlink(missing_ok=True)
            raise ValueError("That video has no sound ClipAgent can use as a song")
    else:
        dest = SOUND_DIR / f"{int(time.time())}_{stem}{suffix}"
        shutil.move(str(src), dest)
    try:
        analysis = beats.analyze(dest)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise ValueError(str(exc) if isinstance(exc, RuntimeError) else "Couldn't read the beats of that song") from exc
    sid = store.add_sound(Path(name).stem[:80] or "Song", str(dest), analysis["duration"], analysis)
    return store.get_sound(sid)


def _remove(path: Path) -> None:
    """Delete a file we made; on Windows a file that's open (playing in the browser) is tried again, then left."""
    for _ in range(5):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.3)
        except OSError:
            return


def delete_sound(sid: str) -> None:
    sound = store.get_sound(sid)
    if not sound:
        raise ValueError("Song not found")
    busy = [e for e in store.list_edits(200) if e["status"] in ("queued", "running")
            and (e.get("settings") or {}).get("sound") == sid]
    if busy:
        raise ValueError("An edit with this song is being made right now — wait until it's done")
    if sound.get("file"):
        _remove(Path(sound["file"]))
    store.delete_sound(sid)


def sound_json(s: Dict[str, Any]) -> Dict[str, Any]:
    a = s.get("analysis") or {}
    return {"id": s["id"], "name": s["name"], "duration": s.get("duration"), "bpm": a.get("bpm"),
            "drop": a.get("drop"), "beats": len(a.get("beats") or []), "energy": a.get("energy") or [],
            "url": f"/media/sound/{s['id']}"}


# --- footage --------------------------------------------------------------------------

def usable_sources() -> List[Dict[str, Any]]:
    """Videos an edit can be cut from: finished, still on this PC, with words."""
    out = []
    for j in store.list_jobs(200):
        if j["status"] != "done":
            continue
        full = store.get_job(j["id"]) or {}
        src = full.get("source_path") or ""
        if not src or not Path(src).is_file() or not full.get("transcript"):
            continue
        clips = [c for c in store.list_clips(j["id"]) if c.get("thumb") and Path(c["thumb"]).is_file()]
        out.append({"id": j["id"], "title": j["title"], "duration": j["duration"], "created_at": j["created_at"],
                    "campaign_id": full.get("campaign_id") or "",
                    "poster": f"/media/thumb/{clips[0]['id']}.jpg" if clips else None})
    # the same video run twice is one source: keep the newest
    seen, unique = set(), []
    for s in out:
        key = (s["title"], round(s["duration"] or 0))
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


def _loads(text: Any, default: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _transcript_block(i: int, job: Dict[str, Any], budget: int) -> str:
    """One video for Claude: the moments ClipAgent already rated highly first,
    then the transcript with times, as much as fits."""
    t = _loads(job.get("transcript"), {})
    segs = t.get("segments") or []
    lines = [f"=== VIDEO {i}: {job.get('title') or 'Untitled'} ({(job.get('duration') or 0) / 60:.0f} min)"]
    picked = [c for c in store.list_clips(job["id"]) if not c.get("alt_of")][:8]
    if picked:
        lines.append("Moments already picked as strong (for reference):")
        for c in picked:
            lines.append(f"  {c['start']:.1f}-{c['end']:.1f}: {c.get('hook') or c.get('title') or ''}")
    lines.append("Transcript (start seconds, text):")
    size = sum(len(x) for x in lines)
    for s in segs:
        line = f"[{s['start']:.1f}] {(s.get('text') or '').strip()}"
        size += len(line) + 1
        if size > budget:
            lines.append("[… the rest of this video is left out]")
            break
        lines.append(line)
    return "\n".join(lines)


def _candidate_block(candidates: List[Dict[str, Any]], index_of: Dict[str, int], budget: int = 90000) -> str:
    """Moments scored elsewhere (Creator Scan), listed instead of whole transcripts."""
    lines = ["CANDIDATE MOMENTS (already found and scored; pick from these by number):"]
    size = len(lines[0])
    for k, c in enumerate(candidates, 1):
        line = (f"C{k}: VIDEO {index_of.get(c['source'], 0)} {float(c['start']):.1f}-{float(c['end']):.1f}"
                f" · {c.get('kind') or 'moment'} · score {c.get('score', '?')}: {str(c.get('text') or '')[:300]}")
        size += len(line) + 1
        if size > budget:
            lines.append("[… more candidates left out]")
            break
        lines.append(line)
    return "\n".join(lines)


# --- honesty: words on screen come from what's said -------------------------------------

_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1e3, "k": 1e3, "grand": 1e3, "million": 1e6, "mil": 1e6, "m": 1e6,
           "billion": 1e9, "bn": 1e9, "b": 1e9}


def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+(?:[.,][0-9]+)*", text.lower().replace("’", "'").replace("'", ""))


def numbers_in(text: str) -> List[float]:
    """Every number a text states, as values: "$20M", "20 million", "twenty million" → 20000000."""
    toks = _tokens(re.sub(r"(\d)([kmb])\b", r"\1 \2", text.lower()))
    out: List[float] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        value: Optional[float] = None
        if re.fullmatch(r"\d+(?:[.,]\d+)*", t):
            raw = t.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", t) else t.replace(",", ".")
            try:
                value = float(raw)
            except ValueError:
                value = None
        elif t in _NUM_WORDS:
            value = float(_NUM_WORDS[t])
            while i + 1 < len(toks) and toks[i + 1] in _NUM_WORDS and _NUM_WORDS[toks[i + 1]] < 10 and value >= 20:
                value += _NUM_WORDS[toks[i + 1]]
                i += 1
        if value is None:
            i += 1
            continue
        while i + 1 < len(toks) and toks[i + 1] in _SCALES:
            value *= _SCALES[toks[i + 1]]
            i += 1
        out.append(value)
        i += 1
    return out


def _numbers_said(text: str, said: str) -> bool:
    """Every number on screen is a number he said (nothing invented)."""
    heard = numbers_in(said)
    for n in numbers_in(text):
        if not any(abs(n - h) <= 1e-6 * max(1.0, abs(h)) for h in heard):
            return False
    return True


_FILLER = {"the", "a", "an", "and", "to", "of", "is", "it", "i", "you", "he", "my", "his", "in", "on", "so", "that",
           "this", "bro", "just", "s", "t", "re", "ve", "ll", "d", "m"}


def _words_said(text: str, said: str, need: float = 0.6) -> bool:
    """Most of the words on screen are words said in that moment."""
    want = [t for t in _tokens(text) if t not in _FILLER]
    if not want:
        return True
    heard = set(_tokens(said))
    stems = {h[:5] for h in heard}
    hit = sum(1 for t in want if t in heard or t[:5] in stems)
    return hit / len(want) >= need


def _said_between(words: List[Dict[str, Any]], start: float, end: float) -> str:
    return " ".join(w.get("w", "") for w in words if start <= w["start"] <= end)


# --- cutting moments to length, on word boundaries ------------------------------------------
# Every style has a window (STYLES[...]["window"]): how long one moment plays. Claude is asked for
# it, and here it is enforced: a moment is cut down to its punchline — its strongest stretch —
# starting on a sentence start (or a breath) and ending right after the punchline on a sentence
# end (or a breath), never inside a word. A little silence around the words may pad a short moment
# (a look, a reaction). A moment that can't be cut cleanly to at least the window's shortest is
# left out, with a note. A moment gs sized himself keeps his size.
# Then the length budget: a voice edit's moments are shortened (still within their windows), then
# the weakest left out, until the edit is within ±15 % of the length asked (a beat edit gets that
# from the song's bars). Everything here is pure: words in, numbers out.

CLAUSE_END = re.compile(r"[,;:—–][\"'”’)\]]*$")
BREATH = 0.25                    # a gap this long inside a sentence is a clean place to cut
LEAD, TAIL = 0.1, 0.22           # air kept before the first word and after the last
PAD_BEFORE, PAD_AFTER = 1.0, 1.5  # picture without words a moment may take before / after its words
REACH_BEFORE, REACH_AFTER = 3.0, 2.0  # how far outside Claude's pick a cut may reach (setup / reaction)
AFTER_PUNCH = 2.5                # at most this much talking after the punchline (silence may follow)
UNIT = 0.1                       # the length budget works in tenths of a second
BUDGET = 0.15                    # a finished edit stays within ±15 % of the length asked
MIN_MOMENT = 0.8                 # nothing plays shorter than this, even when asked for by hand
NEVER = 1e12


def _edge_costs(style: str) -> Tuple[float, float, float]:
    """What a cut costs at a sentence edge, at a breath or comma, and just between two words.
    His words play in voice styles, so a half thought is expensive there; Flow follows motion."""
    if STYLES[style]["pace"] == "speech":
        return 0.0, 1.2, 4.0
    if style == "flow":
        return 0.0, 0.0, 0.0
    return 0.0, 0.3, 0.6


def _clean_words(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Words in order, none overlapping the next (a cut between two words is then always clean)."""
    out: List[Dict[str, Any]] = []
    for w in words or []:
        text = str(w.get("w") or "").strip()
        if not text:
            continue
        try:
            s, e = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if out:
            s = max(s, out[-1]["start"] + 0.01)
            if out[-1]["end"] > s:
                out[-1]["end"] = s
        out.append({"w": text, "start": s, "end": max(e, s + 0.01)})
    return out


def word_source(words: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A video's words with, for each, how cleanly a moment can start on it (sq) or end after it (eq):
    0 = a sentence edge or a real pause, 1 = a comma or a breath, 2 = just between two words."""
    ws = _clean_words(words)
    n = len(ws)
    sq, eq = [2] * n, [2] * n
    for k in range(n):
        gap = ws[k]["start"] - ws[k - 1]["end"] if k else 1e9
        prev = ws[k - 1]["w"] if k else ""
        if gap >= highlights.PAUSE or (highlights.SENTENCE_END.search(prev) and highlights._capital(ws[k]["w"])):
            sq[k] = 0
        elif gap >= BREATH or CLAUSE_END.search(prev) or highlights.SENTENCE_END.search(prev):
            sq[k] = 1
        if k:
            eq[k - 1] = sq[k]
    if n:
        eq[n - 1] = 0
    return {"ws": ws, "sq": sq, "eq": eq, "starts": [w["start"] for w in ws]}


def _start_range(ws: List[Dict[str, Any]], i: int) -> Tuple[float, float]:
    """Where a moment opening on word i may start: (earliest, with silence before it; latest, tight)."""
    prev_end = ws[i - 1]["end"] + 0.02 if i else 0.0
    tight = min(ws[i]["start"], max(prev_end, ws[i]["start"] - LEAD))
    return min(tight, max(prev_end, ws[i]["start"] - PAD_BEFORE, 0.0)), tight


def _end_range(ws: List[Dict[str, Any]], j: int, src_dur: float) -> Tuple[float, float]:
    """Where a moment closing on word j may end: (tight; latest, with silence after it)."""
    nxt = ws[j + 1]["start"] - 0.02 if j + 1 < len(ws) else src_dur
    tight = max(ws[j]["end"], min(nxt, ws[j]["end"] + TAIL))
    return tight, max(tight, min(nxt, ws[j]["end"] + PAD_AFTER, src_dur))


def _tok(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower().replace("’", "'").replace("'", ""))


def find_words(src: Dict[str, Any], phrase: str, a: float, b: float, reach: float = 2.0,
               fuzzy: bool = False) -> Optional[Tuple[int, int]]:
    """Where a phrase is said, nearest the stretch a–b: (first word, last word) indices, or None.
    Exact words first; with `fuzzy`, the shortest run holding most of them (on-screen lines are
    sometimes written a little differently from how he says them)."""
    want = [t for t in (_tok(x) for x in re.split(r"[\s/]+", phrase or "")) if t]
    ws = src["ws"]
    if not want or not ws:
        return None
    k0 = bisect.bisect_left(src["starts"], a - reach)
    k1 = bisect.bisect_right(src["starts"], b + reach)
    toks = [_tok(ws[k]["w"]) for k in range(k0, k1)]

    def dist(i: int, j: int) -> float:
        mid = (ws[i]["start"] + ws[j]["end"]) / 2
        return 0.0 if a <= mid <= b else min(abs(mid - a), abs(mid - b))

    best = None
    for i in range(len(toks) - len(want) + 1):
        if toks[i:i + len(want)] == want:
            cand = (dist(k0 + i, k0 + i + len(want) - 1), k0 + i, k0 + i + len(want) - 1)
            best = cand if best is None or cand < best else best
    if best or not fuzzy:
        return (best[1], best[2]) if best else None
    need = set(want) - set(_FILLER) or set(want)
    for i in range(len(toks)):
        if toks[i] not in need:
            continue
        seen = set()
        for j in range(i, min(len(toks), i + len(want) * 2 + 4)):
            if toks[j] in need:
                seen.add(toks[j])
            if len(seen) >= 0.7 * len(need) and toks[j] in need:
                cand = (j - i, dist(k0 + i, k0 + j), k0 + i, k0 + j)
                best = cand if best is None or cand < best else best
                break
    return (best[2], best[3]) if best else None


def _punchline(m: Dict[str, Any], src: Dict[str, Any], style: str, a: float, b: float,
               hit: float) -> Tuple[Optional[int], Optional[Tuple[int, int]]]:
    """The word a moment builds to (the key word, the end of its line, or what's said at the hit) and,
    when its on-screen text is his own words, where that line is said. (None, None): no words in it."""
    ws = src["ws"]
    k0 = max(0, bisect.bisect_left(src["starts"], a - 30.0))
    inside = [k for k in range(k0, bisect.bisect_right(src["starts"], b))
              if ws[k]["end"] > a + 0.05 and ws[k]["start"] < b - 0.05]
    if not inside:
        return None, None
    line = None
    if STYLES[style]["text"] in ("punch", "quote", "subtitle", "build") and str(m.get("text") or "").strip():
        line = find_words(src, str(m["text"]), a, b, reach=1.5, fuzzy=True)
    p = None
    if str(m.get("key") or "").strip():
        found = find_words(src, str(m["key"]), a, b, reach=0.5)
        p = found[1] if found else None
    if p is None and m.get("hit_auto"):
        p = line[1] if line else inside[-1]                # no punchline given: his line's end, or where Claude's pick ends
    if p is None:
        p = min(inside, key=lambda k: 0.0 if ws[k]["start"] <= hit <= ws[k]["end"]
                else min(abs(ws[k]["start"] - hit), abs(ws[k]["end"] - hit)))
    return p, line


Cut = Tuple[float, float, float, float, float, bool]     # cost, start earliest, start tight, end tight, end latest, centred


def _cuts(src: Dict[str, Any], *, a: float, b: float, p: int, line: Optional[Tuple[int, int]], lo: float,
          hi: float, costs: Tuple[float, float, float], bounds: Tuple[float, float], src_dur: float,
          contain: Optional[Tuple[float, float]] = None, extra: float = 0.0,
          hit: Optional[float] = None) -> List[Cut]:
    """Every clean way to cut a moment around its punchline (word p): start on a word i ≤ p, end after
    a word j ≥ p, inside `bounds`, lo–hi seconds long (silence may pad it). `contain`: the cut must
    hold that stretch (for "make it longer"). A `hit` in the silence just before or after the words
    is kept inside the cut (the reaction, the look)."""
    ws, sq, eq = src["ws"], src["sq"], src["eq"]
    lo_t, hi_t = bounds
    line_fits = bool(line) and ws[line[1]]["end"] - ws[line[0]]["start"] + LEAD + TAIL <= hi
    close = next((j for j in range(p, len(ws)) if eq[j] == 0), len(ws) - 1)   # the punchline's sentence ends here
    stop_at = ws[close]["end"] + AFTER_PUNCH                                     # talking past that: only a little
    out: List[Cut] = []
    for i in range(bisect.bisect_left(src["starts"], lo_t - 1e-6), p + 1):
        s_lo, s_hi = _start_range(ws, i)
        s_lo = max(s_lo, lo_t)
        if s_hi < lo_t - 1e-6:
            continue
        if contain and s_hi > contain[0] + 0.05:
            break
        if hit is not None and s_lo <= hit < s_hi:
            s_hi = max(s_lo, hit - 0.05)
        for j in range(p, len(ws)):
            e_lo, e_hi = _end_range(ws, j, src_dur)
            e_hi = min(e_hi, hi_t)
            if hit is not None and e_lo < hit <= e_hi:
                e_lo = min(e_hi, hit + 0.05)
            if e_lo > hi_t + 1e-6 or e_lo - s_hi > hi + 0.05 or ws[j]["end"] > stop_at:
                break
            if contain and e_lo < contain[1] - 0.05:
                continue
            if e_hi - s_lo < lo - 0.05:
                continue
            c = extra + costs[sq[i]] + costs[eq[j]]
            c += 0.25 * max(0.0, ws[j]["end"] - ws[p]["end"] - 0.6)          # end right after the punchline
            if line_fits and (i > line[0] or j < line[1]):
                c += 2.5                                                      # keep his line whole when it fits
            c += 0.6 * (max(0.0, a - ws[i]["start"]) + max(0.0, ws[j]["end"] - b))   # stay in the pick
            out.append((round(c, 4), s_lo, s_hi, e_lo, e_hi, False))
    return out


def _silent_cut(src: Dict[str, Any], a: float, b: float, hit: float, bounds: Tuple[float, float],
                src_dur: float) -> List[Cut]:
    """A moment with no words in it (a look, a reaction): any stretch of its silence around the hit."""
    ws, starts = src["ws"], src["starts"]
    k = bisect.bisect_left(starts, b - 0.05)              # the first word after it; the one before ends before it
    r_lo = max(bounds[0], ws[k - 1]["end"] + 0.02 if k > 0 else 0.0, 0.0)
    r_hi = min(bounds[1], ws[k]["start"] - 0.02 if k < len(ws) else src_dur, src_dur)
    if r_hi <= r_lo:
        return []
    hit = min(max(hit, r_lo), r_hi)
    return [(0.0, r_lo, hit, hit, r_hi, True)]


def _options(cuts: List[Cut], lo: float, hi: float, natural: float) -> List[Tuple[int, float, int]]:
    """For each length (in tenths of a second) inside lo–hi: the best cut and what it costs —
    edges, silence used as padding, distance from the moment's natural length."""
    best: Dict[int, Tuple[float, int]] = {}
    u_lo, u_hi = int(math.ceil(lo / UNIT - 1e-6)), int(math.floor((hi + 0.05) / UNIT + 1e-6))
    for idx, (c, s_lo, s_hi, e_lo, e_hi, centred) in enumerate(cuts):
        core, top = max(0.0, e_lo - s_hi), e_hi - s_lo
        for u in range(max(u_lo, int(math.ceil((core - 0.05) / UNIT))),
                       min(u_hi, int(math.floor((top + 0.05) / UNIT))) + 1):
            d = min(max(u * UNIT, core), top)
            cost = c + (0.0 if centred else 0.35 * (d - core)) + 0.25 * abs(d - natural)
            key = int(math.ceil(d / UNIT - 1e-6))            # counted at its real length, rounded up
            if key not in best or cost < best[key][0]:
                best[key] = (cost, idx)
    return sorted((u, round(cost, 4), idx) for u, (cost, idx) in best.items())


def _realize(cut: Cut, d: float) -> Tuple[float, float]:
    """The cut at length d: silence goes after the words first (the reaction), then before them."""
    _, s_lo, s_hi, e_lo, e_hi, centred = cut
    d = min(max(d, e_lo - s_hi), e_hi - s_lo)
    if centred:
        s = min(max(s_hi - 0.4 * d, s_lo), e_hi - d)
        return s, s + d
    extra = d - (e_lo - s_hi)
    e = e_lo + min(extra, e_hi - e_lo)
    s = s_hi - min(extra - (e - e_lo), s_hi - s_lo)
    return s, e


def _pick_span(m: Dict[str, Any]) -> Tuple[float, float, float]:
    """The stretch a moment is cut from — what Claude picked (or what gs set by hand) — and its hit."""
    pick = m.get("pick") or [m["start"], m["end"], m.get("hit")]
    a, b = float(pick[0]), float(pick[1])
    hit = pick[2] if len(pick) > 2 and pick[2] is not None else m.get("hit")
    hit = float(hit) if hit is not None else (a + b) / 2
    return a, b, min(max(hit, a), b)


def prepare_moment(m: Dict[str, Any], style: str, src: Dict[str, Any], src_dur: float) -> Dict[str, Any]:
    """All the clean ways to play one moment in this style, priced by length (see _options)."""
    st = STYLES[style]
    lo, hi = st["window"]
    a, b, hit = _pick_span(m)
    costs = _edge_costs(style)
    p, line = _punchline(m, src, style, a, b, hit) if src["ws"] else (None, None)
    prep: Dict[str, Any] = {"lo": lo, "hi": hi, "hit": hit, "a": a, "b": b, "manual": bool(m.get("manual")),
                            "punch_at": src["ws"][p]["start"] if p is not None else None}
    if m.get("manual"):                                   # his size wins over the style's window
        d = b - a
        prep["lo"], prep["hi"] = min(lo, d), max(hi, d)
        cuts: List[Cut] = [(0.0, a, a, b, b, False)]
        if p is not None:                                 # only if the length budget leaves no other way
            cuts += _cuts(src, a=a, b=b, p=p, line=line, lo=min(lo, d), hi=d, costs=costs, bounds=(a, b),
                          src_dur=src_dur, extra=15.0)
        natural = d
    else:
        natural = min(max(st["ideal"], lo), max(lo, b - a), hi)
        bounds = (max(0.0, a - REACH_BEFORE), min(src_dur, b + REACH_AFTER))
        if p is None:
            cuts = _silent_cut(src, a, b, hit, bounds, src_dur)
        else:
            cuts = _cuts(src, a=a, b=b, p=p, line=line, lo=lo, hi=hi, costs=costs, bounds=bounds, src_dur=src_dur,
                         hit=None if m.get("hit_auto") else hit)
    prep.update(cuts=cuts, natural=natural, opts=_options(cuts, prep["lo"], prep["hi"], natural))
    if not prep["opts"]:
        prep["why"] = (f"it's only {b - a:.1f} s, and there's no clean way to stretch it to {lo:g} s"
                       if b - a < lo else f"it can't be cut to {window_text(style)} without cutting into his words")
    return prep


def cut_moment(m: Dict[str, Any], prep: Dict[str, Any], units: Optional[int] = None,
               idx: Optional[int] = None) -> Dict[str, Any]:
    """The moment cut one way (its cheapest, unless told which): new start, end and hit."""
    if units is None or idx is None:
        units, _, idx = min(prep["opts"], key=lambda o: o[1])
    s, e = _realize(prep["cuts"][idx], units * UNIT)
    s, e = math.floor(s * 100 + 1e-6) / 100, math.ceil(e * 100 - 1e-6) / 100     # rounding never cuts a word
    a, b, hit = prep["a"], prep["b"], prep["hit"]
    at = hit
    if prep.get("punch_at") is not None and (m.get("hit_auto") or not s <= hit <= e):
        at = prep["punch_at"]                                # the punchline is the instant it builds to
    out = dict(m)
    out.update(start=s, end=e, hit=round(min(max(at, s), e), 2))
    out["pick"] = [round(a, 2), round(b, 2), round(hit, 2)]
    return out


def _label(m: Dict[str, Any]) -> str:
    text = str(m.get("text") or "").strip()
    return f"“{text[:40]}”" if text else f"the moment at {_clock(float(m.get('start') or 0))}"


def _clock(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def _drop_cost(m: Dict[str, Any], k: int, n: int) -> float:
    """What leaving a moment out costs: far more than shortening any; the weakest go first,
    the opener and the closer last; the drop never; one gs sized himself almost never."""
    if m.get("drop"):
        return NEVER
    if m.get("manual"):
        return 400.0
    strength = highlights._num(m.get("strength"), 5.0)
    return 40.0 + 6.0 * min(10.0, max(0.0, strength)) + (8.0 if k == 0 else 0.0) + (5.0 if k == n - 1 else 0.0)


def fit_budget(preps: List[Dict[str, Any]], moments: List[Dict[str, Any]], target: float, lower: float,
               upper: float, lam: float = 0.4) -> Tuple[List[Optional[Tuple[int, int]]], float]:
    """Choose each moment's length (or leave it out) so that together they come to `target`, never
    outside lower–upper when that can be helped. Returns per moment (units, cut index) or None (left
    out), and the total seconds. Shortening is always tried before leaving anything out."""
    import numpy as np
    n = len(preps)
    size = sum(max((o[0] for o in p["opts"]), default=0) for p in preps) + 2
    best = np.full(size, NEVER)
    best[0] = 0.0
    backs = []
    for k, prep in enumerate(preps):
        new = best + _drop_cost(moments[k], k, n)
        arg = np.where(best < NEVER / 2, -1, -2)
        for oi, (u, cost, _) in enumerate(prep["opts"]):
            if u >= size:
                continue
            cand = np.full(size, NEVER)
            cand[u:] = best[:size - u] + cost
            better = cand < new
            new[better] = cand[better]
            arg[better] = oi
        best = np.minimum(new, NEVER)
        backs.append(arg)
    units = np.arange(size)
    ok = best < NEVER / 2
    t_lo, t_hi = int(math.ceil(lower / UNIT - 1e-6)), int(math.floor(upper / UNIT + 1e-6))
    inside = ok & (units >= t_lo) & (units <= t_hi)
    if inside.any():
        score = np.where(inside, best + lam * np.abs(units - target / UNIT) * UNIT, np.inf)
    else:                                       # too little material (or too much sized by hand): as near as it's
        miss = np.maximum(t_lo - units, 0) + np.maximum(units - t_hi, 0)          # worth getting, not at any price
        score = np.where(ok, best + 1.0 * miss * UNIT, np.inf)
    t = int(np.argmin(score))
    total = t * UNIT
    picks: List[Optional[Tuple[int, int]]] = [None] * n
    for k in range(n - 1, -1, -1):
        oi = int(backs[k][t])
        if oi >= 0:
            u, _, idx = preps[k]["opts"][oi]
            picks[k] = (u, idx)
            t -= u
    return picks, total


def length_budget(length: float, limits: Optional[Dict[str, Any]], notes: List[str]) -> Dict[str, float]:
    """The length to aim for and the most / least the edit may be: ±15 % of the length asked, inside the
    campaign's own limits when it has them."""
    L = float(length)
    limits = limits or {}
    name = str(limits.get("name") or "The campaign")
    cmax = highlights._num(limits.get("max"), 0.0)
    cmin = highlights._num(limits.get("min"), 0.0)
    if cmax and cmax < 10:
        notes.append(f"{name} allows at most {cmax:.0f} s — shorter than any edit can be — so its check will "
                     "block this one. Make clips for it instead.")
        cmax = 0.0
    if cmax and L > cmax + 1e-6:
        notes.append(f"{name} allows at most {cmax:.0f} s, so the edit is kept under that.")
        L = cmax
    if cmin and L < cmin - 1e-6 and (not cmax or cmin <= cmax):
        notes.append(f"{name} asks for at least {cmin:.0f} s, so the edit is made that long.")
        L = cmin
    upper, lower = L * (1 + BUDGET), L * (1 - BUDGET)
    if cmax:
        upper = min(upper, cmax)
    if cmin:
        lower = max(lower, min(cmin, upper))
    return {"length": L, "upper": upper, "lower": lower, "target": min(L, upper - 0.02 * L)}


# --- Claude picks the moments --------------------------------------------------------------

PICK_TOOL = {
    "name": "pick_moments",
    "description": "Choose the moments for the edit, in the order they play, and write its hook.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "A short name for this edit (for the library)."},
            "hook": {"type": "string", "description": "The text on screen for the first 3 seconds, max 9 words. "
                     "Lore (\"bro turned $500 into $2M and still…\") or a claim (\"the most disciplined trader on "
                     "YouTube\") built ONLY from facts said in these videos. Understandable to someone who has "
                     "never heard of him. Never a vague teaser like \"wait for it\" or \"you won't believe this\"."},
            "caption": {"type": "string", "description": "The caption to post it with: one or two lines, no hashtags."},
            "hashtags": {"type": "array", "items": {"type": "string"}, "description": "3-6 hashtags, no '#'."},
            "moments": {"type": "array", "items": {"type": "object", "properties": {
                "candidate": {"type": "integer", "description": "The candidate number (C3 → 3) when candidates "
                              "are listed; then video/start/end may be left out."},
                "video": {"type": "integer", "description": "The VIDEO number."},
                "start": {"type": "number", "description": "Start, seconds in that video (from the transcript times)."},
                "end": {"type": "number"},
                "hit": {"type": "number", "description": "The instant it builds to: the punch word, the number, "
                        "the punchline (seconds in that video)."},
                "text": {"type": "string"},
                "key": {"type": "string", "description": "Optional: the one word in `text` to highlight."},
                "kind": {"type": "string", "enum": ["quote", "funny", "hype", "reaction", "money", "story", "action"]},
                "drop": {"type": "boolean", "description": "true for the ONE moment that lands on the song's drop."},
                "strength": {"type": "integer", "description": "1-10: how strong this moment is on its own "
                             "(10 = the best). When the edit runs long, the weakest are left out first."},
                "why": {"type": "string", "description": "A few words: why this moment."},
            }, "required": ["text"]}},
        },
        "required": ["hook", "moments"],
    },
}

PICK_SYSTEM = """You cut short edits for a clip page — the kind that get millions of views on TikTok, Reels and \
Shorts. You pick the moments from the videos below and decide the order they play in.

Rules:
- Only moments that are really there: use the transcript's times. Start on the first word of a sentence and end \
right after the last word of one, unless the brief says the moments are short hits.
- On-screen text uses only what is said in the video (or a viewer-style caption for funny edits). Never invent \
names, numbers or claims. Every number on screen must be a number he says.
- The person is the hero of the edit: never make them look bad.
- Order matters: open with something that stops the scroll, build, put the strongest moment on the drop, end \
on a line that sticks — ideally one that leads naturally back into the opening, because the edit loops.
- The hook is the most important text: it must make a stranger stay. Lore or a bold claim, from the facts said.
"""


def before_drop(style: str, sound: Optional[Dict[str, Any]], length: Optional[float] = None) -> Optional[Tuple[int, float]]:
    """For a beat edit on this song: how many moments fit before the drop, and how many seconds in it comes."""
    style = style_key(style)
    st = STYLES[style]
    analysis = (sound or {}).get("analysis") or {}
    if st["pace"] != "beat" or len(analysis.get("beats") or []) < 4:
        return None
    try:
        c = _beat_cuts(style, float(length or st["length"]), analysis, [])
    except ValueError:
        return None
    return max(1, c["cap_before"]) if c["kd"] else 0, c["kd"] * c["period"]


def pick_moments(sources: List[Dict[str, Any]], style: str, theme: str, length: int,
                 guidance: str = "", candidates: Optional[List[Dict[str, Any]]] = None,
                 sound: Optional[Dict[str, Any]] = None, look_for: Optional[List[str]] = None) -> Dict[str, Any]:
    """One Claude call → the edit's hook, caption, hashtags and moments in play order.

    `candidates` (moments already found and scored, e.g. by Creator Scan) are
    offered instead of whole transcripts when given. `look_for`: the campaign brief's
    "what to look for" items, word for word."""
    style = style_key(style)
    st = STYLES[style]
    index_of = {s["id"]: i + 1 for i, s in enumerate(sources)}
    if candidates:
        heads = [f"=== VIDEO {i + 1}: {s.get('title') or 'Untitled'}" for i, s in enumerate(sources)]
        body = "\n".join(heads) + "\n\n" + _candidate_block(candidates, index_of)
    else:
        budget = max(8000, 90000 // max(1, len(sources)))
        body = "\n\n".join(_transcript_block(i + 1, s, budget) for i, s in enumerate(sources))
    lo, hi = count_for(style, length)
    w_lo, w_hi = st["window"]
    prompt = (body
              + f"\n\nTHE EDIT: a {st['name']} edit, about {length} seconds long. {st['what']}\n"
              + f"What it's about: {theme.strip() or 'the best moments'}\n"
              + f"Moments: {st['brief'].format(w=f'{w_lo:g} to {w_hi:g} seconds')} Return {lo} to {hi} moments.\n"
              + f"LENGTH: every moment plays {w_lo:g} to {w_hi:g} seconds — ClipAgent cuts a longer one down to its "
                f"punchline on whole words, so give each moment's `hit` (the punchline, the key word, the instant it "
                f"builds to) and keep its start/end tight around it. Together the moments should fill about "
                f"{length} seconds (±15%). Give every moment a `strength` (1-10): when it runs long, the weakest "
                f"are left out first.")
    wants = [str(x).strip() for x in (look_for or []) if str(x).strip()][:12]
    if wants:
        prompt += ("\nWHAT THIS CAMPAIGN WANTS TO SEE (from its brief, word for word) — pick moments that show "
                   "these:\n" + "\n".join(f"- {x[:200]}" for x in wants))
    fit = before_drop(style, sound, length)
    if fit and fit[0]:
        k, secs = fit
        prompt += (f"\nThe song drops {secs:.1f} seconds in: only {k} moment{'s' if k != 1 else ''} play before the "
                   f"drop (the opener{' and the build' if k > 1 else ''}), the drop moment comes next, the rest after.")
    elif fit:
        prompt += "\nThe song drops right at the start: the drop moment plays first, the rest after it."
    system = PICK_SYSTEM + (f"\n\nCAMPAIGN RULES (these override the rest):\n{guidance}" if guidance else "")
    client = highlights._client()
    message = client.messages.create(model=CLAUDE_MODEL, max_tokens=6000, system=system, tools=[PICK_TOOL],
                                     tool_choice={"type": "tool", "name": "pick_moments"},
                                     messages=[{"role": "user", "content": prompt}])
    got = toolio.tool_inputs(message)
    if not got:
        raise RuntimeError("Claude's answer couldn't be read — try again")
    return read_pick(got[0], sources, style, candidates, length)


def read_pick(reply: Dict[str, Any], sources: List[Dict[str, Any]], style: str,
              candidates: Optional[List[Dict[str, Any]]] = None, length: Optional[float] = None) -> Dict[str, Any]:
    """Claude's picks, checked: real times, no overlaps, text that was really said, one drop.
    (Their length is checked later, when the timeline is laid out: see fit_moments.)"""
    style = style_key(style)
    st = STYLES[style]
    words_by = {s["id"]: transcribe.in_order(_loads(s.get("transcript"), {}).get("words") or []) for s in sources}
    all_said = " ".join(" ".join(w.get("w", "") for w in ws) for ws in words_by.values())
    min_len = 0.8 if st["pace"] == "beat" else 1.5
    moments: List[Dict[str, Any]] = []
    notes: List[str] = []
    raw = toolio.coerce(reply.get("moments"))
    for k, m in enumerate(raw if isinstance(raw, list) else []):
        m = toolio.as_dict(m)
        cand = None
        if candidates and m.get("candidate") is not None:
            ci = highlights._int(m.get("candidate"), 0) - 1
            cand = candidates[ci] if 0 <= ci < len(candidates) else None
        hit_given = m.get("hit") is not None
        if cand:
            job = next((s for s in sources if s["id"] == cand["source"]), None)
            start, end = float(cand["start"]), float(cand["end"])
            hit_default = float(cand.get("hit", (start + end) / 2))
            hit_given = hit_given or cand.get("hit") is not None
        else:
            v = highlights._int(m.get("video"), 0) - 1
            job = sources[v] if 0 <= v < len(sources) else None
            try:
                start, end = float(m["start"]), float(m["end"])
            except (KeyError, TypeError, ValueError):
                continue
            hit_default = (start + end) / 2
        if not job:
            continue
        dur = float(job.get("duration") or 0) or 1e9
        start, end = max(0.0, start), min(dur, end)
        if end - start < min_len:
            continue
        if any(o["source"] == job["id"] and min(o["end"], end) - max(o["start"], start) > 0.5 * (end - start)
               for o in moments):
            continue                                         # the same bit twice
        hit = highlights._num(m.get("hit"), hit_default)
        if not start <= hit <= end:
            hit = hit_default if start <= hit_default <= end else (start + end) / 2
            hit_given = False
        text = highlights._text(m.get("text"))[:160]
        said = _said_between(words_by[job["id"]], start - 3, end + 3)
        if text and st["text"] in ("punch", "quote") and not (_words_said(text, said) and _numbers_said(text, said)):
            notes.append(f"Left out the words “{text}” — they aren't what he says there.")
            text = ""
        elif text and not _numbers_said(text, said):
            notes.append(f"Left out the words “{text}” — the number isn't one he says.")
            text = ""
        key = highlights._text(m.get("key"))
        if key and key.lower() not in text.lower():
            key = ""
        strength = highlights._num(m.get("strength"), highlights._num((cand or {}).get("score"), 50.0) / 10.0)
        moments.append({
            "id": f"m{k + 1}", "source": job["id"], "start": round(start, 2), "end": round(end, 2),
            "hit": round(hit, 2), "text": text, "key": key[:40],
            "kind": str(m.get("kind") or "quote"), "drop": bool(m.get("drop")),
            "strength": round(min(10.0, max(1.0, strength)), 1),
            "why": highlights._text(m.get("why"))[:160],
        })
        if not hit_given:
            moments[-1]["hit_auto"] = True                   # no punchline given: the cut finds it from his words
    if not moments:
        raise RuntimeError("Claude didn't find moments that fit — try another theme or more videos")
    lo, hi = count_for(style, length or st["length"])
    if len(moments) > hi:                                    # keep the opener, the drop and the closer
        keep = {0, len(moments) - 1} | {i for i, m in enumerate(moments) if m["drop"]}
        for i in range(len(moments)):
            if len(keep) >= hi:
                break
            keep.add(i)
        moments = [m for i, m in enumerate(moments) if i in keep]
    if not any(m["drop"] for m in moments):
        moments[min(len(moments) - 1, len(moments) // 2)]["drop"] = True
    first = next(i for i, m in enumerate(moments) if m["drop"])
    for i, m in enumerate(moments):                       # only one drop
        m["drop"] = i == first
    hook = re.sub(r"\s+", " ", highlights._text(reply.get("hook"))).strip()[:90]
    if hook and not _numbers_said(hook, all_said):
        notes.append(f"Changed the hook “{hook}” — it had a number he never says.")
        hook = ""
    if not hook:
        hook = next((m["text"] for m in moments if m["drop"] and m["text"]), "") or \
            next((m["text"] for m in moments if m["text"]), "") or highlights._text(reply.get("title"))[:60]
    tags = [str(t).strip().lstrip("#") for t in toolio.as_list(reply.get("hashtags")) if str(t).strip()][:8]
    return {"title": highlights._text(reply.get("title"))[:80], "hook": hook,
            "caption": highlights._text(reply.get("caption"))[:600], "hashtags": tags, "moments": moments,
            "notes": notes}


# --- speed curves ------------------------------------------------------------------------
# A segment's speed over its own time, as keyframes [[t, speed], ...] eased
# with smoothstep in between. The source time used up to t is the integral,
# which smoothstep keeps exact: ∫₀ᵘ (a + (b−a)(3x²−2x³)) dx = a·u + (b−a)(u³ − u⁴/2).

def flat(dur: float, speed: float) -> List[List[float]]:
    return [[0.0, round(speed, 4)], [round(dur, 4), round(speed, 4)]]


def ramp(dur: float, dip: float, fast: float, low: float) -> List[List[float]]:
    """Fast in → slow on the hit at `dip` → fast out."""
    w = min(0.24, 0.28 * dur, dip / 1.5, (dur - dip) / 1.5)
    if w < 0.03:
        return flat(dur, (fast + low) / 2)
    keys = [(0.0, fast), (dip - 1.5 * w, fast), (dip - 0.35 * w, low), (dip + 0.35 * w, low),
            (dip + 1.5 * w, fast), (dur, fast)]
    out: List[List[float]] = []
    for t, v in keys:
        if out and t - out[-1][0] < 1e-4:                  # the same instant twice (same speed too)
            continue
        out.append([round(t, 4), round(v, 4)])
    return out


def drop_curve(dur: float, period: float, low: float, out_speed: float, ramp_out: bool) -> List[List[float]]:
    """Slow-mo from the drop, picking up speed over the last beat (when ramps are on)."""
    if not ramp_out or dur < period * 1.5:
        return flat(dur, low)
    return [[0.0, low], [round(dur - period, 4), low], [round(dur, 4), out_speed]]


def _smooth(u: float) -> float:
    return u * u * (3 - 2 * u)


def speed_at(curve: List[List[float]], t: float) -> float:
    if t <= curve[0][0]:
        return curve[0][1]
    for (t0, a), (t1, b) in zip(curve, curve[1:]):
        if t <= t1:
            return a + (b - a) * _smooth((t - t0) / (t1 - t0)) if t1 > t0 else b
    return curve[-1][1]


def curve_src(curve: List[List[float]], t: float) -> float:
    """Seconds of source used from the segment's start up to t."""
    total = 0.0
    for (t0, a), (t1, b) in zip(curve, curve[1:]):
        if t <= t0:
            break
        span = t1 - t0
        if span <= 0:
            continue
        u = min(1.0, (t - t0) / span)
        total += span * (a * u + (b - a) * (u ** 3 - u ** 4 / 2))
    if t > curve[-1][0]:
        total += (t - curve[-1][0]) * curve[-1][1]
    return total


def curve_time(curve: List[List[float]], dur: float, src: float) -> float:
    """The segment time at which `src` seconds of source have been used (inverse of curve_src)."""
    lo, hi = 0.0, dur
    if src <= 0:
        return 0.0
    if curve_src(curve, dur) <= src:
        return dur
    for _ in range(40):
        mid = (lo + hi) / 2
        if curve_src(curve, mid) < src:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _scaled(curve: List[List[float]], k: float) -> List[List[float]]:
    return [[t, round(max(0.15, v * k), 4)] for t, v in curve]


def src_time(seg: Dict[str, Any], t: float) -> float:
    """Where in the source a segment is `t` seconds after it starts."""
    return seg["src_start"] + curve_src(seg["curve"], t)


# --- the beat grid ----------------------------------------------------------------------

def beat_grid(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """The song's beats, extended past both ends at the same tempo (so cuts
    can be snapped before the music starts or after it stops), with the bar
    phase carried over."""
    found = sorted(float(b) for b in analysis.get("beats") or [])
    if len(found) < 4:
        raise ValueError("This song has no steady beat ClipAgent can find — pick another song")
    gaps = sorted(b - a for a, b in zip(found, found[1:]))
    period = gaps[len(gaps) // 2]
    duration = float(analysis.get("duration") or found[-1] + period)
    before, t = [], found[0] - period
    while t > -90.0:
        before.append(round(t, 4))
        t -= period
    after, t = [], found[-1] + period
    while t < duration + 90.0:
        after.append(round(t, 4))
        t += period
    grid = before[::-1] + found + after
    offset = len(before)
    bars = analysis.get("bars") or []
    phase = 0
    if bars:
        i = min(range(len(found)), key=lambda j: abs(found[j] - bars[0]))
        phase = (i + offset) % 4
    return {"t": grid, "period": period, "phase": phase, "duration": duration}


def _nearest(grid: List[float], t: float) -> int:
    i = bisect.bisect_left(grid, t)
    if i <= 0:
        return 0
    if i >= len(grid):
        return len(grid) - 1
    return i if grid[i] - t < t - grid[i - 1] else i - 1


def _at_or_after(grid: List[float], t: float, tol: float = 0.02) -> int:
    return min(len(grid) - 1, bisect.bisect_left(grid, t - tol))


def _at_or_before(grid: List[float], t: float, tol: float = 0.02) -> int:
    return max(0, bisect.bisect_right(grid, t + tol) - 1)


# --- the timeline -----------------------------------------------------------------------

PACES = {"slower": -1, "normal": 0, "faster": 1}
FLASHES = {"few": "Only on the drop", "normal": "As the style does", "many": "On every cut"}


def _patterns(style: str, period: float, pace: int = 0) -> Tuple[List[int], int, List[int]]:
    """How many beats each cut lasts: before the drop, the drop's hold, after the drop —
    halved for "faster", doubled for "slower" — and never one cut longer than the style's
    longest moment (a slow song halves it)."""
    build, hold, after = _base_patterns(style, period)
    if pace > 0:
        build, hold, after = [max(1, b // 2) for b in build], max(2, hold // 2), [max(1, a // 2) for a in after]
    elif pace < 0:
        build, hold, after = [min(8, b * 2) for b in build], min(16, hold * 2), [min(8, a * 2) for a in after]
    hi = STYLES[style]["window"][1]

    def cap(n: int) -> int:
        while n > 1 and n * period > hi + 1e-6:
            n //= 2
        return n

    return [cap(b) for b in build], cap(hold), [cap(a) for a in after]


def _base_patterns(style: str, period: float) -> Tuple[List[int], int, List[int]]:
    if style == "velocity":
        fast = period < 0.545                                # above ~110 BPM
        return [2], (4 if 4 * period <= 2.4 else 2), ([2, 2, 2, 1, 1] if fast else [2, 1, 1, 2, 1, 1])
    if style == "aura":
        long = 8 if 8 * period <= 4.4 else 4
        return [4], long, [long]
    if style == "flow":
        k = 1 if period >= 0.75 else (2 if period >= 0.375 else 4)       # about one cut a second
        return [k], 2 * k, [k]
    # money
    k = 4 if 4 * period >= 1.6 else 8
    return [k], k, [k]


def _counts(durs: List[float], lo: float, hi: float) -> set:
    """Into how many runs the shots `durs` (in order) can be grouped, every run lo–hi seconds long."""
    reach = [set() for _ in range(len(durs) + 1)]
    reach[0].add(0)
    for j in range(1, len(durs) + 1):
        d = 0.0
        for j0 in range(j - 1, -1, -1):
            d += durs[j0]
            if d > hi + 1e-6:
                break
            if d >= lo - 1e-6 and reach[j0]:
                reach[j] |= {g + 1 for g in reach[j0]}
    return reach[len(durs)]


def _split(durs: List[float], wants: List[float], los: List[float], his: List[float]) -> Optional[List[int]]:
    """Group the shots `durs` into exactly len(wants) runs in order — run k lasting los[k]–his[k]
    seconds, as near wants[k] as the beats allow. → shots per run, or None."""
    n, k = len(wants), len(durs)
    if n == 0:
        return [] if k == 0 else None
    pre = [0.0]
    for d in durs:
        pre.append(pre[-1] + d)
    inf = float("inf")
    best = [[inf] * (k + 1) for _ in range(n + 1)]
    back = [[-1] * (k + 1) for _ in range(n + 1)]
    best[0][0] = 0.0
    for g in range(1, n + 1):
        for j in range(g, k + 1):
            for j0 in range(j - 1, g - 2, -1):
                d = pre[j] - pre[j0]
                if d > his[g - 1] + 1e-6:
                    break
                if d < los[g - 1] - 1e-6 or best[g - 1][j0] == inf:
                    continue
                c = best[g - 1][j0] + (d - wants[g - 1]) ** 2
                if c < best[g][j]:
                    best[g][j], back[g][j] = c, j0
    if best[n][k] == inf:
        return None
    counts, j = [], k
    for g in range(n, 0, -1):
        counts.append(j - back[g][j])
        j = back[g][j]
    return counts[::-1]


def _weakest(moments: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """The n moments to leave out first: never one gs sized himself if it can be helped, then the
    lowest strength, keeping the closer, later ones before earlier ones."""
    last = len(moments) - 1
    order = sorted(range(len(moments)), key=lambda i: (bool(moments[i].get("manual")),
                                                         highlights._num(moments[i].get("strength"), 5.0),
                                                         i == last, -i))
    return [moments[i] for i in order[:max(0, n)]]


def _beat_speed(style: str, fx: Dict[str, bool]) -> float:
    """About how fast a beat style plays its footage (slow-mo styles use less source per second)."""
    if fx.get("slowmo"):
        return {"aura": 0.6, "money": 0.8}.get(style, 1.0)
    return 1.0


def _assign(ordered: List[Dict[str, Any]], di: int, c: Dict[str, Any], style: str, speed: float,
            notes: List[str], left: List[Tuple[Dict[str, Any], str]], move: bool) -> Optional[List[Tuple[Dict[str, Any], int]]]:
    """Which moment plays on which shots of a beat edit: each moment one run of consecutive shots
    lasting its style's window, in Claude's order; the drop moment's run starts on the drop.
    `move`: moments that don't fit before the drop may play right after it. None: these moments
    can't fill this many beats (the caller makes the edit shorter)."""
    lo, hi = STYLES[style]["window"]
    durs, ds = c["durs"], c["ds"]

    def win(m: Dict[str, Any]) -> Tuple[float, float]:
        if m.get("manual"):
            d = (m["end"] - m["start"]) / speed
            return min(lo, d), max(hi, d)
        return lo, hi

    def want(m: Dict[str, Any]) -> float:
        a, b = win(m)
        return min(max((m["end"] - m["start"]) / speed, a), b)

    sec_lo = min(win(m)[0] for m in ordered)
    sec_hi = max(win(m)[1] for m in ordered)
    drop_m = ordered[di]
    before, after = list(ordered[:di]), list(ordered[di + 1:])
    moved = 0
    if ds:
        feas = _counts(durs[:ds], sec_lo, sec_hi)
        if not feas:
            return None
        if len(before) < min(feas):                         # too few to fill the build: borrow the next ones
            while len(before) < min(feas) and after:
                before.append(after.pop(0))
            if len(before) < min(feas):
                return None
        fit = [g for g in feas if g <= len(before)]
        nb = max(fit)
        if nb < len(before):
            if not move:
                return None
            moved = len(before) - nb
            after = before[nb:] + after
            before = before[:nb]
    elif before:
        if not move:
            return None
        moved = len(before)
        after = before + after
        before = []
    best = None
    lo_d, hi_d = win(drop_m)
    total = 0.0
    for r in range(1, len(durs) - ds + 1):
        total += durs[ds + r - 1]
        if total > hi_d + 1e-6:
            break
        if total < lo_d - 1e-6:
            continue
        rest = durs[ds + r:]
        feas_a = _counts(rest, sec_lo, sec_hi) if rest else {0}
        fit = [g for g in feas_a if g <= len(after)]
        if not fit:
            continue
        key = (len(after) - max(fit), r)
        if best is None or key < best[0]:
            best = (key, r, max(fit))
    if best is None:
        return None
    _, r, na = best
    if na < len(after):
        out = {id(m) for m in _weakest(after, len(after) - na)}
        for m in after:
            if id(m) in out:
                left.append((m, "it didn't fit the length"))
        after = [m for m in after if id(m) not in out]
    rest = durs[ds + r:]

    def split(ms: List[Dict[str, Any]], part: List[float]) -> Optional[List[int]]:
        got = _split(part, [want(m) for m in ms], [win(m)[0] for m in ms], [win(m)[1] for m in ms])
        return got if got is not None else _split(part, [want(m) for m in ms], [sec_lo] * len(ms),
                                                  [sec_hi] * len(ms))

    counts_b, counts_a = split(before, durs[:ds]), split(after, rest)
    if counts_b is None or counts_a is None:
        return None
    if moved:
        notes.append(f"{moved} moment{'s' if moved > 1 else ''} didn't fit before the drop, so "
                     f"{'they play' if moved > 1 else 'it plays'} right after it.")
    return list(zip(before, counts_b)) + [(drop_m, r)] + list(zip(after, counts_a))


def _place(m: Dict[str, Any], segs: List[Dict[str, Any]], src_dur: float, mode: str) -> None:
    """Put a moment's run of segments on its footage: the hit on the slowest
    instant (or on the drop), everything inside the moment where it fits."""
    lo, hi = max(0.0, m["start"] - EXTEND), min(src_dur, m["end"] + EXTEND)
    used = [curve_src(s["curve"], s["dur"]) for s in segs]
    total = sum(used)
    if total > hi - lo and total > 0:                       # too little footage: slow the run down to fit
        k = max(0.35, (hi - lo) / total)
        for s in segs:
            s["curve"] = _scaled(s["curve"], k)
        used = [curve_src(s["curve"], s["dur"]) for s in segs]
        total = sum(used)
    if total > hi - lo:                                     # still too long: carry on further into the video
        lo, hi = 0.0, src_dur
    hit = m["hit"]
    if mode == "drop":
        a = hit
    elif mode == "ramp":
        best = None
        before = 0.0
        for s, u in zip(segs, used):
            if s.get("dip") is not None:
                cand = hit - (before + curve_src(s["curve"], s["dip"]))
                if lo - 1e-6 <= cand <= hi - total + 1e-6:
                    if best is None or abs(cand - m["start"]) < abs(best - m["start"]):
                        best = cand
            before += u
        a = best if best is not None else hit - total / 2
    else:
        a = hit - 0.35 * total
    a = min(max(a, lo), max(lo, hi - total))
    a = min(max(0.0, a), max(0.0, src_dur - total))
    for s, u in zip(segs, used):
        s["src_start"] = round(a, 3)
        s["speed"] = round(u / s["dur"], 3) if s["dur"] > 0 else 1.0
        a += u


def _seg(m: Dict[str, Any], at: float, dur: float, curve: List[List[float]], **kw: Any) -> Dict[str, Any]:
    seg = {"moment": m["id"], "source": m["source"], "src_start": m["start"], "curve": curve, "speed": 1.0,
           "at": round(at, 4), "dur": round(dur, 4), "beats": [], "pulses": [], "shakes": [], "flashes": [],
           "glitches": [], "zoom": 1.0, "drop": False, "hit": None, "voice": None, "text": "", "key": "",
           "first": False, "last": False, "dip_in": False, "dip_out": False, "blur_in": False, "words": []}
    seg.update(kw)
    return seg


def section_drop(analysis: Dict[str, Any], a: float, b: float) -> Optional[float]:
    """The moment the song kicks in hardest between song seconds a and b: the song's own drop when it's
    there, else the bar line after which it gets loudest (None when nothing in there kicks in)."""
    drop = float(analysis.get("drop") or -1)
    if a + 1.0 <= drop <= b - 3.0:
        return drop
    energy = analysis.get("energy") or []
    per = 4.0                                                     # energy points a second
    best, score = None, 0.06
    for t in analysis.get("bars") or []:
        if not a + 1.0 <= t <= b - 3.0:
            continue
        i = int(t * per)
        before, after = energy[max(0, i - 8):i], energy[i:i + 8]
        if before and after:
            rise = sum(after) / len(after) - sum(before) / len(before)
            if rise > score:
                best, score = float(t), rise
    return best


def _beat_cuts(style: str, length: float, analysis: Dict[str, Any], notes: List[str], pace: int = 0,
               song_start: Optional[float] = None, *, want_beats: Optional[int] = None,
               pre_beats: Optional[int] = None, budget: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Where a beat edit cuts on this song: the song section, the drop, and every slot (in beats).
    `song_start` (song seconds) picks the part of the song by hand; otherwise it's the part around the drop.
    `want_beats` forces the length in beats, `pre_beats` how many beats play before the drop, and
    `budget` keeps the length inside the edit's length budget (see length_budget)."""
    st = STYLES[style]
    lo_w, hi_w = st["window"]
    g = beat_grid(analysis)
    grid, period, song_len = g["t"], g["period"], g["duration"]
    first_real = next(i for i, t in enumerate(grid) if t >= -0.01)
    if want_beats is None:
        want = max(8, int(round(length / period / 4.0)) * 4)            # whole bars
        if budget:                                                      # …inside ±15 % (and the campaign's limits)
            while want > 8 and want * period > budget["upper"] + 1e-6:
                want -= 4
            while want * period < budget["lower"] - 1e-6 and (want + 4) * period <= budget["upper"] + 1e-6:
                want += 4
    else:
        want = int(want_beats)
    asked = want
    pre_n = st["pre_beats"] if pre_beats is None else int(pre_beats)
    if song_start is not None:
        bars = [i for i in range(first_real, len(grid)) if (i - g["phase"]) % 4 == 0 and grid[i] <= song_len]
        start_i = min(bars, key=lambda i: abs(grid[i] - float(song_start))) if bars else first_real
        while grid[start_i + want] > song_len + 0.05 and start_i - 4 >= first_real:
            start_i -= 4
        if abs(grid[start_i] - float(song_start)) > 4 * period + 0.05:
            notes.append("The song ends soon after the part you picked, so the edit starts a little earlier in it.")
        kick = section_drop(analysis, grid[start_i], grid[min(len(grid) - 1, start_i + want)])
        drop_i = _nearest(grid, kick) if kick is not None else start_i + min(want - 4, 8)
        if kick is None:
            notes.append("Nothing in the part you picked kicks in hard, so the strongest moment lands on bar 3.")
    else:
        drop_i = _nearest(grid, float(analysis.get("drop") or grid[first_real]))
        start_i = max(first_real, drop_i - pre_n)
        while grid[start_i + want] > song_len + 0.05 and start_i - 4 >= first_real:
            start_i -= 4
    shortest = max(8, -int(-7.0 // (4 * period)) * 4)                 # never under ~7 s
    while grid[start_i + want] > song_len + 0.05 and want > shortest:
        want -= 4
    if grid[start_i + want] > song_len + 0.05:
        raise ValueError("This song is too short for an edit — pick a song of at least 10 seconds")
    if want < asked:
        notes.append(f"The song only allows {want * period:.0f} s here, so the edit is that long.")
    kd = drop_i - start_i
    if not 0 <= kd < want - 2:
        kd = min(want - 4, 8)
        notes.append("This song has no clear drop in the part used, so the strongest moment lands on bar 3.")
    build, hold, after = _patterns(style, period, pace)
    cuts, k, i = [0], 0, 0
    while k < kd:
        k = min(kd, k + build[i % len(build)])
        cuts.append(k)
        i += 1
    k = min(want, kd + hold)
    if k > cuts[-1]:
        cuts.append(k)
    i = 0
    while k < want:
        k = min(want, k + after[i % len(after)])
        cuts.append(k)
        i += 1
    if len(cuts) > 3 and cuts[-1] - cuts[-2] < 2 and cuts[-2] != kd and cuts[-2] - cuts[-3] < 4:
        last = grid[start_i + cuts[-1]] - grid[start_i + cuts[-3]]
        if last <= hi_w + 1e-6:
            cuts.pop(-2)                                     # the last shot gets at least two beats
    slots = list(zip(cuts[:-1], cuts[1:]))
    durs = [grid[start_i + b] - grid[start_i + a] for a, b in slots]
    ds = next(j for j, (a, _) in enumerate(slots) if a == kd)
    before = _counts(durs[:ds], lo_w, hi_w) if ds else set()
    return {"grid": grid, "period": period, "phase": g["phase"], "start_i": start_i, "want": want, "kd": kd,
            "s0": grid[start_i], "slots": slots, "ds": ds, "durs": durs, "shortest": shortest,
            "cap_before": max(before) if before else 0, "drop_i": start_i + kd, "first_real": first_real}


def slots_for(style: str, sound: Optional[Dict[str, Any]], length: float, pace: int = 0,
              song_start: Optional[float] = None) -> Optional[Tuple[int, int]]:
    """For a beat edit: (moments that fit before the drop, moments that fit in all) — each moment
    one run of shots lasting the style's window."""
    analysis = (sound or {}).get("analysis") or {}
    style = style_key(style)
    if STYLES[style]["pace"] != "beat" or len(analysis.get("beats") or []) < 4:
        return None
    c = _beat_cuts(style, length, analysis, [], pace, song_start)
    lo, hi = STYLES[style]["window"]
    durs, ds = c["durs"], c["ds"]
    total, after = 0.0, 0
    for r in range(1, len(durs) - ds + 1):                   # the drop's run, then as many runs as fit after it
        total += durs[ds + r - 1]
        if total > hi + 1e-6:
            break
        if total >= lo - 1e-6:
            rest = durs[ds + r:]
            after = max(after, max(_counts(rest, lo, hi) if rest else {0}, default=0))
    return c["cap_before"], c["cap_before"] + 1 + after


def _beat_plan(ordered: List[Dict[str, Any]], style: str, length: float, analysis: Dict[str, Any],
               fx: Dict[str, bool], notes: List[str], left: List[Tuple[Dict[str, Any], str]], pace: int,
               song_start: Optional[float], budget: Optional[Dict[str, float]]) -> Tuple[Dict[str, Any], list]:
    """The song section and which moment plays on which shots, every moment inside its window:
    the longest edit (up to the length asked) these moments can fill; when Claude put more moments
    before the drop than its usual build holds, the song starts up to one build earlier so they
    keep their order — else they play right after the drop."""
    st = STYLES[style]
    drop_m = next(m for m in ordered if m.get("drop"))
    di = ordered.index(drop_m)
    speed = _beat_speed(style, fx)
    c0 = _beat_cuts(style, length, analysis, [], pace, song_start, budget=budget)
    want0, shortest, period = c0["want"], c0["shortest"], c0["period"]
    lower = (budget or {}).get("lower", 0.0)
    base = st["pre_beats"]
    tries = ([(p, False) for p in range(base, 2 * base + 1, 4)] + [(base, True), (0, True)]
             if song_start is None else [(None, True)])
    ids = [m["id"] for m in ordered]
    found = []
    for want in range(want0, min(want0, shortest) - 1, -4):
        seen = set()
        for pre, move in tries:
            cn: List[str] = []
            try:
                c = _beat_cuts(style, length, analysis, cn, pace, song_start, want_beats=want, pre_beats=pre)
            except ValueError:
                continue
            if (c["want"], c["kd"], move) in seen or (c["ds"] and not c["cap_before"]):
                continue                                     # the same cut again, or no room for one shot before the drop
            seen.add((c["want"], c["kd"], move))
            ln: List[Tuple[Dict[str, Any], str]] = []
            runs = _assign(ordered, di, c, style, speed, cn, ln, move)
            if runs is None:
                continue
            played = [m["id"] for m, _ in runs]
            kept = played == [i for i in ids if i in played]
            # best first: within the length budget, in the order given, the longest, the shortest build
            found.append(((c["want"] * period >= lower - 1e-6, kept, c["want"], -c["kd"]), c, runs, cn, ln))
            break                                            # the first that works is the best for this length
        if found and max(f[0][:2] for f in found) == (True, True):
            break
        if found and want * period < lower - 1e-6:
            break                                            # nothing longer is left to find
    if not found:
        n = len(ordered)
        raise ValueError(f"There aren't enough moments for {_a(st['name'], low=True)} edit: each one plays "
                         f"{window_text(style)}, and only {n} {'is' if n == 1 else 'are'} on. Switch more moments "
                         "on, or press New moments.")
    _, c, runs, cn, ln = max(found, key=lambda f: f[0])
    if c["kd"] == 0 and c0["kd"] > 0:
        cn.append("The song drops too early for a shot before it, so the edit starts right on the drop."
                  if not c0["cap_before"] else
                  "To fill the length with these moments, the edit starts right on the drop.")
    if c["want"] < want0 and c["want"] < c0["want"]:
        cn.append(f"Only {len(runs)} moment{'s' if len(runs) != 1 else ''} could play, and in "
                  f"{_a(st['name'], low=True)} edit each one plays {window_text(style)} — so the edit is "
                  f"{c['want'] * period:.0f} s instead of {want0 * period:.0f} s. Switch more moments on or press "
                  "New moments for a full-length one.")
    notes.extend(cn)
    left.extend(ln)
    return c, runs


def _beat_timeline(ordered: List[Dict[str, Any]], style: str, length: float, analysis: Dict[str, Any],
                   fx: Dict[str, bool], durations: Dict[str, float], notes: List[str], pace: int = 0,
                   flashes: str = "normal", song_start: Optional[float] = None,
                   budget: Optional[Dict[str, float]] = None,
                   left: Optional[List[Tuple[Dict[str, Any], str]]] = None) -> Dict[str, Any]:
    st = STYLES[style]
    c, runs = _beat_plan(ordered, style, length, analysis, fx, notes, left if left is not None else [], pace,
                         song_start, budget)
    grid, period, start_i, want, kd, s0 = c["grid"], c["period"], c["start_i"], c["want"], c["kd"], c["s0"]
    slots, ds = c["slots"], c["ds"]
    drop_i = start_i + kd

    def is_bar(i: int) -> bool:
        return (i - c["phase"]) % 4 == 0

    segments: List[Dict[str, Any]] = []
    j = 0
    use_text = fx.get("text") and st["text"] in ("punch", "quote")
    for m, count in runs:
        run_segs = []
        for n in range(count):
            ka, kb = slots[j]
            a, b = grid[start_i + ka] - s0, grid[start_i + kb] - s0
            dur = b - a
            is_drop = ka == kd
            inner = [grid[start_i + x] - s0 - a for x in range(ka, kb)]
            dip = None
            if is_drop:
                if fx.get("slowmo"):
                    low = {"velocity": 0.4, "flow": 0.55, "aura": 0.5}.get(style, 0.7)
                    curve = drop_curve(dur, period, low, 1.6 if style == "velocity" else 1.3,
                                       bool(fx.get("ramp")) and style in ("velocity", "flow"))
                else:
                    curve = flat(dur, 1.0)
            elif style in ("velocity", "flow") and fx.get("ramp") and kb - ka >= 2:
                dip = inner[(kb - ka) // 2]
                fast, low = (1.7, 0.4) if style == "velocity" else (1.45, 0.65)
                curve = ramp(dur, dip, fast, low)
            elif style in ("velocity", "flow"):
                curve = flat(dur, 1.15 if fx.get("ramp") else 1.0)
            elif style == "aura":
                curve = flat(dur, 0.6 if fx.get("slowmo") else 1.0)
            else:                                            # money
                curve = flat(dur, 0.8 if fx.get("slowmo") else 1.0)
            on_bar = is_bar(start_i + ka)
            seg = _seg(m, a, dur, curve, drop=is_drop, dip=dip, first=n == 0, last=n == count - 1,
                       beats=[round(x, 4) for x in inner])
            if fx.get("pulse"):
                if style == "velocity":
                    seg["pulses"] = [round(x, 4) for x in inner]
                elif style == "flow" and (on_bar or is_drop):
                    seg["pulses"] = [0.0]
            if fx.get("flash"):
                if flashes == "many":
                    big = is_drop or (ka > 0 and n == 0)
                elif flashes == "few":
                    big = is_drop
                else:
                    big = is_drop or (on_bar and ka > 0 and (style == "velocity" or
                                                             (style == "flow" and (ka - kd) % 16 == 0)))
                if big:
                    seg["flashes"] = [0.0]
            if fx.get("shake") and (is_drop or (style == "flow" and on_bar and ka > 0)):
                seg["shakes"] = [0.0]
            if fx.get("glitch") and is_drop:
                seg["glitches"] = [0.0]
            if fx.get("blur") and ka > 0:
                seg["blur_in"] = True
            if style in ("velocity", "flow") and count > 1 and n % 2 == 1 and fx.get("pulse"):
                seg["zoom"] = 1.14                           # a punch-in on the beat inside one shot
            if use_text and n == 0 and m.get("text") and (is_drop or a >= HOOK_SECONDS - 0.05):
                seg["text"], seg["key"] = m["text"], m.get("key", "")
            run_segs.append(seg)
            j += 1
        if st.get("dip"):
            run_segs[0]["dip_in"] = bool(segments) and not run_segs[0]["drop"]   # the drop hits, never fades in
            run_segs[-1]["dip_out"] = True
        _place(m, run_segs, durations.get(m["source"], 1e9),
               "drop" if run_segs[0]["drop"] else ("ramp" if any(s.get("dip") is not None for s in run_segs)
                                                    else "even"))
        segments.extend(run_segs)
    segments[-1]["dip_out"] = False
    for a, b in zip(segments, segments[1:]):
        if b["drop"]:
            a["dip_out"] = False                            # straight into the drop
    total = grid[start_i + want] - s0
    return {"segments": segments, "length": total, "drop_at": grid[start_i + kd] - s0,
            "music": {"start": round(s0, 4), "end": round(s0 + total, 4), "at": 0.0,
                      "drop_at": round(grid[start_i + kd] - s0, 4), "fade_out": 0.0}}


def _speech_timeline(chosen: List[Dict[str, Any]], style: str, analysis: Optional[Dict[str, Any]],
                     fx: Dict[str, bool], durations: Dict[str, float], notes: List[str],
                     flashes: str = "normal", song_start: Optional[float] = None) -> Dict[str, Any]:
    """Voice edits: each moment (already cut to length — see _fit_speech) plays whole at 1×, in order;
    with a song, its drop meets the drop moment's hit and every cut waits for the next beat."""
    st = STYLES[style]
    drop_m = next((m for m in chosen if m.get("drop")), None) or chosen[len(chosen) // 2]

    def text_for(m: Dict[str, Any]) -> Tuple[str, str]:
        if not fx.get("text"):
            return "", ""
        return m.get("text") or "", m.get("key") or ""

    segments: List[Dict[str, Any]] = []
    if not analysis:
        at = 0.0
        for n, m in enumerate(chosen):
            span = m["end"] - m["start"]
            hold = 0.35 if n == len(chosen) - 1 else 0.0
            text, key = text_for(m)
            segments.append(_seg(m, at, span + hold, flat(span + hold, 1.0), src_start=m["start"], speed=1.0,
                                 drop=m is drop_m, hit=round(m["hit"] - m["start"], 3), voice=[0.0, round(span, 3)],
                                 text=text, key=key, first=True, last=True))
            at += span + hold
        result = {"segments": segments, "length": at, "music": None,
                  "drop_at": next(s["at"] + s["hit"] for s in segments if s["drop"])}
    else:
        g = beat_grid(analysis)
        grid, period = g["t"], g["period"]
        kick = float(analysis.get("drop") or 0.0)
        if song_start is not None:                            # the part picked by hand: its strongest kick-in
            found = section_drop(analysis, float(song_start), float(song_start) + 40.0)
            if found is None:
                notes.append("Nothing in the part of the song you picked kicks in hard, so its first bar is used.")
            kick = found if found is not None else float(song_start)
        drop = grid[_nearest(grid, kick)]
        di = chosen.index(drop_m)
        song: List[Dict[str, Any]] = [{} for _ in chosen]      # song-time start, end and pre-roll per moment

        hit_rel = drop_m["hit"] - drop_m["start"]
        s_start = grid[_at_or_before(grid, drop - hit_rel, 0.0)]
        pre = (drop - hit_rel) - s_start
        span = drop_m["end"] - drop_m["start"]
        song[di] = {"start": s_start, "pre": pre, "end": grid[_at_or_after(grid, s_start + pre + span, 0.0)]}
        for n in range(di + 1, len(chosen)):                  # after the drop: hold to the next beat
            span = chosen[n]["end"] - chosen[n]["start"]
            s = song[n - 1]["end"]
            song[n] = {"start": s, "pre": 0.0, "end": grid[_at_or_after(grid, s + span, 0.0)]}
        for n in range(di - 1, -1, -1):                       # before it: start on the beat before, a breath early
            span = chosen[n]["end"] - chosen[n]["start"]
            e = song[n + 1]["start"]
            s = grid[_at_or_before(grid, e - span, 0.0)]
            song[n] = {"start": s, "pre": (e - span) - s, "end": e}
        s0 = song[0]["start"]
        end = song[-1]["end"]
        if fx.get("loop"):                                    # end on a bar (or half bar) so the music loops too
            ei = _nearest(grid, end)
            for every in (4, 2, 1):
                cand = next((x for x in range(ei, ei + 9) if (x - _nearest(grid, s0)) % every == 0), None)
                if cand is not None and grid[cand] - end <= 1.25:
                    end = grid[cand]
                    break
            song[-1]["end"] = end
        for n, m in enumerate(chosen):
            sp = song[n]
            span = m["end"] - m["start"]
            at, dur = sp["start"] - s0, sp["end"] - sp["start"]
            text, key = text_for(m)
            seg = _seg(m, at, dur, flat(dur, 1.0), src_start=round(m["start"] - sp["pre"], 3), speed=1.0,
                       drop=m is drop_m, hit=round(sp["pre"] + m["hit"] - m["start"], 3),
                       voice=[round(sp["pre"], 3), round(sp["pre"] + span, 3)], text=text, key=key,
                       first=True, last=True,
                       beats=[round(b - s0 - at, 4) for b in grid if sp["start"] + 0.05 < b < sp["end"] - 0.05])
            segments.append(seg)
        total = end - s0
        song_len = g["duration"]
        music = {"start": round(max(0.0, s0), 4), "at": round(max(0.0, -s0), 4), "drop_at": round(drop - s0, 4),
                 "end": round(min(song_len, s0 + total), 4), "fade_out": 0.0}
        if s0 < 0:
            notes.append("The music comes in a moment after the start, so its drop meets the best line.")
        if s0 + total > song_len:
            music["fade_out"] = 1.0
            notes.append("The song ends before the edit does, so the music fades out at the end.")
        result = {"segments": segments, "length": total, "music": music, "drop_at": drop - s0}
    segs = result["segments"]
    for n, seg in enumerate(segs):
        if st.get("dip"):
            seg["dip_in"], seg["dip_out"] = n > 0, n < len(segs) - 1
        if style == "funny" and seg["hit"] is not None:
            if fx.get("pulse"):
                seg["pulses"] = [seg["hit"]]
            if fx.get("shake"):
                seg["shakes"] = [seg["hit"]]
        if fx.get("flash") and (seg["drop"] or (flashes == "many" and n > 0)):
            seg["flashes"] = [seg["hit"]] if seg["drop"] else [0.0]
    return result


def _words_for(seg: Dict[str, Any], words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The words heard in a segment, on the edit's clock."""
    a = seg["src_start"]
    used = curve_src(seg["curve"], seg["dur"])
    lo, hi = a, a + used
    if seg.get("voice"):
        lo, hi = a + curve_src(seg["curve"], seg["voice"][0]), a + curve_src(seg["curve"], seg["voice"][1])
    out = []
    for w in words:
        if w["start"] < lo - 0.02 or w["start"] >= hi:
            continue
        t0 = curve_time(seg["curve"], seg["dur"], w["start"] - a)
        t1 = curve_time(seg["curve"], seg["dur"], min(w["end"], hi) - a)
        out.append({"w": w["w"], "t": round(seg["at"] + t0, 3), "end": round(seg["at"] + max(t1, t0 + 0.05), 3)})
    return out


def _fit_speech(usable: List[Dict[str, Any]], preps: List[Dict[str, Any]], style: str, budget: Dict[str, float],
                analysis: Optional[Dict[str, Any]], fx: Dict[str, bool], durations: Dict[str, float],
                flashes: str, song_start: Optional[float], notes: List[str],
                left: List[Tuple[Dict[str, Any], str]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Voice edits: every moment's length chosen together (fit_budget) so the finished edit — with the
    beat holds a song adds — lands within ±15 % of the length asked; tried again with the real holds."""
    period = beat_grid(analysis)["period"] if analysis else 0.0
    over = (len(usable) + 1) * period * 0.5 + 0.6 if analysis else 0.35
    lower, upper, L = budget["lower"], budget["upper"], budget["length"]
    best = None
    slack = 0.02 * len(usable)                                 # cut edges are rounded to the hundredth, outwards
    for _ in range(5):
        picks, _spoken = fit_budget(preps, usable, budget["target"] - over, max(0.0, lower - over),
                                    upper - over - slack)
        chosen = [cut_moment(m, p, pk[0], pk[1]) for m, p, pk in zip(usable, preps, picks) if pk]
        tn: List[str] = []
        built = _speech_timeline(chosen, style, analysis, fx, durations, tn, flashes, song_start)
        total = built["length"]
        miss = 0.0 if lower - 1e-6 <= total <= upper + 1e-6 else min(abs(total - lower), abs(total - upper))
        if best is None or miss < best[0]:
            best = (miss, built, chosen, picks, tn)
        if not miss:
            break
        over = total - sum(m["end"] - m["start"] for m in chosen)       # the holds this layout really has
    _, built, chosen, picks, tn = best
    notes.extend(tn)
    for m, pk in zip(usable, picks):
        if pk is None:
            left.append((m, f"to keep the edit near {L:.0f} s"))
    total = built["length"]
    if total < lower - 1e-6:
        notes.append(f"The moments only make {total:.0f} s — less than the {L:.0f} s asked, even with each one as "
                     f"long as {_a(STYLES[style]['name'], low=True)} moment can be ({window_text(style)}). Switch "
                     "more moments on or press New moments for a full-length edit.")
    elif total > upper + 1e-6:
        notes.append(f"The moments you sized by hand need {total:.0f} s — more than this {L:.0f} s edit allows. "
                     "Shorten one of them or make the edit longer.")
    return built, chosen


def build_timeline(moments: List[Dict[str, Any]], style: str, length: float,
                   sound: Optional[Dict[str, Any]], effects: Optional[Dict[str, Any]],
                   words_by_source: Dict[str, List[Dict[str, Any]]],
                   voice_level: Optional[float] = None, music_level: Optional[float] = None,
                   durations: Optional[Dict[str, float]] = None, grade: Optional[str] = None,
                   hook: str = "", pace: Any = 0, flashes: str = "normal",
                   song_start: Optional[float] = None, limits: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Where every moment sits in the edit: each cut to its style's window on word boundaries, the
    whole within ±15 % of the length asked (and inside a campaign's `limits` {min, max, name}), cut on
    the beat, the best one on the drop, ending on a bar line. Pure: the words come in, nothing is read.
    The result's `moments` says how each moment was cut, `left_out` which ones didn't fit and why."""
    style = style_key(style)
    st = STYLES[style]
    fx = {**st["effects"], **{k: bool(v) for k, v in (effects or {}).items() if k in EFFECTS}}
    analysis = (sound or {}).get("analysis") or None
    ordered = [dict(m) for m in moments if not m.get("off")]
    if not ordered:
        raise ValueError("Every moment is switched off — turn at least one back on")
    if sum(1 for m in ordered if m.get("drop")) != 1:          # exactly one drop, wherever the user moved it
        first = next((i for i, m in enumerate(ordered) if m.get("drop")), len(ordered) // 2)
        for i, m in enumerate(ordered):
            m["drop"] = i == first
    notes: List[str] = []
    durations = durations or {}
    pace = PACES.get(pace, pace) if isinstance(pace, str) else int(pace or 0)
    pace = max(-1, min(1, pace))
    flashes = flashes if flashes in FLASHES else "normal"
    if st["pace"] == "beat" and (not analysis or not analysis.get("beats")):
        raise ValueError(f"{_a(st['name'])} edit is cut to music — pick a song first")
    budget = length_budget(float(length), limits, notes)
    # every moment cut to the style's window, on word boundaries, around its punchline
    srcs = {sid: word_source(words_by_source.get(sid) or []) for sid in {m["source"] for m in ordered}}
    left: List[Tuple[Dict[str, Any], str]] = []
    usable, preps = [], []
    for m in ordered:
        prep = prepare_moment(m, style, srcs[m["source"]], float(durations.get(m["source"]) or 1e9))
        if prep["opts"]:
            usable.append(m)
            preps.append(prep)
        else:
            left.append((m, prep["why"]))
    if not usable:
        raise ValueError(f"None of the moments can be cut cleanly to {_a(st['name'], low=True)} moment's length "
                         f"({window_text(style)}) — press New moments to pick again.")
    if not any(m.get("drop") for m in usable):
        lead = max(usable, key=lambda m: highlights._num(m.get("strength"), 5.0))
        for m in usable:
            m["drop"] = m is lead
        notes.append(f"The drop moment couldn't be cut cleanly, so {_label(lead)} is on the drop now.")
    if st["pace"] == "beat":
        fitted = [cut_moment(m, p) for m, p in zip(usable, preps)]
        built = _beat_timeline(fitted, style, budget["length"], analysis, fx, durations, notes, pace, flashes,
                               song_start, budget, left)
    else:
        built, fitted = _fit_speech(usable, preps, style, budget, analysis if analysis and analysis.get("beats")
                                    else None, fx, durations, flashes, song_start, notes, left)
    segments = built["segments"]
    motion = {m["id"]: m.get("motion") for m in ordered}
    for i, seg in enumerate(segments):
        seg["words"] = _words_for(seg, words_by_source.get(seg["source"]) or [])
        seg.pop("dip", None)
        if seg.get("blur_in") and i > 0:                      # the cut's streak follows the motion through it
            vec = motionmatch.blur_vector(motion.get(segments[i - 1]["moment"]), motion.get(seg["moment"]))
            if vec:
                seg["blur_vec"] = vec
    # how each moment ended up, in plain words where it matters
    shown: Dict[str, float] = {}
    for seg in segments:
        shown[seg["moment"]] = shown.get(seg["moment"], 0.0) + seg["dur"]
    played = [{"id": m["id"], "source": m["source"], "start": m["start"], "end": m["end"], "hit": m["hit"],
               "pick": m.get("pick"),
               "shown": round(shown[m["id"]], 2), "manual": bool(m.get("manual"))}
              for m in fitted if m["id"] in shown]
    lo, hi = st["window"]
    cut_down = sum(1 for m in fitted if m["id"] in shown and not m.get("manual") and m.get("pick")
                   and m["pick"][1] - m["pick"][0] - (m["end"] - m["start"]) > 1.0)
    if cut_down:
        notes.insert(0, f"Cut {cut_down} long moment{'s' if cut_down > 1 else ''} down to the punchline — "
                        f"{_a(st['name'], low=True)} moment plays {window_text(style)}.")
    for m in fitted:
        d = m["end"] - m["start"]
        if m.get("manual") and m["id"] in shown and not lo - 0.05 <= d <= hi + 0.05:
            notes.append(f"{_label(m)[0].upper() + _label(m)[1:]} plays {d:.1f} s — "
                         f"{'longer' if d > hi else 'shorter'} than {_a(st['name'], low=True)} moment usually "
                         f"does ({window_text(style)}), because you asked for it.")
    for m, why in left:
        notes.append(f"Left out {_label(m)} — {why}.")
    total = round(built["length"], 4)
    vl = st["voice"] if voice_level is None else max(0.0, min(1.5, float(voice_level)))
    ml = st["music"] if music_level is None else max(0.0, min(1.5, float(music_level)))
    music = built.get("music")
    if music:
        music.update({"sound": (sound or {}).get("id"), "level": ml})
    hook = (hook or "").strip() if fx.get("text") else ""
    return {
        "version": 2, "style": style, "fps": FPS, "length": total, "segments": segments, "music": music,
        "voice": vl, "effects": fx, "grade": grade if grade in GRADES else st["grade"], "text": st["text"],
        "hook": {"text": hook, "start": 0.0, "end": round(min(HOOK_SECONDS, total), 3)} if hook else None,
        "loop": LOOP_SECONDS if fx.get("loop") and total > 3 else 0.0,
        "cuts": [s["at"] for s in segments],
        "drop_at": None if built.get("drop_at") is None else round(built["drop_at"], 4),
        "notes": notes, "moments": played,
        "left_out": [{"id": m["id"], "why": why} for m, why in left],
        "budget": {k: round(v, 2) for k, v in budget.items()}, "window": [lo, hi],
    }


# --- the whole edit ---------------------------------------------------------------------

def words_for_sources(source_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    out = {}
    for sid in set(source_ids):
        job = store.get_job(sid) or {}
        out[sid] = transcribe.in_order(_loads(job.get("transcript"), {}).get("words") or [])
    return out


def durations_for(source_ids: List[str]) -> Dict[str, float]:
    return {sid: float((store.get_job(sid) or {}).get("duration") or 0) or 1e9 for sid in set(source_ids)}


def _rules(settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    camp = store.get_campaign(settings.get("campaign_id") or "") if settings.get("campaign_id") else None
    return camp["rulebook"] if camp else None


def campaign_fit(rules: Dict[str, Any], style: str) -> Tuple[Dict[str, bool], List[str], str]:
    """What a campaign's brief lets an edit do: (effects it forces off, notes saying why, a refusal or "")."""
    from . import campaign
    r = campaign.resolve(rules)
    ok = r["allowed"]
    name = r["name"] or "This campaign"
    st = STYLES[style_key(style)]
    if not ok["stitch"]:
        said = campaign.explain(rules, "stitch")[1] == "brief"
        return {}, [], (f"{name}: " + ("the brief doesn't allow joining different moments"
                                       if said else "the brief doesn't say whether joining different moments is "
                                                    "allowed, so it's off")
                        + ", and an edit is made of several. If you're sure the brief allows it, open Campaigns → "
                          f"{r['name'] or 'this campaign'} → Rules and set “Join different moments” to Allow — "
                          "or make clips instead.")
    if not ok["crop"]:
        return {}, [], (f"{name}: the brief doesn't allow cropping the picture, and an edit is cut to vertical. "
                        "Make clips with the whole frame kept instead.")
    off: Dict[str, bool] = {}
    notes: List[str] = []
    if not ok["speed"]:
        off.update(ramp=False, slowmo=False, smooth=False)
        notes.append(f"{name}: the brief doesn't allow speed changes, so slow-mo and speed ramps are off. "
                     "If it does, switch “Speed changes” on in the campaign's rules.")
    if not ok["zoom"]:
        off.update(pulse=False, shake=False, push=False)
        notes.append(f"{name}: the brief doesn't allow zooms or camera moves, so zoom punches, shake and push-ins "
                     "are off.")
    if not ok["hook"]:
        off.update(text=False)
        notes.append(f"{name}: the brief doesn't allow text on screen, so the edit has none.")
    elif not ok["captions"] and st["text"] in ("subtitle", "build"):
        off.update(text=False)
        notes.append(f"{name}: the brief doesn't allow captions, so his words aren't shown.")
    if not ok["borders"]:
        off.update(letterbox=False)
    if r["captions_required"] and st["text"] not in ("subtitle", "build"):
        notes.append(f"{name}: the brief requires captions — pick Cinematic or Motivation, which show his words.")
    return off, notes, ""


def check_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Check an edit request; return it cleaned, or raise ValueError in plain words."""
    from . import campaign
    style = style_key(settings.get("style"))
    st = STYLES[style]
    sources = [s for s in (settings.get("sources") or []) if store.get_job(s)]
    if not sources:
        raise ValueError("Pick at least one video to cut the edit from")
    for s in sources:
        job = store.get_job(s) or {}
        if not job.get("source_path") or not Path(job["source_path"]).is_file():
            raise ValueError(f"“{job.get('title') or s}” isn't on this PC any more — make clips from it again first")
        if not job.get("transcript"):
            raise ValueError(f"“{job.get('title') or s}” has no words yet — wait until its clips are made")
    sound = store.get_sound(settings.get("sound") or "") if settings.get("sound") else None
    if settings.get("sound") and not sound:
        raise ValueError("That song isn't in your songs any more — pick another one")
    if sound and not Path(sound.get("file") or "").is_file():
        raise ValueError(f"The file for “{sound['name']}” is missing — add the song again")
    if st.get("needs_music") and not sound:
        raise ValueError(f"{_a(st['name'])} edit is cut to music — add or pick a song first")
    camp_id = str(settings.get("campaign_id") or "")
    if camp_id:
        camp = store.get_campaign(camp_id)
        if not camp:
            raise ValueError("That campaign doesn't exist any more")
        _, _, refusal = campaign_fit(camp["rulebook"], style)
        if refusal:
            raise ValueError(refusal)
        if sound and not campaign.allowed(camp["rulebook"], "music"):
            raise ValueError(f"{camp['name']}: the brief doesn't allow added music. If it does, switch music on in "
                             "the campaign's rules" + (", or pick “No music”." if st.get("music_optional") else
                                                       ". Or pick a style that works without music: Cinematic, "
                                                       "Motivation or Funny."))
    try:
        length = int(settings.get("length") or st["length"])
    except (TypeError, ValueError):
        length = st["length"]
    out = {"style": style, "sources": sources, "sound": sound["id"] if sound else "",
           "theme": str(settings.get("theme") or "")[:300], "length": max(10, min(60, length)),
           "effects": {k: bool(v) for k, v in (settings.get("effects") or {}).items() if k in EFFECTS},
           "grade": settings.get("grade") if settings.get("grade") in GRADES else "",
           "pace": settings.get("pace") if settings.get("pace") in PACES else "normal",
           "flashes": settings.get("flashes") if settings.get("flashes") in FLASHES else "normal",
           "song_start": None, "campaign_id": camp_id}
    if sound and settings.get("song_start") not in (None, "", "auto"):
        try:
            out["song_start"] = round(max(0.0, min(float(sound.get("duration") or 0) - 5.0,
                                                   float(settings["song_start"]))), 2)
        except (TypeError, ValueError):
            pass
    for key in ("voice", "music"):
        if settings.get(key) is not None:
            out[key] = max(0.0, min(1.5, float(settings[key])))
    return out


def create(settings: Dict[str, Any], plan: Optional[Dict[str, Any]] = None, title: str = "") -> str:
    """Check the request and start making the edit in the background. A `plan`
    (moments already chosen) skips Claude — used for versions and for moments
    sent from elsewhere."""
    clean = check_settings(settings)
    titles = [(store.get_job(s) or {}).get("title") or "" for s in clean["sources"]]
    eid = store.create_edit(title or f"{STYLES[clean['style']]['name']} edit — {titles[0][:50]}", clean,
                            clean["campaign_id"])
    if plan:
        plan = dict(plan)
        post = plan.get("post") or {}
        rules = _rules(clean)
        if rules:                                     # a campaign's own lines and hashtags, whatever came in
            plan["post"] = _post(rules, post.get("caption") or "", post.get("hashtags") or [])
        store.update_edit(eid, plan=plan, caption=(plan.get("post") or {}).get("caption", ""),
                          hashtags=(plan.get("post") or {}).get("hashtags", []))
    threading.Thread(target=run, args=(eid, not (plan and plan.get("moments"))), daemon=True).start()
    return eid


def _stage(eid: str, stage: str, progress: int) -> None:
    store.update_edit(eid, stage=stage, progress=max(0, min(100, progress)), status="running")


def _post(rules: Optional[Dict[str, Any]], caption: str, tags: List[str]) -> Dict[str, Any]:
    """What to paste when posting: a campaign's own lines and hashtags first, when it's for one."""
    from . import campaign
    if rules:
        return campaign.build_post(rules, 0, "", extra=caption, own_tags=tags)
    tags = [t.lstrip("#") for t in tags if t]
    tail = " ".join("#" + t for t in tags)
    return {"text": (caption + ("\n\n" + tail if tail else "")).strip(), "caption": caption, "hashtags": tags,
            "mentions": [], "line": "", "platform": "", "checklist": []}


def plan_edit(eid: str, repick: bool) -> Dict[str, Any]:
    """Pick the moments (unless re-making) and lay out the timeline; stores and returns the plan."""
    from . import campaign
    edit = store.get_edit(eid)
    s = edit["settings"]
    plan = dict(edit.get("plan") or {})
    sources = [x for x in (store.get_job(j) for j in s["sources"]) if x]
    rules = _rules(s)
    words = words_for_sources([x["id"] for x in sources])
    sound = store.get_sound(s.get("sound") or "") if s.get("sound") else None
    if repick or not plan.get("moments"):
        _stage(eid, "Picking the moments", 8)
        look_for = [str(x) for x in (rules or {}).get("look_for") or [] if str(x).strip()]
        picked = pick_moments(sources, s["style"], s.get("theme", ""), s["length"],
                              campaign.picker_guidance(rules) if rules else "", plan.get("candidates"), sound,
                              look_for)
        plan.update({"moments": picked["moments"], "title": picked["title"], "hook": picked["hook"],
                     "pick_notes": picked["notes"], "post": _post(rules, picked["caption"], picked["hashtags"])})
        if s["style"] == "flow":
            plan["moments"], more = match_motion(eid, plan["moments"], sources, sound, s["length"],
                                                 s.get("pace", "normal"))
            plan["pick_notes"] = plan["pick_notes"] + more
        store.update_edit(eid, title=picked["title"] or edit["title"], caption=plan["post"]["caption"],
                          hashtags=plan["post"]["hashtags"])
    if not plan.get("post"):
        plan["post"] = _post(rules, edit.get("caption") or "", edit.get("hashtags") or [])
    fx = dict(s.get("effects") or {})
    fit_notes: List[str] = []
    if rules:
        off, fit_notes, refusal = campaign_fit(rules, s["style"])
        if refusal:
            raise ValueError(refusal)
        fx.update(off)
        said = (plan.get("hook") or "", plan["post"].get("caption") or "")
        if plan.get("tone_for") != list(said):            # the words changed: check them against the brief again
            _stage(eid, "Checking the words against the campaign's rules", 20)
            tone = campaign.check_text(rules, [{"id": 0, "hook": said[0],
                                                "extra": said[1].replace(plan["post"].get("line") or "", "").strip()}])
            plan["tone"], plan["tone_for"] = tone.get(0), list(said)
    _stage(eid, "Fitting it to the beat" if sound else "Laying out the moments", 22)
    timeline = build_timeline(plan["moments"], s["style"], s["length"], sound, fx, words,
                              s.get("voice"), s.get("music"), durations_for(s["sources"]), s.get("grade"),
                              plan.get("hook", ""), s.get("pace", "normal"), s.get("flashes", "normal"),
                              s.get("song_start"), length_limits(rules))
    timeline["notes"] = fit_notes + timeline["notes"]
    plan["timeline"] = timeline
    plan["moments"] = keep_cuts(plan["moments"], timeline)
    store.update_edit(eid, plan=plan)
    return plan


def length_limits(rules: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A campaign's own shortest / longest (from its brief), for the length budget."""
    if not rules:
        return None
    from . import campaign
    r = campaign.resolve(rules)
    return {"min": r.get("min_len"), "max": r.get("max_len"), "name": r.get("name") or "The campaign"}


def keep_cuts(moments: List[Dict[str, Any]], timeline: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Store how each moment was cut (what plays, how long it's on screen, or why it was left out),
    keeping what it was cut from (`pick`) so a later re-make — another length, another style — cuts
    it again from the whole of it."""
    played = {x["id"]: x for x in timeline.get("moments") or []}
    left = {x["id"]: x["why"] for x in timeline.get("left_out") or []}
    out = []
    for m in moments:
        m = dict(m)
        m.setdefault("pick", [m["start"], m["end"], m.get("hit")])
        m.pop("shown", None)
        m.pop("left_out", None)
        if m["id"] in played:
            x = played[m["id"]]
            m.update(start=x["start"], end=x["end"], hit=x["hit"], shown=x["shown"])
            if x.get("pick"):
                m["pick"] = x["pick"]
        elif m["id"] in left and not m.get("off"):
            m["left_out"] = left[m["id"]]
        out.append(m)
    return out


def match_motion(eid: str, moments: List[Dict[str, Any]], sources: List[Dict[str, Any]],
                 sound: Optional[Dict[str, Any]], length: float,
                 pace: Any = "normal") -> Tuple[List[Dict[str, Any]], List[str]]:
    """Flow: measure how every shot moves and chain them so each cut carries the motion on."""
    _stage(eid, "Matching the movement between shots", 14)
    fit = slots_for("flow", sound, length, PACES.get(pace, 0) if isinstance(pace, str) else int(pace or 0),
                    (store.get_edit(eid) or {}).get("settings", {}).get("song_start"))
    paths = {x["id"]: Path(x["source_path"]) for x in sources}
    durs = {x["id"]: float(x.get("duration") or 0) or 1e9 for x in sources}
    return motionmatch.order(moments, paths, durs, keep=fit[1] if fit else None, drop_at=fit[0] if fit else None,
                             progress=lambda p: _stage(eid, f"Matching the movement between shots — {p}%",
                                                       14 + int(p * 0.07)))


def gate(eid: str) -> Optional[Dict[str, Any]]:
    """A campaign edit's check against its brief, stored on the edit (None when it isn't for a campaign)."""
    from . import compliance
    edit = store.get_edit(eid) or {}
    rules = _rules(edit.get("settings") or {})
    plan = edit.get("plan") or {}
    if not rules or not edit.get("file") or not plan.get("timeline"):
        return None
    tl = plan["timeline"]
    result = compliance.check_edit(rules, tl, Path(edit["file"]), plan.get("post") or {},
                                   (tl.get("hook") or {}).get("text", ""), plan.get("tone"))
    store.update_edit(eid, compliance=result)
    return result


def run(eid: str, repick: bool = True) -> None:
    """Pick (unless re-making), lay out, render, check. Never raises: a failure is stored on the edit,
    and the last good video stays."""
    from . import editrender, notify
    with _lock:
        try:
            if not store.get_edit(eid):
                return
            plan = plan_edit(eid, repick)
            edit = store.get_edit(eid)
            sources = {j: store.get_job(j) for j in edit["settings"]["sources"]}
            sound = store.get_sound(edit["settings"].get("sound") or "") if edit["settings"].get("sound") else None
            EDIT_DIR.mkdir(parents=True, exist_ok=True)
            out = EDIT_DIR / f"{eid}.mp4"
            thumb = EDIT_DIR / f"{eid}.jpg"
            made = editrender.render(plan["timeline"], sources, sound, out, thumb,
                                     progress=lambda p: _stage(eid, f"Rendering — {p}%", 25 + int(p * 0.7)))
            if (made or {}).get("notes"):           # e.g. "Moved the frame so the title at the top isn't cut off."
                tl_notes = plan["timeline"].get("notes") or []
                plan["timeline"]["notes"] = tl_notes + [n for n in made["notes"] if n not in tl_notes]
                store.update_edit(eid, plan=plan)
            store.update_edit(eid, file=str(out), thumb=str(thumb), stage="Checking it", progress=97)
            verdict = gate(eid)
            store.update_edit(eid, status="done", stage="Done", progress=100, error="")
            edit = store.get_edit(eid) or {}
            if notify.connected():
                post = (edit.get("plan") or {}).get("post") or {}
                lines = [f"🎬 <b>{notify.esc(edit.get('title') or 'Your edit')}</b> is ready"]
                if verdict:
                    lines.append({"ready": "✅ ", "check": "🟡 ", "blocked": "⛔ "}.get(verdict["status"], "")
                                 + notify.esc(verdict["summary"]))
                if post.get("text"):
                    lines.append(notify.esc(post["text"]))
                notify.send_video(out, "\n".join(lines)[:1000], plan["timeline"]["length"])
        except Exception as exc:
            traceback.print_exc()
            store.update_edit(eid, status="failed", stage="Failed", error=str(exc)[:400], progress=100)
            if notify.connected():
                edit = store.get_edit(eid) or {}
                notify.send(f"❌ The edit <b>{notify.esc(edit.get('title') or '')}</b> didn't work: "
                            f"{notify.esc(str(exc)[:300])}")


# --- changing an edit ------------------------------------------------------------------

def _undo_dir() -> Path:
    d = EDIT_DIR / "undo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot(eid: str) -> bool:
    """Keep the current version (its settings, moments and video) so the next change can be undone."""
    edit = store.get_edit(eid)
    if not edit or not edit.get("file") or not Path(edit["file"]).is_file():
        return False
    d = _undo_dir()
    shutil.copyfile(edit["file"], d / f"{eid}.mp4")
    if edit.get("thumb") and Path(edit["thumb"]).is_file():
        shutil.copyfile(edit["thumb"], d / f"{eid}.jpg")
    store.update_edit(eid, undo={"settings": edit["settings"], "plan": edit.get("plan") or {},
                                 "title": edit.get("title"), "caption": edit.get("caption"),
                                 "hashtags": edit.get("hashtags") or [], "compliance": edit.get("compliance")})
    return True


def undo(eid: str) -> Dict[str, Any]:
    edit = store.get_edit(eid)
    if not edit:
        raise ValueError("Edit not found")
    if edit["status"] in ("queued", "running"):
        raise ValueError("This edit is still being made — wait a moment")
    prev = edit.get("undo") or {}
    d = _undo_dir()
    if not prev or not (d / f"{eid}.mp4").is_file():
        raise ValueError("There's nothing to undo")
    EDIT_DIR.mkdir(parents=True, exist_ok=True)
    from .editrender import swap_in
    try:
        swap_in(d / f"{eid}.mp4", EDIT_DIR / f"{eid}.mp4")
        if (d / f"{eid}.jpg").is_file():
            swap_in(d / f"{eid}.jpg", EDIT_DIR / f"{eid}.jpg")
    except RuntimeError as exc:
        raise ValueError(str(exc).replace("press Re-make", "press Undo again")) from exc
    store.update_edit(eid, settings=prev["settings"], plan=prev["plan"], title=prev.get("title") or edit["title"],
                      caption=prev.get("caption") or "", hashtags=prev.get("hashtags") or [],
                      compliance=prev.get("compliance"), undo=None, status="done", stage="Done", progress=100,
                      error="", file=str(EDIT_DIR / f"{eid}.mp4"), thumb=str(EDIT_DIR / f"{eid}.jpg"))
    return store.get_edit(eid)


RESIZE_KEYS = ("size", "steps", "seconds", "trim_start", "trim_end", "start_words", "end_words")


def _snap_start(src: Dict[str, Any], t: float, voice: bool) -> Optional[float]:
    """The clean place to start nearest second t: a sentence start or a breath if one is close,
    else the next word's start — never inside a word. None when t is past all the words."""
    ws, starts = src["ws"], src["starts"]
    if not ws:
        return t
    k = bisect.bisect_left(starts, t - 0.15)
    near = [i for i in range(max(0, k - 3), min(len(ws), k + 4))]
    if not near or k >= len(ws) and t > ws[-1]["end"]:
        return max(t, ws[-1]["end"] + 0.02) if ws else t
    costs = (0.0, 1.2, 4.0) if voice else (0.0, 0.3, 0.6)
    i = min(near, key=lambda i: costs[src["sq"][i]] + 3.0 * abs(ws[i]["start"] - t))   # his amount counts most
    return _start_range(ws, i)[1]


def _snap_end(src: Dict[str, Any], t: float, voice: bool, src_dur: float) -> float:
    """The clean place to end nearest second t: right after a sentence or a breath if one is close,
    else right after the last word before it — never inside a word."""
    ws, starts = src["ws"], src["starts"]
    if not ws:
        return t
    k = bisect.bisect_right(starts, t + 0.15) - 1
    near = [j for j in range(max(0, k - 3), min(len(ws), k + 4))]
    if not near or k < 0:
        return min(t, ws[0]["start"] - 0.02) if ws else t
    costs = (0.0, 1.2, 4.0) if voice else (0.0, 0.3, 0.6)
    j = min(near, key=lambda j: costs[src["eq"][j]] + 3.0 * abs(ws[j]["end"] - t))
    return _end_range(ws, j, src_dur)[0]


def resize_moment(m: Dict[str, Any], change: Dict[str, Any], style: str, words: List[Dict[str, Any]],
                  src_dur: float = 1e9) -> Tuple[Optional[Dict[str, Any]], str]:
    """One moment made shorter, longer or trimmed, as gs asked — by hand ("Shorter" / "Longer") or in
    words ("cut the first 2 seconds", "end it right after he says 'let's go'"). Cuts land on word
    boundaries, a voice style keeps whole sentences where it can, and the punchline stays in.
    His size then wins over the style's window (the edit's total length still holds).
    Returns (the moment, a plain note) — or (None, why it couldn't be done)."""
    style = style_key(style)
    st = STYLES[style]
    lo, hi = st["window"]
    src = word_source(words)
    voice = st["pace"] == "speech"
    s, e = float(m["start"]), float(m["end"])
    hit = min(max(float(m.get("hit") if m.get("hit") is not None else (s + e) / 2), s), e)
    d = e - s
    name = _label(m)
    ns, ne = s, e
    told = []
    if str(change.get("start_words") or "").strip():
        found = find_words(src, str(change["start_words"]), s, e, reach=20.0, fuzzy=True)
        if not found:
            return None, f"Couldn't find “{change['start_words']}” near {name}, so it stays as it was."
        ns = _start_range(src["ws"], found[0])[1]
        told.append(f"starts on “{change['start_words']}”")
    elif change.get("trim_start") not in (None, ""):
        ns = _snap_start(src, s + highlights._num(change["trim_start"], 0.0), voice)
    if str(change.get("end_words") or "").strip():
        found = find_words(src, str(change["end_words"]), s, e, reach=20.0, fuzzy=True)
        if not found:
            return None, f"Couldn't find “{change['end_words']}” near {name}, so it stays as it was."
        ne = _end_range(src["ws"], found[1], src_dur)[0]
        told.append(f"ends right after “{change['end_words']}”")
    elif change.get("trim_end") not in (None, ""):
        ne = _snap_end(src, e - highlights._num(change["trim_end"], 0.0), voice, src_dur)
    target = None
    if change.get("seconds") not in (None, ""):
        target = max(MIN_MOMENT, highlights._num(change["seconds"], d))
    else:
        steps = highlights._int(change.get("steps"), 0)
        if change.get("size") in ("shorter", "longer") and not steps:
            steps = -1 if change["size"] == "shorter" else 1
        if steps:
            target = d
            for _ in range(min(5, abs(steps))):
                target = target + (1 if steps > 0 else -1) * max(1.0, 0.3 * target)
            target = max(MIN_MOMENT, target)
    if target is not None and (ns, ne) == (s, e):
        costs = _edge_costs(style)
        sub = dict(m, hit=hit)
        p, line = _punchline(sub, src, style, s, e, hit) if src["ws"] else (None, None)
        if target < d - 0.05:                                 # shorter: cut inside it, keeping the punchline
            cuts = (_cuts(src, a=s, b=e, p=p, line=line, lo=MIN_MOMENT, hi=d - 0.3, costs=costs, bounds=(s, e),
                          src_dur=src_dur) if p is not None else _silent_cut(src, s, e, hit, (s, e), src_dur))
            opts = _options(cuts, MIN_MOMENT, max(MIN_MOMENT, d - 0.3), target)
        else:                                                 # longer: more before and after it
            grow = target - d + 2.0
            bounds = (max(0.0, s - grow), min(src_dur, e + grow))
            cuts = (_cuts(src, a=s, b=e, p=p, line=line, lo=d + 0.3, hi=target + 1.5, costs=costs, bounds=bounds,
                          src_dur=src_dur, contain=(s, e)) if p is not None
                    else _silent_cut(src, s, e, hit, bounds, src_dur))
            opts = _options(cuts, d + 0.3, target + 1.5, target)
        if not opts:
            return None, (f"There's no clean way to make {name} {'shorter' if target < d else 'longer'} without "
                          "cutting into his words, so it stays as it was.")
        u, _, idx = min(opts, key=lambda o: o[1] + 0.6 * abs(o[0] * UNIT - target))
        ns, ne = _realize(cuts[idx], u * UNIT)
    ns, ne = max(0.0, math.floor(ns * 100 + 1e-6) / 100), min(src_dur, math.ceil(ne * 100 - 1e-6) / 100)
    if ne - ns < MIN_MOMENT:
        return None, f"That would leave less than a second of {name}, so it stays as it was."
    if abs(ns - s) < 0.02 and abs(ne - e) < 0.02:
        return None, f"{name[0].upper() + name[1:]} is already cut there, so it stays as it was."
    new_hit = min(max(hit, ns), ne - 0.1 if ne - ns > 0.3 else ne)
    out = dict(m, start=round(ns, 2), end=round(ne, 2), hit=round(new_hit, 2), manual=True,
               pick=[round(ns, 2), round(ne, 2), round(new_hit, 2)])
    out.pop("left_out", None)
    nd = ne - ns
    note = f"{name[0].upper() + name[1:]}: {nd:.1f} s now (was {d:.1f} s)" + (f", {' and '.join(told)}" if told else "")
    if nd > hi + 0.05 or nd < lo - 0.05:
        note += (f" — {'longer' if nd > hi else 'shorter'} than {_a(st['name'], low=True)} moment usually is "
                 f"({window_text(style)}), because you asked for it")
    return out, note + "."


def apply_resizes(moments: List[Dict[str, Any]], asks: List[Dict[str, Any]], style: str,
                  words_by_source: Dict[str, List[Dict[str, Any]]],
                  durations: Dict[str, float]) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Every size change asked for, applied in turn: (the moments, what was done, what couldn't be)."""
    by_id = {m["id"]: i for i, m in enumerate(moments)}
    out = [dict(m) for m in moments]
    done, cant = [], []
    for ask_ in asks:
        ask_ = toolio.as_dict(ask_)
        i = by_id.get(str(ask_.get("id") or ""))
        if i is None:
            cant.append("One of the moments asked about isn't in this edit any more.")
            continue
        if not any(ask_.get(k) not in (None, "", 0) for k in RESIZE_KEYS):
            continue
        m = out[i]
        new, note = resize_moment(m, ask_, style, words_by_source.get(m["source"]) or [],
                                  float(durations.get(m["source"]) or 1e9))
        if new is None:
            cant.append(note)
        else:
            if new.get("off"):
                new["off"] = False
            out[i] = new
            done.append(note)
    return out, done, cant


def remake(eid: str, changes: Dict[str, Any]) -> Dict[str, List[str]]:
    """Apply changes from the edit page (style, song, effects, levels, pace, order,
    moments off or on, new text, a moment shorter / longer / trimmed — `resize`:
    [{id, size|steps|seconds|trim_start|trim_end|start_words|end_words}]) and render
    again — Claude only when asked for new moments. The current version is kept for
    Undo. Returns what the size changes did and couldn't do, in plain words."""
    edit = store.get_edit(eid)
    if not edit:
        raise ValueError("Edit not found")
    if edit["status"] in ("queued", "running"):
        raise ValueError("This edit is still being made — wait a moment")
    s = dict(edit["settings"])
    plan = dict(edit.get("plan") or {})
    if changes.get("style"):
        s["style"] = style_key(changes["style"])
    if "sound" in changes:
        if (changes["sound"] or "") != (s.get("sound") or ""):
            s["song_start"] = None                            # a new song starts from its own best part
        s["sound"] = changes["sound"] or ""
    if "song_start" in changes:
        s["song_start"] = changes["song_start"]
    if changes.get("length"):
        s["length"] = changes["length"]
    if isinstance(changes.get("effects"), dict):
        s["effects"] = {**(s.get("effects") or {}),
                        **{k: bool(v) for k, v in changes["effects"].items() if k in EFFECTS}}
    for key in ("grade", "pace", "flashes", "theme"):
        if key in changes and changes[key] is not None:
            s[key] = changes[key]
    for key in ("voice", "music"):
        if changes.get(key) is not None:
            s[key] = max(0.0, min(1.5, float(changes[key])))
    checked = check_settings(s)                               # same rules as a new edit (music, campaign…)
    if changes.get("hook") is not None:
        plan["hook"] = re.sub(r"\s+", " ", str(changes["hook"])).strip()[:90]
    if changes.get("caption") is not None or changes.get("hashtags") is not None:
        post = plan.get("post") or {}
        caption = str(changes["caption"]) if changes.get("caption") is not None else post.get("caption", "")
        tags = changes.get("hashtags") if isinstance(changes.get("hashtags"), list) else post.get("hashtags", [])
        plan["post"] = _post(_rules(checked), caption[:600], [str(t) for t in tags][:12])
    moments = plan.get("moments") or []
    if isinstance(changes.get("moments"), list):
        by_id = {m["id"]: m for m in moments}
        new = []
        for m in changes["moments"]:
            base = by_id.get(m.get("id"))
            if not base or base in new:
                continue
            base = dict(base)
            if "text" in m:
                base["text"] = str(m["text"])[:160]
            if "off" in m:
                base["off"] = bool(m["off"])
            if "drop" in m:
                base["drop"] = bool(m["drop"])
            new.append(base)
        new += [dict(m) for m in moments if m["id"] not in {x["id"] for x in new}]   # never lose a moment
        if new:
            on = [m for m in new if not m.get("off")]
            if not on:
                raise ValueError("Every moment is switched off — turn at least one back on")
            if sum(1 for m in on if m.get("drop")) != 1:
                first = next((m for m in on if m.get("drop")), on[len(on) // 2])
                for m in new:
                    m["drop"] = m is first
            plan["moments"] = new
    sized: Dict[str, List[str]] = {"done": [], "cant": []}
    plan.pop("resize_notes", None)
    if isinstance(changes.get("resize"), list) and changes["resize"]:
        cur = plan.get("moments") or []
        new, sized["done"], sized["cant"] = apply_resizes(cur, changes["resize"], checked["style"],
                                                          words_for_sources([m["source"] for m in cur]),
                                                          durations_for(checked["sources"]))
        others = [k for k, v in changes.items() if k not in ("resize", "_ask") and v not in (None, "", [], {})]
        if not sized["done"] and not others:
            raise ValueError(" ".join(sized["cant"]) or "Nothing to change — say which moment and how")
        plan["moments"] = new
        plan["resize_notes"] = sized["done"] + sized["cant"]
    repick = bool(changes.get("repick")) or checked["sources"] != edit["settings"].get("sources")
    if not repick and checked["style"] != edit["settings"].get("style") and \
            STYLES[checked["style"]]["pace"] != STYLES[style_key(edit["settings"].get("style"))]["pace"]:
        repick = True                                          # speech moments and beat moments differ
    if not repick and checked["style"] == "flow" and edit["settings"].get("style") != "flow":
        repick = True                                          # Flow needs its own short, moving shots
    if isinstance(changes.get("_ask"), dict):
        plan["asks"] = (plan.get("asks") or [])[-9:] + [changes["_ask"]]
    snapshot(eid)
    store.update_edit(eid, settings=checked, plan=plan, status="queued", stage="Waiting", progress=0, error="")
    if plan.get("post") and not repick:
        store.update_edit(eid, caption=plan["post"].get("caption", ""), hashtags=plan["post"].get("hashtags", []))
    threading.Thread(target=run, args=(eid, repick), daemon=True).start()
    return sized


def versions(eid: str, sound_ids: List[str]) -> List[str]:
    """The same edit cut to other songs — same moments and words, no new Claude call — so you can
    post each and see which song does better."""
    edit = store.get_edit(eid)
    if not edit:
        raise ValueError("Edit not found")
    plan = edit.get("plan") or {}
    if not plan.get("moments"):
        raise ValueError("Wait until the edit is made, then make versions of it")
    made = []
    for sid in [x for x in sound_ids if x != edit["settings"].get("sound")][:5]:
        sound = store.get_sound(sid) if sid else None
        if sid and not sound:
            continue
        keep = {k: plan[k] for k in ("moments", "title", "hook", "post", "pick_notes", "tone", "tone_for") if k in plan}
        name = sound["name"] if sound else "no music"
        made.append(create({**edit["settings"], "sound": sid, "song_start": None}, keep,
                           f"{edit['title'][:70]} · {name}"))
    if not made:
        raise ValueError("Pick at least one other song")
    return made


def delete(eid: str) -> None:
    edit = store.get_edit(eid)
    if not edit:
        raise ValueError("Edit not found")
    if edit["status"] in ("queued", "running"):
        raise ValueError("This edit is still being made — wait until it's done, then delete it")
    for p in [EDIT_DIR / f"{eid}.mp4", EDIT_DIR / f"{eid}.jpg", EDIT_DIR / "undo" / f"{eid}.mp4",
              EDIT_DIR / "undo" / f"{eid}.jpg"] + list((EDIT_DIR / "moments").glob(f"{eid}_*.jpg")):
        _remove(p)
    store.delete_edit(eid)


def set_post(eid: str, caption: str, tags: List[str]) -> None:
    """New words to post with (no re-render). For a campaign, checked against the brief again."""
    from . import campaign
    edit = store.get_edit(eid)
    if not edit:
        raise ValueError("Edit not found")
    if edit["status"] in ("queued", "running"):
        raise ValueError("This edit is still being made — wait a moment")
    rules = _rules(edit["settings"])
    plan = dict(edit.get("plan") or {})
    plan["post"] = _post(rules, caption.strip()[:600], [t.strip().lstrip("#") for t in tags if t.strip()][:12])
    if rules:
        said = [plan.get("hook") or "", plan["post"].get("caption") or ""]
        tone = campaign.check_text(rules, [{"id": 0, "hook": said[0],
                                            "extra": said[1].replace(plan["post"].get("line") or "", "").strip()}])
        plan["tone"], plan["tone_for"] = tone.get(0), said
    store.update_edit(eid, plan=plan, caption=plan["post"]["caption"], hashtags=plan["post"]["hashtags"])
    gate(eid)


def moment_thumb(eid: str, mid: str) -> Optional[Path]:
    """A small picture of a moment (its hit), made once and kept."""
    edit = store.get_edit(eid) or {}
    m = next((x for x in (edit.get("plan") or {}).get("moments") or [] if x["id"] == mid), None)
    job = store.get_job(m["source"]) if m else None
    if not m or not job or not Path(job.get("source_path") or "").is_file():
        return None
    d = EDIT_DIR / "moments"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w]", "", mid)
    out = d / f"{eid}_{safe}_{int(float(m['hit']) * 10)}.jpg"
    if not out.is_file():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{float(m['hit']):.2f}", "-i", job["source_path"],
                        "-frames:v", "1", "-vf", "scale=-2:320,crop='min(iw,ih*9/16)':ih", "-q:v", "4", str(out)],
                       capture_output=True)
    return out if out.is_file() else None


def settle_interrupted() -> None:
    """Edits still marked running when the app starts were cut off by a restart: say so."""
    for e in store.list_edits(200):
        if e["status"] in ("running", "queued"):
            store.update_edit(e["id"], status="failed", stage="Failed", progress=100,
                              error="ClipAgent was closed or restarted while this edit was being made. "
                                    "Press Re-make — your moments are kept.")


# --- typed changes ("faster", "black and white", "put the $20M line on the drop") ------------

ASK_TOOL = {
    "name": "change_edit",
    "description": "Turn the person's request into changes to their edit.",
    "input_schema": {
        "type": "object",
        "properties": {
            "understood": {"type": "string", "description": "1-2 friendly, plain sentences to them: what you will "
                           "change. No jargon, no field names."},
            "question": {"type": "string", "description": "Only when you truly can't tell what they want: one "
                         "short question. Then change nothing."},
            "cant": {"type": "array", "items": {"type": "string"}, "description": "Each thing asked that the edit "
                     "maker can't do, in plain words, with the closest thing it can do."},
            "style": {"type": "string", "enum": list(STYLES)},
            "song": {"type": "string", "description": "The exact name of one of their songs, or \"none\" for no music."},
            "length": {"type": "integer", "enum": list(LENGTHS)},
            "pace": {"type": "string", "enum": list(PACES), "description": "How fast it cuts: faster = more cuts."},
            "flashes": {"type": "string", "enum": list(FLASHES), "description": "few = only on the drop; many = on "
                        "every cut."},
            "effects": {"type": "object", "description": "Effects to switch on (true) or off (false).",
                        "properties": {k: {"type": "boolean", "description": v} for k, v in EFFECTS.items()}},
            "grade": {"type": "string", "enum": list(GRADES), "description": "The colour look: " +
                      ", ".join(f"{k} = {v}" for k, v in GRADES.items())},
            "voice": {"type": "number", "description": "His voice level 0-1.5 (0 = off, 1 = normal)."},
            "music": {"type": "number", "description": "Music level 0-1.5 (1 = normal)."},
            "hook": {"type": "string", "description": "New text for the first 3 seconds (max 9 words), only from "
                     "what he says."},
            "caption": {"type": "string", "description": "New caption to post with."},
            "moments": {"type": "array", "description": "ALL the moments, in the new playing order, when the order, "
                        "the drop, a moment's words or which are on changes.",
                        "items": {"type": "object", "properties": {
                            "id": {"type": "string"}, "text": {"type": "string"}, "off": {"type": "boolean"},
                            "drop": {"type": "boolean", "description": "true for the ONE moment on the drop."}},
                            "required": ["id"]}},
            "resize": {"type": "array", "description": "Moments to make shorter, longer or trim — only the ones "
                       "that change (the order stays). ClipAgent cuts on whole words and keeps the punchline.",
                       "items": {"type": "object", "properties": {
                           "id": {"type": "string", "description": "The moment's id (m1, m2…), from the list."},
                           "size": {"type": "string", "enum": ["shorter", "longer"],
                                    "description": "A step shorter or longer (about a third) when no amount is said."},
                           "seconds": {"type": "number", "description": "Make it about this many seconds long."},
                           "trim_start": {"type": "number", "description": "Seconds to cut off its start "
                                          "(negative = start that much earlier)."},
                           "trim_end": {"type": "number", "description": "Seconds to cut off its end "
                                        "(negative = let it run that much longer)."},
                           "start_words": {"type": "string", "description": "Start right on these words, as he "
                                           "says them in or near that moment."},
                           "end_words": {"type": "string", "description": "End right after these words, as he says "
                                         "them (e.g. \"let's go\")."}},
                           "required": ["id"]}},
            "repick": {"type": "boolean", "description": "true only when they want different moments that aren't in "
                       "the list (Claude picks again from the videos)."},
            "theme": {"type": "string", "description": "With repick: what the new moments should be about."},
        },
        "required": ["understood"],
    },
}

ASK_SYSTEM = """You are ClipAgent's edit maker. The person made a short music edit (for TikTok, Reels and Shorts) \
and tells you in their own words what to change. Turn that into changes using only the change_edit tool's \
controls, then tell them in plain words what you'll do.

Rules:
- Change only what they asked for. Leave everything else as it is.
- "Faster" / "more cuts" → pace faster; "slower" / "calmer" → pace slower (or the next pace step from where it is).
- "More flashes" → flashes many; "fewer flashes" → few; "no flashes" → effects.flash false.
- "Black and white" → grade mono; "warmer"/"gold" → gold; "film look" → film; "natural" / "no filter" → none.
- "Put the line about X on the drop" → find the moment whose words say it and send ALL moments with drop on that \
one. If none of the moments says it, set repick with a theme naming it.
- "Different song" → song, by the exact name of one of their songs; if they name none, pick a different one.
- A moment shorter / longer / trimmed → resize, with that moment's id. Moments are numbered in playing order: \
"the second moment" is number 2, "the last one" the last. Match "the funny one", "the 2 million line" by what's \
said in it. "Shorter" / "longer" with no amount → size; "make it 4 seconds" → seconds; "cut the first 2 seconds" → \
trim_start 2; "end it sooner" → trim_end; "let it run 2 seconds longer" → trim_end -2; "end right after he says \
X" → end_words X; "start when he says X" → start_words X. Their request may go past the style's usual moment \
length — that's fine (the whole edit still keeps its length).
- "Make the whole thing N seconds" / "shorter overall" → length (the nearest allowed).
- Words on screen: only what he says (numbers too) — never invent.
- Things the controls can't do (download a song, add stickers, sound effects, other people's footage, post it for \
them) go in cant, with the closest thing you can do. Never pretend.
- Campaign rules, when given, override everything: never switch on something the brief forbids — say so in cant.
"""


def _ask_prompt(edit: Dict[str, Any]) -> str:
    s = edit["settings"]
    plan = edit.get("plan") or {}
    tl = plan.get("timeline") or {}
    fx = tl.get("effects") or {}
    sounds = store.list_sounds()
    current = store.get_sound(s.get("sound") or "") if s.get("sound") else None
    words = words_for_sources([m["source"] for m in plan.get("moments") or []])
    titles = {j: (store.get_job(j) or {}).get("title") or "" for j in s["sources"]}
    lines = [f"Style: {s['style']} ({STYLES[s['style']]['what']})",
             f"Song: {current['name'] if current else 'none'} · their songs: " +
             (", ".join(f"“{x['name']}” ({(x.get('analysis') or {}).get('bpm', '?')} BPM)" for x in sounds) or "none"),
             f"Length: {s['length']} s · pace: {s.get('pace', 'normal')} · flashes: {s.get('flashes', 'normal')} · "
             f"grade: {tl.get('grade') or s.get('grade') or STYLES[s['style']]['grade']}",
             "Effects on: " + (", ".join(k for k, v in fx.items() if v) or "none"),
             f"Voice level {tl.get('voice', 0)} · music level {(tl.get('music') or {}).get('level', 0)}",
             f"Hook: {plan.get('hook') or '(none)'}",
             f"Edit length now: {tl.get('length') or s['length']:.0f} s · a {STYLES[s['style']]['name']} moment "
             f"usually plays {window_text(s['style'])}",
             "Moments, in playing order (number. id):"]
    for n, m in enumerate(plan.get("moments") or [], 1):
        said = _said_between(words.get(m["source"]) or [], m["start"], m["end"])[:260]
        state = (" [DROP]" if m.get("drop") else "") + (" [off]" if m.get("off") else "") + \
            (" [left out: " + str(m["left_out"])[:60] + "]" if m.get("left_out") else "") + \
            (" [sized by hand]" if m.get("manual") else "")
        lines.append(f"  {n}. {m['id']}{state} — {titles.get(m['source'], '')[:40]} {m['start']:.1f}-{m['end']:.1f}s "
                     f"({m['end'] - m['start']:.1f} s) · on screen: “{m.get('text') or ''}” · he says: “{said}”")
    return "\n".join(lines)


def ask(eid: str, text: str) -> Dict[str, Any]:
    """A typed change request → Claude maps it to the edit's controls → re-made (Undo keeps the old one)."""
    from . import campaign
    edit = store.get_edit(eid)
    if not edit:
        raise ValueError("Edit not found")
    if edit["status"] in ("queued", "running"):
        raise ValueError("This edit is still being made — wait until it's done, then ask")
    if not (edit.get("plan") or {}).get("moments"):
        raise ValueError("This edit has no moments yet — press Re-make first")
    text = (text or "").strip()
    if not text:
        raise ValueError("Type what you'd like changed")
    rules = _rules(edit["settings"])
    system = ASK_SYSTEM + (f"\n\nCAMPAIGN RULES:\n{campaign.picker_guidance(rules)}" if rules else "")
    client = highlights._client()
    message = client.messages.create(model=CLAUDE_MODEL, max_tokens=3000, system=system, tools=[ASK_TOOL],
                                     tool_choice={"type": "tool", "name": "change_edit"},
                                     messages=[{"role": "user", "content": _ask_prompt(edit)
                                                + f"\n\nTHEIR REQUEST:\n{text}"}])
    got = toolio.tool_inputs(message)
    if not got:
        raise RuntimeError("Claude's answer couldn't be read — try saying it another way")
    r = got[0]
    cant = toolio.coerce(r.get("cant"))
    reply = {"understood": highlights._text(r.get("understood"))[:600],
             "question": highlights._text(r.get("question"))[:300],
             "cant": [str(c).strip()[:300] for c in (cant if isinstance(cant, list) else [cant] if cant else [])
                      if str(c).strip()][:6], "changed": False}
    changes: Dict[str, Any] = {}
    if not reply["question"]:
        if r.get("style"):
            changes["style"] = style_key(r["style"])
        if r.get("song"):
            name = str(r["song"]).strip().strip("“”\"").lower()
            if name in ("none", "no music", "no song"):
                changes["sound"] = ""
            else:
                match = next((x for x in store.list_sounds() if x["name"].lower() == name), None) or \
                    next((x for x in store.list_sounds() if name and name in x["name"].lower()), None)
                if match:
                    changes["sound"] = match["id"]
                else:
                    reply["cant"].append(f"There's no song called “{r['song']}” in your songs — add it first.")
        asked_len = highlights._num(r.get("length"), 0.0)
        if asked_len > 0:
            changes["length"] = min(LENGTHS, key=lambda x: abs(x - asked_len))
        for key, allowed in (("pace", PACES), ("flashes", FLASHES), ("grade", GRADES)):
            if r.get(key) in allowed:
                changes[key] = r[key]
        fx = toolio.as_dict(r.get("effects"))
        if fx:
            changes["effects"] = {k: bool(v) for k, v in fx.items() if k in EFFECTS}
        for key in ("voice", "music"):
            if r.get(key) is not None:
                changes[key] = highlights._num(r.get(key), 1.0)
        if highlights._text(r.get("hook")):
            hook = highlights._text(r["hook"])
            heard = " ".join(" ".join(w.get("w", "") for w in ws)
                             for ws in words_for_sources(edit["settings"]["sources"]).values())
            if _numbers_said(hook, heard):
                changes["hook"] = hook
            else:
                reply["cant"].append("That hook has a number he never says, so it stays as it is.")
        if highlights._text(r.get("caption")):
            changes["caption"] = highlights._text(r["caption"])
        moments = toolio.coerce(r.get("moments"))
        if isinstance(moments, list) and moments:
            changes["moments"] = [toolio.as_dict(m) for m in moments if toolio.as_dict(m).get("id")]
            ids = {m["id"] for m in edit["plan"]["moments"]}
            sent = [m["id"] for m in changes["moments"]]
            sized = [m for m in changes["moments"] if any(m.get(k) not in (None, "", 0) for k in RESIZE_KEYS)]
            if sized:                                       # sizes sent with the order: treat them as resizes
                changes["resize"] = sized
            if len(set(sent) & ids) < len(ids) and not any(k in m for m in changes["moments"]
                                                           for k in ("off", "drop", "text")):
                changes.pop("moments")                      # only some moments, nothing but sizes: keep the order
        resize = toolio.coerce(r.get("resize"))
        if isinstance(resize, list) and resize:
            changes["resize"] = (changes.get("resize") or []) + [toolio.as_dict(x) for x in resize
                                                                 if toolio.as_dict(x).get("id")]
        if r.get("repick") is True:
            changes["repick"] = True
            if highlights._text(r.get("theme")):
                changes["theme"] = highlights._text(r["theme"])
    if changes.get("resize"):                               # what the size changes can do, said up front
        style = changes.get("style") or edit["settings"]["style"]
        cur = edit["plan"]["moments"]
        _, done, cant_sz = apply_resizes(cur, changes["resize"], style, words_for_sources([m["source"] for m in cur]),
                                         durations_for(edit["settings"]["sources"]))
        reply["done"] = done
        reply["cant"] += [c for c in cant_sz if c not in reply["cant"]]
        if not done:
            changes.pop("resize")
    entry = {"text": text[:300], **reply, "changed": bool(changes), "at": time.time()}
    if changes:
        remake(eid, {**changes, "_ask": entry})        # saved with the plan before the re-make starts
        reply["changed"] = True
    else:
        plan = dict(edit.get("plan") or {})
        plan["asks"] = (plan.get("asks") or [])[-9:] + [entry]
        store.update_edit(eid, plan=plan)
    return reply


def edit_json(e: Dict[str, Any]) -> Dict[str, Any]:
    plan = e.get("plan") or {}
    tl = plan.get("timeline") or {}
    s = e.get("settings") or {}
    style = style_key(s.get("style"))
    st = STYLES[style]
    sound = store.get_sound(s.get("sound") or "") if s.get("sound") else None
    titles = {j: (store.get_job(j) or {}).get("title") or "" for j in s.get("sources") or []}
    moments = [{**{k: v for k, v in m.items() if k not in ("motion", "pick")},
                "source_title": titles.get(m["source"], ""),
                "length": round(m.get("shown") or (m["end"] - m["start"]), 1),
                "thumb": f"/media/edit-moment/{e['id']}/{m['id']}.jpg"} for m in plan.get("moments") or []]
    post = plan.get("post") or {}
    return {
        "id": e["id"], "title": e.get("title") or "", "status": e.get("status"), "stage": e.get("stage"),
        "progress": e.get("progress") or 0, "error": e.get("error") or "", "created_at": e.get("created_at"),
        "style": style, "style_name": st["name"], "pace_kind": st["pace"], "settings": s,
        "hook": plan.get("hook") or "", "length": tl.get("length"), "moments": moments,
        "drop_at": tl.get("drop_at"), "segments": len(tl.get("segments") or []), "cuts": tl.get("cuts") or [],
        "effects": tl.get("effects") or {**st["effects"], **(s.get("effects") or {})},
        "grade": tl.get("grade") or s.get("grade") or st["grade"], "pace": s.get("pace", "normal"),
        "flashes": s.get("flashes", "normal"),
        "voice": tl.get("voice", s.get("voice", st["voice"])),
        "music": (tl.get("music") or {}).get("level", s.get("music", st["music"])),
        "sound": sound_json(sound) if sound else None, "song_start": s.get("song_start"),
        "song_part": [(tl.get("music") or {}).get("start"), (tl.get("music") or {}).get("end")] if tl.get("music") else None,
        "notes": (plan.get("resize_notes") or []) + (plan.get("pick_notes") or []) + (tl.get("notes") or []),
        "window": list(st["window"]),
        "caption": post.get("caption") or e.get("caption") or "", "hashtags": post.get("hashtags") or e.get("hashtags") or [],
        "post_text": post.get("text") or "", "checklist": post.get("checklist") or [],
        "compliance": e.get("compliance") or None, "can_undo": bool(e.get("undo")),
        "asks": [{k: a.get(k) for k in ("text", "understood", "question", "cant", "changed", "at")}
                 for a in (plan.get("asks") or [])[-5:]],
        "video_url": f"/media/edit/{e['id']}.mp4?v={int(e.get('updated_at') or 0)}"
        if e.get("file") and Path(e["file"]).is_file() else None,
        "thumb_url": f"/media/edit/{e['id']}.jpg?v={int(e.get('updated_at') or 0)}"
        if e.get("thumb") and Path(e["thumb"]).is_file() else None,
        "campaign_id": e.get("campaign_id") or "",
    }
