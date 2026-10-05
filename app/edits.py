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


STYLES: Dict[str, Dict[str, Any]] = {
    "velocity": {
        "name": "Velocity", "pace": "beat", "voice": 0.0, "music": 1.0, "grade": "punchy", "text": "punch",
        "length": 20, "needs_music": True, "pre_beats": 8, "count": (8, 12),
        "what": "Cut on every beat: speed ramps, slow-mo and a glitch on the drop, flashes and zoom punches.",
        "effects": _fx("ramp slowmo flash pulse shake glitch vignette text loop"),
        "brief": "8 to 12 short moments, 1.5 to 4 seconds each, with the most energy: a big claim, a number, a hard "
                 "line, a laugh, a reaction. `text`: 1 to 4 punch words he actually says in that moment (\"SEVEN "
                 "YEARS\", \"STICK TO IT\"), no emoji. Mark the single hardest moment as the drop.",
    },
    "aura": {
        "name": "Aura", "pace": "beat", "voice": 0.0, "music": 1.0, "grade": "teal", "text": "hook",
        "length": 15, "needs_music": True, "pre_beats": 4, "count": (4, 7),
        "what": "Slow and cold: long slow-mo shots, one cut every two bars, a lore hook on top.",
        "effects": _fx("slowmo flash push grain vignette text loop"),
        "brief": "4 to 7 moments, 2 to 5 seconds each, where he looks most in control: a calm flex, a knowing look "
                 "after a big line, a win. The words don't play (music only), so pick moments that look strong. "
                 "Mark the strongest as the drop. `text` can be empty: the hook carries the edit.",
    },
    "flow": {
        "name": "Flow", "pace": "beat", "voice": 0.0, "music": 1.0, "grade": "tealorange", "text": "hook",
        "length": 25, "needs_music": True, "pre_beats": 8, "count": (15, 30),
        "what": "Smooth match cuts about once a second: every cut carries the movement into the next shot.",
        "effects": _fx("ramp slowmo flash pulse shake blur vignette text loop"),
        "brief": "15 to 30 short moments, 1 to 3 seconds each, where he MOVES: gestures, turns, leans in, laughs, "
                 "stands up, points, reacts. Movement matters more than words here (the words don't play). Mark the "
                 "most energetic as the drop. `text` can be empty.",
    },
    "cinematic": {
        "name": "Cinematic", "pace": "speech", "voice": 1.0, "music": 0.30, "grade": "film", "text": "subtitle",
        "length": 30, "music_optional": True, "dip": True, "count": (3, 5),
        "what": "Film look with cinema bars and grain; his words with the music underneath.",
        "effects": _fx("push grain vignette letterbox text loop"),
        "brief": "3 to 5 moments, 5 to 12 seconds each, that each say something complete and quotable — a lesson, "
                 "a turning point, a truth. They should flow as one short story. `text`: the line itself.",
    },
    "motivation": {
        "name": "Motivation", "pace": "speech", "voice": 1.0, "music": 0.36, "grade": "mono", "text": "build",
        "length": 20, "music_optional": True, "dip": True, "count": (2, 4),
        "what": "Black and white, his strongest lines building up word by word, music swelling behind.",
        "effects": _fx("flash push grain vignette text loop"),
        "brief": "2 to 4 moments, 4 to 10 seconds each: his most powerful lines about discipline, mindset, money or "
                 "winning — each a complete thought on sentence edges. `text`: the line itself; `key`: the one word "
                 "that hits hardest. Mark the strongest as the drop.",
    },
    "funny": {
        "name": "Funny", "pace": "speech", "voice": 1.0, "music": 0.12, "grade": "none", "text": "meme",
        "length": 30, "music_optional": True, "count": (4, 7),
        "what": "The funniest bits back to back: hard cuts, a zoom punch and shake on every punchline, meme text.",
        "effects": _fx("pulse shake text loop"),
        "brief": "4 to 7 moments, 3 to 9 seconds each: the funniest bits — jokes, reactions, chaos, awkward moments. "
                 "Each must land its punchline inside it; `hit` is the punchline. `text`: a short meme caption in a "
                 "viewer's voice (max 8 words), or empty.",
    },
    "money": {
        "name": "Money", "pace": "beat", "voice": 0.0, "music": 1.0, "grade": "gold", "text": "quote",
        "length": 20, "needs_music": True, "pre_beats": 4, "dip": True, "count": (6, 9),
        "what": "Warm gold grade, smooth slow-mo and push-ins, a cut every four beats, the money lines on screen.",
        "effects": _fx("slowmo push grain vignette text loop"),
        "brief": "6 to 9 moments, 2 to 5 seconds each, about money, wins, the lifestyle and big numbers. `text`: a "
                 "short money line he actually says there (max 6 words).",
    },
}
ALIASES = {"hype": "velocity", "luxury": "money"}      # names used by the first draft
LENGTHS = (15, 20, 25, 30, 40, 60)


