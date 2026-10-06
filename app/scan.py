"""Creator Scan, part two: the engine.

A scan goes through a creator's whole catalog in stages, each one resumable
from what's stored (the catalog's statuses are the source of truth):

  list   → every video on the creator's links (catalog.py), filtered, de-duplicated
  words  → each video's words (subtitles first, Whisper only without them), and
           straight after, its screening: Claude reads it in ~25-minute blocks
           and returns candidate moments, each with an exact quote that is
           checked against the words (a quote that isn't there drops the moment)
  screen → anything read but not screened yet (e.g. Claude was busy)
  rank   → the best ~100 of each kind across the whole catalog, put on one scale
           by Claude, the same story told in several videos kept once, a hook each
  fetch  → only the best N moments' parts of the video are downloaded (±8 s)
  check  → the picture: movement, faces, cuts, and for crazy/reaction/funny a
           Claude look at a few frames; nobody on screen when the campaign needs
           the creator → dropped
  done

One scan works at a time (a second waits its turn). Pause stops at the next
safe point; a restart leaves the scan "paused" (settle_interrupted) and Resume
carries on with nothing read twice. When the transcription allowance is used
up the scan shows "waiting" with the time it carries on, and does so by itself.

The score of a moment (0-100) is `merge_score`: a fixed, documented weighting
of what Claude thought of the words and what the numbers say (most replayed,
loudness/laughter, how the video did against its channel, how recent it is,
and — once checked — the picture). Pure, so it is unit-tested.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import catalog, creators, highlights, media, notify, store, toolio
from .config import CLAUDE_MODEL, CLAUDE_SCREEN_MODEL, DATA_DIR, SCAN_REQUEST_GAP

SECTION_DIR = DATA_DIR / "sections"
AUDIO_WORK = DATA_DIR / "scan_audio"
STAGES = ["list", "words", "screen", "rank", "fetch", "check"]
STAGE_SHARE = {"list": (0.0, 0.05), "words": (0.05, 0.70), "screen": (0.70, 0.75), "rank": (0.75, 0.80),
               "fetch": (0.80, 0.93), "check": (0.93, 1.0)}
GREAT = 60.0                    # a moment this good counts as "great" in the progress line
SECTION_PAD = 8.0
FETCH_PAUSE = 6.0               # seconds between two section downloads
MAX_FETCH_TRIES = 3
RANK_TOP = 100                  # per kind, ranked across the catalog
MIN_LEN, MAX_LEN = 6.0, 90.0
VISUAL_KINDS = {"crazy", "reaction", "funny"}
SCREEN_VERSION = 1              # bump when the screening prompt changes: old cached screenings are not reused
READ_MILESTONE = 100            # Telegram: "read 100 of 412 videos …"

KIND_TO_CLIP = {"crazy": "hype", "funny": "funny", "success": "reveal", "money": "reveal", "quote": "hot_take",
                "reaction": "reaction", "hype": "hype", "story": "story"}
KIND_TO_EDIT = {"crazy": "hype", "funny": "funny", "success": "money", "money": "money", "quote": "quote",
                "reaction": "reaction", "hype": "hype", "story": "story"}
KIND_WORDS = {
    "crazy": "crazy — wild statements, shouting, unhinged energy, a \"did he really just say that\"",
    "funny": "funny — jokes, banter, roasts, chaos, people cracking up",
    "success": "success — wins, proof it worked, milestones, a student's result, the payoff of the work",
    "money": "money — profit and loss shown or said, big numbers, what something cost or earned, flexes",
    "quote": "quote — a memorable line or piece of advice that stands on its own",
    "reaction": "reaction — him reacting to something (a chart, a call, a clip, news): the reaction is the moment",
    "hype": "hype — motivation, pumped-up energy, a speech that gets you going",
    "story": "story — a story with a turn: the setup, what was at stake, how it ended",
}

# Stand-ins for tests: the scan's sleep, the clock the request gaps are measured on, and "now".
SLEEP: Callable[[float], None] = time.sleep
CLOCK: Callable[[], float] = time.monotonic
NOW: Callable[[], float] = time.time


class Paused(Exception):
    """Pause was pressed: stop at this safe point."""


# --- the score -----------------------------------------------------------------------------
#
# Every signal becomes a number from 0 to 1, then a weighted average makes the score:
#   text     0.55  Claude's score of the words (after ranking: its one-scale score across the catalog)
#   heat     0.15  YouTube's "most replayed": the highest point of the curve inside the moment
#   outlier  0.10  views ÷ the channel's median: 1× → 0.5, 4× or more → 1, ¼× or less → 0
#   loud     0.06  how loud the moment gets, against the rest of the video (0 = quietest, 1 = loudest)
#   recency  0.05  uploaded in the last month → 1, falling to 0 at three years
#   picture  0.06  after the picture check: Claude's look, the movement, a person on screen
#   laugh    0.03  laughter/applause marked in the words
# A signal that isn't known yet counts as middling (0.5; laughter as 0) so moments with and
# without it stay comparable, and the score never jumps around when one arrives. Every weight
# is positive, so a better signal can only raise a score.

WEIGHTS = {"text": 0.55, "heat": 0.15, "outlier": 0.10, "loud": 0.06, "recency": 0.05, "picture": 0.06,
           "laugh": 0.03}
NEUTRAL = {"text": 0.5, "heat": 0.5, "outlier": 0.5, "loud": 0.5, "recency": 0.5, "picture": 0.5, "laugh": 0.0}


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _f(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def signal_values(sig: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Each signal on a 0-1 scale (None when unknown)."""
    out: Dict[str, Optional[float]] = {k: None for k in WEIGHTS}
    rank = _f(sig.get("rank"))
    text = _f(sig.get("text"))
    out["text"] = _clamp(rank) if rank is not None else (_clamp(text) if text is not None else None)
    heat = _f(sig.get("heat"))
    out["heat"] = _clamp(heat) if heat is not None else None
    ratio = _f(sig.get("outlier"))
    out["outlier"] = _clamp(0.5 + math.log2(ratio) / 4) if ratio and ratio > 0 else None
    loud = _f(sig.get("loud"))
    out["loud"] = _clamp(loud) if loud is not None else None
    days = _f(sig.get("age_days"))
    out["recency"] = _clamp(1 - (max(0.0, days) - 30) / 1065) if days is not None else None
    laugh = _f(sig.get("laugh"))
    out["laugh"] = _clamp(laugh) if laugh is not None else None
    pic = [v for v in (_f(sig.get("visual")), _f(sig.get("motion")), _f(sig.get("face_share"))) if v is not None]
    out["picture"] = _clamp(sum(pic) / len(pic)) if pic else None
    return out


def merge_score(sig: Dict[str, Any]) -> float:
    """The moment's score, 0-100, from its signals (see WEIGHTS above)."""
    vals = signal_values(sig)
    total = sum(w * (vals[k] if vals[k] is not None else NEUTRAL[k]) for k, w in WEIGHTS.items())
    return round(100.0 * total / sum(WEIGHTS.values()), 1)


def heat_peak(heatmap: Sequence[Dict[str, Any]], start: float, end: float) -> Optional[float]:
    """The highest "most replayed" value inside [start, end] (YouTube scales it 0-1 per video)."""
    vals = [float(h["value"]) for h in heatmap or []
            if float(h.get("end_time", 0)) > start and float(h.get("start_time", 0)) < end]
    return round(max(vals), 4) if vals else None


def loud_level(loud: Optional[Dict[str, Any]], start: float, end: float) -> Optional[float]:
    """How loud the moment's loudest point is against the whole recording, 0-1 (a percentile)."""
    if not loud or not loud.get("db"):
        return None
    hop = float(loud.get("hop") or 1.0)
    db = loud["db"]
    inside = db[max(0, int(start / hop)):max(0, int(math.ceil(end / hop))) + 1]
    if not inside:
        return None
    peak = max(inside)
    return round(sum(1 for v in db if v <= peak) / len(db), 3)


_LAUGH = re.compile(r"\[(?:laughter|laughs|laughing|applause|cheering)\]|\(laughs?\)|\b(?:haha+|lmao+|hahaha+)\b", re.I)


def laugh_level(text: str) -> Optional[float]:
    hits = len(_LAUGH.findall(text or ""))
    return min(1.0, hits / 2.0) if hits else None


def age_days(upload_date: str, now: Optional[float] = None) -> Optional[float]:
    day = catalog.ymd(upload_date)
    if not day:
        return None
    try:
        then = datetime.strptime(day, "%Y%m%d").timestamp()
    except ValueError:
        return None
    return max(0.0, ((now or NOW()) - then) / 86400.0)


