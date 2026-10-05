"""The edit maker: short, music-driven edits cut from your videos.

A clip is one moment cut out of a long video. An edit is built: several
moments, chosen by Claude for a theme ("his best trading advice", "the
funniest bits"), laid onto the beats of a song you added, with the strongest
one landing on the drop — and finished with the things edits are made of:
flashes on the cut, zoom hits on the beat, shake and an RGB glitch on the
drop, slow-motion, colour grades, grain, letterbox bars and words on screen.

This module decides WHAT goes WHERE (the styles, the moments, the timeline);
editrender.py draws it.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import beats, highlights, store, toolio, transcribe
from .config import CLAUDE_MODEL, DATA_DIR

SOUND_DIR = DATA_DIR / "sounds"
EDIT_DIR = DATA_DIR / "edits"
_lock = threading.Lock()                  # one edit renders at a time

EFFECTS = {
    "flash": "Flash on the cut",
    "pulse": "Zoom on the beat",
    "shake": "Shake on the hits",
    "slowmo": "Slow-mo on the drop",
    "glitch": "RGB glitch on the drop",
    "push": "Slow push-in",
    "grain": "Film grain",
    "vignette": "Dark corners",
    "letterbox": "Cinema bars",
    "text": "Words on screen",
}

STYLES: Dict[str, Dict[str, Any]] = {
    "hype": {
        "name": "Hype", "what": "Fast cuts on the beat, flashes, zoom hits, slow-mo and a glitch on the drop, "
        "big punch words.", "pace": "beat", "cut": 0.9, "build_cut": 1.8, "max_run": 1.9, "voice": 0.0, "music": 1.0,
        "grade": "punchy", "text": "punch", "length": 20, "needs_music": True,
        "effects": {"flash": True, "pulse": True, "shake": True, "slowmo": True, "glitch": True, "push": False,
                    "grain": False, "vignette": True, "letterbox": False, "text": True},
        "brief": "8 to 12 short moments, 1.5 to 4 seconds each, with the most energy: a big claim, a number, a hard "
                 "line, a laugh, a reaction. `text`: 1 to 5 punch words he actually says there (\"SEVEN YEARS\", "
                 "\"STICK TO IT\"), no emoji. Mark the single hardest line as the drop.",
    },
    "cinematic": {
        "name": "Cinematic", "what": "Film grade, cinema bars, grain and slow push-ins; his words with the music "
        "underneath.", "pace": "speech", "voice": 1.0, "music": 0.30, "grade": "film", "text": "subtitle",
        "length": 30, "dip": "black",
        "effects": {"flash": False, "pulse": False, "shake": False, "slowmo": False, "glitch": False, "push": True,
                    "grain": True, "vignette": True, "letterbox": True, "text": True},
        "brief": "3 to 5 moments, 5 to 12 seconds each, that each say something complete and quotable — a lesson, "
                 "a turning point, a truth. They should flow as one short story. `text`: the line itself.",
    },
    "motivation": {
        "name": "Motivation", "what": "Black and white, his strongest lines building up word by word, music "
        "swelling behind.", "pace": "speech", "voice": 1.0, "music": 0.36, "grade": "mono", "text": "build",
        "length": 25, "dip": "black",
        "effects": {"flash": True, "pulse": False, "shake": False, "slowmo": False, "glitch": False, "push": True,
                    "grain": True, "vignette": True, "letterbox": False, "text": True},
        "brief": "2 to 4 moments, 4 to 10 seconds each: his most powerful lines about discipline, mindset, money or "
                 "winning — each a complete thought on sentence edges. `text`: the line itself. Mark the strongest "
                 "as the drop.",
    },
    "funny": {
        "name": "Funny", "what": "The funniest bits back to back: hard cuts, a zoom punch on every punchline, "
        "meme text on top.", "pace": "speech", "voice": 1.0, "music": 0.12, "grade": "none", "text": "meme",
        "length": 35, "music_optional": True,
        "effects": {"flash": False, "pulse": False, "shake": True, "slowmo": False, "glitch": False, "push": False,
                    "grain": False, "vignette": False, "letterbox": False, "text": True},
        "brief": "4 to 7 moments, 3 to 9 seconds each: the funniest bits — jokes, reactions, chaos, awkward moments. "
                 "Each must land its punchline inside it; `hit` is the punchline. `text`: a short meme caption in a "
                 "viewer's voice (max 8 words), or empty.",
    },
    "luxury": {
        "name": "Money", "what": "Warm gold grade, smooth zooms and dips, the money lines on screen.",
        "pace": "beat", "cut": 2.6, "build_cut": 2.6, "max_run": 5.2, "voice": 0.0, "music": 1.0, "grade": "gold",
        "text": "quote", "length": 20, "needs_music": True, "dip": "black",
        "effects": {"flash": False, "pulse": False, "shake": False, "slowmo": True, "glitch": False, "push": True,
                    "grain": True, "vignette": True, "letterbox": False, "text": True},
        "brief": "6 to 9 moments, 2 to 5 seconds each, about money, wins, the lifestyle and big numbers. `text`: a "
                 "short money line from what he says (max 6 words).",
    },
}
LENGTHS = (15, 20, 25, 30, 40, 60)


# --- sounds ----------------------------------------------------------------------------

def add_sound(src: Path, name: str) -> Dict[str, Any]:
    """Keep a song you added, read its beats once, and remember them."""
    analysis = beats.analyze(src)
    SOUND_DIR.mkdir(parents=True, exist_ok=True)
    sid_name = re.sub(r"[^\w.-]+", "_", Path(name).stem)[:60] or "sound"
    dest = SOUND_DIR / f"{int(time.time())}_{sid_name}{src.suffix.lower() or '.mp3'}"
    shutil.move(str(src), dest)
    sid = store.add_sound(Path(name).stem[:80] or "Sound", str(dest), analysis["duration"], analysis)
    return store.get_sound(sid)


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
        out.append({"id": j["id"], "title": j["title"], "duration": j["duration"], "created_at": j["created_at"],
                    "campaign_id": full.get("campaign_id") or ""})
    # the same video run twice is one source: keep the newest
    seen, unique = set(), []
    for s in out:
        key = (s["title"], round(s["duration"] or 0))
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


def _loads(text: Any, default: Any) -> Any:
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


# --- Claude picks the moments --------------------------------------------------------------

PICK_TOOL = {
    "name": "pick_moments",
    "description": "Choose the moments for the edit, in the order they play.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "A short name for this edit (for the library)."},
            "caption": {"type": "string", "description": "The caption to post it with: one or two lines, no hashtags."},
            "hashtags": {"type": "array", "items": {"type": "string"}, "description": "3-6 hashtags, no '#'."},
            "moments": {"type": "array", "items": {"type": "object", "properties": {
                "video": {"type": "integer", "description": "The VIDEO number."},
                "start": {"type": "number", "description": "Start, seconds in that video (from the transcript times)."},
                "end": {"type": "number"},
                "hit": {"type": "number", "description": "The instant it builds to: the punch word, the number, "
                        "the punchline (seconds in that video)."},
                "text": {"type": "string"},
                "kind": {"type": "string", "enum": ["quote", "funny", "hype", "reaction", "money", "story"]},
                "drop": {"type": "boolean", "description": "true for the ONE moment that lands on the song's drop."},
                "why": {"type": "string", "description": "A few words: why this moment."},
            }, "required": ["video", "start", "end", "text"]}},
        },
        "required": ["moments"],
    },
}

PICK_SYSTEM = """You cut short edits for a clip page — the kind that get millions of views on TikTok, Reels and \
Shorts. You pick the moments from the transcripts below and decide the order they play in.

