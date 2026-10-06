"""The clip doctor: every finished clip gets checked the way an editor would
check it before posting, and whatever can be fixed is fixed.

Two kinds of check:
* technical, measured on the file — length for the platform, sound level,
  black or frozen picture, a hook on the first frame, whether it starts and
  ends on whole sentences;
* visual, by Claude looking at frames of the finished clip — text over a face
  or cut off, a badly cropped subject, typos on screen, a weak first frame.

Fixes are applied in one re-render, never more, and only the ones the clip's
campaign rules allow. The report is kept with the clip: what was found, what
was fixed, and what still needs a human look.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import highlights, store, toolio
from .config import CLAUDE_MODEL

# How long winning clips run, by platform (Clip Style Database, clip pages).
PLATFORM_LENGTH = {"youtube": (15.0, 40.0), "tiktok": (20.0, 70.0), "instagram": (15.0, 60.0)}
DEFAULT_LENGTH = (15.0, 70.0)
QUIET_DB = -30.0                  # mean volume below this: viewers turn it up, or scroll
LEAD_WORDS = {"and", "but", "so", "because", "um", "uh", "like", "or", "then", "also"}
SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*$")


def _short(text: str, n: int = 70) -> str:
    """Shortened on a word boundary, with an ellipsis — never "his OWN D"."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(",;:—-")
    return cut + "…"


def _item(cid: str, label: str, status: str, detail: str = "") -> Dict[str, str]:
    return {"id": cid, "label": label, "status": status, "detail": detail}