def dedupe_stories(moments: Sequence[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """The same story told in several videos is kept once — its best telling.
    Returns (dropped id, kept id) pairs. A moment already used for a clip or an edit is
    never dropped (and wins its story)."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for m in moments:
        key = re.sub(r"[^a-z0-9]+", "-", str(m.get("story_key") or "").lower()).strip("-")
        if key and m.get("status") != "dropped":
            groups.setdefault(key, []).append(m)
    out: List[Tuple[str, str]] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        best = max(group, key=lambda m: (m.get("status") == "used", float(m.get("score") or 0),
                                         -float(m.get("created_at") or 0), str(m.get("id"))))
        out += [(m["id"], best["id"]) for m in group if m is not best and m.get("status") != "used"]
    return out


def prune_overlaps(moments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Two candidates covering the same stretch of one video: keep the better one."""
    kept: List[Dict[str, Any]] = []
    for m in sorted(moments, key=lambda m: -float(m.get("score") or 0)):
        length = m["end"] - m["start"]
        if any(min(k["end"], m["end"]) - max(k["start"], m["start"]) > 0.5 * min(length, k["end"] - k["start"])
               for k in kept):
            continue
        kept.append(m)
    return kept


def words_between(transcript: Dict[str, Any], start: float, end: float, limit: int = 1500) -> str:
    """What's said in [start, end], as text."""
    segs = [s for s in transcript.get("segments") or []
            if float(s["end"]) > start and float(s["start"]) < end and str(s.get("text") or "").strip()]
    return " ".join(str(s["text"]).strip() for s in segs)[:limit]


def _numbers_said(text: str, said: str) -> bool:
    from . import edits
    return edits._numbers_said(text, said)


def clean_hook(hook: Any, said: str, speaker: str, name: str) -> str:
    """A hook that may go on screen: at most 8 words, every number one that is said, and never
    putting someone else's words in the creator's mouth."""
    h = re.sub(r"\s+", " ", str(hook or "")).strip().strip('"“”').strip()
    if not h:
        return ""
    words = h.split()
    if len(words) > 8:
        words = words[:8]
        while words and words[-1].lower().strip(",.;:—-") in ("and", "the", "a", "an", "to", "of", "for", "with",
                                                               "his", "her", "in", "on", "at", "but", "or"):
            words.pop()
        h = " ".join(words).rstrip(",;:—-")
    if not _numbers_said(h, said):
        return ""
    if speaker == "other" and name:
        n = re.escape(name.strip().lower())
        if re.search(rf"\b{n}\b\s*(:|says|said|on why|explains|reveals)", h.lower()):
            return ""
    return h[:80]


def needs_person(rules: Optional[Dict[str, Any]]) -> bool:
    """Does the campaign need the creator in the picture? (its main person, or a rule that says so)"""
    if not rules:
        return False
    if ((rules.get("primary_focus") or {}).get("name") or "").strip():
        return True
    text = " ".join(str(x) for x in (rules.get("other_rules") or []) + (rules.get("look_for") or []))
    return bool(re.search(r"\b(must|has to|have to|needs? to|should|only)\b[^.]{0,60}\b(feature|show|include|be on "
                          r"screen|on camera|appear|visible)", text, re.I))


def creator_visible(section_path: Optional[Path], moment: Dict[str, Any], look: Dict[str, Any],
                    rules: Optional[Dict[str, Any]] = None) -> Optional[bool]:
    """Is the creator on screen in this moment? True / False / None (can't tell).

    Today this answers "is a person clearly on screen": faces found in the frames (YuNet) and
    Claude's look ("is the main person clearly visible?"). Nobody is identified from their face.
    This is the one place Part 1's identity check (is it really the campaign's person?) will be
    plugged in — keep the signature."""
    faces = look.get("face_share")
    size = look.get("face_size") or 0.0
    said = ((look.get("claude") or {}).get("person_visible") or "").lower()
    if said == "yes" or (faces is not None and faces >= 0.3 and size >= 0.04):
        return True
    if said == "no" and (faces or 0.0) < 0.15:
        return False
    if faces == 0 and said in ("", "no"):
        return False
    return None


def _rules_for(creator: Dict[str, Any]) -> Dict[str, Any]:
    camp = store.get_campaign(creator.get("campaign_id") or "") if creator.get("campaign_id") else None
    rb = (camp or {}).get("rulebook")
    return rb if isinstance(rb, dict) else {}


def guidance(rules: Dict[str, Any]) -> str:
    """What the campaign asks for, for screening and ranking (read defensively: old rulebooks lack keys)."""
    if not rules:
        return ""
    from . import campaign
    lines = []
    try:
        lines.append(campaign.picker_guidance(rules))
    except Exception:
        pass
    look = [str(x) for x in (rules.get("look_for") or []) if str(x).strip()]
    if look:
        lines.append("The brief asks for: " + "; ".join(look[:10]) + ".")
    focus = ((rules.get("primary_focus") or {}).get("name") or "").strip()
    if focus:
        lines.append(f"{focus} must be the main person: skip moments that someone else carries.")
    avoid = [str(x) for x in (rules.get("tone_avoid") or []) if str(x).strip()]
    if avoid and not any("Never write hooks" in ln for ln in lines):
        lines.append("Never pick or word anything that touches: " + "; ".join(avoid[:8]) + ".")
    return "\n".join(x for x in lines if x)


# --- Claude ------------------------------------------------------------------------------------

SCREEN_TOOL = {
    "name": "submit_moments",
    "description": "Return the moments in this stretch of the transcript that are worth clipping.",
    "input_schema": {"type": "object", "properties": {"moments": {"type": "array", "items": {
        "type": "object", "properties": {
            "kind": {"type": "string", "enum": creators.KINDS},
            "start": {"type": "number", "description": "Seconds: the first word of the sentence that sets it up."},
            "end": {"type": "number", "description": "Seconds: right after the payoff. 10-75 seconds after start."},
            "hit": {"type": "number", "description": "Seconds: the instant it lands — the punchline, the number, "
                                                     "the peak of the reaction."},
            "score": {"type": "integer", "description": "0-10. 10 = would go viral cut on its own. Most moments in "
                                                        "most videos are 3-6; 8+ is rare."},
            "reason": {"type": "string", "description": "One line: why it works."},
            "quote": {"type": "string", "description": "The key words, copied EXACTLY from the transcript, "
                                                       "5-25 words."},
            "speaker": {"type": "string", "enum": ["creator", "other", "unclear"],
                        "description": "Who says the quote: the creator, someone else (a guest, a caller, a clip "
                                       "he plays), or unclear."},
        }, "required": ["kind", "start", "end", "hit", "score", "reason", "quote", "speaker"]}}},
        "required": ["moments"]},
}

SCREEN_SYSTEM = """You read the transcript of a video by {name} for a clipping page and pick the moments \
worth cutting into TikToks, Reels and Shorts. You see one stretch of the transcript at a time, with the \
start of each line in seconds.

The kinds of moment (use only these):
{kinds}

For each moment: its kind; start (the first word of the sentence that sets it up) and end (right after \
the payoff), 10-75 seconds apart; the hit (the instant it lands); a score from 0 to 10 — honest and \
spread out: most moments are 3-6, 8 or more is rare; one line on why; the quote: the line that makes it, \
copied word for word from the transcript (5-25 words); and who says the quote — {name} ("creator"), \
someone else ("other": a guest, a caller, a clip he plays) or "unclear".

Only moments really in the words. Skip greetings, sponsor reads and ads, housekeeping, technical setup \
talk, and anything that only makes sense with what came before. If this stretch has nothing worth \
clipping, return no moments."""

RANK_TOOL = {
    "name": "rank_moments",
    "description": "Score every candidate on one scale, mark the same story, write the hooks.",
    "input_schema": {"type": "object", "properties": {"moments": {"type": "array", "items": {
        "type": "object", "properties": {
            "id": {"type": "integer", "description": "The candidate's number."},
            "score": {"type": "integer", "description": "0-100 on ONE scale across all of them. Spread them out."},
            "story_key": {"type": "string", "description": "A short lowercase label for the story or topic, e.g. "
                          "'first-million-2019'. Candidates telling the SAME story (in different videos too) get "
                          "the SAME label; different stories get different labels."},
            "hook": {"type": "string", "description": "The on-screen hook: at most 8 words, makes sense to someone "
                     "who never saw the video (who or what), only facts said in the moment, every number one that "
                     "is said. If someone other than the creator says it, the hook must not credit the creator."},
            "why": {"type": "string", "description": "A few words on why it ranks there."},
        }, "required": ["id", "score", "story_key", "hook"]}}}, "required": ["moments"]},
}

RANK_SYSTEM = """You rank moments found across {name}'s whole catalog — many videos, read separately, so \
their first scores are not comparable. Put them all on one scale (0-100): a few moments carry a catalog, \
most are ordinary — say so with the numbers.

The same story often comes up in several videos (the first big win, a famous trade, a family story). Give \
every candidate a story label; candidates telling the same story get the same label, so only the best \
telling is kept.

Write each hook for a stranger scrolling with the sound off: who or what, in at most 8 words, only from \
what is said in that moment. Never put someone else's words in {name}'s mouth."""

LOOK_TOOL = {
    "name": "rate_picture",
    "description": "Judge the picture of this moment.",
    "input_schema": {"type": "object", "properties": {
        "strong": {"type": "integer", "description": "1-10: how strong the moment is visually for a short "
                                                     "(movement, expression, energy, something happening)."},
        "person_visible": {"type": "string", "enum": ["yes", "no", "unclear"],
                           "description": "Is the main person clearly visible — face or body big enough to read "
                                          "on a phone?"},
        "what": {"type": "string", "description": "Under 15 words: what the frames show."},
    }, "required": ["strong", "person_visible", "what"]},
}

SEARCH_TOOL = {
    "name": "pick_stretches",
    "description": "Pick the stretches that really answer the search.",
    "input_schema": {"type": "object", "properties": {"moments": {"type": "array", "items": {
        "type": "object", "properties": {
            "n": {"type": "integer", "description": "The stretch's number."},
            "start": {"type": "number"}, "end": {"type": "number"},
            "kind": {"type": "string", "enum": creators.KINDS},
            "quote": {"type": "string", "description": "The key words, copied exactly from the stretch."},
            "score": {"type": "integer", "description": "0-10: how well it answers the search and works as a clip."},
            "why": {"type": "string"},
            "speaker": {"type": "string", "enum": ["creator", "other", "unclear"]},
        }, "required": ["n", "start", "end", "kind", "quote", "score"]}}}, "required": ["moments"]},
}


def _client():
    return highlights._client()


def _key(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _screen_block(name: str, title: str, when: str, text: str, a: float, b: float, rules_text: str) -> List[Dict[str, Any]]:
    system = SCREEN_SYSTEM.format(name=name, kinds="\n".join(f"- {KIND_WORDS[k]}" for k in creators.KINDS))
    if rules_text:
        system += "\n\nCAMPAIGN RULES — a paid campaign's brief; these override anything above:\n" + rules_text
    prompt = (f"Video: {title or 'Untitled'}{f' ({when})' if when else ''}\n"
              f"Transcript from {a:.0f}s to {b:.0f}s (times are seconds in the whole video):\n\n{text}\n\n"
              "Up to 8 moments.")
    return toolio.ask(_client(), "moments", retries=0, model=CLAUDE_SCREEN_MODEL, max_tokens=4000, system=system,
                      tools=[SCREEN_TOOL], tool_choice={"type": "tool", "name": "submit_moments"},
                      messages=[{"role": "user", "content": prompt}])


def verify(cands: Iterable[Dict[str, Any]], transcript: Dict[str, Any], duration: Optional[float],
           name: str = "") -> Tuple[List[Dict[str, Any]], int]:
    """Claude's candidates, checked: real kinds and times, and a quote that is really said
    there (fuzzy match ≥ 0.8 after normalising) — a moment whose quote isn't in the words is
    dropped. Returns (moments, how many were dropped)."""
    out: List[Dict[str, Any]] = []
    dropped = 0
    words = transcript.get("words") or []
    dur = float(duration) if duration else None
    for c in cands:
        c = toolio.as_dict(c)
        kind = str(c.get("kind") or "").lower()
        start, end = _f(c.get("start")), _f(c.get("end"))
        if kind not in creators.KINDS or start is None or end is None or end <= start:
            dropped += 1
            continue
        found = catalog.find_quote(str(c.get("quote") or ""), transcript, start, end)
        if not found:
            dropped += 1
            continue
        qs, qe, match = found
        start, end = max(0.0, min(start, qs - 0.3)), max(end, qe + 0.3)
        if end - start > MAX_LEN:                         # keep the quote, trim around it
            start = max(start, qs - 25.0)
            end = min(end, max(qe + 10.0, start + MAX_LEN))
            end = min(end, start + MAX_LEN)
        if dur:
            end = min(end, dur)
        if end - start < MIN_LEN:
            end = start + MIN_LEN if not dur else min(dur, start + MIN_LEN)
        if words and any(w.get("w", "").strip()[-1:] in ".?!" for w in words[:200]):
            start, end = highlights.clean_bounds(start, end, words, min_len=MIN_LEN)   # sentence edges, when known
        hit = _f(c.get("hit"))
        if hit is None or not start <= hit <= end:
            hit = min(end, max(start, qe))
        speaker = c.get("speaker") if c.get("speaker") in ("creator", "other", "unclear") else "unclear"
        score = _clamp((_f(c.get("score")) or 5.0) / 10.0)
        out.append({"kind": kind, "start": round(start, 2), "end": round(end, 2), "hit": round(hit, 2),
                    "speaker": speaker, "reason": highlights._text(c.get("reason"))[:300],
                    "quote": highlights._text(c.get("quote"))[:300], "match": match, "text_score": score})
    return out, dropped


# --- one run of a scan -----------------------------------------------------------------------

class _Run:
    def __init__(self, scan_id: str, creator_id: str, pause: threading.Event):
        self.scan_id = scan_id
        self.creator_id = creator_id
        self.pause = pause
        self.creator = creators.get_creator(creator_id) or {}
        self.settings = self.creator.get("settings") or creators.clean_settings({})
        self.rules = _rules_for(self.creator)
        self.rules_text = guidance(self.rules)
        self.name = self.creator.get("name") or "the creator"
        self.net = catalog.Net(sleep=self.sleep, clock=CLOCK)
        scan = creators.get_scan(scan_id) or {}
        self.counters = dict(scan.get("counters") or {})
        self.claude_trouble = 0
        self.notes: List[str] = list(self.counters.get("notes") or [])

    # -- pacing and status --
    def check(self) -> None:
        if self.pause.is_set():
            raise Paused()

    def sleep(self, seconds: float) -> None:
        left = float(seconds or 0)
        while left > 0:
            self.check()
            step = min(1.0, left)
            SLEEP(step)
            left -= step
        self.check()

    def wait(self, seconds: float, why: str = "quota") -> None:
        """A long wait shows as "waiting" with the time it carries on; short ones just pass."""
        if seconds <= 60:
            self.sleep(seconds)
            return
        mins = max(1, round(seconds / 60))
        until = NOW() + seconds
        text = (f"Waiting {mins} min for transcription quota — the free allowance is used up; the scan carries "
                "on by itself." if why == "quota" else f"Waiting {mins} min — {why}")
        creators.update_scan(self.scan_id, status="waiting", wait_until=until, message=text)
        if seconds >= 600:
            notify.problem(self.scan_id, f"wait-{int(until // 3600)}",
                           f"⏳ <b>{notify.esc(self.name)} scan</b>: {notify.esc(text)}")
        self.sleep(seconds)
        creators.update_scan(self.scan_id, status="running", wait_until=None, message="")

    def say(self, message: str) -> None:
        creators.update_scan(self.scan_id, message=message[:300])

    def progress(self, stage: str, frac: float) -> None:
        a, b = STAGE_SHARE.get(stage, (0.0, 1.0))
        self.counters.update(_counts(self.creator_id))
        self.counters["notes"] = self.notes[-12:]
        creators.update_scan(self.scan_id, progress=round(a + (b - a) * _clamp(frac), 4), counters=self.counters)

    # -- the stages --
    def go(self) -> None:
        scan = creators.get_scan(self.scan_id) or {}
        stage = scan.get("stage") if scan.get("stage") in STAGES else "list"
        for st in STAGES[STAGES.index(stage):]:
            self.check()
            creators.update_scan(self.scan_id, stage=st, status="running", wait_until=None, error="")
            getattr(self, f"stage_{st}")()
            self.progress(st, 1.0)
            if st == "list" and self.counters.get("empty"):
                break
        creators.mark_done(self.creator_id)
        self.progress("check", 1.0)
        counts = _counts(self.creator_id)
        final = (f"Done — {counts['moments']} moments ({counts['great']} great)" if counts["moments"]
                 else "Done — no moments found")
        if self.counters.get("empty"):
            final = "No videos found on these links — " + (" ".join(self.notes[-3:]) or "check the links.")
        creators.update_scan(self.scan_id, stage="done", status="done", progress=1.0, finished_at=NOW(),
                             message=final[:300], wait_until=None, counters={**self.counters, **counts})
        creators.update_creator(self.creator_id, last_scan_at=NOW(), status="done")
        self.tell_done(counts)

    def stage_list(self) -> None:
        if self.counters.get("mode") == "new_uploads" or self.counters.get("listed_at"):
            return
        self.say("Listing the videos")
        rows: List[Dict[str, Any]] = []
        for link in self.creator.get("links") or []:
            self.check()
            try:
                got, notes = catalog.list_link(self.net, link, self.settings)
            except (catalog.Gone, catalog.FetchFailed) as exc:
                got, notes = [], [f"Couldn't list {link}: {exc}"]
            rows += got
            self.notes += notes
        catalog.outlier_factors(rows)
        rows = catalog.apply_filters(rows, self.settings, self.rules)
        with store.connect() as conn:
            existing = [dict(r) for r in conn.execute("SELECT platform, video_id, title, duration FROM catalog "
                                                      "WHERE creator_id=?", (self.creator_id,))]
        kept, dups = catalog.dedupe(rows, existing)
        if dups:
            self.notes.append(f"{len(dups)} video{'s are' if len(dups) != 1 else ' is'} on two channels — "
                              "read once.")
        new = creators.upsert_catalog(self.creator_id, kept)
        self.counters["listed_at"] = NOW()
        self.counters["new_videos"] = new
        counts = _counts(self.creator_id)
        if not counts["listed"] and not counts["skipped"]:
            self.counters["empty"] = True
            return
        cc = creators.catalog_counts(self.creator_id)
        to_read = cc["listed"]
        _tell(f"🔎 <b>{notify.esc(self.name)} scan</b>: found {counts['listed']} videos ({to_read} to read"
              + (f", {counts['skipped']} skipped by your filters" if counts["skipped"] else "")
              + "). Reading them now — I'll tell you how it goes.")

    def stage_words(self) -> None:
        tried: set = set()
        total = max(1, _counts(self.creator_id)["listed"])
        can_screen = _claude_ready()
        while True:
            self.check()
            row = creators.next_catalog(self.creator_id, "listed", exclude=tried)
            if not row:
                break
            tried.add(row["id"])
            self.say(f"Reading “{(row.get('title') or row['url'])[:80]}”")
            self.read_one(row)
            row = creators.get_catalog(row["id"]) or row
            if row.get("status") == "words" and can_screen:
                self.screen_one(row)
            c = _counts(self.creator_id)
            self.progress("words", c["read"] / total)
            self.milestone(c)

    def stage_screen(self) -> None:
        if not creators.catalog_counts(self.creator_id)["words"]:
            return
        if not _claude_ready():
            raise catalog.ScanStop("Finding the moments needs Claude — add ANTHROPIC_API_KEY to the .env file, "
                                   "restart ClipAgent and press Resume. The words already read are kept.")
        tried: set = set()
        while True:
            self.check()
            row = creators.next_catalog(self.creator_id, "words", exclude=tried)
            if not row:
                break
            tried.add(row["id"])
            self.screen_one(row)
            self.progress("screen", len(tried) / max(1, len(tried) + creators.catalog_counts(self.creator_id)["words"]))

    def stage_rank(self) -> None:
        if not _claude_ready():
            self.notes.append("Ranking across the catalog needs Claude — the first scores are kept.")
            return
        kinds = [k for k in self.settings.get("kinds") or creators.KINDS if k in creators.KINDS]
        for i, kind in enumerate(kinds):
            self.check()
            self.say(f"Ranking the {kind} moments across the catalog")
            try:
                rank_kind(self.creator_id, kind, self.name, self.rules_text)
            except (Paused, catalog.ScanStop):
                raise
            except Exception as exc:  # noqa: BLE001 — a failed ranking keeps the first scores
                traceback.print_exc()
                self.notes.append(f"Ranking the {kind} moments didn't work ({str(exc)[:120]}) — first scores kept.")
            self.progress("rank", (i + 1) / max(1, len(kinds)))
        drop_repeated_stories(self.creator_id)

    def stage_fetch(self) -> None:
        top = int(self.settings.get("fetch_top") or 0)
        if top <= 0:
            return
        kinds = set(self.settings.get("kinds") or creators.KINDS)
        have = creators.moment_counts(self.creator_id)["sections"]
        want = max(0, top - have)
        if not want:
            return
        pool = [m for m in creators.list_moments(self.creator_id, status="candidate", limit=top * 3 + 30)
                if m["kind"] in kinds and int((m.get("signals") or {}).get("fetch_tries") or 0) < MAX_FETCH_TRIES]
        pick = pool[:want]
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for m in pick:
            groups.setdefault(m["catalog_id"], []).append(m)
        for i, (cat_id, ms) in enumerate(groups.items()):
            self.check()
            row = creators.get_catalog(cat_id)
            if not row:
                continue
            self.say(f"Downloading {len(ms)} part{'s' if len(ms) != 1 else ''} of “{(row.get('title') or '')[:70]}”")
            fetch_moments(self.net, self.creator_id, row, ms)
            self.progress("fetch", (i + 1) / max(1, len(groups)))
            if i + 1 < len(groups):
                self.sleep(FETCH_PAUSE)

    def stage_check(self) -> None:
        todo = creators.list_moments(self.creator_id, status="fetched", limit=100000)
        for i, m in enumerate(todo):
            self.check()
            self.say(f"Checking the picture of moment {i + 1} of {len(todo)}")
            try:
                check_moment(m, self.rules)
            except (Paused, catalog.ScanStop):
                raise
            except Exception as exc:  # noqa: BLE001 — one unreadable section never stops the scan
                traceback.print_exc()
                creators.update_moment(m["id"], status="checked",
                                       signals={**(m.get("signals") or {}),
                                                "check_note": f"The picture couldn't be checked: {str(exc)[:120]}"})
            self.progress("check", (i + 1) / max(1, len(todo)))

    # -- one video --
    def read_one(self, row: Dict[str, Any]) -> None:
        platform = row.get("platform") or ""
        other = creators.words_elsewhere(platform, row["video_id"], row["id"])
        if other:                                       # read for another creator already: never paid twice
            creators.save_words(row["id"], other[0], other[1])
            creators.update_catalog(row["id"], status="words", words_source=other[1], error="")
            return
        try:
            facts = catalog.read_info(self.net, row)
        except catalog.Gone as exc:
            creators.update_catalog(row["id"], status="skipped", error=str(exc)[:300])
            return
        except catalog.FetchFailed as exc:
            creators.update_catalog(row["id"], status="failed", error=str(exc)[:300])
            return
        fields: Dict[str, Any] = {"heatmap": facts["heatmap"]}
        for key in ("title", "duration", "upload_date", "views", "likes"):
            if facts.get(key) not in (None, ""):
                fields[key] = facts[key]
        creators.update_catalog(row["id"], **fields)
        row = {**row, **fields}
        reason = catalog.skip_reason(
            row, self.settings, catalog._day(self.settings.get("since") or ""),
            catalog._day(((self.rules or {}).get("min_upload_date") or {}).get("date") or ""),
            int(self.settings.get("min_views") or 0), float(self.settings.get("min_minutes") or 0) * 60)
        if reason:
            creators.update_catalog(row["id"], status="skipped", error=reason)
            return
        transcript = None
        try:
            transcript = catalog.read_subtitles(self.net, facts, platform)
        except (catalog.Gone, catalog.FetchFailed):
            transcript = None
        source = "subs"
        if not transcript:
            transcript, source = self.read_audio(row, facts)
            if transcript is None:
                return
        if not transcript.get("segments"):
            creators.update_catalog(row["id"], status="skipped", words_source="none",
                                    error="No speech found in this video.")
            return
        creators.save_words(row["id"], transcript, source)
        creators.update_catalog(row["id"], status="words", words_source=source, error="")

    def read_audio(self, row: Dict[str, Any], facts: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        from .config import WHISPER_API_KEY
        if not WHISPER_API_KEY:
            creators.update_catalog(row["id"], status="failed", words_source="none",
                                    error="No subtitles, and no transcription key is set (WHISPER_API_KEY in .env). "
                                          "Add one and scan again to read this video.")
            return None, "none"
        work = AUDIO_WORK / row["id"]
        work.mkdir(parents=True, exist_ok=True)
        platform = row.get("platform") or ""
        title = (row.get("title") or "this video")[:70]
        try:
            self.say(f"Downloading the sound of “{title}” to transcribe it (it has no subtitles)")
            audio = self.download(platform, row["url"],
                                  lambda: catalog.need("download_audio_only")(row["url"], work))
            if audio is None:
                return None, "none"
            audio = Path(audio)
            dur = float(facts.get("duration") or row.get("duration") or 0) or media.probe(audio)["duration"]
            envelope = None
            try:
                envelope = catalog.need("loudness_envelope")(audio, hop=1.0)
            except (Paused, catalog.ScanStop):
                raise
            except Exception:
                envelope = None
            if dur > catalog.LONG_VOD:
                if envelope:
                    windows = catalog.need("loud_windows")(envelope, hop=1.0, count=8, min_len=180, max_len=360,
                                                           max_total=3600)
                else:
                    step = dur / 8
                    windows = [{"start": step * i + step / 2 - 150, "end": step * i + step / 2 + 150,
                                "why": "spread evenly (loudness couldn't be measured)"} for i in range(8)]
                windows = [w for w in windows if float(w["end"]) > float(w["start"])]
                self.say(f"Transcribing the {len(windows)} loudest stretches of “{title}” ({dur / 3600:.1f} h long)")
                transcript = catalog.whisper_windows(audio, windows, self.wait, work)
                minutes = sum(float(w["end"]) - float(w["start"]) for w in windows) / 60
                source = "partial"
            else:
                self.say(f"Transcribing “{title}”")
                transcript = catalog.whisper(audio, self.wait)
                minutes = dur / 60
                source = "whisper"
            if envelope:
                transcript["loud"] = catalog.loud_summary(list(envelope))
            self.counters["whisper_minutes"] = round(float(self.counters.get("whisper_minutes") or 0) + minutes, 1)
            return transcript, source
        except (Paused, catalog.ScanStop):
            raise
        except catalog.Gone as exc:
            creators.update_catalog(row["id"], status="skipped", error=str(exc)[:300])
            return None, "none"
        except Exception as exc:  # noqa: BLE001 — one video's trouble never stops the scan
            traceback.print_exc()
            creators.update_catalog(row["id"], status="failed", error=_plain(exc))
            return None, "none"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def download(self, platform: str, url: str, fn: Callable[[], Any]) -> Any:
        """A download through Part 1's helpers, at the same polite pace, read for the bot check."""
        return guarded_download(self.net, platform, url, fn)

    def screen_one(self, row: Dict[str, Any]) -> None:
        try:
            screen_video(self.creator_id, row, self.name, self.rules_text, self.check)
            self.claude_trouble = 0
        except (Paused, catalog.ScanStop):
            raise
        except Exception as exc:  # noqa: BLE001 — Claude busy or out of credit: leave it for later
            traceback.print_exc()
            self.claude_trouble += 1
            if self.claude_trouble >= 3:
                raise catalog.ScanStop(f"Claude isn't answering right now ({str(exc)[:140]}). The scan is paused "
                                       "with everything kept — press Resume to try again.") from exc

    # -- telling gs --
    def milestone(self, c: Dict[str, int]) -> None:
        step = int(c["read"] // READ_MILESTONE)
        if step >= 1 and step > int(self.counters.get("told_read") or 0) and c["listed"] > READ_MILESTONE:
            self.counters["told_read"] = step
            _tell(f"📖 <b>{notify.esc(self.name)} scan</b>: read {c['read']} of {c['listed']} videos · "
                  f"{c['great']} great moments so far")

    def tell_done(self, counts: Dict[str, int]) -> None:
        new_rows = self.counters.get("new_rows") or []
        if self.counters.get("mode") == "new_uploads" and new_rows:
            fresh = [m for cid in new_rows for m in creators.list_moments(self.creator_id, catalog_id=cid)]
            if not fresh:
                return
            rows = [creators.get_catalog(c) for c in new_rows]
            rows = [r for r in rows if r]
            if len(rows) == 1:
                r = rows[0]
                today = catalog.ymd(r.get("upload_date")) == datetime.now().strftime("%Y%m%d")
                what = (f"today's {notify.esc(self.name)} stream" if today and r.get("kind") in ("stream", "vod")
                        else f"{notify.esc(self.name)}'s new video “{notify.esc((r.get('title') or '')[:70])}”")
            else:
                what = f"{len(rows)} new {notify.esc(self.name)} uploads"
            _tell(f"🆕 {len(fresh)} new moment{'s' if len(fresh) != 1 else ''} from {what} — want clips? "
                  "Open Creators in ClipAgent.")
            return
        _tell(f"✅ <b>{notify.esc(self.name)}</b>: scan finished — {counts['moments']} moments"
              + (f" ({counts['great']} great)" if counts["great"] else "") + " — open Creators in ClipAgent.")


def _claude_ready() -> bool:
    try:
        _client()
        return True
    except Exception:
        return False


def _tell(text: str) -> None:
    try:
        notify.send(text)
    except Exception:
        pass


def _plain(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    if isinstance(exc, (catalog.FetchFailed, catalog.Gone, media.DownloadError)):
        return text[:300]
    return media.explain_download_error(text)[:300] if "error" in text.lower() else text[:300]


def _counts(creator_id: str) -> Dict[str, Any]:
    cc = creators.catalog_counts(creator_id)
    mc = creators.moment_counts(creator_id, GREAT)
    total = sum(cc.values())
    return {"listed": total - cc["skipped"], "skipped": cc["skipped"], "failed": cc["failed"],
            "read": cc["words"] + cc["scored"] + cc["done"], "scored": cc["scored"] + cc["done"],
            "fetched": mc["sections"], "checked": mc["checked"] + mc["used"], "moments": mc["kept"],
            "great": mc["great"]}


def guarded_download(net: catalog.Net, platform: str, url: str, fn: Callable[[], Any]) -> Any:
    """Run one of Part 1's download helpers at the scan's polite pace: the bot check stops
    the scan, "too many requests" rests it, a gone video raises Gone, anything else FetchFailed."""
    if platform == "youtube":
        block = catalog.bot_block()
        if block:
            raise catalog.BotBlocked(block.get("message") or media.explain_download_error("not a bot", url))
    net._turn(platform)
    try:
        return fn()
    except (Paused, catalog.ScanStop):
        raise
    except Exception as exc:  # noqa: BLE001 — sorted into plain kinds
        text = str(exc)
        kind = catalog.classify(text)
        if kind == "bot":
            message = media.explain_download_error(text, url)
            catalog.set_bot_block(message, platform)
            raise catalog.BotBlocked(message) from exc
        if kind == "rate":
            raise catalog.SlowDown(f"{catalog.PLATFORM_NAMES.get(platform, platform)} says ClipAgent is asking too "
                                   "often, so the scan is resting for half an hour and then carries on by itself.") \
                from exc
        if kind == "gone":
            raise catalog.Gone(media.explain_download_error(text, url)) from exc
        raise catalog.FetchFailed(text if isinstance(exc, media.DownloadError) else
                                  media.explain_download_error(text, url)) from exc


# --- screening one video ----------------------------------------------------------------------

def _blocks(segments: List[Dict[str, Any]], size: float = highlights.BLOCK_SECONDS) -> List[Tuple[float, float]]:
    if not segments:
        return []
    end = max(float(s["end"]) for s in segments)
    out, a = [], 0.0
    while a < end:
        out.append((a, min(end, a + size)))
        a += size
    return out


def screen_video(creator_id: str, row: Dict[str, Any], name: str, rules_text: str,
                 check: Callable[[], None] = lambda: None) -> List[str]:
    """Claude reads one video in ~25-minute blocks; the candidates are checked, scored and saved.
    Each block's answer is cached, so a scan cut off mid-video (or the same video read for
    another creator under the same guidance) never pays for it twice."""
    transcript = creators.get_words(row["id"]) or {}
    segs = transcript.get("segments") or []
    when = catalog.ymd(row.get("upload_date"))
    when_text = f"{when[:4]}-{when[4:6]}-{when[6:]}" if when else ""
    raw: List[Dict[str, Any]] = []
    for a, b in _blocks(segs):
        check()
        text = highlights._transcript_text(segs, a, b)
        if len(text) < 80:
            continue
        key = _key("screen", SCREEN_VERSION, CLAUDE_SCREEN_MODEL, name, rules_text, row.get("title") or "", text)
        got = creators.cache_get(key)
        if got is None:
            got = _screen_block(name, row.get("title") or "", when_text, text, a, b, rules_text)
            creators.cache_put(key, got)
        raw += [g for g in got if isinstance(g, dict)]
    found, dropped = verify(raw, transcript, row.get("duration"), name)
    rows = []
    heat = row.get("heatmap") or []
    for c in found:
        said = words_between(transcript, c["start"], c["end"])
        sig = {"text": c["text_score"], "heat": heat_peak(heat, c["start"], c["end"]),
               "loud": loud_level(transcript.get("loud"), c["start"], c["end"]), "laugh": laugh_level(said),
               "outlier": row.get("outlier"), "age_days": age_days(row.get("upload_date") or ""),
               "quote": c["quote"], "match": c["match"], "words": row.get("words_source") or ""}
        sig = {k: v for k, v in sig.items() if v is not None}
        rows.append({"kind": c["kind"], "start": c["start"], "end": c["end"], "hit": c["hit"],
                     "score": merge_score(sig), "signals": sig, "text": said, "reason": c["reason"],
                     "speaker": c["speaker"], "hook": ""})
    rows = prune_overlaps(rows)
    if dropped:
        rows_note = f"{dropped} suggestion{'s' if dropped != 1 else ''} dropped: the words weren't really said there"
        for r in rows:
            r["signals"]["screen_note"] = rows_note
    return creators.save_screened(creator_id, row["id"], rows)


# --- ranking across the catalog ------------------------------------------------------------------

def rank_kind(creator_id: str, kind: str, name: str, rules_text: str) -> int:
    """Claude puts the best ~100 moments of one kind on one scale, labels their stories and
    writes their hooks. Cached by the exact list, so a resumed scan doesn't pay again."""
    cands = creators.list_moments(creator_id, kind=kind, status=["candidate", "fetched", "checked"], limit=RANK_TOP)
    if not cands:
        return 0
    titles: Dict[str, Dict[str, Any]] = {}
    lines = []
    for i, m in enumerate(cands):
        row = titles.setdefault(m["catalog_id"], creators.get_catalog(m["catalog_id"]) or {})
        day = catalog.ymd(row.get("upload_date"))
        lines.append(f"[{i}] “{(row.get('title') or '')[:80]}”{f' ({day[:4]})' if day else ''} · first score "
                     f"{m['score']:.0f} · said by {m.get('speaker') or 'unclear'}\n    {(m.get('text') or '')[:380]}")
    prompt = (f"{len(cands)} {kind} moments ({KIND_WORDS.get(kind, kind)}):\n\n" + "\n\n".join(lines)
              + "\n\nScore them all on one scale, label their stories, write their hooks.")
    system = RANK_SYSTEM.format(name=name)
    if rules_text:
        system += "\n\nCAMPAIGN RULES — these override anything above:\n" + rules_text
    key = _key("rank", CLAUDE_MODEL, system, prompt)
    replies = creators.cache_get(key)
    if replies is None:
        replies = toolio.ask(_client(), "moments", retries=1, model=CLAUDE_MODEL, max_tokens=12000, system=system,
                             tools=[RANK_TOOL], tool_choice={"type": "tool", "name": "rank_moments"},
                             messages=[{"role": "user", "content": prompt}])
        creators.cache_put(key, replies)
    done = 0
    seen = set()
    for r in replies:
        if not isinstance(r, dict):
            continue
        i = highlights._int(r.get("id"), -1)
        if not 0 <= i < len(cands) or i in seen:
            continue
        seen.add(i)
        m = cands[i]
        sig = dict(m.get("signals") or {})
        sig["rank"] = _clamp((_f(r.get("score")) or 0) / 100.0)
        hook = clean_hook(r.get("hook"), m.get("text") or "", m.get("speaker") or "", name)
        story = re.sub(r"[^a-z0-9]+", "-", str(r.get("story_key") or "").lower()).strip("-")[:60]
        creators.update_moment(m["id"], signals=sig, score=merge_score(sig), story_key=story,
                               hook=hook or m.get("hook") or "",
                               reason=(highlights._text(r.get("why")) or m.get("reason") or "")[:300])
        done += 1
    return done


def drop_repeated_stories(creator_id: str) -> int:
    ms = creators.list_moments(creator_id, status=["candidate", "fetched", "checked", "used"], limit=100000)
    by_id = {m["id"]: m for m in ms}
    pairs = dedupe_stories(ms)
    for drop, keep in pairs:
        k = by_id[keep]
        row = creators.get_catalog(k["catalog_id"]) or {}
        creators.update_moment(drop, status="dropped",
                               drop_reason=f"The same story as a better telling in “{(row.get('title') or '')[:70]}” "
                                           f"(score {k['score']:.0f}).")
    return len(pairs)


# --- the parts of the video ------------------------------------------------------------------------

def fetch_moments(net: catalog.Net, creator_id: str, row: Dict[str, Any], moments: List[Dict[str, Any]]) -> int:
    """Download only these moments' parts of one video (±8 s) through Part 1's
    media.download_sections. A failure is remembered on the moment; after three it's dropped
    with the reason. Returns how many came down."""
    out_dir = SECTION_DIR / creator_id
    out_dir.mkdir(parents=True, exist_ok=True)
    sections = [(float(m["start"]), float(m["end"])) for m in moments]
    platform = row.get("platform") or ""

    def failed(text: str, final: bool = False) -> None:
        for m in moments:
            sig = dict(m.get("signals") or {})
            tries = int(sig.get("fetch_tries") or 0) + 1
            sig.update(fetch_tries=tries, fetch_error=text[:200])
            if final or tries >= MAX_FETCH_TRIES:
                creators.update_moment(m["id"], signals=sig, status="dropped",
                                       drop_reason=f"Couldn't download this part of the video: {text[:160]}")
            else:
                creators.update_moment(m["id"], signals=sig)

    try:
        got = guarded_download(net, platform, row["url"], lambda: catalog.need("download_sections")(
            row["url"], sections, out_dir, pad=SECTION_PAD))
    except catalog.Gone as exc:
        failed(str(exc), final=True)
        return 0
    except catalog.FetchFailed as exc:
        failed(str(exc))
        return 0
    n = 0
    got = list(got or [])
    for i, m in enumerate(moments):
        g = got[i] if i < len(got) and isinstance(got[i], dict) else {}
        path = Path(str(g.get("path") or ""))
        if not g or not path.is_file():
            sig = dict(m.get("signals") or {})
            sig["fetch_tries"] = int(sig.get("fetch_tries") or 0) + 1
            sig["fetch_error"] = "The download finished without this part."
            creators.update_moment(m["id"], signals=sig)
            continue
        sig = {k: v for k, v in (m.get("signals") or {}).items() if k not in ("fetch_error",)}
        creators.update_moment(m["id"], section_path=str(path), section_offset=round(float(g.get("offset") or 0.0), 3),
                               status="fetched" if m.get("status") == "candidate" else m.get("status"), signals=sig)
        n += 1
    return n


def section_file(moment_id: str) -> Optional[Path]:
    """The downloaded part of the video for a moment, when it's on this PC."""
    m = creators.get_moment(moment_id)
    p = Path(m["section_path"]) if m and m.get("section_path") else None
    return p if p and p.is_file() else None


def moment_thumb(moment_id: str) -> Optional[Path]:
    """A picture of the moment (at its hit), made once from its section and kept."""
    m = creators.get_moment(moment_id)
    src = section_file(moment_id)
    if not m or not src:
        return None
    out = SECTION_DIR / m["creator_id"] / "thumbs" / f"{moment_id}_{int(float(m['hit']) * 10)}.jpg"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        at = max(0.0, float(m["hit"]) - float(m.get("section_offset") or 0.0))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=-2:360", "-q:v", "4", str(out)], capture_output=True)
    return out if out.is_file() else None


# --- the picture check -------------------------------------------------------------------------

def _grey_frames(path: Path, start: float, end: float, fps: float = 5.0, width: int = 160) -> List[Any]:
    import numpy as np
    info = media.probe(path)
    w, h = info["width"] or 0, info["height"] or 0
    if not w or not h:
        return []
    height = max(2, int(round(width * h / w / 2)) * 2)
    proc = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, start):.3f}",
                           "-t", f"{max(0.5, end - start):.3f}", "-i", str(path),
                           "-vf", f"fps={fps},scale={width}:{height}:flags=area", "-an", "-f", "rawvideo",
                           "-pix_fmt", "gray", "-"], capture_output=True)
    size = width * height
    return [np.frombuffer(proc.stdout[i * size:(i + 1) * size], np.uint8).reshape(height, width)
            for i in range(len(proc.stdout) // size)]


def look_at_section(path: Path, start: float, end: float) -> Dict[str, Any]:
    """What the picture does in [start, end] of a section: movement (dense optical flow and
    frame differences), scene cuts, and faces (YuNet). No Claude here."""
    import cv2
    import numpy as np
    end = min(end, start + MAX_LEN)
    frames = _grey_frames(path, start, end)
    out: Dict[str, Any] = {"frames": len(frames)}
    if len(frames) >= 2:
        dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST)
        energy, diffs, cuts = [], [], 0
        for a, b in zip(frames, frames[1:]):
            d = float(np.mean(cv2.absdiff(a, b)))
            diffs.append(d)
            ha = cv2.calcHist([a], [0], None, [32], [0, 256])
            hb = cv2.calcHist([b], [0], None, [32], [0, 256])
            if d > 40 and cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL) < 0.6:
                cuts += 1
                continue                                    # a cut isn't movement
            flow = dis.calc(a, b, None)
            energy.append(float(np.hypot(flow[..., 0], flow[..., 1]).mean()) * 5.0 / a.shape[1])
        e = sorted(energy) or [0.0]
        out["motion_raw"] = round(float(np.mean(e)), 4)
        out["motion_peak"] = round(e[int(0.9 * (len(e) - 1))], 4)
        out["motion"] = round(_clamp((out["motion_raw"] - 0.01) / 0.15), 3)
        out["cuts"] = cuts
        out["cuts_per_min"] = round(cuts / max(0.1, (end - start) / 60), 2)
        out["diff"] = round(float(np.mean(diffs)), 2)
    try:
        from . import framing
        count = 8
        faces = framing.sample_faces(path, start, end, count=count)
        times = {round(f.t, 2) for f in faces}
        out["face_share"] = round(len(times) / count, 3)
        sizes = sorted(max(f.w for f in faces if round(f.t, 2) == t) for t in times)
        out["face_size"] = round(sizes[len(sizes) // 2], 3) if sizes else 0.0
        out["faces_max"] = max((sum(1 for f in faces if round(f.t, 2) == t) for t in times), default=0)
    except Exception as exc:  # noqa: BLE001 — no detector: say so, don't pretend
        out["face_note"] = f"Faces couldn't be looked for: {str(exc)[:120]}"
    return out


def _frames_jpeg(path: Path, start: float, end: float, count: int = 5, width: int = 512) -> List[bytes]:
    out = []
    for i in range(count):
        t = start + (end - start) * (i + 0.5) / count
        proc = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1",
                               "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                              capture_output=True)
        if proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8":
            out.append(proc.stdout)
    return out


def claude_look(path: Path, start: float, end: float, kind: str, said: str) -> Optional[Dict[str, Any]]:
    """Claude looks at 5 frames: is this visually strong, is the main person clearly visible.
    Context only — nobody is identified from their face."""
    import base64
    frames = _frames_jpeg(path, start, end)
    if not frames:
        return None
    content: List[Dict[str, Any]] = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                                  "data": base64.b64encode(f).decode("ascii")}}
                                     for f in frames]
    content.append({"type": "text", "text": (
        f"These are {len(frames)} frames, in order, from a {end - start:.0f}-second {kind} moment of a video. "
        f"What's said: “{said[:300]}”.\n\nJudge only the picture: how strong is it visually for a short (1-10), "
        "and is the main person clearly visible (face or body big enough to read on a phone)? Describe what you "
        "see; don't identify anyone from their face and don't guess names.")})
    message = _client().messages.create(model=CLAUDE_MODEL, max_tokens=400, tools=[LOOK_TOOL],
                                        tool_choice={"type": "tool", "name": "rate_picture"},
                                        messages=[{"role": "user", "content": content}])
    inputs = toolio.tool_inputs(message)
    if not inputs:
        return None
    r = inputs[0]
    strong = _f(r.get("strong"))
    vis = str(r.get("person_visible") or "").lower()
    return {"strong": int(_clamp(strong or 5, 1, 10)), "person_visible": vis if vis in ("yes", "no", "unclear") else "unclear",
            "what": highlights._text(r.get("what"))[:120]}


def check_moment(m: Dict[str, Any], rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The picture check for one fetched moment: measured, (for visual kinds) looked at by Claude,
    the score updated, and dropped when the campaign needs the creator on screen and nobody is."""
    path = Path(m.get("section_path") or "")
    if not path.is_file():
        creators.update_moment(m["id"], status="candidate", section_path="")      # gone: fetch again later
        return {}
    off = float(m.get("section_offset") or 0.0)
    s, e = max(0.0, float(m["start"]) - off), max(0.5, float(m["end"]) - off)
    look = look_at_section(path, s, e)
    if m.get("kind") in VISUAL_KINDS:
        if _claude_ready():
            try:
                look["claude"] = claude_look(path, s, e, m["kind"], m.get("text") or "")
            except Exception as exc:  # noqa: BLE001 — the measured check still counts
                look["claude_note"] = f"Claude's look didn't work: {str(exc)[:120]}"
        else:
            look["claude_note"] = "Claude's look didn't run (no Claude key)."
    sig = dict(m.get("signals") or {})
    for k in ("motion", "motion_raw", "cuts_per_min", "face_share", "face_size", "faces_max"):
        if look.get(k) is not None:
            sig[k] = look[k]
    if (look.get("claude") or {}).get("strong"):
        sig["visual"] = round(look["claude"]["strong"] / 10.0, 2)
        sig["picture_note"] = look["claude"].get("what", "")
    for k in ("face_note", "claude_note"):
        if look.get(k):
            sig[k] = look[k]
    visible = creator_visible(path, m, look, rules)
    sig["on_screen"] = {True: "yes", False: "no", None: "unclear"}[visible]
    fields: Dict[str, Any] = {"signals": sig, "score": merge_score(sig)}
    if visible is False and needs_person(rules):
        who = ((rules or {}).get("primary_focus") or {}).get("name") or (rules or {}).get("creator") or "the creator"
        fields.update(status="dropped", drop_reason=f"Nobody is clearly on screen in this moment, and the campaign "
                                                    f"needs {who} in the picture.")
    elif m.get("status") == "fetched":
        fields["status"] = "checked"
    creators.update_moment(m["id"], **fields)
    return look


# --- the engine ------------------------------------------------------------------------------------

_lock = threading.Lock()
_engine = threading.Lock()
_threads: Dict[str, threading.Thread] = {}
_pauses: Dict[str, threading.Event] = {}
_again: Dict[str, bool] = {}
_pending_new: Dict[str, List[str]] = {}
_timers: Dict[str, threading.Timer] = {}

INTERRUPTED = ("ClipAgent was closed while this scan was running. Press Resume — it carries on where it "
               "stopped, and nothing already read is read again.")


def start(creator_id: str, new_rows: Optional[List[str]] = None) -> str:
    """Start a scan for this creator, or resume its paused/waiting one. Returns the scan's id.
    One scan works at a time; another one waits its turn."""
    creator = creators.get_creator(creator_id)
    if not creator:
        raise ValueError("That creator doesn't exist any more")
    if not creator.get("links"):
        raise ValueError("Add at least one link first — a YouTube channel, a Twitch or Kick channel, or video links")
    with _lock:
        timer = _timers.pop(creator_id, None)
        if timer:
            timer.cancel()
        scan = creators.latest_scan(creator_id)
        alive = _threads.get(creator_id)
        if alive and alive.is_alive() and scan:
            if new_rows:
                _pending_new.setdefault(creator_id, []).extend(new_rows)
            if _pauses.get(creator_id) and _pauses[creator_id].is_set():
                _pauses[creator_id].clear()
                _again[creator_id] = True
            return scan["id"]
        if new_rows:
            sid = creators.create_scan(creator_id)
            creators.update_scan(sid, stage="words", counters={"mode": "new_uploads", "new_rows": list(new_rows)},
                                 message="Reading the new uploads")
        elif scan and scan.get("status") in ("paused", "waiting", "running") and scan.get("stage") != "done":
            sid = scan["id"]
        else:
            creators.reset_failed(creator_id)
            sid = creators.create_scan(creator_id)
        creators.update_scan(sid, status="running", wait_until=None, error="",
                             message="Starting" if not scan or sid != scan["id"] else "Carrying on")
        creators.update_creator(creator_id, status="scanning")
        _spawn(creator_id, sid)
        return sid


def _spawn(creator_id: str, scan_id: str) -> None:
    ev = _pauses.setdefault(creator_id, threading.Event())
    ev.clear()
    t = threading.Thread(target=_work, args=(creator_id, scan_id), name=f"scan-{creator_id}", daemon=True)
    _threads[creator_id] = t
    t.start()


def _work(creator_id: str, scan_id: str) -> None:
    ev = _pauses[creator_id]
    status = "paused"
    try:
        while not _engine.acquire(timeout=1.0):
            if ev.is_set():
                creators.update_scan(scan_id, status="paused", message="Paused — press Resume to carry on.")
                return
            if (creators.get_scan(scan_id) or {}).get("status") != "waiting":
                creators.update_scan(scan_id, status="waiting", wait_until=None,
                                     message="Waiting for another creator's scan to finish first")
        try:
            creators.update_scan(scan_id, status="running", message="")
            _Run(scan_id, creator_id, ev).go()
            status = "done"
        except Paused:
            creators.update_scan(scan_id, status="paused", wait_until=None,
                                 message="Paused — press Resume to carry on where it stopped.")
        except catalog.SlowDown as exc:
            until = NOW() + exc.wait
            creators.update_scan(scan_id, status="waiting", wait_until=until, message=exc.message[:300])
            _resume_later(creator_id, exc.wait)
            status = "waiting"
        except catalog.ScanStop as exc:
            creators.update_scan(scan_id, status="paused", wait_until=None, message=exc.message[:300],
                                 error=exc.message[:300])
            name = (creators.get_creator(creator_id) or {}).get("name") or "Creator"
            _tell(f"⏸ <b>{notify.esc(name)} scan</b> paused: {notify.esc(exc.message[:400])}")
        except Exception as exc:  # noqa: BLE001 — the scan fails in plain words, everything kept
            traceback.print_exc()
            creators.update_scan(scan_id, status="failed", wait_until=None, error=str(exc)[:500],
                                 message="Something went wrong inside ClipAgent. Everything read so far is kept — "
                                         "press Start scan to carry on.")
            status = "failed"
        finally:
            _engine.release()
    finally:
        creators.update_creator(creator_id, status={"done": "done", "waiting": "waiting"}.get(status, "paused"))
        again = False
        pending: List[str] = []
        with _lock:
            if _threads.get(creator_id) is threading.current_thread():
                _threads.pop(creator_id, None)
            again = _again.pop(creator_id, False)
            pending = _pending_new.pop(creator_id, [])
        if again and status != "done":
            start(creator_id)
        elif pending:
            start(creator_id, new_rows=pending)


def pause(creator_id: str) -> Dict[str, Any]:
    """Stop at the next safe point (between requests, between videos, inside any wait)."""
    with _lock:
        _again.pop(creator_id, None)
        timer = _timers.pop(creator_id, None)
        if timer:
            timer.cancel()
        ev = _pauses.setdefault(creator_id, threading.Event())
        ev.set()
        alive = _threads.get(creator_id)
        scan = creators.latest_scan(creator_id)
        if scan and scan.get("status") in ("waiting", "running") and not (alive and alive.is_alive()):
            creators.update_scan(scan["id"], status="paused", wait_until=None,
                                 message="Paused — press Resume to carry on where it stopped.")
    return status(creator_id)


def _resume_later(creator_id: str, seconds: float) -> None:
    def go() -> None:
        with _lock:
            _timers.pop(creator_id, None)
        scan = creators.latest_scan(creator_id)
        if scan and scan.get("status") == "waiting":
            try:
                start(creator_id)
            except Exception:
                traceback.print_exc()

    with _lock:
        old = _timers.pop(creator_id, None)
        if old:
            old.cancel()
        t = threading.Timer(max(1.0, seconds), go)
        t.daemon = True
        _timers[creator_id] = t
        t.start()


def settle_interrupted() -> None:
    """At startup: a scan cut off by a restart is paused with a plain note (Resume carries on);
    one that was waiting for the transcription allowance resumes by itself when its time comes."""
    now = NOW()
    for s in creators.scans_with_status(["running"]):
        creators.update_scan(s["id"], status="paused", wait_until=None, message=INTERRUPTED)
        creators.update_creator(s["creator_id"], status="paused")
    for s in creators.scans_with_status(["waiting"]):
        latest = creators.latest_scan(s["creator_id"])
        if latest and latest["id"] == s["id"]:
            _resume_later(s["creator_id"], max(5.0, float(s.get("wait_until") or now) - now))
    for e in store.list_edits(200):                    # edits waiting on moments' video when ClipAgent closed
        plan = e.get("plan") or {}
        if e.get("status") in ("queued", "running") and plan.get("given_moments") and \
                not (e.get("settings") or {}).get("sources"):
            store.update_edit(e["id"], status="failed", stage="Failed", progress=100,
                              error="ClipAgent was closed while getting the moments' video. Select the moments "
                                    "in Creators again and make the edit.")


def status(creator_id: str) -> Dict[str, Any]:
    """The scan as the page shows it, with one plain line: "Found 412 videos · Read 160 of 412 ·
    38 great moments so far · Waiting 20 min for transcription quota"."""
    creator = creators.get_creator(creator_id) or {}
    scan = creators.latest_scan(creator_id)
    c = _counts(creator_id)
    out: Dict[str, Any] = dict(scan or {"id": "", "creator_id": creator_id, "stage": "", "status": "none",
                                        "progress": 0.0, "counters": {}, "message": "", "wait_until": None})
    out["counts"] = c
    out["counters"] = {**(out.get("counters") or {}), **c}
    with _lock:
        t = _threads.get(creator_id)
        out["working"] = bool(t and t.is_alive())
    if not scan:
        out["text"] = "Not scanned yet" if not c["listed"] else f"{c['listed']} videos listed · not scanned yet"
        return out
    parts = [f"Found {c['listed']} video{'s' if c['listed'] != 1 else ''}"]
    if c["listed"]:
        parts.append(f"Read {c['read']} of {c['listed']}")
    parts.append(f"{c['great']} great moment{'s' if c['great'] != 1 else ''} so far" if scan["status"] != "done"
                 else f"{c['moments']} moments ({c['great']} great)")
    top = int((creator.get("settings") or {}).get("fetch_top") or 0)
    if scan.get("stage") in ("fetch", "check") and top:
        parts.append(f"Downloaded {min(c['fetched'], top)} of the {top} best")
    if scan.get("stage") == "check":
        parts.append(f"Checked {c['checked']}")
    if scan["status"] == "waiting":
        left = max(0.0, float(scan.get("wait_until") or 0) - NOW()) if scan.get("wait_until") else 0
        if left:
            msg = scan.get("message") or ""
            what = "transcription quota" if "transcription" in msg else ("the site to cool down" if "resting" in msg
                                                                          else "its turn")
            parts.append(f"Waiting {max(1, round(left / 60))} min for {what}")
        else:
            parts.append(scan.get("message") or "Waiting")
    elif scan["status"] == "paused":
        parts.append(scan.get("message") or "Paused")
    elif scan["status"] == "failed":
        parts.append(scan.get("message") or "Stopped")
    elif scan["status"] == "running" and scan.get("message"):
        parts.append(scan["message"])
    out["text"] = " · ".join(p for p in parts if p)
    return out


# --- the estimate ------------------------------------------------------------------------------------

# Rough prices, dollars per million tokens (input, output), by model family.
PRICES = {"haiku": (1.0, 5.0), "sonnet": (2.0, 10.0), "opus": (4.0, 20.0), "fable": (10.0, 50.0),
          "mythos": (10.0, 50.0)}
TOKENS_PER_SPEECH_MIN = 220     # ~160 spoken words a minute, plus the time marks
SUBS_SHARE = 0.9                # an assumption until each video is opened: most YouTube videos have captions
SECTION_MB_PER_MIN = 30.0       # a 1080p part of a video
AUDIO_MB_PER_HOUR = 60.0        # sound only, for videos without subtitles
WHISPER_HOURS_PER_HOUR = 2.0    # Groq's free tier: about 2 hours of audio an hour (8 a day)


def _price(model: str) -> Tuple[float, float]:
    m = (model or "").lower()
    return next((p for k, p in PRICES.items() if k in m), PRICES["sonnet"])


def estimate(creator_id: str, list_if_needed: bool = True) -> Dict[str, Any]:
    """What a scan will take, before it starts: videos, hours of speech, how many have
    subtitles, Whisper hours, Claude tokens and rough cost, download size, time — and the
    assumptions in `text`. Lists the catalog first (metadata only) when it hasn't been."""
    creator = creators.get_creator(creator_id)
    if not creator:
        raise ValueError("That creator doesn't exist any more")
    settings = creator["settings"]
    notes: List[str] = []
    if list_if_needed and not creators.catalog_counts(creator_id).get("listed") and \
            not sum(creators.catalog_counts(creator_id).values()):
        try:
            notes = list_now(creator_id)
        except catalog.ScanStop as exc:
            notes = [exc.message]
    todo = creators.catalog_totals(creator_id, ["listed", "failed"])
    unscreened = creators.catalog_totals(creator_id, ["words"])
    hours = sum(float(r.get("duration") or 0) for r in todo) / 3600
    yt = [r for r in todo if r["platform"] == "youtube"]
    other = [r for r in todo if r["platform"] != "youtube"]
    with_subs = int(round(len(yt) * SUBS_SHARE))
    need = len(todo) - with_subs
    need_rows = sorted(other, key=lambda r: -float(r.get("duration") or 0)) + \
        sorted(yt, key=lambda r: float(r.get("duration") or 0))[:max(0, len(yt) - with_subs)]
    whisper_hours = sum(min(float(r.get("duration") or 0), 3600.0 if float(r.get("duration") or 0) > catalog.LONG_VOD
                            else float(r.get("duration") or 0)) for r in need_rows[:need]) / 3600
    speech_min = (sum(float(r.get("duration") or 0) for r in todo if r not in need_rows[:need]) / 60
                  + whisper_hours * 60 + sum(float(r.get("duration") or 0) for r in unscreened) / 60)
    blocks = max(0, int(math.ceil(speech_min / 25)))
    screen_in = speech_min * TOKENS_PER_SPEECH_MIN + blocks * 1500
    screen_out = blocks * 700
    kinds = len(settings.get("kinds") or creators.KINDS)
    rank_in, rank_out = kinds * RANK_TOP * 150 + kinds * 800, kinds * RANK_TOP * 60
    top = int(settings.get("fetch_top") or 0)
    looks = int(round(top * 0.4))
    look_in, look_out = looks * 2100, looks * 120
    tokens = int(screen_in + screen_out + rank_in + rank_out + look_in + look_out)
    pi, po = _price(CLAUDE_SCREEN_MODEL)
    ri, ro = _price(CLAUDE_MODEL)
    cost = (screen_in * pi + screen_out * po + (rank_in + look_in) * ri + (rank_out + look_out) * ro) / 1e6
    download_mb = top * (50 + 2 * SECTION_PAD) / 60 * SECTION_MB_PER_MIN + \
        sum(float(r.get("duration") or 0) for r in need_rows[:need]) / 3600 * AUDIO_MB_PER_HOUR
    gap = SCAN_REQUEST_GAP
    minutes = (len(todo) * (2 * gap + 4) / 60 + whisper_hours / WHISPER_HOURS_PER_HOUR * 60 + blocks * 20 / 60
               + kinds * 1.0 + top * (gap + FETCH_PAUSE + 25) / 60 + top * 10 / 60)
    left_note = ""
    done_rows = creators.catalog_counts(creator_id)
    already = done_rows["words"] + done_rows["scored"] + done_rows["done"]
    if already:
        left_note = f" ({already} already read — only what's left is counted)"
    text = (f"{len(todo)} video{'s' if len(todo) != 1 else ''} to read{left_note} — about {hours:.0f} hours. "
            f"About {with_subs} should have YouTube's own subtitles (free to read; an assumption until each video "
            f"is opened — about 9 in 10 YouTube videos have them); about {need} need transcription: roughly "
            f"{whisper_hours:.0f} hours of audio (streams over 2 hours: only their loudest hour). "
            f"Claude: about {tokens / 1e6:.1f} million tokens to screen, rank and look — roughly "
            f"${cost:.2f} (screening with {CLAUDE_SCREEN_MODEL}, ranking with {CLAUDE_MODEL}; rough prices). "
            f"Downloads: about {download_mb / 1024:.1f} GB — the {top} best moments' parts of the video, plus "
            f"the sound of the videos without subtitles; never whole videos. "
            f"Time: about {minutes / 60:.1f} hours, mostly waiting politely between requests "
            f"({gap:.0f} s apart) and for the free transcription allowance (about 2 hours of audio an hour).")
    if notes:
        text += " Notes: " + " ".join(notes[:4])
    return {"videos": len(todo), "hours": round(hours, 1), "with_subs": with_subs, "need_whisper": need,
            "whisper_hours": round(whisper_hours, 1), "claude_tokens": tokens, "claude_cost_usd": round(cost, 2),
            "download_mb": int(round(download_mb)), "minutes": int(round(minutes)), "text": text, "notes": notes}


def list_now(creator_id: str) -> List[str]:
    """List the catalog right now (metadata only), outside a scan — for the estimate."""
    creator = creators.get_creator(creator_id) or {}
    rules = _rules_for(creator)
    net = catalog.Net(sleep=SLEEP, clock=CLOCK)
    rows: List[Dict[str, Any]] = []
    notes: List[str] = []
    for link in creator.get("links") or []:
        try:
            got, n = catalog.list_link(net, link, creator["settings"])
        except (catalog.Gone, catalog.FetchFailed) as exc:
            got, n = [], [f"Couldn't list {link}: {exc}"]
        rows += got
        notes += n
    catalog.outlier_factors(rows)
    rows = catalog.apply_filters(rows, creator["settings"], rules)
    with store.connect() as conn:
        existing = [dict(r) for r in conn.execute("SELECT platform, video_id, title, duration FROM catalog "
                                                  "WHERE creator_id=?", (creator_id,))]
    kept, _ = catalog.dedupe(rows, existing)
    creators.upsert_catalog(creator_id, kept)
    return notes


# --- search ----------------------------------------------------------------------------------------------

_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "at", "for", "is", "it", "he", "his", "him", "her",
         "she", "they", "them", "about", "when", "every", "time", "that", "this", "with", "what", "how", "was",
         "talks", "says", "said", "say", "where", "who", "does", "did", "do", "from", "first"}


def _terms(query: str) -> List[str]:
    toks = re.findall(r"[a-z0-9$]+", (query or "").lower())
    terms = [t for t in toks if t not in _STOP and len(t) >= 3]
    return terms or [t for t in toks if len(t) >= 2]


def score_chunks(chunks: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Simple relevance: rarer words count more, the whole phrase counts most."""
    terms = _terms(query)
    if not terms or not chunks:
        return []
    df = {t: sum(1 for c in chunks if t in c["text"].lower()) for t in terms}
    phrase = " ".join(re.findall(r"[a-z0-9$]+", query.lower()))
    out = []
    for c in chunks:
        low = c["text"].lower()
        s = sum(low.count(t) * math.log(1 + len(chunks) / max(1, df[t])) for t in terms)
        if phrase and phrase in " ".join(re.findall(r"[a-z0-9$]+", low)):
            s += 5.0
        s *= (sum(1 for t in terms if t in low) / len(terms))
        if s > 0:
            out.append({**c, "relevance": round(s, 3)})
    return sorted(out, key=lambda c: -c["relevance"])


def search(creator_id: str, query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """“every time he talks about his first million”: the words of every video read so far are
    searched, Claude picks the stretches that really answer it, and they come back as moments
    (status candidate, so they can be downloaded, checked and clipped like the rest)."""
    query = (query or "").strip()[:200]
    if not query:
        return []
    creator = creators.get_creator(creator_id)
    if not creator:
        raise ValueError("That creator doesn't exist any more")
    chunks = score_chunks(creators.search_chunks(creator_id, _terms(query)), query)[:30]
    if not chunks:
        return []
    picks: List[Dict[str, Any]] = []
    if _claude_ready():
        lines = []
        for i, c in enumerate(chunks):
            lines.append(f"[{i}] “{(c.get('title') or '')[:70]}” {c['start']:.0f}s-{c['end']:.0f}s: "
                         f"{_context(c['catalog_id'], c['start'], c['end'])[:900]}")
        prompt = (f"Search: {query}\n\nStretches from {creator['name']}'s videos (start-end in seconds of that "
                  f"video):\n\n" + "\n\n".join(lines) + f"\n\nPick up to {limit} that really answer the search and "
                  "would work as a clip. Give start/end inside the stretch (10-75 s), the kind, and the key quote "
                  "copied exactly.")
        rules_text = guidance(_rules_for(creator))
        system = (f"You find moments in {creator['name']}'s videos that answer a search, for a clipping page. Only "
                  "stretches that really answer it." + (f"\n\nCAMPAIGN RULES:\n{rules_text}" if rules_text else ""))
        key = _key("search", CLAUDE_MODEL, system, prompt)
        got = creators.cache_get(key)
        if got is None:
            got = toolio.ask(_client(), "moments", retries=0, model=CLAUDE_MODEL, max_tokens=4000, system=system,
                             tools=[SEARCH_TOOL], tool_choice={"type": "tool", "name": "pick_stretches"},
                             messages=[{"role": "user", "content": prompt}])
            creators.cache_put(key, got)
        for g in got:
            i = highlights._int((g or {}).get("n"), -1)
            if 0 <= i < len(chunks):
                picks.append({**g, "_chunk": chunks[i]})
    else:
        for c in chunks[:limit]:
            picks.append({"start": c["start"], "end": min(c["end"], c["start"] + 45), "kind": "quote",
                          "quote": "", "score": 5, "why": "Matched your words (Claude wasn't available to judge).",
                          "_chunk": c, "_plain": True})
    out: List[Dict[str, Any]] = []
    for p in picks[:limit]:
        c = p["_chunk"]
        row = creators.get_catalog(c["catalog_id"]) or {}
        transcript = creators.get_words(c["catalog_id"]) or {}
        if p.get("_plain"):
            found = [{"kind": "quote", "start": float(c["start"]), "end": float(p["end"]), "hit": float(c["start"]),
                      "speaker": "unclear", "reason": p["why"], "quote": "", "match": 0.0, "text_score": 0.5}]
        else:
            found, _ = verify([p], transcript, row.get("duration"), creator["name"])
        for f in found:
            same = [m for m in creators.list_moments(creator_id, catalog_id=c["catalog_id"], status="all")
                    if min(m["end"], f["end"]) - max(m["start"], f["start"]) > 0.5 * (f["end"] - f["start"])]
            if same:
                out.append(same[0])
                continue
            said = words_between(transcript, f["start"], f["end"])
            sig = {"text": f["text_score"], "heat": heat_peak(row.get("heatmap") or [], f["start"], f["end"]),
                   "loud": loud_level(transcript.get("loud"), f["start"], f["end"]), "laugh": laugh_level(said),
                   "outlier": row.get("outlier"), "age_days": age_days(row.get("upload_date") or ""),
                   "quote": f["quote"], "search": query}
            sig = {k: v for k, v in sig.items() if v is not None}
            mid = creators.add_moments(creator_id, c["catalog_id"], [{
                "kind": f["kind"], "start": f["start"], "end": f["end"], "hit": f["hit"], "score": merge_score(sig),
                "signals": sig, "text": said, "reason": f["reason"] or highlights._text(p.get("why")),
                "speaker": p.get("speaker") if p.get("speaker") in ("creator", "other", "unclear") else f["speaker"]}])[0]
            out.append(creators.get_moment(mid))
    seen, unique = set(), []
    for m in out:
        if m and m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)
    return [moment_json(m) for m in unique]


def _context(catalog_id: str, start: float, end: float) -> str:
    t = creators.get_words(catalog_id) or {}
    return highlights._transcript_text(t.get("segments") or [], start - 10, end + 10)


# --- moments for the browser ---------------------------------------------------------------------------

def _ts(seconds: float) -> str:
    return highlights._ts(seconds)


def _link_at(row: Dict[str, Any], t: float) -> str:
    url = row.get("url") or ""
    s = max(0, int(t))
    if row.get("platform") == "youtube" and "watch?v=" in url:
        return f"{url}&t={s}s"
    if row.get("platform") == "twitch":
        return f"{url}?t={s // 3600}h{s % 3600 // 60}m{s % 60}s"
    return url


def signals_text(sig: Dict[str, Any]) -> List[str]:
    """The signals in plain words, for the moment's card."""
    out = []
    heat = _f(sig.get("heat"))
    if heat is not None and heat >= 0.6:
        out.append("One of the most replayed parts of the video" if heat >= 0.85 else "Replayed more than most")
    ratio = _f(sig.get("outlier"))
    if ratio and ratio >= 2:
        out.append(f"The video did {ratio:.1f}× the channel's usual views")
    loud = _f(sig.get("loud"))
    if loud is not None and loud >= 0.9:
        out.append("Loud — shouting, laughing or a crowd")
    if sig.get("laugh"):
        out.append("Laughter in the room")
    days = _f(sig.get("age_days"))
    if days is not None and days <= 31:
        out.append("Uploaded in the last month")
    motion = _f(sig.get("motion"))
    if motion is not None and motion >= 0.5:
        out.append("Big movement on screen")
    if sig.get("on_screen") == "yes":
        out.append("A person is clearly on screen")
    elif sig.get("on_screen") == "no":
        out.append("Nobody clearly on screen")
    if sig.get("picture_note"):
        out.append(f"Picture: {sig['picture_note']}")
    for k in ("check_note", "claude_note", "face_note", "fetch_error"):
        if sig.get(k):
            out.append(str(sig[k]))
    if sig.get("search"):
        out.append(f"Found by searching “{sig['search']}”")
    return out


def moment_json(m: Dict[str, Any]) -> Dict[str, Any]:
    """A moment as the browser shows it."""
    row = creators.get_catalog(m.get("catalog_id") or "") or {}
    sig = m.get("signals") or {}
    day = catalog.ymd(row.get("upload_date"))
    has_section = bool(m.get("section_path")) and Path(m["section_path"]).is_file()
    off = float(m.get("section_offset") or 0.0)
    yt_thumb = (f"https://i.ytimg.com/vi/{row['video_id']}/hqdefault.jpg"
                if row.get("platform") == "youtube" and row.get("video_id") else None)
    return {
        "id": m["id"], "kind": m.get("kind"), "score": round(float(m.get("score") or 0)), "hook": m.get("hook") or "",
        "text": m.get("text") or "", "quote": sig.get("quote") or "", "reason": m.get("reason") or "",
        "speaker": m.get("speaker") or "unclear", "status": m.get("status"), "drop_reason": m.get("drop_reason") or "",
        "story_key": m.get("story_key") or "",
        "start": m.get("start"), "end": m.get("end"), "hit": m.get("hit"),
        "length": round(float(m.get("end") or 0) - float(m.get("start") or 0), 1),
        "at": _ts(float(m.get("start") or 0)),
        "video": {"id": row.get("id"), "title": row.get("title") or "", "url": row.get("url") or "",
                  "platform": row.get("platform") or "", "kind": row.get("kind") or "", "views": row.get("views"),
                  "date": f"{day[:4]}-{day[4:6]}-{day[6:]}" if day else "",
                  "link_at": _link_at(row, float(m.get("start") or 0))},
        "section_url": f"/media/section/{m['id']}.mp4" if has_section else None,
        "section_start": round(float(m["start"]) - off, 2) if has_section else None,
        "section_end": round(float(m["end"]) - off, 2) if has_section else None,
        "thumb_url": f"/media/moment-thumb/{m['id']}.jpg" if has_section else yt_thumb,
        "used_in": m.get("used_in") or [],
        "signals": signals_text(sig),
        "numbers": {k: sig.get(k) for k in ("text", "rank", "heat", "loud", "outlier", "age_days", "motion",
                                            "face_share", "visual") if sig.get(k) is not None},
    }


# --- moments → clips, an edit --------------------------------------------------------------------------

_jobs: "List[Tuple[str, str]]" = []
_jobs_cv = threading.Condition()
_jobs_worker = False

CLIP_SETTING_KEYS = ("layout", "caption_style", "caption_position", "tighten", "drop_fillers", "max_gap",
                     "auto_frame", "motion", "headline", "auto_style", "style_recipe", "doctor", "platforms", "accent",
                     "logo", "logo_corner")
CLIP_DEFAULTS = {"max_clips": 1, "layout": "auto", "caption_style": "impact", "caption_position": "bottom",
                 "tighten": True, "drop_fillers": True, "max_gap": 0.6, "auto_frame": True, "motion": True,
                 "structure": False, "alternates": False, "headline": True, "auto_style": True,
                 "style_recipe": "auto", "doctor": True, "platforms": [], "accent": "", "logo": False,
                 "logo_corner": "top-right"}


def _campaign_job_settings(campaign_id: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """The same as a campaign run from the Make page: the brief's rules on the settings, a copy of the
    rulebook kept with the job. Plain ValueError when the campaign can't take it."""
    from . import brandlogo, campaign
    camp = store.get_campaign(campaign_id)
    if not camp:
        raise ValueError("That campaign doesn't exist any more")
    rb = camp["rulebook"]
    if rb.get("mode") == "overlay":
        raise ValueError(f"{camp['name']} is a clip-bank campaign: it posts the brand's own clips, so moments "
                         "from a creator's videos can't be used for it.")
    has = brandlogo.exists(campaign_id)
    if campaign.needs_brand_logo(rb) and not has:
        raise ValueError("This brief requires the brand's logo on every post — add the logo file on the campaign "
                         "first (Brand logo → Add logo).")
    out, notes = campaign.source_settings(rb, settings)
    logo = brandlogo.path_for(campaign_id) if has and campaign.wants_brand_logo(rb) else None
    out["brand_logo"] = str(logo) if logo else ""
    out["campaign"] = {"id": campaign_id, "name": camp["name"], "mode": "source", "rules": rb, "notes": notes,
                       "logo": out["brand_logo"]}
    out["structure"] = False
    out["alternates"] = False
    return out


def _window(m: Dict[str, Any]) -> Dict[str, Any]:
    """The clip wanted from a job's source: section-relative times when the section is down,
    with `offset` saying where the section starts in the full video."""
    off = float(m.get("section_offset") or 0.0) if m.get("section_path") else 0.0
    return {"start": round(float(m["start"]) - off, 2), "end": round(float(m["end"]) - off, 2),
            "hit": round(float(m.get("hit") or m["start"]) - off, 2), "offset": round(off, 3),
            "hook": m.get("hook") or "", "title": (m.get("hook") or (m.get("signals") or {}).get("quote") or "")[:80],
            "kind": m.get("kind") or "", "type": KIND_TO_CLIP.get(m.get("kind") or "", "story"),
            "score": int(round(float(m.get("score") or 0))), "reason": m.get("reason") or "",
            "moment_id": m["id"]}


def make_clips(moment_ids: Sequence[str], settings: Optional[Dict[str, Any]] = None) -> List[str]:
    """Each moment becomes a normal job (framing, style brain, clip doctor, campaign gate) whose
    source is its downloaded part of the video and whose only clip is the moment. Moments not
    downloaded yet are fetched first. Jobs run one after another. Returns the job ids."""
    settings = dict(settings or {})
    moments = [m for m in (creators.get_moment(i) for i in moment_ids or []) if m]
    if not moments:
        raise ValueError("Select at least one moment")
    if len(moments) > 40:
        raise ValueError("40 moments at a time is the limit")
    base = {**CLIP_DEFAULTS, **{k: settings[k] for k in CLIP_SETTING_KEYS if k in settings}}
    if isinstance(base.get("platforms"), str):
        base["platforms"] = [p for p in base["platforms"].split(",") if p.strip()]
    job_ids = []
    for m in moments:
        creator = creators.get_creator(m["creator_id"]) or {}
        camp_id = str(settings.get("campaign_id") or creator.get("campaign_id") or "")
        js = _campaign_job_settings(camp_id, dict(base)) if camp_id else dict(base)
        js["only_window"] = _window(m)
        js["creator_scan"] = {"creator_id": m["creator_id"], "moment_id": m["id"], "catalog_id": m["catalog_id"]}
        row = creators.get_catalog(m["catalog_id"]) or {}
        title = f"{(row.get('title') or creator.get('name') or 'Moment')[:70]} · {_ts(float(m['start']))}"
        jid = store.create_job(title=title, source=row.get("url") or "creator-scan", settings=js)
        store.update_job(jid, stage="Waiting in the queue" if m.get("section_path") else
                         "Waiting to download this moment's part of the video",
                         **({"campaign_id": camp_id} if camp_id else {}))
        creators.update_moment(m["id"], status="used" if m.get("status") != "dropped" else "dropped",
                               used_in=list(dict.fromkeys((m.get("used_in") or []) + [jid])))
        job_ids.append(jid)
    with _jobs_cv:
        _jobs.extend((j, m["id"]) for j, m in zip(job_ids, moments))
        _jobs_cv.notify()
    _ensure_worker()
    return job_ids


def _ensure_worker() -> None:
    global _jobs_worker
    with _jobs_cv:
        if _jobs_worker:
            return
        _jobs_worker = True
    threading.Thread(target=_job_loop, name="creator-clips", daemon=True).start()


def _job_loop() -> None:
    while True:
        with _jobs_cv:
            while not _jobs:
                _jobs_cv.wait()
            job_id, moment_id = _jobs.pop(0)
        try:
            run_moment_job(job_id, moment_id)
        except Exception:
            traceback.print_exc()


def ensure_section(moment_id: str, net: Optional[catalog.Net] = None) -> Dict[str, Any]:
    """The moment with its part of the video on this PC (downloaded now when it isn't).
    Raises ValueError in plain words when it can't be had."""
    m = creators.get_moment(moment_id)
    if not m:
        raise ValueError("That moment doesn't exist any more")
    if m.get("section_path") and Path(m["section_path"]).is_file():
        return m
    row = creators.get_catalog(m["catalog_id"])
    if not row:
        raise ValueError("The video this moment came from isn't in the catalog any more")
    try:
        fetch_moments(net or catalog.Net(sleep=SLEEP, clock=CLOCK), m["creator_id"], row, [m])
    except catalog.ScanStop as exc:
        raise ValueError(exc.message) from exc
    m = creators.get_moment(moment_id) or m
    if not (m.get("section_path") and Path(m["section_path"]).is_file()):
        raise ValueError((m.get("signals") or {}).get("fetch_error") or m.get("drop_reason")
                         or "This moment's part of the video couldn't be downloaded — try again later.")
    return m


def run_moment_job(job_id: str, moment_id: str) -> None:
    """Fetch the moment's section if needed, then run the normal pipeline on it."""
    from . import pipeline
    job = store.get_job(job_id)
    if not job or job.get("status") not in ("queued", None, ""):
        return
    try:
        store.update_job(job_id, status="running", stage="Downloading this moment's part of the video", progress=2)
        m = ensure_section(moment_id)
    except Exception as exc:  # noqa: BLE001 — the job says why, in plain words
        store.update_job(job_id, status="failed", stage="Failed at: Downloading this moment's part of the video",
                         error=str(exc)[:500], progress=100)
        notify.job_finished(job_id)
        return
    settings = json.loads(job.get("settings") or "{}")
    settings["only_window"] = _window(m)
    store.update_job(job_id, settings=json.dumps(settings))
    _seed_transcript(m)
    pipeline.run_job(job_id, None, Path(m["section_path"]))


def _section_words(m: Dict[str, Any], length: float) -> Optional[Dict[str, Any]]:
    """The catalog's words for this section, on the section's clock — only when they came from
    Whisper (subtitles have no punctuation or capitals, so the clip gets its own Whisper pass)."""
    row = creators.get_catalog(m["catalog_id"]) or {}
    if row.get("words_source") not in ("whisper", "partial"):
        return None
    t = creators.get_words(m["catalog_id"]) or {}
    off = float(m.get("section_offset") or 0.0)
    words = [{"w": w["w"], "start": round(float(w["start"]) - off, 3), "end": round(float(w["end"]) - off, 3)}
             for w in t.get("words") or [] if off - 0.05 <= float(w["start"]) <= off + length]
    segs = [{"text": s["text"], "start": round(max(0.0, float(s["start"]) - off), 2),
             "end": round(min(length, float(s["end"]) - off), 2)}
            for s in t.get("segments") or [] if float(s["end"]) > off and float(s["start"]) < off + length]
    if len(words) < 5:
        return None
    return {"words": words, "segments": segs, "text": " ".join(s["text"] for s in segs)}


def _seed_transcript(m: Dict[str, Any]) -> None:
    """When the scan already paid Whisper for these words, the clip job reuses them (the pipeline's
    transcript cache is keyed by the file), so the same audio is never transcribed twice."""
    try:
        from . import pipeline
        path = Path(m["section_path"])
        length = media.probe(path)["duration"]
        words = _section_words(m, length)
        if not words:
            return
        fp = pipeline.source_fingerprint(path)
        if fp and not store.cached_transcript(fp):
            store.cache_transcript(fp, words, length)
    except Exception:
        traceback.print_exc()


def _section_job(m: Dict[str, Any]) -> str:
    """A finished "video" whose file is the moment's section and whose words are its words —
    what the Edit Maker cuts from. Reused when one already exists for this section."""
    from . import transcribe
    path = m["section_path"]
    for jid in reversed(m.get("used_in") or []):
        j = store.get_job(jid)
        if j and j.get("status") == "done" and j.get("source_path") == path and j.get("transcript") \
                and Path(path).is_file():
            return jid
    with store.connect() as conn:
        for r in conn.execute("SELECT id, transcript FROM jobs WHERE source_path=? AND status='done'", (path,)):
            if r["transcript"]:
                return r["id"]
    length = media.probe(Path(path))["duration"]
    words = _section_words(m, length)
    if not words:
        from . import pipeline
        fp = pipeline.source_fingerprint(Path(path))
        words = store.cached_transcript(fp) if fp else None
        if not words:
            from .config import WHISPER_API_KEY
            if WHISPER_API_KEY:
                wav = media.extract_audio(Path(path), f"section_{m['id']}")
                try:
                    words = transcribe.transcribe(wav)
                finally:
                    wav.unlink(missing_ok=True)
                if fp:
                    store.cache_transcript(fp, words, length)
            else:
                t = creators.get_words(m["catalog_id"]) or {}
                off = float(m.get("section_offset") or 0.0)
                segs = [{"text": s["text"], "start": round(max(0.0, float(s["start"]) - off), 2),
                         "end": round(min(length, float(s["end"]) - off), 2)}
                        for s in t.get("segments") or [] if float(s["end"]) > off and float(s["start"]) < off + length]
                timed = [{"w": w["w"], "start": round(float(w["start"]) - off, 3), "end": round(float(w["end"]) - off, 3)}
                         for w in t.get("words") or [] if off <= float(w["start"]) <= off + length]
                words = {"segments": segs, "words": timed or catalog.spread_words(segs),
                         "text": " ".join(s["text"] for s in segs)}
    row = creators.get_catalog(m["catalog_id"]) or {}
    title = f"{(row.get('title') or 'Moment')[:70]} · {_ts(float(m['start']))}"
    jid = store.create_job(title=title, source=row.get("url") or "creator-scan",
                           settings={"creator_scan": {"creator_id": m["creator_id"], "moment_id": m["id"],
                                                      "catalog_id": m["catalog_id"], "for": "edit"}})
    store.update_job(jid, status="done", stage="Done — a part of a video from Creator Scan, for edits", progress=100,
                     source_path=path, duration=length, transcript=json.dumps(words))
    return jid


def _edit_moment(m: Dict[str, Any], source_id: str, drop: bool) -> Dict[str, Any]:
    off = float(m.get("section_offset") or 0.0)
    quote = ((m.get("signals") or {}).get("quote") or "").split()
    return {"source": source_id, "start": round(float(m["start"]) - off, 2), "end": round(float(m["end"]) - off, 2),
            "hit": round(float(m.get("hit") or m["start"]) - off, 2), "text": " ".join(quote[:9]),
            "kind": KIND_TO_EDIT.get(m.get("kind") or "", "quote"), "drop": drop,
            "why": (m.get("reason") or "")[:160]}


def make_edit(moment_ids: Sequence[str], settings: Optional[Dict[str, Any]] = None) -> str:
    """An Edit Maker edit made of these moments (no Claude pick: they're already chosen and scored;
    the Edit Maker's own checks still run — real times, words really said, one drop). The moments'
    parts of the video are downloaded first when needed. Returns the edit's id at once; it fills in
    in the background like any edit."""
    from . import edits
    settings = dict(settings or {})
    moments = [m for m in (creators.get_moment(i) for i in moment_ids or []) if m]
    if not moments:
        raise ValueError("Select at least one moment")
    style = edits.style_key(settings.get("style"))
    st = edits.STYLES[style]
    sound = store.get_sound(settings.get("sound") or "") if settings.get("sound") else None
    if settings.get("sound") and not sound:
        raise ValueError("That song isn't in your songs any more — pick another one")
    if st.get("needs_music") and not sound:
        raise ValueError(f"{edits._a(st['name'])} edit is cut to music — add or pick a song first")
    creator = creators.get_creator(moments[0]["creator_id"]) or {}
    camp_id = str(settings.get("campaign_id") or creator.get("campaign_id") or "")
    if camp_id and not store.get_campaign(camp_id):
        raise ValueError("That campaign doesn't exist any more")
    best = max(moments, key=lambda m: float(m.get("score") or 0))
    hook = best.get("hook") or ""
    theme = str(settings.get("theme") or "").strip()
    title = f"{creator.get('name') or 'Creator'} — {theme or 'best moments'}"[:80]
    eid = store.create_edit(title, {**{k: v for k, v in settings.items() if k != "sources"}, "style": style,
                                    "sources": [], "campaign_id": camp_id}, camp_id)
    store.update_edit(eid, stage="Getting the moments' part of the video", plan={
        "given_moments": [], "hook": hook, "title": title, "moment_ids": [m["id"] for m in moments]})
    threading.Thread(target=_build_edit, args=(eid, [m["id"] for m in moments], settings, camp_id, hook, title),
                     name=f"creator-edit-{eid}", daemon=True).start()
    return eid


def _build_edit(eid: str, moment_ids: List[str], settings: Dict[str, Any], camp_id: str, hook: str,
                title: str) -> None:
    from . import edits
    try:
        net = catalog.Net(sleep=SLEEP, clock=CLOCK)
        ready: List[Dict[str, Any]] = []
        problems: List[str] = []
        for i, mid in enumerate(moment_ids):
            store.update_edit(eid, status="running", stage=f"Getting the moments' video — {i + 1} of {len(moment_ids)}",
                              progress=1 + int(6 * i / max(1, len(moment_ids))))
            try:
                ready.append(ensure_section(mid, net))
            except ValueError as exc:
                problems.append(str(exc))
        if not ready:
            raise ValueError("None of the moments' video could be downloaded: " + (problems[0] if problems else ""))
        sources: List[str] = []
        given = []
        best = max(ready, key=lambda m: float(m.get("score") or 0))
        for m in ready:
            sid = _section_job(m)
            if sid not in sources:
                sources.append(sid)
            given.append(_edit_moment(m, sid, m is best))
            creators.update_moment(m["id"], status="used" if m.get("status") != "dropped" else "dropped",
                                   used_in=list(dict.fromkeys((m.get("used_in") or []) + [eid])))
        clean = edits.check_settings({**settings, "sources": sources, "campaign_id": camp_id})
        plan = {"given_moments": given, "hook": hook, "title": title, "moment_ids": moment_ids,
                "post": {"caption": "", "hashtags": []}}
        if problems:
            plan["given_notes"] = [f"Left out a moment: {p}" for p in problems]
        store.update_edit(eid, settings=clean, plan=plan, status="queued", stage="Waiting")
        edits.run(eid, True)
    except Exception as exc:  # noqa: BLE001 — stored on the edit in plain words
        traceback.print_exc()
        store.update_edit(eid, status="failed", stage="Failed", progress=100, error=str(exc)[:400])


# --- keep watching ----------------------------------------------------------------------------------

def keep_watching(creator_id: str, on: bool = True) -> List[str]:
    """Add (or remove) the creator's YouTube channels to the channel watch (money.scout), so new
    uploads join the catalog and get scanned by themselves. Returns the watched links."""
    from . import money
    creator = creators.get_creator(creator_id)
    if not creator:
        raise ValueError("That creator doesn't exist any more")
    links = [catalog.classify_link(u) for u in creator["links"]]
    chans = [c["url"] for c in links if c["platform"] == "youtube" and c["type"] == "channel"]
    for url in chans:
        if on:
            money.watch_channel(url, creator.get("campaign_id") or "")
        else:
            money.unwatch_channel(url)
    return chans


def on_new_uploads(entries: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """New uploads found by money.scout on watched channels: those on a creator's channels join its
    catalog and a short scan reads them; Telegram says how many moments came out of them.
    Returns {creator_id: new videos added}."""
    added: Dict[str, int] = {}
    if not entries:
        return added
    for creator in creators.list_creators():
        mine = [e for e in entries if any(catalog.same_channel(e.get("channel") or "", link)
                                          for link in creator["links"])]
        if not mine:
            continue
        rules = _rules_for(creator)
        rows = []
        for e in mine:
            c = catalog.classify_link(e.get("url") or "")
            vid = str(e.get("id") or c.get("name") or "")
            if not vid:
                continue
            rows.append({"platform": c["platform"] or "youtube", "video_id": vid,
                         "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                         "title": str(e.get("title") or "")[:300], "duration": _f(e.get("duration")),
                         "upload_date": datetime.now().strftime("%Y%m%d"), "views": None, "likes": None,
                         "kind": "short" if "/shorts/" in (e.get("url") or "") else "video",
                         "_approx_date": True, "_group": e.get("channel") or ""})
        rows = catalog.apply_filters(rows, creator["settings"], rules)
        creators.upsert_catalog(creator["id"], rows)
        ids = []
        for r in rows:
            if r.get("status") != "listed":
                continue
            with store.connect() as conn:
                hit = conn.execute("SELECT id, status FROM catalog WHERE creator_id=? AND platform=? AND video_id=?",
                                   (creator["id"], r["platform"], r["video_id"])).fetchone()
            if hit and hit["status"] == "listed":
                ids.append(hit["id"])
        if not ids:
            continue
        added[creator["id"]] = len(ids)
        try:
            start(creator["id"], new_rows=ids)
        except Exception:
            traceback.print_exc()
    return added