Rules:
- Only moments that are really there: use the transcript's times. Start on the first word of a sentence and end \
right after the last word of one, unless the brief says the moments are short hits.
- On-screen text uses only what is said in the video (or a viewer-style caption for funny edits). Never invent \
names, numbers or claims.
- The person is the hero of the edit: never make them look bad.
- Order matters: open with something that stops the scroll, build, put the strongest moment on the drop, end \
on a line that sticks.
"""


def pick_moments(sources: List[Dict[str, Any]], style: str, theme: str, length: int,
                 guidance: str = "") -> Dict[str, Any]:
    st = STYLES[style]
    budget = max(8000, 90000 // max(1, len(sources)))
    blocks = [_transcript_block(i + 1, s, budget) for i, s in enumerate(sources)]
    prompt = ("\n\n".join(blocks)
              + f"\n\nTHE EDIT: a {st['name'].lower()} edit, about {length} seconds long. {st['what']}\n"
              + f"What it's about: {theme.strip() or 'the best moments'}\n"
              + f"Moments: {st['brief']}")
    system = PICK_SYSTEM + (f"\n\nCAMPAIGN RULES (these override the rest):\n{guidance}" if guidance else "")
    client = highlights._client()
    message = client.messages.create(model=CLAUDE_MODEL, max_tokens=4000, system=system, tools=[PICK_TOOL],
                                     tool_choice={"type": "tool", "name": "pick_moments"},
                                     messages=[{"role": "user", "content": prompt}])
    got = toolio.tool_inputs(message)
    if not got:
        raise RuntimeError("Claude's answer couldn't be read — try again")
    reply = got[0]
    moments = []
    raw = toolio.coerce(reply.get("moments"))
    for k, m in enumerate(raw if isinstance(raw, list) else []):
        m = toolio.as_dict(m)
        v = highlights._int(m.get("video"), 0) - 1
        if not 0 <= v < len(sources):
            continue
        job = sources[v]
        dur = float(job.get("duration") or 0) or 1e9
        try:
            start, end = max(0.0, float(m["start"])), min(dur, float(m["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end - start < 0.8:
            continue
        hit = highlights._num(m.get("hit"), start)
        moments.append({
            "id": f"m{k + 1}", "source": job["id"], "start": round(start, 2), "end": round(end, 2),
            "hit": round(min(max(hit, start), end), 2), "text": highlights._text(m.get("text"))[:160],
            "kind": str(m.get("kind") or "quote"), "drop": bool(m.get("drop")),
            "why": highlights._text(m.get("why"))[:160],
        })
    if not moments:
        raise RuntimeError("Claude didn't find moments that fit — try another theme or more videos")
    if not any(m["drop"] for m in moments):
        moments[min(len(moments) - 1, len(moments) // 2)]["drop"] = True
    first = next(i for i, m in enumerate(moments) if m["drop"])
    for i, m in enumerate(moments):                       # only one drop
        m["drop"] = i == first
    tags = [str(t).strip().lstrip("#") for t in toolio.as_list(reply.get("hashtags")) if str(t).strip()][:8]
    return {"title": highlights._text(reply.get("title"))[:80], "caption": highlights._text(reply.get("caption"))[:600],
            "hashtags": tags, "moments": moments}


def snap_moments(moments: List[Dict[str, Any]], style: str) -> None:
    """Voice edits play whole sentences: put each moment's edges on sentence edges."""
    if STYLES[style]["pace"] != "speech":
        return
    cache: Dict[str, List[Dict[str, Any]]] = {}
    for m in moments:
        if m["source"] not in cache:
            job = store.get_job(m["source"]) or {}
            cache[m["source"]] = transcribe.in_order(_loads(job.get("transcript"), {}).get("words") or [])
        words = cache[m["source"]]
        if words:
            s, e = highlights.clean_bounds(m["start"], m["end"], words, min_len=1.5)
            m["start"], m["end"] = round(s, 2), round(e, 2)
            m["hit"] = round(min(max(m["hit"], m["start"]), m["end"]), 2)