def _ffmpeg_stats(path: Path) -> Dict[str, Any]:
    """One decode pass: loudness, black stretches, frozen stretches."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-vf", "blackdetect=d=0.4:pix_th=0.08,freezedetect=n=0.002:d=1.5",
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace")
    log = proc.stderr or ""
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", log)
    peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", log)
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", log)]
    freezes = [float(d) for d in re.findall(r"freeze_duration:\s*([\d.]+)", log)]
    return {"mean": float(mean.group(1)) if mean else None, "peak": float(peak.group(1)) if peak else None,
            "blacks": blacks, "freezes": freezes}


def _probe(path: Path) -> Dict[str, Any]:
    from . import media
    try:
        return media.probe(path)
    except Exception:
        return {}


def opening_text(edits: Dict[str, Any]) -> str:
    for card in edits.get("cards") or []:
        if (card.get("text") or "").strip() and not float(card.get("start") or 0):
            return card["text"].strip()
    if edits.get("hook_on", True) and (edits.get("hook") or "").strip():
        return edits["hook"].strip()
    return ""


def technical(path: Path, edits: Dict[str, Any], words: List[Dict[str, Any]],
              platform: str = "") -> List[Dict[str, str]]:
    checks: List[Dict[str, str]] = []
    info = _probe(path)
    secs = float(info.get("duration") or 0)
    if not info or info.get("width") != 1080 or info.get("height") != 1920:
        checks.append(_item("file", "Plays as 1080x1920", "fail",
                            f"Came out {info.get('width')}x{info.get('height')}." if info else "Can't read the file."))
        return checks
    lo, hi = PLATFORM_LENGTH.get(platform, DEFAULT_LENGTH)
    where = {"youtube": "YouTube Shorts", "tiktok": "TikTok", "instagram": "Reels"}.get(platform, "short-form")
    checks.append(_item("length", f"Length for {where}", "pass" if lo <= secs <= hi else "warn",
                        f"{secs:.0f}s" + ("" if lo <= secs <= hi else
                                         f" — winning {where} clips mostly run {lo:.0f}-{hi:.0f}s.")))
    if not info.get("has_audio"):
        checks.append(_item("audio", "Has sound", "fail", "No audio track."))
    stats = _ffmpeg_stats(path)
    if info.get("has_audio") and stats["mean"] is not None:
        quiet = stats["mean"] < QUIET_DB
        checks.append(_item("loud", "Loud enough", "warn" if quiet else "pass",
                            f"Average {stats['mean']:.0f} dB" + (" — quiet; people will scroll past." if quiet else ".")))
    black = [(a, b) for a, b in stats["blacks"] if b - a >= 0.4]
    checks.append(_item("black", "No black screen", "warn" if black else "pass",
                        ", ".join(f"{a:.1f}-{b:.1f}s" for a, b in black[:3]) if black else ""))
    frozen = [d for d in stats["freezes"] if d >= 1.5]
    checks.append(_item("frozen", "Picture keeps moving", "warn" if frozen else "pass",
                        f"Frozen for {max(frozen):.1f}s." if frozen else ""))
    first = opening_text(edits)
    checks.append(_item("hook", "Text on the first frame", "pass" if first else "warn",
                        f"“{_short(first)}”" if first else "Nothing on screen at the start — the feed shows that frame."))
    spoken = [w for w in words if (w.get("w") or "").strip()]
    if spoken:
        head = re.sub(r"[^\w']", "", spoken[0]["w"].lower())
        checks.append(_item("start", "Starts on a fresh sentence", "warn" if head in LEAD_WORDS else "pass",
                            f"Opens on “{spoken[0]['w']}”." if head in LEAD_WORDS else ""))
        tail = spoken[-1]["w"]
        checks.append(_item("end", "Ends on a finished sentence",
                            "pass" if SENTENCE_END.search(tail) else "warn",
                            "" if SENTENCE_END.search(tail) else f"Last word “{tail}” — may stop mid-thought."))
    return checks


# --- Claude looks at it ------------------------------------------------------------

LOOK_TOOL = {
    "name": "review_clip",
    "description": "Your review of this finished clip.",
    "input_schema": {
        "type": "object",
        "properties": {
            "postable": {"type": "integer", "minimum": 1, "maximum": 10,
                         "description": "Would a top clip page post this as is? 10 = yes, instantly."},
            "issues": {"type": "array", "items": {"type": "object", "properties": {
                "problem": {"type": "string", "description": "What is wrong, in plain words, with the frame time."},
                "severity": {"type": "string", "enum": ["fix", "minor"]},
                "fix": {"type": "string", "enum": ["move_text", "rewrite_text", "captions_up", "captions_down",
                                                   "show_whole_frame", "fill_frame", "none"]},
                "text_y": {"type": "integer", "description": "move_text: new top edge of the TOP text (hook, label, "
                                                             "title or bubble), in px on the 1080x1920 frame."},
                "new_text": {"type": "string", "description": "rewrite_text: the corrected or shorter text."},
            }, "required": ["problem", "severity", "fix"]}},
            "best_thing": {"type": "string", "description": "One line: what works best in this clip."},
        },
        "required": ["postable", "issues"],
    },
}

LOOK_SYSTEM = """You check finished vertical clips (1080x1920) for a clip page before they are posted to \
TikTok, Reels and Shorts. You see frames in order with their times; the first is the frame the feed shows \
before the video plays. Look for what would cost views or look amateur:
- On-screen text covering a face's eyes or mouth, cut off at an edge, overlapping other text, or hard to read.
- Typos or wrong names in on-screen text (compare with what is said).
- The subject cut off or badly cropped (half a face, someone talking off-screen, an empty frame), \
or big black bars.
- A first frame that gives no reason to stop.
Only report real problems you can see. If it's good, say so with no issues. Fix options: move_text \
(the top text only — give text_y), rewrite_text (the top text, ONLY to fix a typo, a wrong name or fact, or \
text that is cut off — keep its style, voice and length; give new_text), captions_up / captions_down \
(the word captions sit on a face or too low), show_whole_frame (when cropping loses what matters), \
fill_frame (when there are bars or the subject is tiny), none. Use severity "fix" only for problems that \
would really cost views."""


def frames(path: Path, seconds: float, width: int = 360) -> List[Tuple[float, bytes]]:
    times = [0.0, 1.0, 2.6, seconds * 0.35, seconds * 0.6, seconds * 0.85, max(0.0, seconds - 0.4)]
    out = []
    for t in sorted({round(min(max(0.0, t), max(0.0, seconds - 0.05)), 2) for t in times}):
        proc = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                               "-vf", f"scale={width}:-2", "-q:v", "6", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                              capture_output=True)
        if proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8":
            out.append((t, proc.stdout))
    return out


