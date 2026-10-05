"""Tell ClipAgent what to change, in your own words.

"Clip 2: end it right after the punchline." "All of them: karaoke captions,
a bit bigger." "Change the top text on 4 to say he lost $2M."

Claude reads the request next to what each clip is now (its look, its text,
its caption settings, where it sits in the long video and what is said) and
answers with the same changes the editor makes. Those are checked against
what's possible here (and a campaign's brief), then each clip is re-made in
turn, measured again by the clip doctor, and the version before is kept so
one press undoes it. Anything the controls can't do is said plainly instead
of being faked.
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

from . import captions, highlights, render, store, styles, toolio, transcribe
from .config import CLAUDE_MODEL, DATA_DIR

UNDO_DIR = DATA_DIR / "undo"
_render_lock = threading.Lock()          # one request's renders at a time, whoever asked

LOOKS = ["wordpop", "label", "titlebar", "bubble", "stack", "classic"]
CARD_KIND = {"label": "label", "titlebar": "title", "bubble": "bubble"}
OVERLAY_COLORS = ["pink", "cyan", "green", "yellow", "purple", "orange"]


# --- which clip is which ------------------------------------------------------------

def label(clip: Dict[str, Any]) -> str:
    """The clip's number as people see it: "3", or "3B" for its second version."""
    return f"{clip.get('rank') or 0}{'B' if clip.get('alt_of') else ''}"


