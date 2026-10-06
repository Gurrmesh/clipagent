"""Smart Stitch, part 1 — better clips from inside one video.

  teaser    the clip opens on 1.2-3.5 s of its own best moment, then a quick
            rewind and "HOW IT STARTED", then the story — which still plays in
            full, the teaser's words included
  proof     when he says "look at this chart" or "$50k", the picture cuts to
            where that is really on screen in the same video while his voice
            keeps going (a video-only insert, shown whole so it can be read)
  reaction  a laugh or shout that really followed the line, cut in right after it
  callback  the earlier line he refers back to, cut in with its own sound

Claude proposes, `stitchrules` checks (quotes, whole sentences, reactions
after what they react to, true labels, length caps, the campaign's brief),
the judge decides whether a teaser helps, and motion.py draws inserts over the
main sound. Every clip still goes through the clip doctor and the campaign gate.

Stored with a clip (in its edits JSON, so the editor, Undo and typed changes
all carry it):
  teaser      {start, end, quote, why, label}  the checked teaser — kept when off,
                                                so "add a teaser" can put it back
  teaser_on   bool
  inserts     [{id, kind, at, start, end, audio, fit, why, quote}]
              at = the moment in the video the insert belongs to (source seconds)
  inserts_on  bool
  smart       the receipt of the last render: the parts in play order with
              their roles, where the teaser, rewind and every insert sit on the
              clip's own clock, its length, and notes on anything left out
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import stitchrules

TEASER_MIN, TEASER_MAX = 1.2, 3.5
TEASER_LABELS = ("HOW IT STARTED", "BUT FIRST")
TEASER_MODES = ("decide", "always", "never")
REWIND_SECONDS = 0.4
HOOK_GUARD = 1.5              # the first seconds are the hook: no insert there
PUNCH_GUARD = 6.0             # at most this much of the ending is protected as the punchline
MAX_INSERTS = 3
LENGTHS = {"proof": (1.0, 3.0), "reaction": (0.7, 2.0), "callback": (1.5, 4.0)}
NAMES = {"teaser": "Teaser", "proof": "Proof shot", "reaction": "Reaction", "callback": "Callback"}
LEAD_WORDS = {"and", "but", "so", "because", "um", "uh", "like", "or", "then", "also"}
REACTION_SOUNDS = ("laugh", "shout", "silent")


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _text(v: Any) -> str:
    return " ".join(str(v or "").split()).strip()


def mode_of(settings: Dict[str, Any]) -> str:
    m = str(settings.get("teaser") or "decide").lower()
    return m if m in TEASER_MODES else "decide"


def allowed(rules: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Can this clip have a teaser or inserts at all? (yes/no, why not)."""
    from . import render
    if render.ENGINE == "classic":
        return False, "the classic renderer (CLIPAGENT_ENGINE=classic) can't draw teasers or inserts"
    if not stitchrules.campaign_allows(rules)["stitch"]:
        return False, "the brief doesn't allow joining moments from different points of the video"
    return True, ""


# --- the payoff, and checking a teaser ----------------------------------------------------