def look(path: Path, edits: Dict[str, Any], words: List[Dict[str, Any]], seconds: float) -> Dict[str, Any]:
    shots = frames(path, seconds)
    if not shots:
        raise RuntimeError("no frames")
    said = re.sub(r"\s+", " ", " ".join(w["w"] for w in words))[:900]
    content: List[Dict[str, Any]] = []
    for t, jpg in shots:
        content.append({"type": "text", "text": f"t = {t:.1f}s"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(jpg).decode("ascii")}})
    on_screen = opening_text(edits)
    content.append({"type": "text", "text": f"Top text on screen: “{on_screen}”\nWhat is said: {said}"})
    client = highlights._client()
    message = client.messages.create(model=CLAUDE_MODEL, max_tokens=900, system=LOOK_SYSTEM,
                                     tools=[LOOK_TOOL], tool_choice={"type": "tool", "name": "review_clip"},
                                     messages=[{"role": "user", "content": content}])
    got = toolio.tool_inputs(message)
    if not got:
        raise RuntimeError("no review came back")
    review = got[0]
    issues = []
    for it in toolio.as_list(review.get("issues")):
        it = toolio.as_dict(it)
        if it.get("problem"):
            issues.append({"problem": highlights._text(it["problem"])[:200],
                           "severity": "fix" if it.get("severity") == "fix" else "minor",
                           "fix": it.get("fix") if it.get("fix") in ("move_text", "rewrite_text", "captions_up",
                                                                     "captions_down", "show_whole_frame",
                                                                     "fill_frame") else "none",
                           "text_y": it.get("text_y"), "new_text": highlights._text(it.get("new_text"))[:160]})
    return {"postable": highlights._int(review.get("postable"), 5), "issues": issues,
            "best": highlights._text(review.get("best_thing"))[:160]}


# --- fixing ------------------------------------------------------------------------------

def plan_fixes(checks: List[Dict[str, str]], review: Optional[Dict[str, Any]], edits: Dict[str, Any],
               clip: Dict[str, Any], all_words: List[Dict[str, Any]],
               may_trim: bool = True, may_crop: bool = True) -> Tuple[Dict[str, Any], List[str]]:
    """Edits that fix what was found, and a line for each fix."""
    change: Dict[str, Any] = {}
    notes: List[str] = []
    stitched = len(json.loads(clip.get("parts") or "[]")) > 1 \
        or len((edits.get("smart") or {}).get("parts") or []) > 1      # a teaser or a callback: no trims
    by_id = {c["id"]: c for c in checks}
    if may_trim and not stitched:
        start, end = float(clip["start"]), float(clip["end"])
        if by_id.get("start", {}).get("status") == "warn":
            inside = [w for w in all_words if start - 0.05 <= w["start"] < end]
            if len(inside) > 6:
                change["start"] = round(inside[1]["start"] - 0.08, 3)
                notes.append(f"Cut the opening “{inside[0]['w']}” so it starts on the point")
        if by_id.get("end", {}).get("status") == "warn":
            after = [w for w in all_words if end - 0.05 <= w["end"] <= end + 4.0]
            stop = next((w for w in after if SENTENCE_END.search(w["w"])), None)
            if stop and stop["end"] > end + 0.05:
                change["end"] = round(stop["end"] + 0.25, 3)
                notes.append(f"Ran on {stop['end'] - end:.1f}s to finish the sentence")
    for issue in (review or {}).get("issues", []):
        if issue["severity"] != "fix":
            continue
        if issue["fix"] == "move_text" and issue.get("text_y") is not None:
            y = int(max(190, min(1150, int(issue["text_y"]))))
            if edits.get("cards"):
                change["cards"] = [{**c, "y": y} for c in edits["cards"]]
            else:
                change["hook_y"] = y
            notes.append(f"Moved the top text to clear the face ({issue['problem'][:80]})")
        elif issue["fix"] == "rewrite_text" and issue.get("new_text"):
            if edits.get("cards"):
                change["cards"] = [{**c, "text": issue["new_text"]} for c in (change.get("cards") or edits["cards"])]
            else:
                change["hook"] = issue["new_text"]
            notes.append(f"Rewrote the on-screen text: “{_short(issue['new_text'])}”")
        elif issue["fix"] in ("captions_up", "captions_down") and edits.get("captions_on", True):
            ladder = ["bottom", "pop", "middle"]
            now = edits.get("caption_position", "bottom")
            i = ladder.index(now) if now in ladder else 0
            j = min(len(ladder) - 1, i + 1) if issue["fix"] == "captions_up" else max(0, i - 1)
            if j != i:
                change["caption_position"] = ladder[j]
                notes.append(f"Moved the captions {'up' if j > i else 'down'} ({issue['problem'][:70]})")
        elif issue["fix"] == "show_whole_frame" and may_crop and edits.get("layout") != "blur":
            change["layout"] = "blur"
            notes.append("Showed the whole frame — the crop was losing the subject")
        elif issue["fix"] == "fill_frame" and may_crop and edits.get("layout") != "fill":
            change["layout"] = "fill"
            notes.append("Filled the frame — the subject was too small")
    return change, notes