def clips_for(job_id: str, clip_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """The clips a request may touch: the ones picked, or every main clip."""
    clips = [c for c in store.list_clips(job_id) if c.get("status") != "failed" or c.get("file")]
    if clip_ids:
        picked = [c for c in clips if c["id"] in set(clip_ids)]
        if picked:
            return picked
    return [c for c in clips if not c.get("alt_of")]


# --- what Claude is shown -------------------------------------------------------------

def _loads(text: Any, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _seconds(clip: Dict[str, Any]) -> float:
    parts = _loads(clip.get("parts"), [])
    span = sum(p["end"] - p["start"] for p in parts) if len(parts) > 1 else clip["end"] - clip["start"]
    return max(0.0, span - float(clip.get("saved") or 0))


def _lines(segments: List[Dict[str, Any]], start: float, end: float, limit: int = 3000) -> str:
    """The transcript between two times, a line per sentence-ish segment with its start time."""
    out, size = [], 0
    for seg in segments:
        if seg.get("end", 0) <= start or seg.get("start", 0) >= end:
            continue
        line = f"[{seg['start']:.1f}] {(seg.get('text') or '').strip()}"
        size += len(line)
        if size > limit:
            out.append("[…]")
            break
        out.append(line)
    return "\n".join(out) or "(no speech)"


def describe(clip: Dict[str, Any], job: Dict[str, Any], segments: List[Dict[str, Any]],
             detail: bool = True) -> str:
    """One clip as Claude needs to see it to change it."""
    e = _loads(clip.get("edits"), {})
    parts = _loads(clip.get("parts"), [])
    style = e.get("style") or ""
    look = style if style in styles.RECIPES else ("clip-bank overlay" if clip.get("source_path") else "classic")
    cards = [c for c in e.get("cards") or [] if (c.get("text") or "").strip()]
    lines = [f"=== CLIP {label(clip)} — look: {look}, {_seconds(clip):.0f} s long"
             + (", a second version of clip " + str(clip.get("rank")) if clip.get("alt_of") else "")]
    if clip.get("source_path"):
        lines.append(f"Hook on screen: \"{clip.get('hook') or ''}\" — style {e.get('hook_style', 'neon')}, "
                     f"colour {e.get('hook_color', 'pink')}, position {e.get('hook_position', 'auto')}, "
                     f"held {e.get('hook_hold', 'whole')}, bars {e.get('bars', 'black')}")
        lines.append("(A campaign clip-bank edit: only the hook and its look can change.)")
        return "\n".join(lines)
    if cards:
        lines.append(f"Top text ({cards[0].get('kind')} card): \"{cards[0]['text']}\"")
    if e.get("hook_on", True) and e.get("hook"):
        lines.append(f"Hook (first seconds): \"{e['hook']}\"")
    if e.get("headline_on") and (e.get("headline") or clip.get("headline")):
        lines.append(f"Small headline at the top: \"{e.get('headline') or clip.get('headline')}\"")
    cap_look = e.get("caption_look") or {}
    lines.append(f"Captions: {'on' if e.get('captions_on', True) else 'off'}, style {e.get('caption_style', 'impact')}, "
                 f"position {e.get('caption_position', 'bottom')}, size {float(e.get('caption_size', 1.0)):.2f}"
                 + (f", {cap_look.get('max_words')} words at a time" if cap_look.get("max_words") else "")
                 + (f", highlight colour {e['accent']}" if e.get("accent") else ""))
    framing = _loads(clip.get("framing"), {})
    lines.append(f"Picture: layout {e.get('layout', 'auto')}"
                 + (f" ({framing.get('note')})" if framing.get("note") else "")
                 + f", camera moves {'on (' + e.get('motion_style', 'punchy') + ')' if e.get('motion', True) else 'off'}"
                 + f", pauses cut {'on' if e.get('tighten', True) else 'off'}")
    if e.get("spell"):
        lines.append("Word fixes already made: " + ", ".join(f"{k} → {v}" for k, v in e["spell"].items()))
    if clip.get("caption") or clip.get("hashtags"):
        tags = " ".join("#" + t for t in _loads(clip.get("hashtags"), []))
        lines.append(f"Post caption: \"{(clip.get('caption') or '')[:300]}\" {tags}".rstrip())
    doc = _loads(clip.get("doctor"), None)
    if doc and doc.get("status") == "check":
        lines.append(f"Clip doctor said: {doc.get('summary', '')[:300]}")
    if not detail:
        said = " ".join(w["w"] for w in _loads(clip.get("words"), []))
        lines.append(f"What is said: {said[:400]}{'…' if len(said) > 400 else ''}")
        return "\n".join(lines)
    if len(parts) > 1:
        lines.append("Stitched from these stretches of the long video (changing start/end makes it one "
                     "continuous stretch):")
        for p in parts:
            lines.append(f"  {p['start']:.1f}–{p['end']:.1f} ({p.get('role', '')})")
            lines.append(_lines(segments, p["start"], p["end"], 1500))
    else:
        lines.append(f"Runs {clip['start']:.1f} → {clip['end']:.1f} s in the long video. What is said:")
        lines.append(_lines(segments, clip["start"], clip["end"]))
    duration = float(job.get("duration") or 0) or clip["end"] + 30
    lines.append("Just before it:\n" + _lines(segments, max(0, clip["start"] - 25), clip["start"], 900))
    lines.append("Just after it:\n" + _lines(segments, clip["end"], min(duration, clip["end"] + 25), 900))
    return "\n".join(lines)


# --- asking Claude --------------------------------------------------------------------

def _tool() -> Dict[str, Any]:
    cap_styles = [s["id"] for s in captions.style_catalogue()]
    props: Dict[str, Any] = {
        "clip": {"type": "string", "description": "The clip's number as shown, e.g. \"2\" or \"3B\"."},
        "start": {"type": "number", "description": "New start, absolute seconds in the long video (from the "
                  "timestamps shown). Only when they asked to trim, extend or move the clip."},
        "end": {"type": "number", "description": "New end, absolute seconds in the long video."},
        "look": {"type": "string", "enum": LOOKS, "description": "Switch the clip's whole look. label, titlebar "
                 "and bubble need card_text; wordpop and classic need hook."},
        "hook": {"type": "string", "description": "New hook text shown in the first seconds (max 8 words). For a "
                 "clip-bank clip: its on-screen hook."},
        "hook_on": {"type": "boolean"},
        "card_text": {"type": "string", "description": "New top text for a label / titlebar / bubble card. "
                      "Titlebar: wrap the one word to show in yellow in [brackets]."},
        "card_on": {"type": "boolean", "description": "false removes the top card."},
        "headline": {"type": "string"},
        "headline_on": {"type": "boolean"},
        "captions_on": {"type": "boolean"},
        "caption_style": {"type": "string", "enum": cap_styles},
        "caption_position": {"type": "string", "enum": list(captions.POSITIONS.keys()),
                             "description": "pop = just below the middle."},
        "caption_size": {"type": "number", "description": "1.0 is normal; 0.7 to 1.5. 'Bigger' ≈ +0.2."},
        "words_per_caption": {"type": "integer", "description": "1-4 words shown at a time (word-pop looks)."},
        "highlight_color": {"type": "string", "description": "#RRGGBB for the highlighted caption word."},
        "censor": {"type": "boolean", "description": "Star out swear words in captions."},
        "layout": {"type": "string", "enum": list(render.LAYOUTS.keys())},
        "crop_x": {"type": "number", "description": "Where the crop sits across the wide video: 0 left, 0.5 "
                   "centre, 1 right. Only when they ask to move the framing."},
        "motion": {"type": "boolean", "description": "Camera moves (punch-ins, zooms) on or off."},
        "motion_style": {"type": "string", "enum": ["punchy", "calm"]},
        "cut_pauses": {"type": "boolean", "description": "Cut the dead air out of the middle."},
        "fix_words": {"type": "array", "description": "Captions that are misheard: what it says now → what was said.",
                      "items": {"type": "object", "properties": {"wrong": {"type": "string"},
                                                                 "right": {"type": "string"}},
                                "required": ["wrong", "right"]}},
        "post_caption": {"type": "string", "description": "New caption to post with the clip."},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "hook_style": {"type": "string", "enum": ["bold", "boxed", "neon"], "description": "Clip-bank clips only."},
        "hook_color": {"type": "string", "enum": OVERLAY_COLORS, "description": "Clip-bank clips only."},
        "hook_position": {"type": "string", "enum": ["auto", "top", "center", "bottom"],
                          "description": "Clip-bank clips only."},
        "hook_hold": {"type": "string", "enum": ["whole", "1", "3"], "description": "Clip-bank clips only: "
                      "hook on screen the whole clip, or the first 1 / 3 seconds."},
        "bars": {"type": "string", "enum": ["black", "blur"], "description": "Clip-bank clips only."},
        "note": {"type": "string", "description": "A few plain words on what changes on this clip."},
    }
    return {
        "name": "change_clips",
        "description": "Turn the person's request into changes to their clips.",
        "input_schema": {
            "type": "object",
            "properties": {
                "understood": {"type": "string", "description": "1-2 friendly, plain sentences to them: what you "
                               "will change on which clip numbers. No jargon, no field names."},
                "question": {"type": "string", "description": "Only when you truly can't tell what they want: one "
                             "short question. Then make no changes."},
                "cant": {"type": "array", "items": {"type": "string"}, "description": "Each thing asked that "
                         "ClipAgent can't do, in plain words, with the closest thing it can do."},
                "changes": {"type": "array", "items": {"type": "object", "properties": props,
                                                       "required": ["clip"]}},
            },
            "required": ["understood", "changes"],
        },
    }


SYSTEM = """You are ClipAgent's editor. The person who made these short clips (for TikTok, YouTube Shorts \
and Reels) tells you in their own words what to change. Turn that into changes using only the controls \
in the change_clips tool, then tell them in plain words what you'll do.

Rules:
- Change only what they asked for. Leave everything else exactly as it is — don't "improve" other things.
- Which clips: the ones they name — by number ("clip 2", "#3", "the first two"), or by what happens in \
them ("the one where he gets the call"). "All", "every clip" or "them" → every clip below. FOCUS, when \
given, is the clip they have open: "this", "it", or no clip named means only that one. With no FOCUS and \
no clip named → every clip below.
- Trims use the timestamps shown (absolute seconds in the long video). Start on the first word of a \
sentence and end right after the last word of one — never mid-sentence. "End it after X" → end right after \
the line where X is said. "Shorter"/"tighter" with no number → drop about a third, keeping the setup that's \
needed and the payoff. "Longer" → take in the lines just before or after that the moment needs.
- Words on screen: only facts from the clip's own words or the video title; never invent names, numbers or \
events. Specific beats vague; never a teaser that hides the thing. Keep their wording when they give it.
- Looks: wordpop (big 1-3 word captions + hook), label (white headline box, no captions), titlebar (dark \
band headline + captions), bubble (viewer-comment bubble + captions), stack (two people, one per panel), \
classic (plain captions + hook). Switching to label/titlebar/bubble needs card_text; to wordpop or classic \
needs a hook. A look the clip can't take is listed under the clip.
- Things the controls can't do (add music or sound effects, B-roll, stickers, emojis flying in, \
transitions, change voices, translate the speech, make a brand-new clip, post it for them) go in cant, in \
plain words, with the closest thing you can do. Never pretend.
- If you can't tell what they want, ask one short question and make no changes.
- Campaign rules, when given, override everything: never change something the brief forbids — say so in cant.
"""


def interpret(job: Dict[str, Any], clips: List[Dict[str, Any]], text: str,
              focus: Optional[Dict[str, Any]] = None, guidance: str = "",
              looks_ok: Optional[List[str]] = None) -> Dict[str, Any]:
    """Claude's reading of the request: {understood, question, cant, changes}."""
    transcript = _loads(job.get("transcript"), {})
    segments = transcript.get("segments") or []
    detail = len(clips) <= 14
    blocks = [describe(c, job, segments, detail) for c in clips]
    looks = ", ".join(looks_ok or LOOKS)
    prompt = (f"Video: {job.get('title') or 'Untitled'}\n"
              + (f"FOCUS: clip {label(focus)}\n" if focus else "")
              + f"Looks these clips can take: {looks}\n\n"
              + "\n\n".join(blocks)
              + f"\n\nTHEIR REQUEST:\n{text.strip()}")
    system = SYSTEM + (f"\n\nCAMPAIGN RULES:\n{guidance}" if guidance else "")
    client = highlights._client()
    message = client.messages.create(model=CLAUDE_MODEL, max_tokens=4000, system=system,
                                     tools=[_tool()], tool_choice={"type": "tool", "name": "change_clips"},
                                     messages=[{"role": "user", "content": prompt}])
    got = toolio.tool_inputs(message)
    if not got:
        raise RuntimeError("Claude's answer couldn't be read — try saying it another way")
    reply = got[0]
    cant = toolio.coerce(reply.get("cant"))
    if isinstance(cant, str):
        cant = [cant]
    changes = toolio.coerce(reply.get("changes"))
    if isinstance(changes, dict):
        changes = [changes]
    return {
        "understood": highlights._text(reply.get("understood"))[:600],
        "question": highlights._text(reply.get("question"))[:300],
        "cant": [str(c).strip()[:300] for c in (cant if isinstance(cant, list) else []) if str(c).strip()][:6],
        "changes": [toolio.as_dict(c) for c in (changes if isinstance(changes, list) else []) if toolio.as_dict(c)],
    }


# --- turning an answer into edits --------------------------------------------------------

def _hex(value: Any) -> str:
    v = str(value or "").strip()
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", v):
        return v if v.startswith("#") else "#" + v
    return ""


def _bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if str(value).lower() in ("true", "yes", "on", "1"):
        return True
    if str(value).lower() in ("false", "no", "off", "0"):
        return False
    return None


def plan_clip(clip: Dict[str, Any], change: Dict[str, Any], job: Dict[str, Any],
              words: List[Dict[str, Any]], looks_ok: List[str]) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """(edits to re-render with, post-text fields to save, problems) for one clip."""
    e = _loads(clip.get("edits"), {})
    edits: Dict[str, Any] = {}
    text: Dict[str, Any] = {}
    problems: List[str] = []

    if clip.get("source_path"):                         # a clip-bank clip: its hook and the look of it
        if highlights._text(change.get("hook")):
            edits["hook"] = highlights._text(change["hook"])[:120]
        for key, ok in (("hook_style", ("bold", "boxed", "neon")), ("hook_color", OVERLAY_COLORS),
                        ("hook_position", ("auto", "top", "center", "bottom")), ("hook_hold", ("whole", "1", "3")),
                        ("bars", ("black", "blur"))):
            if str(change.get(key) or "") in ok:
                edits[key] = str(change[key])
        if any(change.get(k) is not None for k in ("start", "end", "look", "caption_style", "layout")):
            problems.append("a clip-bank edit can only change its hook and how the hook looks")
    else:
        # where it starts and ends
        if change.get("start") is not None or change.get("end") is not None:
            duration = float(job.get("duration") or 0) or 1e9
            try:
                s = float(change.get("start", clip["start"]))
                t = float(change.get("end", clip["end"]))
            except (TypeError, ValueError):
                s, t = clip["start"], clip["end"]
            s, t = max(0.0, s), min(duration, t)
            if t - s < 3:
                problems.append("that trim would leave less than 3 seconds, so the length stays")
            else:
                s, t = highlights.clean_bounds(s, t, words, min_len=3.0)
                if abs(s - clip["start"]) > 0.05 or abs(t - clip["end"]) > 0.05:
                    edits["start"], edits["end"] = round(s, 2), round(t, 2)

        # the look
        look = str(change.get("look") or "")
        current_look = e.get("style") if e.get("style") in styles.RECIPES else "classic"
        hook = highlights._text(change.get("hook"))
        card_text = highlights._text(change.get("card_text"))
        if look in LOOKS and look != current_look:
            if look not in looks_ok:
                problems.append(f"the {look} look doesn't fit this video" if look == "stack"
                                else f"the {look} look isn't allowed here")
            elif look == "classic":
                edits.update({"style": "", "cards": [], "captions_on": True, "hook_on": True,
                              "caption_look": {}, "caption_position": "bottom",
                              "hook": hook or styles.on_screen_text(e) or clip.get("hook") or ""})
            else:
                need = CARD_KIND.get(look)
                plan = {"recipe": look, "colors": [{"text": k, "meaning": v}
                                                   for k, v in ((e.get("caption_look") or {}).get("colors") or {}).items()]}
                if look in ("wordpop", "stack"):
                    plan["hook"] = hook or e.get("hook") or styles.on_screen_text(e) or clip.get("hook") or ""
                if need:
                    plan[need] = card_text or styles.on_screen_text(e) or clip.get("hook") or ""
                edits.update(styles.apply(plan, clip))
                if look != "label":                       # the label look is the only one without captions
                    edits["captions_on"] = True
                if look in ("wordpop", "stack"):
                    edits["hook_on"] = True
                edits["style_why"] = "you asked for it"
        else:
            if hook:
                edits["hook"] = hook[:120]
                if e.get("cards") and not card_text and current_look in CARD_KIND:
                    # on a card look the top text is what people call the hook
                    edits["cards"] = [{**(e["cards"][0]), "text": hook[:200]}]
            if card_text:
                if e.get("cards"):
                    edits["cards"] = [{**(e["cards"][0]), "text": card_text[:200]}]
                elif current_look in ("wordpop", "classic"):
                    edits["hook"] = card_text[:120]
            on = _bool(change.get("card_on"))
            if on is False and e.get("cards"):
                edits["cards"] = []
        for key in ("hook_on", "headline_on", "captions_on", "motion", "censor"):
            v = _bool(change.get(key))
            if v is None:
                continue
            if key == "censor":
                edits["caption_look"] = {**(edits.get("caption_look") or e.get("caption_look") or {}), "censor": v}
            else:
                edits[key] = v
        if highlights._text(change.get("headline")):
            edits["headline"] = highlights._text(change["headline"])[:80]
            edits.setdefault("headline_on", True)
        v = _bool(change.get("cut_pauses"))
        if v is not None:
            edits["tighten"] = v
        cap_ids = {s["id"] for s in captions.style_catalogue()}
        if change.get("caption_style") in cap_ids:
            edits["caption_style"] = change["caption_style"]
        if change.get("caption_position") in captions.POSITIONS:
            edits["caption_position"] = change["caption_position"]
        if change.get("caption_size") is not None:
            try:
                edits["caption_size"] = round(min(1.5, max(0.7, float(change["caption_size"]))), 2)
            except (TypeError, ValueError):
                pass
        if change.get("words_per_caption") is not None:
            try:
                n = int(change["words_per_caption"])
                edits["caption_look"] = {**(edits.get("caption_look") or e.get("caption_look") or {}),
                                         "max_words": min(4, max(1, n))}
            except (TypeError, ValueError):
                pass
        accent = _hex(change.get("highlight_color"))
        if accent:
            edits["accent"] = accent
        if change.get("layout") in render.LAYOUTS:
            if change["layout"] == "stack" and "stack" not in looks_ok:
                problems.append("the two-person split doesn't fit this video")
            else:
                edits["layout"] = change["layout"]
        if change.get("crop_x") is not None:
            try:
                edits["crop_x"] = round(min(1.0, max(0.0, float(change["crop_x"]))), 2)
                edits["crop_auto"] = False
            except (TypeError, ValueError):
                pass
        if change.get("motion_style") in ("punchy", "calm"):
            edits["motion_style"] = change["motion_style"]
        fixes = {}
        for f in toolio.as_list(change.get("fix_words")):
            f = toolio.as_dict(f)
            if highlights._text(f.get("wrong")) and highlights._text(f.get("right")):
                fixes[highlights._text(f["wrong"])[:60]] = highlights._text(f["right"])[:60]
        if fixes:
            edits["spell"] = {**(e.get("spell") or {}), **fixes}

    # what gets posted with it — no re-render needed
    if highlights._text(change.get("post_caption")):
        text["caption"] = highlights._text(change["post_caption"])[:2200]
    if toolio.as_list(change.get("hashtags")):
        text["hashtags"] = [str(t).strip().lstrip("#") for t in toolio.as_list(change["hashtags"])
                            if str(t).strip().lstrip("#")][:30]
    # only real differences count
    edits = {k: v for k, v in edits.items() if k in ("start", "end") or e.get(k) != v}
    return edits, text, problems


def sync_hook(clip_id: str) -> None:
    """The clip's headline text (what cards, Telegram and the spreadsheet show)
    follows what is actually on screen after a re-make."""
    clip = store.get_clip(clip_id)
    if not clip or clip.get("source_path"):
        return
    shown = styles.on_screen_text(_loads(clip.get("edits"), {}))
    if shown and shown != clip.get("hook"):
        store.update_clip(clip_id, hook=shown)


# --- undo ------------------------------------------------------------------------------

SNAP_FIELDS = ("start", "end", "edits", "words", "hook", "headline", "parts", "variant", "file", "thumb",
               "saved", "framing", "doctor", "compliance", "caption", "hashtags")


def snapshot(clip_id: str) -> bool:
    """Keep the clip as it is now, so the next change can be undone. Never raises."""
    try:
        clip = store.get_clip(clip_id)
        if not clip or not clip.get("file") or not Path(clip["file"]).is_file():
            return False
        folder = UNDO_DIR / clip_id
        folder.mkdir(parents=True, exist_ok=True)
        files = {}
        for key, name in (("file", "clip.mp4"), ("thumb", "thumb.jpg")):
            src = Path(clip.get(key) or "")
            if clip.get(key) and src.is_file():
                shutil.copy2(src, folder / name)
                files[key] = str(folder / name)
        if clip.get("thumb"):
            clean = Path(clip["thumb"]).with_name(f"{clip_id}_clean.jpg")
            if clean.is_file():
                shutil.copy2(clean, folder / "clean.jpg")
                files["clean"] = str(folder / "clean.jpg")
        state = {k: clip.get(k) for k in SNAP_FIELDS}
        store.update_clip(clip_id, undo=json.dumps({"state": state, "files": files, "at": time.time()}))
        return True
    except Exception:
        traceback.print_exc()
        return False


def undo(clip_id: str) -> Dict[str, Any]:
    """Put back the version from before the last change."""
    clip = store.get_clip(clip_id)
    snap = _loads((clip or {}).get("undo"), None)
    if not clip or not snap:
        raise RuntimeError("There's nothing to undo on this clip")
    if clip.get("status") == "rendering":
        raise RuntimeError("Wait until this clip has finished re-making, then undo")
    state, files = snap["state"], snap.get("files") or {}
    for key, target in (("file", state.get("file")), ("thumb", state.get("thumb"))):
        if files.get(key) and target and Path(files[key]).is_file():
            shutil.copy2(files[key], target)
    if files.get("clean") and state.get("thumb") and Path(files["clean"]).is_file():
        shutil.copy2(files["clean"], Path(state["thumb"]).with_name(f"{clip_id}_clean.jpg"))
    store.update_clip(clip_id, **{k: state.get(k) for k in SNAP_FIELDS}, undo="", status="ready", render_error="")
    shutil.rmtree(UNDO_DIR / clip_id, ignore_errors=True)
    return store.get_clip(clip_id)


# --- the whole request -----------------------------------------------------------------

def _looks_ok(job: Dict[str, Any], rules: Optional[Dict[str, Any]]) -> List[str]:
    two = float(_loads(job.get("framing"), {}).get("two_shot") or 0)
    ok = styles.campaign_recipes(rules, two) if rules else styles.available_recipes(two)
    return list(ok) + ["classic"]


def _match(changes: List[Dict[str, Any]], clips: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    by_label = {label(c).upper(): c for c in clips}
    out, seen = [], set()
    for ch in changes:
        key = re.sub(r"[^0-9A-Za-z]", "", str(ch.get("clip") or "")).upper()
        clip = by_label.get(key)
        if clip and clip["id"] not in seen:
            seen.add(clip["id"])
            out.append((clip, ch))
    return out


def request(job_id: str, text: str, clip_ids: Optional[List[str]] = None, focus_id: str = "",
            source: str = "web", background: bool = True) -> Dict[str, Any]:
    """Read a typed request, start re-making the clips it changes, and answer
    straight away with what ClipAgent understood."""
    from . import pipeline, campaign
    text = (text or "").strip()
    if not text:
        raise ValueError("Type what you'd like changed")
    job = store.get_job(job_id)
    if not job:
        raise ValueError("Video not found")
    if job.get("status") in ("running", "queued"):
        raise ValueError("Wait until this video's clips are made, then ask for changes")
    focus = store.get_clip(focus_id) if focus_id else None
    clips = clips_for(job_id, clip_ids)          # with a FOCUS, Claude still sees the rest for "all of them"
    if focus and focus["id"] not in {c["id"] for c in clips}:
        clips.append(focus)
    if not clips:
        raise ValueError("This video has no clips to change")
    rules = pipeline.job_rules(job)
    looks_ok = _looks_ok(job, rules)
    req_id = store.create_request(job_id, text, source)
    try:
        answer = interpret(job, clips, text, focus, campaign.picker_guidance(rules) if rules else "", looks_ok)
    except RuntimeError as exc:
        store.update_request(req_id, status="failed", reply={"understood": "", "error": str(exc)})
        raise ValueError(f"Couldn't read the request — {exc}") from exc
    except Exception as exc:
        traceback.print_exc()
        store.update_request(req_id, status="failed", reply={"understood": "", "error": str(exc)[:300]})
        low = str(exc).lower()
        if "401" in low or "api-key" in low or "api key" in low:
            raise ValueError("The Claude key was refused — check ANTHROPIC_API_KEY in the .env file") from exc
        raise ValueError("Claude couldn't be reached to read the request — check the internet and try again") from exc

    words = transcribe.in_order(_loads(job.get("transcript"), {}).get("words") or [])
    work, results = [], []
    for clip, change in _match(answer["changes"], clips):
        busy = clip.get("status") == "rendering"
        edits, post_text, problems = plan_clip(clip, change, job, words, looks_ok)
        if busy and edits:
            problems.append("it's being re-made right now — ask again once it's done")
            edits = {}
        if post_text:
            store.update_clip(clip["id"], **{k: (json.dumps(v) if k == "hashtags" else v) for k, v in post_text.items()})
        note = highlights._text(change.get("note"))[:200]
        results.append({"id": clip["id"], "label": label(clip), "note": note, "problems": problems,
                        "remake": bool(edits), "text_saved": bool(post_text), "status": "waiting" if edits else "done"})
        if edits:
            work.append((clip["id"], edits))
    reply = {"understood": answer["understood"], "question": answer["question"], "cant": answer["cant"],
             "clips": results}
    if not work:
        if not results and not answer["question"] and not answer["cant"]:
            reply["understood"] = reply["understood"] or "I couldn't match that to anything I can change."
        store.update_request(req_id, status="done", reply=reply)
        return {"id": req_id, **reply, "status": "done"}
    for clip_id, _ in work:
        store.update_clip(clip_id, status="rendering", render_error="")
    store.update_request(req_id, status="working", reply=reply)
    if background:
        threading.Thread(target=_remake, args=(req_id, work, source), daemon=True).start()
    else:
        _remake(req_id, work, source)
    return {"id": req_id, **(store.get_request(req_id) or {}).get("reply", reply),
            "status": (store.get_request(req_id) or {}).get("status", "working")}


def _remake(req_id: str, work: List[Tuple[str, Dict[str, Any]]], source: str) -> None:
    """Re-make each changed clip in turn; keep the old one if a re-make fails."""
    from . import doctor, notify, pipeline
    with _render_lock:
        for clip_id, edits in work:
            req = store.get_request(req_id) or {}
            reply = req.get("reply") or {}
            item = next((r for r in reply.get("clips", []) if r["id"] == clip_id), None)
            if item:
                item["status"] = "making"
                store.update_request(req_id, reply=reply)
            before = store.get_clip(clip_id) or {}
            had = snapshot(clip_id)
            try:
                pipeline.rerender_clip(clip_id, edits)
                sync_hook(clip_id)
                doctor.recheck(clip_id)
                ok, err = True, ""
            except Exception as exc:
                traceback.print_exc()
                ok, err = False, str(exc)[:300] or "The re-make failed"
                still = before.get("file") and Path(before["file"]).exists()
                store.update_clip(clip_id, status="ready" if still else "failed", render_error=err,
                                  **({"undo": ""} if had else {}))
            reply = (store.get_request(req_id) or {}).get("reply") or {}
            item = next((r for r in reply.get("clips", []) if r["id"] == clip_id), None)
            if item:
                item["status"] = "done" if ok else "failed"
                if err:
                    item["problems"] = (item.get("problems") or []) + [err]
            store.update_request(req_id, reply=reply)
            if ok and source == "telegram":
                clip = store.get_clip(clip_id) or {}
                if clip.get("file"):
                    notify.send_video(Path(clip["file"]), notify.clip_caption(clip, clip.get("rank") or 0),
                                      notify._clip_seconds(clip))
    store.update_request(req_id, status="done")