def _a(name: str) -> str:
    return ("An " if name[:1].lower() in "aeiou" else "A ") + name


def style_key(name: Any) -> str:
    key = str(name or "").strip().lower()
    key = ALIASES.get(key, key)
    return key if key in STYLES else "velocity"


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


def before_drop(style: str, sound: Optional[Dict[str, Any]]) -> Optional[Tuple[int, float]]:
    """For a beat edit on this song: how many shots fit before the drop, and how many seconds in it comes."""
    style = style_key(style)
    st = STYLES[style]
    analysis = (sound or {}).get("analysis") or {}
    if st["pace"] != "beat" or len(analysis.get("beats") or []) < 4:
        return None
    g = beat_grid(analysis)
    first_real = next(i for i, t in enumerate(g["t"]) if t >= -0.01)
    drop_i = _nearest(g["t"], float(analysis.get("drop") or 0.0))
    pre = drop_i - max(first_real, drop_i - st["pre_beats"])
    build = _patterns(style, g["period"])[0][0]
    return -(-pre // build), pre * g["period"]


def pick_moments(sources: List[Dict[str, Any]], style: str, theme: str, length: int,
                 guidance: str = "", candidates: Optional[List[Dict[str, Any]]] = None,
                 sound: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One Claude call → the edit's hook, caption, hashtags and moments in play order.

    `candidates` (moments already found and scored, e.g. by Creator Scan) are
    offered instead of whole transcripts when given."""
    style = style_key(style)
    st = STYLES[style]
    index_of = {s["id"]: i + 1 for i, s in enumerate(sources)}
    if candidates:
        heads = [f"=== VIDEO {i + 1}: {s.get('title') or 'Untitled'}" for i, s in enumerate(sources)]
        body = "\n".join(heads) + "\n\n" + _candidate_block(candidates, index_of)
    else:
        budget = max(8000, 90000 // max(1, len(sources)))
        body = "\n\n".join(_transcript_block(i + 1, s, budget) for i, s in enumerate(sources))
    lo, hi = st["count"]
    prompt = (body
              + f"\n\nTHE EDIT: a {st['name']} edit, about {length} seconds long. {st['what']}\n"
              + f"What it's about: {theme.strip() or 'the best moments'}\n"
              + f"Moments: {st['brief']} Return {lo} to {hi} moments.")
    fit = before_drop(style, sound)
    if fit:
        k, secs = fit
        prompt += (f"\nThe song drops {secs:.1f} seconds in: only {k} moment{'s' if k != 1 else ''} play before the "
                   f"drop (the opener{' and the build' if k > 1 else ''}), the drop moment comes next, the rest after.")
    system = PICK_SYSTEM + (f"\n\nCAMPAIGN RULES (these override the rest):\n{guidance}" if guidance else "")
    client = highlights._client()
    message = client.messages.create(model=CLAUDE_MODEL, max_tokens=6000, system=system, tools=[PICK_TOOL],
                                     tool_choice={"type": "tool", "name": "pick_moments"},
                                     messages=[{"role": "user", "content": prompt}])
    got = toolio.tool_inputs(message)
    if not got:
        raise RuntimeError("Claude's answer couldn't be read — try again")
    return read_pick(got[0], sources, style, candidates)


def read_pick(reply: Dict[str, Any], sources: List[Dict[str, Any]], style: str,
              candidates: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Claude's picks, checked: real times, no overlaps, text that was really said, one drop."""
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
        if cand:
            job = next((s for s in sources if s["id"] == cand["source"]), None)
            start, end = float(cand["start"]), float(cand["end"])
            hit_default = float(cand.get("hit", (start + end) / 2))
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
        moments.append({
            "id": f"m{k + 1}", "source": job["id"], "start": round(start, 2), "end": round(end, 2),
            "hit": round(hit, 2), "text": text, "key": key[:40],
            "kind": str(m.get("kind") or "quote"), "drop": bool(m.get("drop")),
            "why": highlights._text(m.get("why"))[:160],
        })
    if not moments:
        raise RuntimeError("Claude didn't find moments that fit — try another theme or more videos")
    lo, hi = st["count"]
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


def snap_moments(moments: List[Dict[str, Any]], style: str,
                 words_by_source: Dict[str, List[Dict[str, Any]]]) -> None:
    """Voice edits play whole sentences: put each moment's edges on sentence edges."""
    if STYLES[style_key(style)]["pace"] != "speech":
        return
    for m in moments:
        words = words_by_source.get(m["source"]) or []
        if words:
            s, e = highlights.clean_bounds(m["start"], m["end"], words, min_len=1.5)
            m["start"], m["end"] = round(s, 2), round(e, 2)
            m["hit"] = round(min(max(m["hit"], m["start"]), m["end"]), 2)


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
    halved for "faster", doubled for "slower"."""
    build, hold, after = _base_patterns(style, period)
    if pace > 0:
        return [max(1, b // 2) for b in build], max(2, hold // 2), [max(1, a // 2) for a in after]
    if pace < 0:
        return [min(8, b * 2) for b in build], min(16, hold * 2), [min(8, a * 2) for a in after]
    return build, hold, after


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


def _spread(moments: List[Dict[str, Any]], slots: int, notes: List[str]) -> List[Tuple[Dict[str, Any], int]]:
    """Share `slots` cuts between moments in order: each gets at least one, longer ones get more."""
    if not moments:
        return []
    if slots <= 0:
        notes.append(f"{len(moments)} moment{'s' if len(moments) > 1 else ''} before the drop didn't fit and "
                     f"{'were' if len(moments) > 1 else 'was'} left out.")
        return []
    if len(moments) > slots:
        keep = [0, len(moments) - 1] if slots >= 2 else [0]
        step = (len(moments) - 1) / max(1, slots - 1)
        keep = sorted(set(int(round(i * step)) for i in range(slots)) | set(keep))[:slots]
        dropped = len(moments) - len(keep)
        notes.append(f"{dropped} moment{'s' if dropped > 1 else ''} didn't fit the length and "
                     f"{'were' if dropped > 1 else 'was'} left out.")
        moments = [moments[i] for i in keep]
    weights = [min(6.0, max(1.0, m["end"] - m["start"])) for m in moments]
    extra = slots - len(moments)
    total = sum(weights)
    shares = [w / total * extra for w in weights]
    counts = [1 + int(s) for s in shares]
    rest = slots - sum(counts)
    order = sorted(range(len(moments)), key=lambda i: shares[i] - int(shares[i]), reverse=True)
    for i in order[:rest]:
        counts[i] += 1
    return list(zip(moments, counts))


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
               song_start: Optional[float] = None) -> Dict[str, Any]:
    """Where a beat edit cuts on this song: the song section, the drop, and every slot (in beats).
    `song_start` (song seconds) picks the part of the song by hand; otherwise it's the part around the drop."""
    st = STYLES[style]
    g = beat_grid(analysis)
    grid, period, song_len = g["t"], g["period"], g["duration"]
    first_real = next(i for i, t in enumerate(grid) if t >= -0.01)
    want = max(8, int(round(length / period / 4.0)) * 4)                # whole bars
    asked = want
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
        start_i = max(first_real, drop_i - st["pre_beats"])
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
        cuts.pop(-2)                                         # the last shot gets at least two beats
    slots = list(zip(cuts[:-1], cuts[1:]))
    return {"grid": grid, "period": period, "phase": g["phase"], "start_i": start_i, "want": want, "kd": kd,
            "s0": grid[start_i], "slots": slots, "ds": next(j for j, (a, _) in enumerate(slots) if a == kd)}


def slots_for(style: str, sound: Optional[Dict[str, Any]], length: float, pace: int = 0,
              song_start: Optional[float] = None) -> Optional[Tuple[int, int]]:
    """For a beat edit: (shots before the drop, shots in all)."""
    analysis = (sound or {}).get("analysis") or {}
    if STYLES[style_key(style)]["pace"] != "beat" or len(analysis.get("beats") or []) < 4:
        return None
    c = _beat_cuts(style_key(style), length, analysis, [], pace, song_start)
    return c["ds"], len(c["slots"])


def _beat_timeline(ordered: List[Dict[str, Any]], style: str, length: float, analysis: Dict[str, Any],
                   fx: Dict[str, bool], durations: Dict[str, float], notes: List[str], pace: int = 0,
                   flashes: str = "normal", song_start: Optional[float] = None) -> Dict[str, Any]:
    st = STYLES[style]
    c = _beat_cuts(style, length, analysis, notes, pace, song_start)
    grid, period, start_i, want, kd, s0 = c["grid"], c["period"], c["start_i"], c["want"], c["kd"], c["s0"]
    slots, ds = c["slots"], c["ds"]
    drop_i = start_i + kd

    def is_bar(i: int) -> bool:
        return (i - c["phase"]) % 4 == 0

    drop_m = next((m for m in ordered if m.get("drop")), ordered[len(ordered) // 2])
    di = ordered.index(drop_m)
    before, after_m = ordered[:di], ordered[di + 1:]
    if len(before) > ds >= 1:                                # more than fits before the drop: they play after it
        after_m = before[ds:] + after_m
        before = before[:ds]
    if ds > 0 and not before:
        before = [after_m.pop(0)] if len(after_m) > 1 else [drop_m]
    if len(slots) - ds - 1 > 0 and not after_m:
        after_m = [before.pop()] if len(before) > 1 else [drop_m]
    runs = _spread(before, ds, notes) + [(drop_m, 1)] + _spread(after_m, len(slots) - ds - 1, notes)

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


def _speech_timeline(ordered: List[Dict[str, Any]], style: str, length: float, analysis: Optional[Dict[str, Any]],
                     fx: Dict[str, bool], durations: Dict[str, float], notes: List[str],
                     flashes: str = "normal", song_start: Optional[float] = None) -> Dict[str, Any]:
    st = STYLES[style]
    drop_m = next((m for m in ordered if m.get("drop")), None)
    # whole moments, in order, as many as fit (the drop moment always plays)
    budget = length * 1.15
    chosen, total = [], (drop_m["end"] - drop_m["start"]) if drop_m else 0.0
    for m in ordered:
        if m is drop_m:
            chosen.append(m)
            continue
        span = m["end"] - m["start"]
        if chosen and total + span > budget:
            notes.append(f"Left out “{(m.get('text') or 'a moment')[:40]}” to keep the edit near {length:.0f} s.")
            continue
        chosen.append(m)
        total += span
    if drop_m is None:
        drop_m = chosen[len(chosen) // 2]

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


def build_timeline(moments: List[Dict[str, Any]], style: str, length: float,
                   sound: Optional[Dict[str, Any]], effects: Optional[Dict[str, Any]],
                   words_by_source: Dict[str, List[Dict[str, Any]]],
                   voice_level: Optional[float] = None, music_level: Optional[float] = None,
                   durations: Optional[Dict[str, float]] = None, grade: Optional[str] = None,
                   hook: str = "", pace: Any = 0, flashes: str = "normal",
                   song_start: Optional[float] = None) -> Dict[str, Any]:
    """Where every moment sits in the edit: cut on the beat, the best one on the drop, ending on a bar line."""
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
    if st["pace"] == "beat":
        if not analysis or not analysis.get("beats"):
            raise ValueError(f"{_a(st['name'])} edit is cut to music — pick a song first")
        built = _beat_timeline(ordered, style, float(length), analysis, fx, durations, notes, pace, flashes,
                               song_start)
    else:
        built = _speech_timeline(ordered, style, float(length), analysis if analysis and analysis.get("beats")
                                 else None, fx, durations, notes, flashes, song_start)
    segments = built["segments"]
    motion = {m["id"]: m.get("motion") for m in ordered}
    for i, seg in enumerate(segments):
        seg["words"] = _words_for(seg, words_by_source.get(seg["source"]) or [])
        seg.pop("dip", None)
        if seg.get("blur_in") and i > 0:                      # the cut's streak follows the motion through it
            vec = motionmatch.blur_vector(motion.get(segments[i - 1]["moment"]), motion.get(seg["moment"]))
            if vec:
                seg["blur_vec"] = vec
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
        "notes": notes,
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
        return {}, [], (f"{name}: the brief doesn't allow joining different moments, and an edit is made of several. "
                        "If the brief allows it, switch “Join different moments” on in the campaign's rules — "
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
        picked = pick_moments(sources, s["style"], s.get("theme", ""), s["length"],
                              campaign.picker_guidance(rules) if rules else "", plan.get("candidates"), sound)
        snap_moments(picked["moments"], s["style"], words)
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
                              s.get("song_start"))
    timeline["notes"] = fit_notes + timeline["notes"]
    plan["timeline"] = timeline
    store.update_edit(eid, plan=plan)
    return plan


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
            editrender.render(plan["timeline"], sources, sound, out, thumb,
                              progress=lambda p: _stage(eid, f"Rendering — {p}%", 25 + int(p * 0.7)))
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


def remake(eid: str, changes: Dict[str, Any]) -> None:
    """Apply changes from the edit page (style, song, effects, levels, pace, order,
    moments off or on, new text) and render again — Claude only when asked
    for new moments. The current version is kept for Undo."""
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
             "Moments, in playing order:"]
    for m in plan.get("moments") or []:
        said = _said_between(words.get(m["source"]) or [], m["start"], m["end"])[:260]
        lines.append(f"  {m['id']}{' [DROP]' if m.get('drop') else ''}{' [off]' if m.get('off') else ''} — "
                     f"{titles.get(m['source'], '')[:40]} {m['start']:.1f}-{m['end']:.1f}s · on screen: "
                     f"“{m.get('text') or ''}” · he says: “{said}”")
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
        if r.get("length") in LENGTHS:
            changes["length"] = int(r["length"])
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
        if r.get("repick") is True:
            changes["repick"] = True
            if highlights._text(r.get("theme")):
                changes["theme"] = highlights._text(r["theme"])
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
    moments = [{**{k: v for k, v in m.items() if k != "motion"}, "source_title": titles.get(m["source"], ""),
                "length": round(m["end"] - m["start"], 1),
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
        "notes": (plan.get("pick_notes") or []) + (tl.get("notes") or []),
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