def report(checks: List[Dict[str, str]], review: Optional[Dict[str, Any]], fixed: List[str]) -> Dict[str, Any]:
    warns = [c for c in checks if c["status"] in ("warn", "fail")]
    left = [i for i in (review or {}).get("issues", []) if i["severity"] == "fix" and i["fix"] == "none"]
    minor = [i for i in (review or {}).get("issues", []) if i["severity"] == "minor"]
    if any(c["status"] == "fail" for c in checks) or left:
        status = "check"
    elif warns or minor:
        status = "fixed" if fixed else "check" if warns else "good"
    else:
        status = "fixed" if fixed else "good"
    bits = []
    if fixed:
        bits.append("Fixed: " + "; ".join(fixed))
    look_bits = [c["label"].lower() + (f" ({c['detail']})" if c["detail"] else "") for c in warns]
    look_bits += [i["problem"] for i in left + minor]
    if look_bits:
        bits.append("Look at: " + "; ".join(look_bits[:4]))
    if not bits:
        bits.append("Looks good — nothing to fix.")
    return {"status": status, "postable": (review or {}).get("postable"), "best": (review or {}).get("best", ""),
            "summary": " ".join(bits), "checks": checks, "issues": (review or {}).get("issues", []),
            "fixed": fixed}


def treat(clip_id: str, rules: Optional[Dict[str, Any]] = None, use_claude: bool = True) -> Optional[Dict[str, Any]]:
    """Examine one rendered clip, fix what can be fixed (one re-render), and
    store the report on the clip. Never raises."""
    try:
        from . import campaign, pipeline, transcribe
        clip = store.get_clip(clip_id)
        if not clip or clip.get("status") != "ready" or not clip.get("file"):
            return None
        job = store.get_job(clip["job_id"]) or {}
        settings = json.loads(job.get("settings") or "{}")
        post = json.loads(clip.get("post") or "{}") or {}
        platform = post.get("platform") or (settings.get("platforms") or [""])[0]
        edits = json.loads(clip.get("edits") or "{}")
        words = json.loads(clip.get("words") or "[]")
        all_words = transcribe.in_order(json.loads(job.get("transcript") or "{}").get("words") or [])
        path = Path(clip["file"])
        seconds = float(_probe(path).get("duration") or 0)
        checks = technical(path, edits, words, platform)
        review = None
        if use_claude:
            try:
                review = look(path, edits, words, seconds)
            except Exception as exc:
                checks.append(_item("look", "Claude looked at it", "warn", f"Couldn't: {str(exc)[:120]}"))
        may_trim = not rules or campaign.allowed(rules, "trim")
        may_crop = not rules or campaign.allowed(rules, "crop")
        change, notes = plan_fixes(checks, review, edits, clip, all_words, may_trim, may_crop)
        fixed: List[str] = []
        if change:
            try:
                pipeline.rerender_clip(clip_id, change)
                fixed = notes
                clip = store.get_clip(clip_id) or clip
                edits = json.loads(clip.get("edits") or "{}")
                words = json.loads(clip.get("words") or "[]")
                kept = [c for c in checks if c["id"] == "look"]
                checks = technical(Path(clip["file"]), edits, words, platform) + kept
                # What Claude flagged and we fixed is no longer open.
                if review:
                    review = {**review, "issues": [i for i in review["issues"]
                                                   if i["fix"] == "none" or i["severity"] != "fix"]}
            except Exception as exc:
                traceback.print_exc()
                checks.append(_item("fix", "Applied the fixes", "warn", f"Re-render failed: {str(exc)[:120]}"))
        result = report(checks, review, fixed)
        store.update_clip(clip_id, doctor=json.dumps(result))
        return result
    except Exception:
        traceback.print_exc()
        return None


def recheck(clip_id: str) -> Optional[Dict[str, Any]]:
    """After you re-render a clip yourself: measure it again (length, sound,
    picture, first-frame text, sentence edges) so the report matches the clip
    you now have. Claude's visual read was of the old version, so it is not
    carried over, and nothing is changed for you. Never raises."""
    try:
        clip = store.get_clip(clip_id)
        if not clip or clip.get("status") != "ready" or not clip.get("file") or not clip.get("doctor"):
            return None
        job = store.get_job(clip["job_id"]) or {}
        settings = json.loads(job.get("settings") or "{}")
        post = json.loads(clip.get("post") or "{}") or {}
        platform = post.get("platform") or (settings.get("platforms") or [""])[0]
        edits = json.loads(clip.get("edits") or "{}")
        words = json.loads(clip.get("words") or "[]")
        checks = technical(Path(clip["file"]), edits, words, platform)
        result = report(checks, None, [])
        result["rechecked"] = True
        result["checked_at"] = round(time.time(), 2)          # so the editor can tell the fresh report from the old one
        store.update_clip(clip_id, doctor=json.dumps(result))
        return result
    except Exception:
        traceback.print_exc()
        return None