def payoff_part(parts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    pay = [p for p in parts if p.get("role") == "payoff"]
    return pay[-1] if pay else parts[-1]


def teaser_fits(teaser: Dict[str, Any], parts: Sequence[Dict[str, Any]]) -> Tuple[bool, str]:
    """A teaser lies inside the payoff — and in a one-part clip, not in its
    opening seconds, where it would only repeat how the clip starts anyway."""
    if not teaser or not parts:
        return False, "there's no teaser for this clip"
    s, e = _num(teaser.get("start")), _num(teaser.get("end"))
    if s is None or e is None:
        return False, "the teaser has no times"
    if not TEASER_MIN - 0.01 <= e - s <= TEASER_MAX + 0.01:
        return False, f"the teaser is {e - s:.1f}s — it must be {TEASER_MIN}-{TEASER_MAX}s"
    pay = payoff_part(parts)
    if s < pay["start"] - 0.3 or e > pay["end"] + 0.3:
        return False, "its moment isn't inside the clip's payoff any more"
    if len(parts) == 1 and s < pay["start"] + 3.0:
        return False, "it's the clip's own opening, so it would add nothing"
    return True, ""


def check_teaser(raw: Any, parts: Sequence[Dict[str, Any]],
                 words: Sequence[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Claude's teaser proposal, checked: not a spoiler, inside the payoff,
    whole sentences of 1.2-3.5 s (moved onto the nearest ones), and its quote
    is what's really said there. (teaser or None, why not)."""
    if not isinstance(raw, dict) or not parts:
        return None, "none proposed"
    why = _text(raw.get("why"))[:200]
    if raw.get("spoils_surprise") is True or re.search(r"\b(spoil|give[s]? away|ruin)", why, re.I):
        return None, "a teaser would give away the surprise"
    s, e = _num(raw.get("start")), _num(raw.get("end"))
    if s is None or e is None or e <= s:
        return None, "no usable times"
    pay = payoff_part(parts)
    lo = pay["start"] + (3.0 if len(parts) == 1 else 0.0)
    snapped = stitchrules.snap_sentences(s, e, words, lo, pay["end"], TEASER_MIN, TEASER_MAX)
    if snapped is None:
        said = [w for w in words if s - 0.05 <= float(w["start"]) < e - 0.05]
        if said or not (TEASER_MIN <= e - s <= TEASER_MAX) or s < lo - 0.3 or e > pay["end"] + 0.3:
            return None, f"no whole sentence of {TEASER_MIN}-{TEASER_MAX}s there inside the payoff"
        snapped = (round(s, 2), round(e, 2))            # a wordless moment: a laugh, a reaction
    s, e = snapped
    quote = _text(raw.get("quote"))
    ok, qwhy = stitchrules.check_quote(quote, words, s, e)
    if not ok:
        return None, f"its quote doesn't match the transcript ({qwhy})"
    first = next((w for w in words if s - 0.05 <= float(w["start"]) < e - 0.05), None)
    if first and re.sub(r"[^\w']", "", first["w"].lower()) in LEAD_WORDS:
        return None, f"it would open on “{first['w']}”, mid-thought"
    label = _text(raw.get("label")).upper()
    label = label if label in TEASER_LABELS or label == "" else TEASER_LABELS[0]
    if "label" not in raw:
        label = TEASER_LABELS[0]
    teaser = {"start": s, "end": e, "quote": quote[:200] or stitchrules.span_text(words, s, e)[:200],
              "why": why, "label": label}
    ok, fwhy = teaser_fits(teaser, parts)
    return (teaser, "") if ok else (None, fwhy)


def with_teaser(variant: Dict[str, Any], teaser: Dict[str, Any]) -> Dict[str, Any]:
    """A version of the clip that opens on the teaser (for the judge)."""
    story = [dict(p) for p in variant["parts"]]
    if story and not story[0].get("label") and teaser.get("label"):
        story[0]["label"] = teaser["label"]
    return {**variant, "parts": [{"start": teaser["start"], "end": teaser["end"], "role": "teaser", "label": ""}]
            + story}


# --- checking callbacks and reactions ------------------------------------------------------

def _inside_parts(t: float, parts: Sequence[Dict[str, Any]], slack: float = 0.3) -> bool:
    return any(p["start"] - slack <= t <= p["end"] + slack for p in parts)


def _overlaps_parts(s: float, e: float, parts: Sequence[Dict[str, Any]]) -> bool:
    return any(s < p["end"] - 0.2 and e > p["start"] + 0.2 for p in parts)


def insert_fits(ins: Dict[str, Any], parts: Sequence[Dict[str, Any]]) -> Tuple[bool, str]:
    """An insert belongs to this clip when the moment it goes with is in it,
    and what it shows isn't already in it."""
    at, s, e = _num(ins.get("at")), _num(ins.get("start")), _num(ins.get("end"))
    if at is None or s is None or e is None:
        return False, "it has no times"
    if not _inside_parts(at, parts):
        return False, "the moment it goes with isn't in this clip any more"
    if ins.get("audio") == "own" and _overlaps_parts(s, e, parts):
        return False, "it's already in the clip"
    return True, ""


def check_insert(raw: Any, parts: Sequence[Dict[str, Any]], words: Sequence[Dict[str, Any]],
                 duration: float, n: int) -> Tuple[Optional[Dict[str, Any]], str]:
    """A callback or reaction Claude proposed, checked against the transcript
    and the honesty rules. (insert or None, why not)."""
    if not isinstance(raw, dict):
        return None, "unreadable"
    kind = str(raw.get("kind") or "").lower()
    if kind not in ("callback", "reaction"):
        return None, f"unknown kind “{kind}”"
    s, e = _num(raw.get("start")), _num(raw.get("end"))
    if s is None or e is None or e <= s:
        return None, "no usable times"
    s, e = max(0.0, s), min(duration or e, e)
    lo, hi = LENGTHS[kind]
    line = _text(raw.get("refers_to") or raw.get("reacts_to"))
    near = _num(raw.get("at"))
    if near is None:
        return None, "no moment to place it at"
    anchor = stitchrules.anchor_after(line, words, near)
    if anchor is None:
        return None, "the line it goes with isn't said there"
    if not _inside_parts(anchor, parts):
        return None, "the line it goes with isn't in the clip"
    said = [w for w in words if s - 0.05 <= float(w["start"]) < e - 0.05]
    if said:
        snapped = stitchrules.snap_sentences(s, e, words, s - 1.5, e + 1.5, lo, hi)
        if snapped is None and kind == "callback":
            return None, f"no whole sentence of {lo:.1f}-{hi:.0f}s there"
        if snapped is not None:
            s, e = snapped
    if not lo - 0.05 <= e - s <= hi + 0.05:
        return None, f"{e - s:.1f}s long — a {kind} must be {lo}-{hi:.0f}s"
    ok, why = stitchrules.check_sentences(s, e, words)
    if not ok:
        return None, why
    quote = _text(raw.get("quote"))
    ok, why = stitchrules.check_quote(quote, words, s, e)
    if not ok:
        return None, f"its quote doesn't match the transcript ({why})"
    if _overlaps_parts(s, e, parts):
        return None, "it's already in the clip"
    sound = str(raw.get("sound") or "").lower()
    if kind == "callback":
        if e > anchor - 1.0:
            return None, "a callback has to be an earlier line"
        audio, fit = "own", False
    else:
        ok, why = stitchrules.check_reaction_order(s, anchor)
        if not ok:
            return None, why
        laugh = stitchrules.is_laughter([w for w in words if s - 0.05 <= float(w["start"]) < e - 0.05])
        audio = "own" if sound in ("laugh", "shout") or laugh else "main"
        fit = False
    return {"id": f"{kind[0]}{n}", "kind": kind, "at": anchor, "start": round(s, 2), "end": round(e, 2),
            "audio": audio, "fit": fit, "why": _text(raw.get("why"))[:200], "quote": quote[:200],
            "line": line[:160]}, ""


def loud_gaps(curve: Sequence[Dict[str, float]], words: Sequence[Dict[str, Any]],
              floor: float = 0.6) -> List[Tuple[float, float]]:
    """Loud stretches where nobody is talking — likely a laugh, a shout, a
    crowd — for Claude to consider as reactions. From the run's energy curve."""
    if not curve:
        return []
    vals = sorted(p["energy"] for p in curve)
    thr = max(floor, vals[int(0.9 * (len(vals) - 1))])
    step = (curve[1]["t"] - curve[0]["t"]) if len(curve) > 1 else 1.0
    spoken = sorted((float(w["start"]), float(w["end"])) for w in words)
    out: List[Tuple[float, float]] = []
    j = 0
    for p in curve:
        t0, t1 = p["t"], p["t"] + step
        if p["energy"] < thr:
            continue
        while j < len(spoken) and spoken[j][1] < t0:
            j += 1
        if j < len(spoken) and spoken[j][0] < t1:
            continue
        if out and t0 - out[-1][1] < 0.01:
            out[-1] = (out[-1][0], t1)
        else:
            out.append((t0, t1))
    return out


def loud_text(gaps: Sequence[Tuple[float, float]], start: float, end: float) -> str:
    near = [f"{a:.0f}-{b:.0f}s" for a, b in gaps if start - 5 <= a <= end + 30][:4]
    return ("loud with no words (maybe a laugh or shout): " + ", ".join(near)) if near else ""


# --- planning a render -------------------------------------------------------------------

def _frames_len(a: float, b: float, fps) -> float:
    from .pipeline import _frames
    return _frames(a, b, fps)


def _assemble(parts: Sequence[Dict[str, Any]], all_words, settings, fps, offset: float = 0.0) -> Dict[str, Any]:
    """Like pipeline.build_parts, part by part, remembering where each part
    and each of its kept stretches lands on the clip's clock."""
    from .pipeline import build_parts
    out = {"segments": [], "words": [], "labels": [], "saved": 0.0, "pieces": []}
    prev_end = None
    for part in parts:
        p = dict(part)
        if prev_end is not None and prev_end - 1.0 < p["start"] < prev_end:
            p["start"] = prev_end                       # overlapping the part before: never repeat a word
        segs, words, saved, labels = build_parts([p], all_words, settings, fps)
        length = sum(_frames_len(a, b, fps) for a, b in segs)
        if length <= 0:
            continue
        seg_map, o = [], offset
        for a, b in segs:
            seg_map.append((a, b, o))
            o += _frames_len(a, b, fps)
        out["pieces"].append({"part": p, "out_start": offset, "out_end": offset + length, "segs": seg_map})
        out["segments"] += segs
        out["words"] += [{**w, "start": round(w["start"] + offset, 3), "end": round(w["end"] + offset, 3)}
                         for w in words]
        out["labels"] += [(round(at + offset, 3), text) for at, text in labels]
        out["saved"] += saved
        offset += length
        prev_end = p["end"]
    out["end"] = offset
    return out


def _splice(parts: Sequence[Dict[str, Any]], own: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The story with callbacks and laugh/shout reactions cut in right after
    the line they go with — a part is split around one when needed."""
    story = [dict(p) for p in parts]
    for ins in sorted(own, key=lambda i: i["at"]):
        piece = {"start": ins["start"], "end": ins["end"], "role": ins["kind"],
                 "label": "EARLIER" if ins["kind"] == "callback" else "", "insert_id": ins["id"]}
        for k, p in enumerate(story):
            if p.get("insert_id"):
                continue
            if abs(ins["at"] - p["end"]) <= 0.35:
                story.insert(k + 1, piece)
                break
            if p["start"] + 0.35 < ins["at"] < p["end"] - 0.35:
                story[k:k + 1] = [{**p, "end": ins["at"]}, piece, {**p, "start": ins["at"], "label": ""}]
                break
    return story


def _map(t: float, pieces: Sequence[Dict[str, Any]]) -> Optional[float]:
    """Where source time `t` plays on the clip's clock (story parts only)."""
    for piece in pieces:
        if piece["part"].get("insert_id"):
            continue
        segs = piece["segs"]
        for i, (a, b, o) in enumerate(segs):
            if a - 0.02 <= t < b:
                return o + max(0.0, t - a)
            if i + 1 < len(segs) and b <= t < segs[i + 1][0]:
                return segs[i + 1][2]                   # in a pause that was cut: right where it resumes
        if segs and segs[-1][1] <= t <= piece["part"]["end"] + 0.35:
            return piece["out_end"]                     # the very end of a part: where the next one starts
    return None


def _punchline_from(words: Sequence[Dict[str, Any]], end: float) -> float:
    """Where the last sentence before `end` starts (clip clock) — the
    punchline, which no insert may cover."""
    ws = [w for w in words if float(w["start"]) < end - 0.05]
    if not ws:
        return max(0.0, end - 2.0)
    opens, _ = stitchrules._edges(sorted(ws, key=lambda w: float(w["start"])))
    last = len(ws) - 1
    i = max(k for k in opens if k <= last) if opens else last
    start = float(ws[i]["start"])
    return max(min(start, end - 1.5), end - PUNCH_GUARD)


def plan_render(parts: Sequence[Dict[str, Any]], edits: Dict[str, Any], all_words: Sequence[Dict[str, Any]],
                settings: Dict[str, Any], fps=None, rules: Optional[Dict[str, Any]] = None,
                limits: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Turn a clip's story parts and its Smart Stitch choices into what the
    renderer needs. None when the clip has nothing to add.

    Returns {"extras": bool, "receipt": {...}} and, with extras, "segments",
    "words", "saved", "labels", "overrides" [(out_start, out_end, src_start,
    fit)], "rewind" {"after", "seconds", "sound"} | None.
    `settings` carries the cutting choices (tighten, max_gap, drop_fillers);
    `limits` the run's length rules (platforms, min_len, max_len)."""
    limits = limits if limits is not None else settings
    teaser = edits.get("teaser") or None
    want_teaser = bool(teaser) and bool(edits.get("teaser_on"))
    inserts = [dict(i) for i in (edits.get("inserts") or []) if isinstance(i, dict)] \
        if edits.get("inserts_on", True) is not False else []
    if not want_teaser and not inserts:
        return None
    parts = [dict(p) for p in parts]
    notes: List[str] = []
    ok, why = allowed(rules)
    if not ok:
        return {"extras": False, "receipt": _receipt(notes=[f"No teaser or inserts: {why}."])}
    allow = stitchrules.campaign_allows(rules)
    if want_teaser:
        ok, why = teaser_fits(teaser, parts)
        if not ok:
            notes.append(f"Teaser left out: {why}.")
            want_teaser = False
    kept: List[Dict[str, Any]] = []
    for ins in inserts:
        name = NAMES.get(ins.get("kind"), "Insert")
        ok, why = insert_fits(ins, parts)
        if not ok:
            notes.append(f"{name} left out: {why}.")
        elif ins.get("fit") and not allow["borders"]:
            notes.append(f"{name} left out: the brief doesn't allow a background around the video, and a "
                         "chart cropped to fill the screen can't be read.")
        elif len(kept) >= MAX_INSERTS:
            notes.append(f"{name} left out: {MAX_INSERTS} inserts is the most a clip gets.")
        else:
            kept.append(ins)
    rewind = want_teaser and allow["speed"]
    sound = rewind and allow["music"] and allow["audio"]
    if want_teaser and not allow["speed"]:
        notes.append("No rewind effect — the brief doesn't allow speed changes, so the teaser cuts straight "
                     "to the story.")
    cap = stitchrules.length_cap(limits)
    lo = float(limits.get("min_len") or 0) if rules else 0.0
    cut_settings = dict(settings)
    own = [i for i in kept if i.get("audio") == "own"]
    over = [i for i in kept if i.get("audio") != "own"]

    built: Dict[str, Any] = {}
    for _ in range(8):
        built = _build(teaser if want_teaser else None, parts, own, all_words, cut_settings, fps, rewind)
        lost = [i for i in own if i["id"] not in built["own_at"]]
        if lost:
            own.remove(lost[0])
            notes.append(f"{NAMES[lost[0]['kind']]} left out: the line it goes with isn't where it can be cut in.")
            continue
        early = [i for i in own if built["own_at"].get(i["id"], 1e9) < built["guard"]]
        if early:
            own.remove(early[0])
            notes.append(f"{NAMES[early[0]['kind']]} left out: it would land in the first "
                         f"{HOOK_GUARD:.1f}s, which belong to the hook.")
            continue
        if built["total"] > cap + 0.05:
            if own:
                gone = own.pop()
                notes.append(f"{NAMES[gone['kind']]} left out to keep the clip under {cap:.0f}s.")
                continue
            if want_teaser:
                want_teaser = rewind = sound = False
                notes.append(f"Teaser left out to keep the clip under {cap:.0f}s.")
                continue
        if lo and built["total"] < lo and cut_settings.get("tighten", True):
            cut_settings["tighten"] = False             # under the brief's minimum: keep the pauses
            continue
        break
    overrides, placed = _place(over, built, notes)
    if not want_teaser and not own and not placed:
        return {"extras": False, "receipt": _receipt(notes=notes)}
    rw = {"after": built["t_len"], "seconds": built["rw_len"], "sound": bool(sound)} if built["rw_len"] else None
    receipt = _receipt(built, teaser if want_teaser else None, rw, own, placed, notes)
    return {"extras": True, "segments": built["segments"], "words": built["words"], "saved": built["saved"],
            "labels": built["labels"], "overrides": overrides, "rewind": rw, "receipt": receipt}


def _build(teaser, parts, own, all_words, settings, fps, rewind: bool) -> Dict[str, Any]:
    t = _assemble([{"start": teaser["start"], "end": teaser["end"], "role": "teaser", "label": ""}],
                  all_words, settings, fps) if teaser else {"segments": [], "words": [], "saved": 0.0, "end": 0.0}
    t_len = t["end"]
    rw_len = 0.0
    if teaser and rewind and t_len > 0:
        rw_len = round(REWIND_SECONDS * float(fps)) / float(fps) if fps else REWIND_SECONDS
    story = _splice(parts, own)
    s = _assemble(story, all_words, settings, fps, offset=t_len + rw_len)
    labels = list(s["labels"])
    if teaser and t_len > 0 and teaser.get("label"):
        first = s["pieces"][0]["out_start"] if s["pieces"] else t_len + rw_len
        if not any(abs(at - first) < 0.05 for at, _ in labels):
            labels.insert(0, (round(first, 3), teaser["label"]))
    own_at = {p["part"]["insert_id"]: p["out_start"] for p in s["pieces"] if p["part"].get("insert_id")}
    return {"segments": t["segments"] + s["segments"], "words": t["words"] + s["words"],
            "saved": round(t["saved"] + s["saved"], 2), "labels": labels, "pieces": s["pieces"],
            "t_len": t_len, "rw_len": rw_len, "total": s["end"], "own_at": own_at,
            "guard": max(HOOK_GUARD, t_len + rw_len + 0.25) if t_len else HOOK_GUARD,
            "teaser_words": t["words"]}


def _place(over: Sequence[Dict[str, Any]], built: Dict[str, Any], notes: List[str]
           ) -> Tuple[List[Tuple[float, float, float, bool]], List[Dict[str, Any]]]:
    """Put each video-only insert where its moment plays: never in the hook,
    the teaser or its rewind, never over the punchline, never on top of
    another insert."""
    pieces = built["pieces"]
    story = [p for p in pieces if not p["part"].get("insert_id") or p["part"].get("role") != "reaction"]
    end = story[-1]["out_end"] if story else built["total"]
    punch = _punchline_from(built["words"], end)
    taken = [(p["out_start"], p["out_end"]) for p in pieces if p["part"].get("insert_id")]
    overrides, placed = [], []
    for ins in sorted(over, key=lambda i: i["at"]):
        name = NAMES.get(ins["kind"], "Insert")
        lo, hi = LENGTHS.get(ins["kind"], (0.7, 3.0))
        o = _map(float(ins["at"]), pieces)
        if o is None:
            notes.append(f"{name} left out: the moment it goes with was cut out of the clip.")
            continue
        o_s = max(0.0, o - (0.12 if ins["kind"] == "proof" else 0.0))
        length = min(hi, float(ins["end"]) - float(ins["start"]))
        o_e = o_s + length
        if o_s < built["guard"]:
            notes.append(f"{name} left out: it would land in the first {HOOK_GUARD:.1f}s (the hook)"
                         + (" or the teaser" if built["t_len"] else "") + ".")
            continue
        if o_e > punch - 0.05:
            o_e = punch - 0.05
            if o_e - o_s < lo:
                notes.append(f"{name} left out: it would cover the punchline.")
                continue
        if any(o_s < b and o_e > a for a, b in taken):
            notes.append(f"{name} left out: another insert is already there.")
            continue
        taken.append((o_s, o_e))
        overrides.append((round(o_s, 3), round(o_e, 3), float(ins["start"]), bool(ins.get("fit"))))
        placed.append({**ins, "out_start": round(o_s, 3), "out_end": round(o_e, 3),
                       "src_end": round(float(ins["start"]) + (o_e - o_s), 3)})
    return overrides, placed


def _receipt(built: Optional[Dict[str, Any]] = None, teaser=None, rewind=None, own=(), placed=(),
             notes: Sequence[str] = ()) -> Dict[str, Any]:
    if not built:
        return {"teaser": None, "rewind": None, "inserts": [], "parts": [], "length": None, "notes": list(notes)}
    items = []
    for p in built["pieces"]:
        iid = p["part"].get("insert_id")
        ins = next((i for i in own if i["id"] == iid), None) if iid else None
        if ins:
            items.append({"id": ins["id"], "kind": ins["kind"], "name": NAMES[ins["kind"]],
                          "out_start": round(p["out_start"], 3), "out_end": round(p["out_end"], 3),
                          "src_start": ins["start"], "src_end": ins["end"], "audio": "own", "fit": False,
                          "why": ins.get("why", ""), "quote": ins.get("quote", "")})
    for ins in placed:
        items.append({"id": ins["id"], "kind": ins["kind"], "name": NAMES.get(ins["kind"], "Insert"),
                      "out_start": ins["out_start"], "out_end": ins["out_end"], "src_start": ins["start"],
                      "src_end": ins["src_end"], "audio": "main", "fit": bool(ins.get("fit")),
                      "why": ins.get("why", ""), "quote": ins.get("quote") or ins.get("trigger", "")})
    items.sort(key=lambda i: i["out_start"])
    parts = []
    if teaser and built["t_len"]:
        parts.append({"start": teaser["start"], "end": teaser["end"], "role": "teaser", "label": ""})
    first_label = {round(at, 2): text for at, text in built["labels"]}
    for p in built["pieces"]:
        part = p["part"]
        parts.append({"start": round(part["start"], 2), "end": round(part["end"], 2),
                      "role": part.get("role") or "payoff",
                      "label": first_label.get(round(p["out_start"], 2), part.get("label") or "")})
    return {
        "teaser": ({"start": teaser["start"], "end": teaser["end"], "out_start": 0.0,
                    "out_end": round(built["t_len"], 3), "label": teaser.get("label", ""),
                    "quote": teaser.get("quote", ""), "why": teaser.get("why", "")}
                   if teaser and built["t_len"] else None),
        "rewind": ({"out_start": round(built["t_len"], 3), "out_end": round(built["t_len"] + built["rw_len"], 3),
                    "sound": bool(rewind.get("sound"))} if rewind else None),
        "inserts": items, "parts": parts, "length": round(built["total"], 2), "notes": list(notes),
    }


def summary(receipt: Dict[str, Any]) -> str:
    """One line in plain words: what Smart Stitch did to a clip."""
    bits = []
    if (receipt or {}).get("teaser"):
        bits.append("opens with a teaser")
    n = len((receipt or {}).get("inserts") or [])
    if n:
        bits.append(f"{n} insert{'s' if n != 1 else ''}")
    return ", ".join(bits)


# --- rendering ---------------------------------------------------------------------------

def render(source: Path, clip_id: str, parts: Sequence[Dict[str, Any]], edits: Dict[str, Any],
           all_words: Sequence[Dict[str, Any]], settings: Dict[str, Any], fps, info: Dict[str, Any],
           framing_plan, rules: Optional[Dict[str, Any]] = None, limits: Optional[Dict[str, Any]] = None,
           words: Optional[List[Dict[str, Any]]] = None):
    """Render a clip with its teaser and inserts. Returns (out, words, saved,
    receipt) — or (None, None, None, receipt) when there is nothing to add or
    it couldn't be drawn, and the caller makes the plain cut (the receipt
    then says why)."""
    plan = plan_render(parts, edits, all_words, settings, fps, rules, limits)
    if not plan:
        return None, None, None, {}
    if not plan["extras"]:
        return None, None, None, plan["receipt"]
    return draw(plan, source, clip_id, edits, info, framing_plan, words)


def draw(plan: Dict[str, Any], source: Path, clip_id: str, edits: Dict[str, Any], info: Dict[str, Any],
         framing_plan, words: Optional[List[Dict[str, Any]]] = None):
    """Render a planned clip (see render)."""
    from . import render as renderer
    shown = words if words is not None else plan["words"]
    try:
        out = renderer.render_clip(
            source=source, clip_id=clip_id, start=min(a for a, _ in plan["segments"]),
            end=max(b for _, b in plan["segments"]), words=shown, edits=edits,
            has_audio=info.get("has_audio", True), plan=framing_plan,
            source_size=(info["width"], info["height"]), segments=plan["segments"], labels=plan["labels"],
            video_overrides=plan["overrides"], rewind=plan["rewind"])
    except Exception as exc:
        traceback.print_exc()
        receipt = _receipt(notes=plan["receipt"]["notes"] + [
            f"The teaser and inserts couldn't be drawn ({str(exc)[:120]}), so this is the plain cut."])
        return None, None, None, receipt
    return out, shown, plan["saved"], plan["receipt"]


def same_shape(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Do two receipts lay the clip out the same way (so typed caption words still line up)?"""
    def key(r):
        r = r or {}
        return (bool(r.get("teaser")), bool(r.get("rewind")),
                tuple((i.get("id"), i.get("out_start")) for i in r.get("inserts") or []))
    return key(a) == key(b)


# --- proof shots: what he talks about, shown from where it is on screen ---------------------

TRIGGER = re.compile(
    r"\b(look at (?:this|that|my|the|it)|right here|my account|p ?& ?l|pnl|p and l|this chart|the chart|"
    r"watch this|check (?:this|it) out|on (?:the )?screen|you can see|take a look|"
    r"my (?:balance|profit|payout|results?))\b", re.I)
MONEY = re.compile(r"(\$\s?\d[\d,.]*\s?(?:k|m|grand|thousand|million)?|\b\d[\d,.]*\s?(?:k|grand|thousand|million|"
                   r"percent|%)(?![a-z]))", re.I)
SCREEN_EDGES = 0.06           # share of edge pixels: a screen full of text, candles and grid lines
SCAN_W = 320

PROOF_TOOL = {
    "name": "pick_proof",
    "description": "For each moment, the one frame that shows what he is talking about — or none.",
    "input_schema": {
        "type": "object",
        "properties": {
            "picks": {"type": "array", "items": {"type": "object", "properties": {
                "moment": {"type": "integer", "description": "The moment's number."},
                "frame": {"type": "string", "description": "The frame's label, like F3 — or none."},
                "why": {"type": "string", "description": "One line: what the frame shows that matches what he says."},
            }, "required": ["moment", "frame", "why"]}},
        },
        "required": ["picks"],
    },
}

PROOF_SYSTEM = """You find proof shots for short vertical clips cut from a long video. At each moment below \
the speaker talks about something that can be shown — his P&L, a chart, a number, a result. The frames come \
from screen shares elsewhere in the same video, each with a label and its time. For each moment, pick the ONE \
frame that clearly shows exactly what he is talking about there (the same number, the same chart, the same \
account). None is a fine answer: a wrong proof shot is worse than none, so answer none unless you are sure."""


def _clock(t: float) -> str:
    m, s = divmod(int(max(0.0, t)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _sentence_around(ws: Sequence[Dict[str, Any]], i: int) -> str:
    opens, closes = stitchrules._edges(list(ws))
    a = max([k for k in opens if k <= i] or [0])
    b = min([k for k in closes if k >= i] or [len(ws) - 1])
    return " ".join(w["w"] for w in ws[a:b + 1])[:240]


def triggers(parts: Sequence[Dict[str, Any]], words: Sequence[Dict[str, Any]], limit: int = 2) -> List[Dict[str, Any]]:
    """Where he points at something that could be shown: "look at this chart",
    "my P&L", "right here", money and percentages. The strongest few, apart."""
    found = []
    for p in parts:
        ws = [w for w in words if p["start"] - 0.05 <= float(w["start"]) < p["end"] - 0.05]
        if not ws:
            continue
        text, starts = "", []
        for w in ws:
            starts.append(len(text))
            text += w["w"] + " "
        for rx, strength in ((TRIGGER, 2), (MONEY, 1)):
            for m in rx.finditer(text):
                i = max(k for k, c in enumerate(starts) if c <= m.start())
                found.append({"at": round(float(ws[i]["start"]), 2), "text": m.group(0).strip()[:40],
                              "strength": strength, "sentence": _sentence_around(ws, i)})
    found.sort(key=lambda f: (-f["strength"], f["at"]))
    chosen: List[Dict[str, Any]] = []
    for f in found:
        if all(abs(f["at"] - c["at"]) >= 5.0 for c in chosen):
            chosen.append(f)
        if len(chosen) >= limit:
            break
    return sorted(chosen, key=lambda f: f["at"])


def _stitch_dir() -> Path:
    from .config import DATA_DIR
    folder = Path(DATA_DIR) / "stitch"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def screen_map(source: Path, key: str) -> List[Dict[str, Any]]:
    """The whole video sampled (its keyframes, or every 3 s when those are
    sparse), each sample marked as a screen share or not: no big face, and a
    lot of on-screen text, candles and grid lines. Cached per video."""
    from . import media
    source = Path(source)
    cache = _stitch_dir() / f"{key}_screens.json"
    st = source.stat()
    stamp = f"{st.st_size}:{int(st.st_mtime)}"
    if cache.exists():
        try:
            got = json.loads(cache.read_text(encoding="utf-8"))
            if got.get("stamp") == stamp:
                return got["samples"]
        except (OSError, ValueError, KeyError):
            pass
    info = media.probe(source)
    duration = float(info.get("duration") or 0)
    samples = _scan(source, info, keyframes=True)
    if duration and len(samples) < duration / 12.0:
        samples = _scan(source, info, keyframes=False)
    try:
        cache.write_text(json.dumps({"stamp": stamp, "samples": samples}), encoding="utf-8")
    except OSError:
        pass
    return samples


def _scan(source: Path, info: Dict[str, Any], keyframes: bool) -> List[Dict[str, Any]]:
    import cv2
    import numpy as np
    from .motion import _FaceDetector
    W, H = int(info["width"]), int(info["height"])
    w = SCAN_W
    h = max(2, int(round(H * w / max(1, W) / 2)) * 2)
    vf = f"scale={w}:{h},showinfo" if keyframes else f"fps=1/3,scale={w}:{h},showinfo"
    cmd = (["ffmpeg", "-hide_banner", "-nostdin"] + (["-skip_frame", "nokey"] if keyframes else [])
           + ["-i", str(source), "-an", "-sn", "-dn", "-vf", vf, "-fps_mode", "passthrough",
              "-f", "rawvideo", "-pix_fmt", "bgr24", "-"])
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log)
    det = _FaceDetector(w, h)
    size = w * h * 3
    feats = []
    try:
        while True:
            buf = proc.stdout.read(size)
            if not buf or len(buf) < size:
                break
            img = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            edges = float(cv2.Canny(gray, 60, 150).mean()) / 255.0
            faces = det.detect(img, gray) if det.kind != "none" else []
            feats.append((edges, any(f[2] >= 0.12 for f in faces)))
    finally:
        proc.stdout.close()
        proc.wait()
    log.seek(0)
    times = [float(t) for t in re.findall(r"pts_time:\s*(-?[0-9.]+)", log.read().decode("utf-8", "replace"))]
    log.close()
    return [{"t": round(t, 2), "edges": round(e, 4), "face": big, "screen": (not big) and e >= SCREEN_EDGES}
            for (e, big), t in zip(feats, times)]


def _stretches(samples: Sequence[Dict[str, Any]]) -> List[List[int]]:
    groups: List[List[int]] = []
    for k, s in enumerate(samples):
        if not s["screen"]:
            continue
        if groups and groups[-1][-1] == k - 1:
            groups[-1].append(k)
        else:
            groups.append([k])
    return groups


def _range_for(samples: Sequence[Dict[str, Any]], k: int) -> Optional[Tuple[float, float]]:
    """The stretch of video a picked frame stands for: from it, up to ~2.5 s,
    never past the next sample that isn't a screen share."""
    t = samples[k]["t"]
    nxt = samples[k + 1] if k + 1 < len(samples) else None
    prv = samples[k - 1] if k > 0 else None
    start, end = t, t + 2.5
    if nxt is not None and not nxt["screen"]:
        end = min(end, nxt["t"] - 0.3)
    if end - start < 1.0:
        floor = prv["t"] if prv is not None and prv["screen"] else ((prv["t"] + 0.3) if prv is not None else 0.0)
        start = max(floor, end - 1.0)
    if end - start < 1.0:
        return None
    return round(start, 2), round(min(end, start + LENGTHS["proof"][1]), 2)


def _frame_jpeg(source: Path, t: float, width: int = 480) -> Optional[bytes]:
    proc = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(source), "-frames:v", "1",
                           "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                          capture_output=True)
    return proc.stdout if proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8" else None


def look_for_proof(source: Path, parts: Sequence[Dict[str, Any]], trig: Sequence[Dict[str, Any]],
                   samples: Sequence[Dict[str, Any]], taken_ids: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """One Claude look (4-8 frames): which screen share, if any, shows what
    he's talking about at each trigger. Returns proof inserts."""
    from . import highlights, toolio
    from .config import CLAUDE_MODEL
    groups = _stretches(samples)
    picked: List[int] = []
    moments = []
    for tr in trig:
        if not samples:
            break
        here = min(range(len(samples)), key=lambda k: abs(samples[k]["t"] - tr["at"]))
        if samples[here]["screen"] and abs(samples[here]["t"] - tr["at"]) < 3.0:
            continue                                    # it's already on screen right then
        cands = []
        for g in groups:
            best = min(g, key=lambda k: abs(samples[k]["t"] - tr["at"]))
            if _inside_parts(samples[best]["t"], parts, 0.5):
                continue                                # already in the clip
            cands.append((abs(samples[best]["t"] - tr["at"]), best))
        for _, k in sorted(cands)[:4]:
            if k not in picked:
                picked.append(k)
        moments.append(tr)
    picked = picked[:8]
    if not picked or not moments:
        return []
    labels: Dict[str, int] = {}
    content: List[Dict[str, Any]] = [{"type": "text", "text": "MOMENTS:\n" + "\n".join(
        f"Moment {n}: at {_clock(tr['at'])} he says: “{tr['sentence']}” (points at: “{tr['text']}”)"
        for n, tr in enumerate(moments, 1)) + "\n\nFRAMES from screen shares elsewhere in the same video:"}]
    for k in picked:
        jpg = _frame_jpeg(source, samples[k]["t"])
        if not jpg:
            continue
        label = f"F{len(labels) + 1}"
        labels[label] = k
        content.append({"type": "text", "text": f"{label} — from {_clock(samples[k]['t'])}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(jpg).decode("ascii")}})
    if not labels:
        return []
    client = highlights._client()
    message = client.messages.create(model=CLAUDE_MODEL, max_tokens=800, system=PROOF_SYSTEM, tools=[PROOF_TOOL],
                                     tool_choice={"type": "tool", "name": "pick_proof"},
                                     messages=[{"role": "user", "content": content}])
    out: List[Dict[str, Any]] = []
    used = set(taken_ids)
    for p in toolio.items(message, "picks"):
        m = highlights._int(p.get("moment"), 0) - 1
        lab = str(p.get("frame") or "").strip().upper()
        if not 0 <= m < len(moments) or lab not in labels:
            continue
        k = labels[lab]
        rng = _range_for(samples, k) if samples[k]["screen"] else None
        if not rng or _overlaps_parts(rng[0], rng[1], parts):
            continue
        n = 1
        while f"p{n}" in used:
            n += 1
        used.add(f"p{n}")
        tr = moments[m]
        out.append({"id": f"p{n}", "kind": "proof", "at": tr["at"], "start": rng[0], "end": rng[1],
                    "audio": "main", "fit": True, "why": _text(p.get("why"))[:200], "trigger": tr["text"],
                    "quote": tr["sentence"]})
    return out


def find_proofs(job_id: str, source: Path, clips: List[Dict[str, Any]], words: Sequence[Dict[str, Any]],
                rules: Optional[Dict[str, Any]] = None) -> None:
    """Proof shots for every clip that points at something: one scan of the
    video for screen shares, then one Claude look per clip. Never raises."""
    if not stitchrules.campaign_allows(rules)["borders"]:
        return
    todo = [(c, triggers(c.get("parts") or [], words)) for c in clips]
    todo = [(c, t) for c, t in todo if t]
    if not todo:
        return
    try:
        samples = screen_map(source, job_id)
    except Exception:
        traceback.print_exc()
        return
    if not any(s["screen"] for s in samples):
        return

    def one(item):
        c, trig = item
        try:
            got = look_for_proof(source, c.get("parts") or [], trig, samples,
                                 [i["id"] for i in c.get("inserts") or []])
            c["inserts"] = (c.get("inserts") or []) + got
        except Exception:
            traceback.print_exc()

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(one, todo))


def proof_near(job: Dict[str, Any], parts: Sequence[Dict[str, Any]], words: Sequence[Dict[str, Any]], near: str,
               rules: Optional[Dict[str, Any]], source: Path,
               taken_ids: Sequence[str] = ()) -> Tuple[Optional[Dict[str, Any]], str]:
    """"Show the chart when he says 50k": find where he says it in this clip,
    then the screen share that shows it. (insert or None, why not — in plain words)."""
    ok, why = allowed(rules)
    if not ok:
        return None, f"No proof shot: {why}."
    if not stitchrules.campaign_allows(rules)["borders"]:
        return None, ("No proof shot: the brief doesn't allow a background around the video, and a chart "
                      "cropped to fill the screen can't be read.")
    q = stitchrules.norm_tokens(near)
    ws = sorted((w for w in words if any(p["start"] - 0.05 <= float(w["start"]) < p["end"] - 0.05 for p in parts)),
                key=lambda w: float(w["start"]))
    toks = [(t, i) for i, w in enumerate(ws) for t in stitchrules.norm_tokens(w["w"])]
    hit = None
    if q:
        for a in range(len(toks)):
            window = [t for t, _ in toks[a:a + len(q)]]
            joined, want = "".join(window), "".join(q)
            if window == q or (want and want in joined) or stitchrules._ratio(q, window) >= stitchrules.QUOTE_MIN:
                hit = toks[a][1]
                break
    if hit is None:
        return None, f"Couldn't find where he says “{near}” in this clip, so no proof shot was added."
    trig = [{"at": round(float(ws[hit]["start"]), 2), "text": near[:40], "strength": 2,
             "sentence": _sentence_around(ws, hit)}]
    try:
        samples = screen_map(source, job.get("id") or "job")
    except Exception as exc:
        return None, f"Couldn't look through the video for screen shares ({str(exc)[:80]})."
    if not any(s["screen"] for s in samples):
        return None, "There's no screen share anywhere in this video to show, so no proof shot was added."
    try:
        got = look_for_proof(source, parts, trig, samples, taken_ids)
    except RuntimeError as exc:
        if "ANTHROPIC_API_KEY" in str(exc) or "anthropic" in str(exc).lower():
            return None, "Finding the chart needs Claude to look at frames, and the Claude key isn't set."
        raise
    if not got:
        return None, (f"Claude looked at the screen shares in this video and none of them clearly shows what he "
                      f"means by “{near}”, so nothing was added.")
    return got[0], ""


# --- the run: teasers decided, inserts found ---------------------------------------------

def prepare(job_id: str, source: Path, clips: List[Dict[str, Any]], words: Sequence[Dict[str, Any]],
            settings: Dict[str, Any], rules: Optional[Dict[str, Any]] = None, headline: str = "",
            duration: float = 0.0) -> None:
    """After the judge has picked each clip's version: check Claude's teaser
    and decide whether to use it (Always / Never / the judge decides), check
    the callbacks and reactions it marked, and look for proof shots. Sets
    clip["teaser"], ["teaser_on"], ["inserts"], ["smart_notes"]. Never raises."""
    for c in clips:
        c["teaser"], c["teaser_on"], c["inserts"], c["smart_notes"] = None, False, [], []
    ok, why = allowed(rules)
    if not ok:
        for c in clips:
            c["smart_notes"].append(f"No teaser or inserts: {why}.")
        return
    try:
        mode = mode_of(settings)
        for c in clips:
            if c.get("teaser_raw"):
                c["teaser"], tw = check_teaser(c["teaser_raw"], c.get("parts") or [], words)
                if tw and not c["teaser"]:
                    c["smart_notes"].append(f"No teaser: {tw}.")
        if mode == "always":
            for c in clips:
                c["teaser_on"] = bool(c["teaser"])
        elif mode == "decide":
            todo = [c for c in clips if c["teaser"]]
            if todo:
                from . import judge
                judge.compare_teaser(todo, words, headline)
                for c in todo:
                    c["teaser_on"] = (c.get("teaser_verdict") or {}).get("winner") == "with"
        if settings.get("inserts", True) is not False:
            for c in clips:
                for n, raw in enumerate(c.get("inserts_raw") or [], 1):
                    ins, iw = check_insert(raw, c.get("parts") or [], words, duration, n)
                    if ins:
                        c["inserts"].append(ins)
                    elif isinstance(raw, dict):
                        c["smart_notes"].append(f"{NAMES.get(str(raw.get('kind')), 'Insert')} not used: {iw}.")
            find_proofs(job_id, source, clips, words, rules)
            for c in clips:
                c["inserts"] = c["inserts"][:MAX_INSERTS]
    except Exception:
        traceback.print_exc()
        for c in clips:
            c["teaser_on"], c["inserts"] = False, []


def clip_edits(clip: Dict[str, Any], parts: Sequence[Dict[str, Any]], settings: Dict[str, Any],
               words: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """The Smart Stitch fields for one clip version's edits. The second
    version gets the same choices wherever they fit its own parts."""
    if "teaser" not in clip and "inserts" not in clip:
        return {}
    teaser = clip.get("teaser")
    if teaser and not teaser_fits(teaser, parts)[0]:
        teaser = check_teaser(clip.get("teaser_raw"), parts, words)[0] if clip.get("teaser_raw") and words else None
    inserts = [i for i in clip.get("inserts") or [] if insert_fits(i, parts)[0]]
    return {"teaser": teaser or {}, "teaser_on": bool(teaser) and bool(clip.get("teaser_on")),
            "inserts": inserts, "inserts_on": settings.get("inserts", True) is not False}


# --- the editor's re-render ----------------------------------------------------------------

def rerender(clip: Dict[str, Any], job: Dict[str, Any], merged: Dict[str, Any], asked: Dict[str, Any],
             start: float, end: float, span_changed: bool, all_words: Sequence[Dict[str, Any]],
             info: Dict[str, Any], framing_plan, fps, rules: Optional[Dict[str, Any]],
             source: Path) -> Optional[Dict[str, Any]]:
    """pipeline.rerender_clip for a clip with a teaser or inserts (or asked to
    get one). Returns the updated clip; or None to let the usual re-render run
    — merged["smart"] then says what applies (and why anything was left out)."""
    from . import pipeline, store, transcribe
    job_settings = json.loads(job.get("settings") or "{}")
    before = merged.get("smart") or {}
    req = merged.pop("proof_request", None)
    stored = json.loads(clip.get("parts") or "[]")
    base = [dict(p) for p in stored] if len(stored) > 1 and not span_changed else \
        [{"start": round(start, 2), "end": round(end, 2), "role": "payoff", "label": ""}]
    extra: List[str] = []
    if isinstance(req, dict) and (req.get("near") or "").strip():
        ins, note = proof_near(job, base, all_words, str(req["near"]), rules, source,
                               [i.get("id") for i in merged.get("inserts") or []])
        if ins:
            merged["inserts"] = list(merged.get("inserts") or []) + [ins]
            merged["inserts_on"] = True
        elif set(asked) <= {"proof_request"}:
            raise RuntimeError(note)                    # nothing else was asked: say so, keep the clip
        else:
            extra.append(note)
    plan = plan_render(base, merged, all_words, merged, fps, rules, limits=job_settings)
    if plan and plan["extras"]:
        typed = asked.get("words")
        words = typed if typed and same_shape(before, plan["receipt"]) else None
        if words is not None:
            words = transcribe.respell(words, merged.get("spell"))
        else:
            words = transcribe.respell(plan["words"], merged.get("spell"))
        store.update_clip(clip["id"], status="rendering")
        out, words, saved, receipt = draw(plan, source, clip["id"], merged, info, framing_plan, words)
        if out is not None:
            receipt["notes"] = list(receipt.get("notes") or []) + extra
            merged["smart"] = receipt
            used = out.get("plan") or framing_plan
            fields: Dict[str, Any] = dict(
                status="ready", hook=merged.get("hook", ""), headline=merged.get("headline", ""),
                words=json.dumps(words), edits=json.dumps(merged), saved=saved,
                framing=json.dumps(used.to_json() if used else {}), file=str(out["file"]), thumb=str(out["thumb"]))
            if len(base) == 1:
                fields.update(start=base[0]["start"], end=base[0]["end"], parts=json.dumps(base))
            if span_changed and len(stored) > 1:
                fields["variant"] = "continuous"
            store.update_clip(clip["id"], **fields)
            if rules:
                pipeline._gate_source(rules, clip["id"])
                store.update_job(clip["job_id"], stage=pipeline._campaign_stage(clip["job_id"]))
            return store.get_clip(clip["id"])
        plan = {"receipt": receipt}
    merged["smart"] = dict((plan or {}).get("receipt") or {})
    if extra:
        merged["smart"]["notes"] = list(merged["smart"].get("notes") or []) + extra
    if not same_shape(before, merged["smart"]):
        asked.pop("words", None)                        # typed captions were timed for the old layout
    return None


# --- typed changes ("remove the teaser", "no inserts", "show the chart when he says 50k") ----

def describe_lines(e: Dict[str, Any]) -> List[str]:
    """What Claude is told about a clip's teaser and inserts when reading a typed change."""
    lines = []
    receipt = e.get("smart") or {}
    t = receipt.get("teaser")
    if t:
        lines.append(f"Opens with a teaser (a {t['out_end']:.1f}s flash-forward of {t['start']:.1f}–{t['end']:.1f}: "
                     f"“{t.get('quote', '')[:120]}”), then a rewind, then the story.")
    elif (e.get("teaser") or {}).get("start") is not None:
        tz = e["teaser"]
        lines.append(f"No teaser now; one is ready if asked for ({tz['start']:.1f}–{tz['end']:.1f}: "
                     f"“{tz.get('quote', '')[:120]}”).")
    items = receipt.get("inserts") or []
    if items:
        lines.append("Inserts: " + "; ".join(
            f"{n}. {i['name'].lower()} at {i['out_start']:.1f}s for {i['out_end'] - i['out_start']:.1f}s"
            + (f" (“{i['quote'][:80]}”)" if i.get("quote") else "") for n, i in enumerate(items, 1)) + ".")
    elif e.get("inserts_on") is False and e.get("inserts"):
        lines.append(f"Inserts are switched off ({len(e['inserts'])} ready).")
    return lines


def _flag(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    return True if v in ("true", "yes", "on", "1") else False if v in ("false", "no", "off", "0") else None


def plan_change(clip: Dict[str, Any], e: Dict[str, Any], change: Dict[str, Any], words: Sequence[Dict[str, Any]],
                job: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """A typed change's teaser / insert controls → (edits, problems)."""
    from . import pipeline, toolio
    asked = [k for k in ("teaser", "inserts_on", "remove_inserts", "proof_at")
             if change.get(k) not in (None, "", [])]
    if not asked:
        return {}, []
    ok, why = allowed(pipeline.job_rules(job))
    if not ok:
        return {}, [f"no teaser or inserts on this clip — {why}"]
    out: Dict[str, Any] = {}
    problems: List[str] = []
    receipt = e.get("smart") or {}
    parts = json.loads(clip.get("parts") or "[]") or [{"start": clip["start"], "end": clip["end"], "role": "payoff"}]
    t = _flag(change.get("teaser"))
    if t is True:
        if (e.get("teaser") or {}).get("start") is not None:
            out["teaser_on"] = True
        else:
            raw = {"start": change.get("teaser_start"), "end": change.get("teaser_end"),
                   "quote": change.get("teaser_quote") or "", "why": "you asked for it"}
            teaser, tw = check_teaser(raw, parts, words) if raw["start"] is not None else \
                (None, "ClipAgent didn't find a short best moment in this clip to open on")
            if teaser:
                out["teaser"], out["teaser_on"] = teaser, True
            else:
                problems.append(f"couldn't add a teaser — {tw}")
    elif t is False:
        if receipt.get("teaser") or e.get("teaser_on"):
            out["teaser_on"] = False
        else:
            problems.append("this clip doesn't open with a teaser")
    on = _flag(change.get("inserts_on"))
    if on is False:
        out["inserts_on"] = False
    elif on is True:
        if e.get("inserts"):
            out["inserts_on"] = True
        elif not change.get("proof_at"):
            problems.append("this clip has no inserts to put back — ask for one, e.g. “show the chart when he says …”")
    gone = [str(x).strip().lower() for x in toolio.as_list(change.get("remove_inserts")) if str(x).strip()]
    if gone:
        listed = receipt.get("inserts") or []
        ids = set()
        for g in gone:
            if g.isdigit() and 1 <= int(g) <= len(listed):
                ids.add(listed[int(g) - 1]["id"])
            for i in listed:
                if g.rstrip("s") in (i["kind"], i["name"].lower(), i["name"].lower().split()[0]):
                    ids.add(i["id"])
        if ids:
            out["inserts"] = [i for i in e.get("inserts") or [] if i.get("id") not in ids]
        else:
            problems.append("there's no such insert in this clip")
    near = _text(change.get("proof_at"))
    if near:
        out["proof_request"] = {"near": near[:80]}
        out["inserts_on"] = True
    return out, problems