# --- the timeline ---------------------------------------------------------------------

def _words_for(source_words: List[Dict[str, Any]], src_start: float, src_len: float,
               at: float, speed: float, until: float) -> List[Dict[str, Any]]:
    out = []
    for w in source_words:
        if w["start"] < src_start - 0.02 or w["start"] >= min(src_start + src_len, until):
            continue
        out.append({"w": w["w"], "t": round(at + (w["start"] - src_start) / speed, 3),
                    "end": round(at + (min(w["end"], until) - src_start) / speed, 3)})
    return out


def build_timeline(moments: List[Dict[str, Any]], style: str, length: int,
                   sound: Optional[Dict[str, Any]], effects: Dict[str, bool],
                   words_by_source: Dict[str, List[Dict[str, Any]]],
                   voice_level: Optional[float] = None, music_level: Optional[float] = None) -> Dict[str, Any]:
    """Where every moment sits in the edit, cut on the beat, the best one on the drop."""
    st = STYLES[style]
    fx = {**st["effects"], **{k: bool(v) for k, v in (effects or {}).items() if k in EFFECTS}}
    analysis = (sound or {}).get("analysis") or {}
    song_beats = analysis.get("beats") or []
    bars = set(round(b, 3) for b in analysis.get("bars") or [])
    music = None
    segments: List[Dict[str, Any]] = []
    ordered = [m for m in moments if not m.get("off")]
    if not ordered:
        raise ValueError("Every moment is switched off — turn at least one back on")

    if st["pace"] == "beat":
        if not song_beats:
            raise ValueError(f"A {st['name']} edit is cut to music — pick a song first")
        period = float(analysis.get("bpm") and 60.0 / analysis["bpm"]) or 0.5
        drop = float(analysis.get("drop") or song_beats[0])
        pre_beats = 8 if style == "hype" else 4
        drop_i = min(range(len(song_beats)), key=lambda i: abs(song_beats[i] - drop))
        start_i = max(0, drop_i - pre_beats)
        s0 = song_beats[start_i]
        end_t = s0 + length
        if end_t > analysis.get("duration", end_t):                 # not enough song: start earlier
            s0 = max(0.0, analysis["duration"] - length)
            start_i = min(range(len(song_beats)), key=lambda i: abs(song_beats[i] - s0))
            s0 = song_beats[start_i]
        grid = [b - s0 for b in song_beats[start_i:] if b - s0 <= length + 1e-6]
        drop_at = song_beats[drop_i] - s0
        # cut points: slower before the drop, on the beat after it
        per_cut = max(1, round(st["cut"] / period))
        per_build = max(1, round(st["build_cut"] / period))
        cuts, k = [0.0], 0
        while k < len(grid) - 1:
            step = per_build if grid[k] < drop_at - 1e-6 else per_cut
            nxt = min(len(grid) - 1, k + step)
            if grid[k] < drop_at - 1e-6 < grid[nxt]:            # never cut across the drop: land on it
                nxt = next(i for i, g in enumerate(grid) if g >= drop_at - 1e-6)
            if nxt == k:
                break
            cuts.append(grid[nxt])
            k = nxt
        if cuts[-1] < length - 0.05 and length - cuts[-1] < period * 0.75:
            cuts[-1] = length
        elif cuts[-1] < length - 0.05:
            cuts.append(length)
        slots = list(zip(cuts[:-1], cuts[1:]))
        # moments into slots: the drop moment opens on the drop, the rest in order around it
        drop_m = next((m for m in ordered if m.get("drop")), ordered[len(ordered) // 2])
        rest = [m for m in ordered if m is not drop_m]
        drop_slot = next((i for i, (a, _) in enumerate(slots) if abs(a - drop_at) < 0.02), len(slots) // 3)
        queue_before, queue_after = rest[:max(1, drop_slot // 2)], rest[max(1, drop_slot // 2):]
        used: Dict[str, float] = {}

        def take(pool: List[Dict[str, Any]], i: int) -> Dict[str, Any]:
            return pool[i % len(pool)] if pool else drop_m

        bi = ai = 0
        current, run_left = None, 0.0
        for idx, (a, b) in enumerate(slots):
            dur = b - a
            is_drop = abs(a - drop_at) < 0.02
            if is_drop:
                current, run_left = drop_m, max(dur, min(st.get("max_run", 4.0) * 1.5,
                                                         drop_m["end"] - drop_m["start"]))
            elif current is None or run_left <= 0.05:
                if a < drop_at:
                    current = take(queue_before, bi)
                    bi += 1
                else:
                    current = take(queue_after, ai) if queue_after else take(queue_before + [drop_m], ai)
                    ai += 1
                run_left = min(st.get("max_run", 4.0), max(dur, current["end"] - current["start"]))
            first_slot = used.get(current["id"]) is None
            speed = 0.5 if (is_drop and fx.get("slowmo") and style == "hype") else (
                0.8 if (fx.get("slowmo") and style == "luxury") else 1.0)
            offset = used.get(current["id"], 0.0)
            span = current["end"] - current["start"]
            if is_drop:                                  # the hit lands right on the drop
                offset = max(0.0, min(span - dur * speed, current["hit"] - current["start"] - 0.1))
            if offset + dur * speed > span:              # ran out of this moment: start it again
                offset = 0.0 if span >= dur * speed else 0.0
            src_start = current["start"] + offset
            used[current["id"]] = offset + dur * speed
            run_left -= dur
            seg_beats = [g - a for g in grid if a + 1e-3 < g < b - 1e-3]
            on_bar = any(abs((a + s0) - x) < 0.03 for x in bars)
            segments.append({
                "moment": current["id"], "source": current["source"], "src_start": round(src_start, 3),
                "speed": speed, "at": round(a, 3), "dur": round(dur, 3), "beats": [round(x, 3) for x in seg_beats],
                "drop": is_drop, "flash": bool(fx.get("flash") and (is_drop or on_bar or idx == 0)),
                "shake": bool(fx.get("shake") and is_drop), "glitch": bool(fx.get("glitch") and is_drop),
                "hit": None, "voice_until": None,
                "text": current["text"] if (first_slot or is_drop) else "",
                "first": first_slot,
            })
        music = {"start": round(s0, 3), "drop_at": round(drop_at, 3)}
        total = length
    else:
        # speech pace: each moment plays whole; cuts snapped onto the beat when there is music
        drop_m = next((m for m in ordered if m.get("drop")), None)
        at = 0.0
        for m in ordered:
            span = m["end"] - m["start"]
            segments.append({"moment": m["id"], "source": m["source"], "src_start": m["start"], "speed": 1.0,
                             "at": round(at, 3), "dur": round(span, 3), "beats": [], "drop": m is drop_m,
                             "flash": bool(fx.get("flash") and m is drop_m),
                             "shake": bool(fx.get("shake") and style == "funny"),
                             "glitch": False, "hit": round(m["hit"] - m["start"], 3), "voice_until": m["end"],
                             "text": m["text"], "first": True})
            at += span
            if at >= length * 1.25:
                break
        total = at
        if song_beats:
            # line the song up so its drop meets the drop moment, then snap every cut onto the next
            # beat (the picture holds a moment longer, never cuts a word). Snapping moves the drop
            # moment a little, so line up and snap twice from the speech lengths.
            drop_seg_i = next((i for i, s in enumerate(segments) if s["drop"]), 0)
            drop = float(analysis.get("drop") or song_beats[0])
            natural = [(s["at"], s["dur"]) for s in segments]
            s0 = drop - natural[drop_seg_i][0]
            for _ in range(2):
                if s0 < 0 or s0 + total > analysis.get("duration", 0):
                    s0 = max(0.0, min(song_beats[0], analysis.get("duration", 0) - total))
                shift = 0.0
                for seg, (at0, dur0) in zip(segments, natural):
                    seg["at"], seg["dur"] = round(at0 + shift, 3), dur0
                    end_song = s0 + seg["at"] + seg["dur"]
                    nxt = next((b for b in song_beats if b >= end_song - 0.02), None)
                    if nxt is not None and nxt - end_song <= period_of(analysis) * 0.95:
                        extra = max(0.0, nxt - end_song)
                        seg["dur"] = round(seg["dur"] + extra, 3)
                        shift += extra
                s0 = drop - segments[drop_seg_i]["at"]
            for seg in segments:
                seg["beats"] = [round(b - s0 - seg["at"], 3) for b in song_beats
                                if seg["at"] + 0.05 < b - s0 < seg["at"] + seg["dur"] - 0.05]
            total = round(segments[-1]["at"] + segments[-1]["dur"], 3)
            music = {"start": round(s0, 3), "drop_at": round(drop - s0, 3)}
    for seg in segments:
        words = words_by_source.get(seg["source"]) or []
        until = seg["voice_until"] or (seg["src_start"] + seg["dur"] * seg["speed"])
        seg["words"] = _words_for(words, seg["src_start"], seg["dur"] * seg["speed"], seg["at"], seg["speed"], until)
    vl = st["voice"] if voice_level is None else max(0.0, min(1.5, voice_level))
    ml = st["music"] if music_level is None else max(0.0, min(1.5, music_level))
    if music:
        music.update({"sound": (sound or {}).get("id"), "level": ml})
    return {"style": style, "length": round(total, 3), "segments": segments, "music": music,
            "voice": vl, "effects": fx}


def period_of(analysis: Dict[str, Any]) -> float:
    bpm = analysis.get("bpm") or 120
    return 60.0 / float(bpm)


# --- the whole edit ---------------------------------------------------------------------

def words_for_sources(source_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    out = {}
    for sid in set(source_ids):
        job = store.get_job(sid) or {}
        out[sid] = transcribe.in_order(_loads(job.get("transcript"), {}).get("words") or [])
    return out


def create(settings: Dict[str, Any]) -> str:
    """Check the request and start making the edit in the background."""
    from . import campaign, pipeline
    style = settings.get("style") if settings.get("style") in STYLES else "hype"
    sources = [s for s in (settings.get("sources") or []) if store.get_job(s)]
    if not sources:
        raise ValueError("Pick at least one video to cut the edit from")
    for s in sources:
        job = store.get_job(s) or {}
        if not job.get("source_path") or not Path(job["source_path"]).is_file():
            raise ValueError(f"“{job.get('title') or s}” isn't on this PC any more — make clips from it again first")
    sound = store.get_sound(settings.get("sound") or "") if settings.get("sound") else None
    if STYLES[style].get("needs_music") and not sound:
        raise ValueError(f"A {STYLES[style]['name']} edit is cut to music — add or pick a song first")
    camp_id = str(settings.get("campaign_id") or "")
    if camp_id:
        camp = store.get_campaign(camp_id)
        if not camp:
            raise ValueError("That campaign doesn't exist any more")
        if sound and not campaign.allowed(camp["rulebook"], "music"):
            raise ValueError(f"{camp['name']}: the campaign's rules don't allow added music. If the brief allows it, "
                             "switch music on in the campaign's rules, or pick “No music”.")
    length = int(settings.get("length") or STYLES[style]["length"])
    clean = {"style": style, "sources": sources, "sound": sound["id"] if sound else "",
             "theme": str(settings.get("theme") or "")[:300], "length": max(10, min(90, length)),
             "effects": {k: bool(v) for k, v in (settings.get("effects") or {}).items() if k in EFFECTS},
             "campaign_id": camp_id}
    titles = [(store.get_job(s) or {}).get("title") or "" for s in sources]
    eid = store.create_edit(f"{STYLES[style]['name']} edit — {titles[0][:50]}", clean, camp_id)
    threading.Thread(target=run, args=(eid,), daemon=True).start()
    return eid


def _stage(eid: str, stage: str, progress: int) -> None:
    store.update_edit(eid, stage=stage, progress=max(0, min(100, progress)), status="running")


def run(eid: str, repick: bool = True) -> None:
    """Pick (unless re-making), lay out, render. Never raises: a failure is stored on the edit."""
    from . import campaign, editrender, notify, pipeline
    with _lock:
        try:
            edit = store.get_edit(eid)
            if not edit:
                return
            s = edit["settings"]
            plan = edit.get("plan") or {}
            sources = [store.get_job(x) for x in s["sources"]]
            sources = [x for x in sources if x]
            rules = None
            if s.get("campaign_id"):
                camp = store.get_campaign(s["campaign_id"])
                rules = camp["rulebook"] if camp else None
            if repick or not plan.get("moments"):
                _stage(eid, "Claude is picking the moments", 8)
                picked = pick_moments(sources, s["style"], s.get("theme", ""), s["length"],
                                      campaign.picker_guidance(rules) if rules else "")
                snap_moments(picked["moments"], s["style"])
                tags = picked["hashtags"]
                if rules:
                    must = [h.get("text", "") if isinstance(h, dict) else str(h)
                            for h in ((rules.get("caption") or {}).get("hashtags") or [])]
                    tags = [t.lstrip("#") for t in must if t] + [t for t in tags if ("#" + t).lower() not in
                                                                   {m.lower() for m in must}]
                plan = {"moments": picked["moments"], "title": picked["title"]}
                store.update_edit(eid, title=picked["title"] or edit["title"], caption=picked["caption"],
                                  hashtags=tags[:10])
            _stage(eid, "Fitting it to the beat", 22)
            sound = store.get_sound(s.get("sound") or "") if s.get("sound") else None
            words = words_for_sources([m["source"] for m in plan["moments"]])
            timeline = build_timeline(plan["moments"], s["style"], s["length"], sound, s.get("effects") or {},
                                      words, s.get("voice"), s.get("music"))
            plan["timeline"] = timeline
            store.update_edit(eid, plan=plan)
            EDIT_DIR.mkdir(parents=True, exist_ok=True)
            out = EDIT_DIR / f"{eid}.mp4"
            thumb = EDIT_DIR / f"{eid}.jpg"
            editrender.render(timeline, {x["id"]: x for x in sources}, sound, out, thumb,
                              progress=lambda p: _stage(eid, f"Rendering — {p}%", 25 + int(p * 0.73)))
            store.update_edit(eid, status="done", stage="Done", progress=100, file=str(out), thumb=str(thumb),
                              error="")
            edit = store.get_edit(eid) or {}
            if notify.connected():
                tags = " ".join("#" + t for t in edit.get("hashtags") or [])
                notify.send_video(out, f"🎬 <b>{notify.esc(edit.get('title') or 'Your edit')}</b> is ready\n"
                                       f"{notify.esc(edit.get('caption') or '')}\n{notify.esc(tags)}",
                                  timeline["length"])
        except Exception as exc:
            traceback.print_exc()
            store.update_edit(eid, status="failed", stage="Failed", error=str(exc)[:400], progress=100)


def remake(eid: str, changes: Dict[str, Any]) -> None:
    """Apply changes from the edit page (style, song, effects, levels, order,
    moments off or on, new text) and render again — Claude only when asked
    for new moments."""
    edit = store.get_edit(eid)
    if not edit:
        raise ValueError("Edit not found")
    if edit["status"] in ("queued", "running"):
        raise ValueError("This edit is still being made — wait a moment")
    s = dict(edit["settings"])
    plan = dict(edit.get("plan") or {})
    if changes.get("style") in STYLES:
        s["style"] = changes["style"]
    if "sound" in changes:
        sound = store.get_sound(changes["sound"]) if changes["sound"] else None
        s["sound"] = sound["id"] if sound else ""
    if STYLES[s["style"]].get("needs_music") and not s.get("sound"):
        raise ValueError(f"A {STYLES[s['style']]['name']} edit needs a song")
    if changes.get("length"):
        s["length"] = max(10, min(90, int(changes["length"])))
    if isinstance(changes.get("effects"), dict):
        s["effects"] = {**(s.get("effects") or {}),
                        **{k: bool(v) for k, v in changes["effects"].items() if k in EFFECTS}}
    for key in ("voice", "music"):
        if changes.get(key) is not None:
            s[key] = max(0.0, min(1.5, float(changes[key])))
    if changes.get("theme") is not None:
        s["theme"] = str(changes["theme"])[:300]
    moments = plan.get("moments") or []
    if isinstance(changes.get("moments"), list):
        by_id = {m["id"]: m for m in moments}
        new = []
        for m in changes["moments"]:
            base = by_id.get(m.get("id"))
            if not base:
                continue
            base = dict(base)
            if "text" in m:
                base["text"] = str(m["text"])[:160]
            if "off" in m:
                base["off"] = bool(m["off"])
            if "drop" in m:
                base["drop"] = bool(m["drop"])
            new.append(base)
        if new:
            if sum(1 for m in new if m.get("drop")) != 1:
                for i, m in enumerate(new):
                    m["drop"] = i == next((j for j, x in enumerate(new) if x.get("drop")), len(new) // 2)
            plan["moments"] = new
    store.update_edit(eid, settings=s, plan=plan, status="queued", stage="Waiting", progress=0, error="")
    threading.Thread(target=run, args=(eid, bool(changes.get("repick"))), daemon=True).start()


def edit_json(e: Dict[str, Any]) -> Dict[str, Any]:
    plan = e.get("plan") or {}
    tl = plan.get("timeline") or {}
    s = e.get("settings") or {}
    st = STYLES.get(s.get("style") or "hype", STYLES["hype"])
    return {
        "id": e["id"], "title": e.get("title") or "", "status": e.get("status"), "stage": e.get("stage"),
        "progress": e.get("progress") or 0, "error": e.get("error") or "", "created_at": e.get("created_at"),
        "style": s.get("style"), "style_name": st["name"], "settings": s,
        "length": tl.get("length"), "moments": plan.get("moments") or [],
        "segments": len(tl.get("segments") or []), "effects": tl.get("effects") or {**st["effects"], **(s.get("effects") or {})},
        "voice": tl.get("voice", st["voice"]), "music": (tl.get("music") or {}).get("level", st["music"]),
        "caption": e.get("caption") or "", "hashtags": e.get("hashtags") or [],
        "video_url": f"/media/edit/{e['id']}.mp4?v={int(e.get('updated_at') or 0)}" if e.get("file") else None,
        "thumb_url": f"/media/edit/{e['id']}.jpg?v={int(e.get('updated_at') or 0)}" if e.get("thumb") else None,
        "campaign_id": e.get("campaign_id") or "",
    }
