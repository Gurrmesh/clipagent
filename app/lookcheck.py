"""The campaign look: what a brief's picture-and-words rules need, checked on each finished clip.

Three things went wrong on real TJR campaign clips, and code alone can't see them:
* the wrong person credited — his friend Timmy's words put in TJR's mouth;
* a "use code …" sponsor banner burned into the stream, in a brief that bans logos;
* an offensive word or AI-made footage the brief would reject.

Per campaign clip this makes ONE Claude call with frames of the finished clip
(people boxed and numbered by identity.py), the words said with their times, the
video's title, the creator's name and what the face check found — and asks who
is who (from context only, never from faces), who says the hook, what logos,
banners, promo codes or AI visuals show, and what in the words is a slur, an
offensive joke, a spoken promo or an AI mention. Next to it run checks that need
no Claude: who is on screen and talking (identity.py), a hashed list of the
commonest slurs, and phrase lists for spoken promos and AI mentions.

What comes out is the clip's "look" (kept in its edits as campaign_look);
compliance.py turns it into Ready / Check first / Blocked lines. Where a fix is
safe it is made in the one re-render the clip gets: a hook that credits the
creator with someone else's words is rewritten, and a short offensive stretch
away from the hook and the punchline is cut out. Anything that couldn't be
checked says so — it never counts as a pass.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import traceback
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import identity, toolio
from .config import ANTHROPIC_API_KEY, CLAUDE_MODEL

HOOK_SECONDS = 1.5        # an offensive word inside the opening can't be cut: the hook is lost
MAX_CUT = 4.0             # longer than this and cutting it out breaks the clip
MAX_CUT_SHARE = 0.25      # never cut more than this share of a clip
UNCHECKED_OK = 5.0        # seconds a later trim may add before the look must be redone
OVERVIEW_W = 432          # the boxed overview frames Claude sees
OVERVIEW_N = 8
DETAIL_N = 2              # full-size frames, for small banners and promo codes

_busy = threading.local()


# --- the brief's rules this needs (read defensively: old rulebooks lack them) -------------

_NEGATIVE = re.compile(r"negativ|bad light|disparag|mock|make fun|portray|insult|unflattering", re.I)


def rules_of(rb: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    rb = rb or {}

    def obj(key: str) -> Dict[str, Any]:
        v = rb.get(key)
        return v if isinstance(v, dict) else {}

    focus = str(obj("primary_focus").get("name") or "").strip()
    creator = str(rb.get("creator") or rb.get("brand") or "").strip()
    brand_logo = obj("brand_logo").get("value") or "unstated"
    negative = next((t for t in list(rb.get("tone_avoid") or []) + list(rb.get("other_rules") or [])
                     if isinstance(t, str) and _NEGATIVE.search(t)), "")
    try:
        from . import campaign
        cut_ok = campaign.allowed(rb, "cut") if rb.get("perms") or rb.get("overrides") else True
        hook_ok = campaign.allowed(rb, "hook") if rb.get("perms") or rb.get("overrides") else True
    except Exception:
        cut_ok = hook_ok = True
    return {
        "campaign": str(rb.get("name") or "").strip() or "this campaign",
        "creator": focus or creator,
        "focus": focus,
        "focus_quote": str(obj("primary_focus").get("quote") or ""),
        "no_logos": obj("no_logos").get("value") == "yes" or brand_logo == "forbidden",
        "no_logos_quote": str(obj("no_logos").get("quote") or ""),
        "no_ai": obj("no_ai").get("value") == "yes",
        "no_ai_quote": str(obj("no_ai").get("quote") or ""),
        "brand_logo": brand_logo,
        "negative": negative,
        "mode": rb.get("mode") or "source",
        "cut_ok": cut_ok,
        "hook_ok": hook_ok,
        "min_len": (obj("length").get("min") or None),
    }


def mmss(t: float) -> str:
    t = max(0.0, float(t or 0))
    return f"{int(t // 60)}:{int(t % 60):02d}"


# --- the words -------------------------------------------------------------------------------

def norm_words(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Words on the clip's clock as {"w", "start", "end"} (an edit's words carry "t")."""
    out = []
    for w in words or []:
        try:
            a = float(w["start"] if "start" in w else w["t"])
            b = float(w.get("end", a + 0.2))
        except (KeyError, TypeError, ValueError):
            continue
        if (w.get("w") or "").strip():
            out.append({"w": str(w["w"]), "start": round(a, 3), "end": round(max(b, a + 0.05), 3)})
    out.sort(key=lambda w: w["start"])
    return out


# The most common slurs, as SHA-256 of the lower-case word, so the repo never spells
# them out. A fallback only: Claude reads the words for everything else.
_SLURS = {
    "120f6e5b4ea32f65bda68452fcfaaef06b0136e1d0e4a6f60bc3771fa0936dd6",
    "08a841e996781e9e77d30a4e4420a8f501a280b00624e6d1224bf54aaff73eba",
    "8f5083e3e5c7dc8932f2bf58212f963f3a44752618c96297f82623f736c52738",
    "158869a97379229b7681efae9d7f9c9214134e836d649ba53477c0c111414d59",
    "bd331fb1d24298f52943034a243a341877957b895f4372b11babb87262904ed6",
    "c3de533e9b7fe63b79f648687a30d2861edd92fe7c3cd1f2c485e0a605367624",
    "f9d0d9b18ae9033a5ea36df19bf279b059e887a9ae785db81117bceaecc95933",
    "98b52c4b6b7d1f48e7477a5ccc10955dd195d0ac5a38c8281bfeb08762634909",
    "eef3bd091670c3447022d619c06ad15de96da72b5a66f28bb8b75d1b1c12a05f",
    "cc02032349c833ac5e97bac094560ed40e09acf34cb1978ab7a9840b9bf15b4d",
    "16ea09fc78ca83ca502cbcf2377acdf280bf18f61e259153f0868405eedab5ef",
    "886d51e97ad7931d0d2af8439ca6d9e4887e3c2b469ed247cbd68ceb3649ccde",
    "12e6274e4309293e2d480272b49a6c7c73a6a6b22678ba226b533c67006c17d1",
    "22fc75e65a0e9d34324092a7c6a8dba961853294abca4e5914e60c550f48e0c2",
    "333f7618092958c75b8c5af6f1ec77b42803922a0fc6ff1570a8af3a3aab3b4a",
    "3b1e0d7c5dd45583867e897943e37a940a7e7321022317dd3deea01963ee365e",
    "dc675e448132fd2a4fed47c1736784e83fe01e8cf137dcf97cca9fc7e337e8b4",
}
EXTRA_SLURS: set = set()          # hashes added at run time (the tests use a made-up word)
_LEET = str.maketrans("013457@$", "oieastas")

PROMO = re.compile(r"\b(use (?:my |the |our )?code|promo ?code|discount code|coupon code|code \w+ at checkout|"
                   r"link (?:is )?in (?:my |the )?bio|sponsored by|(?:today'?s|this video'?s) sponsor|"
                   r"this video is sponsored|\d+ ?(?:%|percent) off)\b", re.I)
AI_SPOKEN = re.compile(r"\b(ai[- ]generated|a\.i\.? generated|made (?:it )?(?:with|by) ai|(?:this|that|it)(?:'s| is) ai|"
                       r"ai (?:video|footage|clip|image)s?|sora|veo|deep ?fakes?|midjourney)\b", re.I)


def _token(word: str) -> str:
    return re.sub(r"[^a-z]", "", (word or "").lower().translate(_LEET))


def is_slur(word: str) -> bool:
    t = _token(word)
    if len(t) < 3:
        return False
    for cand in {t, t[:-1] if t.endswith(("s", "z")) else t, t[:-2] if t.endswith("es") else t}:
        if hashlib.sha256(cand.encode()).hexdigest() in (_SLURS | EXTRA_SLURS):
            return True
    return False


def mask(text: str) -> str:
    """Offensive words starred out before anything shows them: 'n*****', 'f*ck'."""
    from .captions import censor
    out = []
    for tok in (text or "").split():
        core = re.sub(r"[^A-Za-z]", "", tok)
        out.append(core[:1] + "*" * (len(core) - 1) if core and is_slur(core) else censor(tok))
    return " ".join(out)


def _phrase_hits(words: List[Dict[str, Any]], pattern: re.Pattern) -> List[Dict[str, Any]]:
    """Where a phrase is said: [{"quote", "at", "i0", "i1"}], by word."""
    text, starts = "", []
    for w in words:
        starts.append(len(text))
        text += w["w"] + " "
    hits = []
    for m in pattern.finditer(text):
        i0 = max(i for i, s in enumerate(starts) if s <= m.start())
        i1 = max(i for i, s in enumerate(starts) if s < m.end())
        quote = " ".join(w["w"] for w in words[max(0, i0 - 2):i1 + 4])
        hits.append({"quote": quote[:120], "at": words[i0]["start"], "i0": i0, "i1": i1})
    return hits


def transcript_flags(words: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """What the words alone show, without Claude."""
    slurs = [{"quote": mask(w["w"]), "from": w["start"], "to": w["end"], "i0": i, "i1": i, "kind": "slur",
              "said_by": "unclear", "punchline": False, "exact": True, "by": "list"}
             for i, w in enumerate(words) if is_slur(w["w"])]
    return {"slurs": slurs, "promos": _phrase_hits(words, PROMO), "ai": _phrase_hits(words, AI_SPOKEN)}


# --- the frames Claude sees ---------------------------------------------------------------------

_COLOURS = [(0, 215, 255), (255, 120, 40), (80, 220, 80), (230, 80, 230), (60, 60, 255), (255, 255, 0)]


def _times(seconds: float) -> Tuple[List[float], List[float]]:
    from . import doctor
    base = doctor.frame_times(seconds)
    extra = [seconds * 0.15, seconds * 0.5, seconds * 0.72]
    picked: List[float] = []
    for t in sorted(base + [round(min(max(0.0, x), max(0.0, seconds - 0.05)), 2) for x in extra]):
        if not picked or t - picked[-1] >= 0.6:
            picked.append(t)
    if len(picked) > OVERVIEW_N:                       # keep the opening, spread the rest
        rest = picked[2:]
        step = len(rest) / (OVERVIEW_N - 2)
        picked = picked[:2] + [rest[int(i * step)] for i in range(OVERVIEW_N - 2)]
    detail = [round(seconds * 0.3, 2), round(seconds * 0.7, 2)][:DETAIL_N] if seconds > 2 else []
    return picked, detail


def _boxes_at(scan: Dict[str, Any], t: float) -> List[Tuple[int, List[float]]]:
    if not scan.get("ok"):
        return []
    k = int(round(t * float(scan.get("fps") or identity.SCAN_FPS)))
    out = []
    for p in scan.get("people") or []:
        boxes = p.get("boxes") or {}
        box = boxes.get(k) or boxes.get(k - 1) or boxes.get(k + 1)
        if box:
            out.append((p["id"], box))
    return out


def draw_people(img: Any, boxes: List[Tuple[int, List[float]]]) -> Any:
    """Numbered boxes round each person, as Claude is told about them."""
    import cv2
    out = img.copy()
    h, w = out.shape[:2]
    thick = max(2, w // 220)
    for pid, (x, y, bw, bh) in boxes:
        colour = _COLOURS[(pid - 1) % len(_COLOURS)]
        x0, y0 = int(x * w), int(y * h)
        x1, y1 = int((x + bw) * w), int((y + bh) * h)
        cv2.rectangle(out, (x0, y0), (x1, y1), colour, thick)
        label = str(pid)
        scale = max(0.6, w / 900)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
        ty = max(th + 6, y0)
        cv2.rectangle(out, (x0, ty - th - 6), (x0 + tw + 8, ty), colour, -1)
        cv2.putText(out, label, (x0 + 4, ty - 4), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick)
    return out


def _side(box: List[float]) -> str:
    cx = box[0] + box[2] / 2
    return "left" if cx < 0.4 else "right" if cx > 0.6 else "middle"


def _said_near(words: List[Dict[str, Any]], t: float, span: float = 2.0) -> str:
    return " ".join(w["w"] for w in words if t - span <= w["start"] <= t + span)[:200]


def _transcript_lines(words: List[Dict[str, Any]]) -> str:
    lines, cur, at = [], [], None
    for w in words:
        if at is None:
            at = w["start"]
        cur.append(w["w"])
        if len(cur) >= 7 or w["w"].rstrip().endswith((".", "?", "!")):
            lines.append(f"[{at:.1f}s] " + " ".join(cur))
            cur, at = [], None
    if cur:
        lines.append(f"[{at:.1f}s] " + " ".join(cur))
    return "\n".join(lines)[:6000]


# --- asking Claude ------------------------------------------------------------------------------

LOOK_TOOL = {
    "name": "submit_campaign_check",
    "description": "What this campaign clip shows and says.",
    "input_schema": {"type": "object", "properties": {
        "people": {"type": "array", "items": {"type": "object", "properties": {
            "box": {"type": "integer", "description": "The number on the person's box."},
            "who": {"type": "string", "enum": ["creator", "other", "unclear"]},
            "name": {"type": "string", "description": "A name ONLY if said aloud, written on screen or in the title; "
                                                      "else a neutral description like 'the friend on the right'."},
            "how_known": {"type": "string", "description": "The context that tells you, in a few words."}},
            "required": ["box", "who"]}},
        "main_person": {"type": "string", "enum": ["creator", "other", "nobody", "unclear"],
                        "description": "Who is on screen most AND does most of the talking."},
        "main_person_name": {"type": "string"},
        "hook_speaker": {"type": "string", "enum": ["creator", "other", "nobody_on_screen", "unclear"],
                         "description": "Who says the lines the hook is about."},
        "hook_speaker_name": {"type": "string"},
        "misattributed": {"type": "boolean", "description": "True when the hook or caption credits the creator with "
                                                            "words that someone else says in this clip."},
        "fixed_hook": {"type": "string", "description": "When misattributed: the hook corrected — same style and length, "
                                                       "naming the real speaker if their name is known, else no one."},
        "logos": {"type": "array", "items": {"type": "object", "properties": {
            "what": {"type": "string", "description": "e.g. 'a \"use code TJR\" sponsor banner', 'a Bitget logo'"},
            "kind": {"type": "string", "enum": ["sponsor_banner", "promo_code", "brand_logo", "watermark",
                                                "campaign_own", "drawn_by_clipagent"]},
            "where": {"type": "string", "description": "top left, bottom right, middle…"},
            "from_s": {"type": "number"}, "to_s": {"type": "number"}},
            "required": ["what", "kind", "where", "from_s", "to_s"]}},
        "ai_visuals": {"type": "array", "items": {"type": "object", "properties": {
            "what": {"type": "string"}, "from_s": {"type": "number"}, "to_s": {"type": "number"}},
            "required": ["what", "from_s", "to_s"]}},
        "spoken_promos": {"type": "array", "items": {"type": "object", "properties": {
            "quote": {"type": "string"}, "at_s": {"type": "number"}}, "required": ["quote", "at_s"]}},
        "ai_mentions": {"type": "array", "items": {"type": "object", "properties": {
            "quote": {"type": "string"}, "at_s": {"type": "number"}}, "required": ["quote", "at_s"]}},
        "offensive": {"type": "array", "items": {"type": "object", "properties": {
            "quote": {"type": "string", "description": "The exact words, as written in the transcript."},
            "kind": {"type": "string", "enum": ["slur", "offensive_joke"]},
            "from_s": {"type": "number"}, "to_s": {"type": "number"},
            "said_by": {"type": "string", "enum": ["creator", "other", "unclear"]},
            "is_punchline": {"type": "boolean", "description": "True when these words are the clip's payoff."}},
            "required": ["quote", "kind", "from_s", "to_s"]}},
        "creator_in_bad_light": {"type": "boolean"},
        "bad_light_why": {"type": "string"},
    }, "required": ["people", "main_person", "hook_speaker", "misattributed", "logos", "ai_visuals", "offensive"]},
}

LOOK_SYSTEM = """You check a finished vertical clip for a paid clipping campaign before it is posted. \
You see frames of the clip in order with their times; faces have numbered boxes on the first frames. \
You also get the words said, with their times. Answer only from what you can see and read.

1. People. For each numbered box: the campaign's creator, someone else, or unclear. Work it out ONLY \
from context — names said aloud or written on screen, who is addressed by name, the video's title, \
questions and answers, and the face-check result you are given. Never identify anyone from their face \
or looks, and never invent a name that isn't said, written or in the title.
2. The main person (on screen most AND doing most of the talking), and who says the lines the hook is about.
3. Does the hook or the caption credit the creator with words someone else says in this clip? If so, \
write the hook corrected: same style and length, naming the real speaker when their name is known, \
else naming no one.
4. Logos and promotions anywhere in the picture, footage included: sponsor banners, "use code X" or \
promo-code text, brands' logos, watermarks (a TikTok watermark, a channel's bug). Say what, where, and \
from when to when. Mark ClipAgent's own hook text, captions and the logo it drew (named below) as \
drawn_by_clipagent, and the campaign's own brand as campaign_own. Look hard at the full-size frames: \
banners are often small.
5. AI-made visuals shown in the clip (AI video or images: the smooth, warped, impossible look, an AI \
tool's tag). Only what you can see.
6. In the words: spoken promotions ("use code", "link in bio", "sponsored by"), mentions of AI video, \
slurs (racist, sexist, homophobic, ableist) and jokes that punch at a protected group — quote the \
exact words with their times. Ordinary swearing is not offensive here.
7. Whether the clip shows the creator in a bad light.
Report nothing that isn't there: empty lists are the normal answer."""


def _client():
    from . import highlights
    return highlights._client()


def _clip_num(v: Any, seconds: float, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        x = default
    return round(min(max(0.0, x), max(0.0, seconds)), 2)


def _txt(v: Any, n: int = 160) -> str:
    return re.sub(r"\s+", " ", v).strip()[:n] if isinstance(v, str) else ""


def _normalize(reply: Dict[str, Any], seconds: float) -> Dict[str, Any]:
    r = toolio.as_dict(reply)
    if not r or ("main_person" not in r and "people" not in r and "offensive" not in r):
        return {"ran": False, "why": "Claude gave no answer"}

    def items(key: str) -> List[Dict[str, Any]]:
        return [toolio.as_dict(x) for x in toolio.as_list(r.get(key)) if toolio.as_dict(x)]

    def pick(v: Any, allowed: Tuple[str, ...], default: str) -> str:
        return v if v in allowed else default

    return {
        "ran": True,
        "people": [{"box": int(p.get("box") or 0), "who": pick(p.get("who"), ("creator", "other", "unclear"), "unclear"),
                    "name": _txt(p.get("name"), 60), "how": _txt(p.get("how_known"), 120)}
                   for p in items("people") if str(p.get("box", "")).strip().lstrip("-").isdigit()],
        "main": pick(r.get("main_person"), ("creator", "other", "nobody", "unclear"), "unclear"),
        "main_name": _txt(r.get("main_person_name"), 60),
        "hook_speaker": pick(r.get("hook_speaker"), ("creator", "other", "nobody_on_screen", "unclear"), "unclear"),
        "hook_speaker_name": _txt(r.get("hook_speaker_name"), 60),
        "misattributed": r.get("misattributed") in (True, "true", "True", 1),
        "fixed_hook": _txt(r.get("fixed_hook"), 100),
        "logos": [{"what": _txt(x.get("what"), 120) or "a logo", "kind": pick(x.get("kind"), (
            "sponsor_banner", "promo_code", "brand_logo", "watermark", "campaign_own", "drawn_by_clipagent"), "brand_logo"),
                   "where": _txt(x.get("where"), 40), "from": _clip_num(x.get("from_s"), seconds),
                   "to": _clip_num(x.get("to_s"), seconds, seconds)} for x in items("logos")],
        "ai_visuals": [{"what": _txt(x.get("what"), 120) or "AI-made footage", "from": _clip_num(x.get("from_s"), seconds),
                        "to": _clip_num(x.get("to_s"), seconds, seconds)} for x in items("ai_visuals")],
        "spoken_promos": [{"quote": _txt(x.get("quote"), 120), "at": _clip_num(x.get("at_s"), seconds)}
                          for x in items("spoken_promos") if _txt(x.get("quote"))],
        "ai_mentions": [{"quote": _txt(x.get("quote"), 120), "at": _clip_num(x.get("at_s"), seconds)}
                        for x in items("ai_mentions") if _txt(x.get("quote"))],
        "offensive": [{"quote": _txt(x.get("quote"), 300), "kind": pick(x.get("kind"), ("slur", "offensive_joke"), "slur"),
                       "from": _clip_num(x.get("from_s"), seconds), "to": _clip_num(x.get("to_s"), seconds, seconds),
                       "said_by": pick(x.get("said_by"), ("creator", "other", "unclear"), "unclear"),
                       "punchline": x.get("is_punchline") in (True, "true", "True", 1)}
                      for x in items("offensive") if _txt(x.get("quote"))],
        "bad_light": r.get("creator_in_bad_light") in (True, "true", "True", 1),
        "bad_light_why": _txt(r.get("bad_light_why"), 200),
    }


def ask_claude(video: Path, words: List[Dict[str, Any]], seconds: float, scan: Dict[str, Any],
               context: str) -> Dict[str, Any]:
    """The one vision call. Never raises: {"ran": False, "why": plain words} when it couldn't happen."""
    if not ANTHROPIC_API_KEY:
        return {"ran": False, "why": "Claude isn't set up (no ANTHROPIC_API_KEY in .env)"}
    from . import doctor
    try:
        overview, detail = _times(seconds)
        shots = doctor.grab(video, overview)
        if not shots:
            return {"ran": False, "why": "couldn't read frames from the clip"}
        content: List[Dict[str, Any]] = [{"type": "text", "text": context}]
        for t, img in shots:
            boxes = _boxes_at(scan, t)
            who = ", ".join(f"box {pid} ({_side(b)})" for pid, b in boxes) or "no faces found"
            content.append({"type": "text", "text": f"Frame at {t:.1f}s — {who}. Said around now: "
                                                    f"“{_said_near(words, t) or '(nothing)'}”"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data":
                            base64.b64encode(doctor.jpeg(draw_people(img, boxes), OVERVIEW_W)).decode("ascii")}})
        for t, img in doctor.grab(video, detail):
            content.append({"type": "text", "text": f"Full-size frame at {t:.1f}s (no boxes) — for small text, "
                                                    "banners and logos."})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                            "data": base64.b64encode(doctor.jpeg(img, img.shape[1], 85)).decode("ascii")}})
        content.append({"type": "text", "text": "Words said, with their times:\n" + (_transcript_lines(words)
                                                                                   or "(no words)")})
        message = _client().messages.create(model=CLAUDE_MODEL, max_tokens=1800, system=LOOK_SYSTEM,
                                            tools=[LOOK_TOOL],
                                            tool_choice={"type": "tool", "name": LOOK_TOOL["name"]},
                                            messages=[{"role": "user", "content": content}])
        got = toolio.tool_inputs(message)
        return _normalize(got[0] if got else {}, seconds)
    except Exception as exc:
        return {"ran": False, "why": "Claude couldn't look at it: " + _plain(str(exc))}


def _plain(err: str) -> str:
    low = err.lower()
    if "credit" in low or "billing" in low:
        return "the Claude account is out of credit"
    if "rate" in low and "limit" in low or "429" in low:
        return "Claude is busy (rate limit) — try the clip again in a minute"
    if "overload" in low or "529" in low:
        return "Claude is overloaded right now — try again later"
    if "timed out" in low or "timeout" in low or "connection" in low:
        return "no connection to Claude"
    return err[:140]


def _context(r: Dict[str, Any], *, title: str, hook: str, caption: str, drawn: str, picker: Optional[Dict[str, Any]],
             scan: Dict[str, Any], match: Dict[str, Any], refs: Dict[str, Any]) -> str:
    lines = [f"Campaign: {r['campaign']}. The creator (the person these clips are for): {r['creator'] or 'not named'}."]
    if r["focus"]:
        lines.append(f"The brief needs {r['focus']} to be the main person on screen"
                     + (f" (“{r['focus_quote'][:160]}”)." if r["focus_quote"] else "."))
    if r["no_logos"]:
        lines.append("The brief bans logos, sponsor banners, promo codes and watermarks anywhere in the video.")
    if r["no_ai"]:
        lines.append("The brief bans AI-generated video or images anywhere in the video.")
    if r["negative"]:
        lines.append(f"The brief says: “{r['negative'][:160]}”.")
    lines.append(f"Video title: {title or '(unknown)'}")
    lines.append(f"On-screen hook: “{hook or '(none)'}”")
    if caption:
        lines.append(f"Post caption: “{caption[:300]}”")
    if drawn:
        lines.append(f"Drawn by ClipAgent (not part of the footage): {drawn}.")
    if picker and picker.get("who"):
        lines.append(f"When the moment was picked from the transcript, the key lines seemed to be said by: "
                     f"{picker['who']}" + (f" ({picker['name']})" if picker.get("name") else "") + ".")
    if scan.get("ok"):
        bits = []
        for p in scan.get("people") or []:
            bit = f"box {p['id']}: on screen {p['share'] * 100:.0f}% of the clip"
            if scan.get("speech_frames"):
                bit += f", mouth moving with {p['talk'] * 100:.0f}% of the speech"
            if p["id"] in (match.get("dist") or {}):
                bit += f", face distance to the creator's reference faces {match['dist'][p['id']]:.2f}"
            bits.append(bit)
        lines.append("Face check (measured, no names): " + ("; ".join(bits) or "no faces") + ".")
        if match.get("status") == "clear":
            lines.append(f"Reference match: box {match['person']} looks like the creator's reference faces "
                         f"({refs.get('photos', 0)} photos, {refs.get('learned', 0)} learned solo clips) — "
                         "approximate, so weigh it against the context.")
        elif match.get("status") == "unclear":
            lines.append("Reference match: unclear — no box clearly looks like the creator's reference faces.")
        else:
            lines.append("Reference match: no reference faces for this creator yet.")
        if scan.get("offcam", 0) >= 0.25:
            lines.append(f"{scan['offcam'] * 100:.0f}% of the speech comes while no face on screen moves its "
                         "mouth — maybe someone off camera.")
    else:
        lines.append(f"Face check: {scan.get('note') or 'not run'}")
    return "\n".join(lines)


# --- deciding -----------------------------------------------------------------------------------

def _who(r: Dict[str, Any], scan: Dict[str, Any], match: Dict[str, Any], claude: Dict[str, Any],
         hook: str, caption: str, picker: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Is the creator the main person, and who says the hook — faces, mouths and context together."""
    from . import highlights
    people = {p["id"]: p for p in scan.get("people") or []} if scan.get("ok") else {}
    cl_people = {p["box"]: p for p in claude.get("people") or []} if claude.get("ran") else {}
    creator, how = None, ""
    if match.get("status") == "clear" and match.get("person") in people:
        creator, how = match["person"], "faces"
    said_creator = [b for b, p in cl_people.items() if p["who"] == "creator" and b in people]
    conflict = False
    if creator is None and len(said_creator) == 1:
        creator, how = said_creator[0], "context"
    elif creator is not None and said_creator and creator not in said_creator:
        conflict = True

    def name_of(pid: Optional[int]) -> str:
        p = cl_people.get(pid) if pid is not None else None
        return p["name"] if p and p.get("name") and p["who"] != "creator" else ""

    speech = bool(scan.get("speech_frames", 0) >= 3)
    talkers = sorted(people.values(), key=lambda p: p["talk"], reverse=True)
    top = talkers[0] if talkers and talkers[0]["talk"] >= 0.3 else None
    local, other_pid = "unavailable", None
    if scan.get("ok") and not people:
        local = "nobody"
    elif people and creator is None:
        local = "unknown"
    elif creator is not None:
        c = people[creator]
        others = [p for p in people.values() if p["id"] != creator]
        rival = max(others, key=lambda p: (p["talk"], p["share"])) if others else None
        if speech:
            if c["share"] >= 0.5 and c["talk"] >= 0.35 and (not rival or c["talk"] >= rival["talk"]):
                local = "creator"
            elif rival and rival["talk"] >= 0.4 and rival["talk"] >= 2 * c["talk"]:
                local, other_pid = "other", rival["id"]
            elif c["share"] < 0.3 and rival and rival["share"] >= 0.6:
                local, other_pid = "other", rival["id"]
            elif scan.get("offcam", 0) + scan.get("nobody", 0) >= 0.5:
                local = "offcam"
            else:
                local = "unclear"
        else:
            if c["share"] >= 0.5:
                local = "creator"
            elif rival and rival["share"] >= 0.6:
                local, other_pid = "other", rival["id"]
            else:
                local = "unclear"
    cl_main = claude.get("main") if claude.get("ran") else None

    if conflict:
        main = "unclear"
    elif local == "creator":
        main = "unclear" if cl_main == "other" else "creator"
    elif local == "other":
        main = "unclear" if cl_main == "creator" else "other"
    elif local == "nobody":
        main = "nobody"
    elif cl_main in ("creator", "other", "nobody"):
        main = cl_main
        if main == "creator" and how != "faces":
            how = how or "context"
    elif not claude.get("ran") and local in ("unavailable", "unknown"):
        main = "unknown"
    else:
        main = "unclear"
    if main == "other" and other_pid is None:
        other_pid = next((b for b, p in cl_people.items() if p["who"] == "other"), None)
    other_name = (name_of(other_pid) or (claude.get("main_name") if cl_main == "other" else "")
                  or (claude.get("hook_speaker_name") if claude.get("hook_speaker") == "other" else "")
                  or highlights._proper_name((picker or {}).get("name") or ""))

    # who says the hook's lines
    hs = claude.get("hook_speaker") if claude.get("ran") else "unclear"
    hs_name = claude.get("hook_speaker_name") or ""
    if hs == "unclear" and top is not None and creator is not None:
        hs = "creator" if top["id"] == creator else "other"
        hs_name = hs_name or name_of(top["id"])
    if hs == "unclear" and main in ("creator", "other"):
        hs = main
    credits = highlights.credits_creator(hook, r["creator"]) or highlights.credits_creator(caption, r["creator"])
    mis = bool(credits and (claude.get("misattributed") or hs in ("other", "nobody_on_screen")))
    fixed = ""
    if mis:
        cand = claude.get("fixed_hook") or ""
        if not cand or highlights.credits_creator(cand, r["creator"]):
            cand = highlights.fix_credit(hook, r["creator"], "other", hs_name or other_name)
        fixed = cand if cand != hook else ""

    c = people.get(creator) if creator is not None else None
    rival = max((p for p in people.values() if p["id"] != creator), key=lambda p: p["talk"], default=None)
    return {
        "creator": r["creator"], "creator_person": creator, "creator_how": how,
        "local": local, "claude": cl_main, "main": main, "conflict": conflict,
        "other_person": other_pid, "other_name": other_name or "",
        "creator_share": round(c["share"], 3) if c else None,
        "creator_talk": round(c["talk"], 3) if c and speech else None,
        "other_talk": round(rival["talk"], 3) if rival and speech else None,
        "other_share": round(rival["share"], 3) if rival else None,
        "speech": speech, "hook_speaker": hs, "hook_speaker_name": hs_name,
        "credits": bool(credits), "misattributed": mis, "fixed_hook": fixed,
    }


def _locate(quote: str, words: List[Dict[str, Any]], near: float, far: float) -> Optional[Tuple[int, int]]:
    """The words a quote is, as indices into the clip's words — or None when it can't be found."""
    q = [_token(t) for t in quote.split() if _token(t)]
    if not q or not words:
        return None
    toks = [_token(w["w"]) for w in words]
    best, at = 0.0, None
    for i in range(len(words)):
        if not (near - 3.0 <= words[i]["start"] <= far + 3.0):
            continue
        window = toks[i:i + len(q)]
        score = sum(1 for a, b in zip(window, q) if a == b) / len(q)
        if score > best:
            best, at = score, i
    if at is None or best < 0.6:
        return None
    return at, min(len(words) - 1, at + len(q) - 1)


def _offensive(r: Dict[str, Any], claude: Dict[str, Any], local: Dict[str, Any], words: List[Dict[str, Any]],
               kind: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for x in (claude.get("offensive") or []) if claude.get("ran") else []:
        span = _locate(x["quote"], words, x["from"], x["to"])
        item = {"quote": mask(x["quote"])[:160], "kind": x["kind"], "said_by": x["said_by"],
                "punchline": x["punchline"], "by": "claude", "exact": span is not None}
        if span:
            item.update(i0=span[0], i1=span[1], **{"from": words[span[0]]["start"], "to": words[span[1]]["end"]})
        else:
            item.update(i0=None, i1=None, **{"from": x["from"], "to": max(x["to"], x["from"])})
        found.append(item)
    for s in local.get("slurs") or []:
        if not any(f["exact"] and f["i0"] is not None and f["i0"] <= s["i0"] <= f["i1"] for f in found):
            found.append(dict(s))
    found.sort(key=lambda f: f["from"])
    merged: List[Dict[str, Any]] = []
    for f in found:
        if merged and f["from"] <= merged[-1]["to"] + 0.3 and f["exact"] and merged[-1]["exact"]:
            m = merged[-1]
            m.update(to=max(m["to"], f["to"]), i1=max(m["i1"], f["i1"]), punchline=m["punchline"] or f["punchline"],
                     kind="offensive_joke" if "offensive_joke" in (m["kind"], f["kind"]) else "slur",
                     quote=(m["quote"] + " … " + f["quote"])[:160] if f["quote"] not in m["quote"] else m["quote"])
        else:
            merged.append(f)
    last_word = words[-1]["end"] if words else 0.0
    for f in merged:
        why = ""
        if kind == "edit":
            why = "an edit can't cut out single words — switch that moment off and make it again"
        elif kind == "overlay":
            why = "clip-bank clips are posted whole"
        elif not r["cut_ok"]:
            why = "the brief doesn't allow cuts inside a clip"
        elif not f["exact"]:
            why = "couldn't pin down exactly where it is said"
        elif f["to"] - f["from"] > MAX_CUT:
            why = f"it runs {f['to'] - f['from']:.0f}s — too long to cut without breaking the clip"
        elif f["from"] < HOOK_SECONDS:
            why = "it's in the opening seconds, where the hook is"
        elif f["punchline"] or last_word - f["to"] < 0.8:
            why = "it's the punchline"
        f["cuttable"] = not why
        f["why_not"] = why
        f["from"], f["to"] = round(f["from"], 2), round(f["to"], 2)
    return merged


def review(video: Path, words: List[Dict[str, Any]], rb: Dict[str, Any], *, title: str = "", hook: str = "",
           caption: str = "", campaign_id: str = "", picker: Optional[Dict[str, Any]] = None, drawn: str = "",
           kind: str = "clip", faces: bool = True) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Look at one finished video for its campaign. Returns (look, the face scan). Never raises."""
    r = rules_of(rb)
    try:
        seconds = identity._probe(Path(video))[2]
    except Exception:
        seconds = 0.0
    w = norm_words(words)
    scan = identity.scan(Path(video), w) if faces else {"ok": False, "note": "", "people": []}
    refs = identity.references(campaign_id) if (campaign_id and scan.get("ok")) else \
        {"vectors": [], "photos": 0, "learned": 0, "kind": identity.kind()}
    match = identity.match(scan, refs) if scan.get("ok") else {"person": None, "status": "no_faces", "dist": {}}
    local = transcript_flags(w)
    context = _context(r, title=title, hook=hook, caption=caption, drawn=drawn, picker=picker, scan=scan,
                       match=match, refs=refs)
    claude = ask_claude(Path(video), w, seconds, scan, context)
    look = {
        "v": 1, "at": round(time.time(), 1), "kind": kind, "seconds": round(seconds, 2),
        "hook": hook, "caption": caption[:400],
        "faces": identity.public(scan) if faces else None,
        "match": {k: v for k, v in match.items() if k != "dist"} | {"dist": {str(k): v for k, v in
                                                                         (match.get("dist") or {}).items()}},
        "refs": {"photos": refs.get("photos", 0), "learned": refs.get("learned", 0)},
        "claude": claude,
        "local": {"promos": local["promos"], "ai": local["ai"], "slurs": len(local["slurs"])},
        "who": _who(r, scan, match, claude, hook, caption, picker) if kind != "overlay" else None,
        "offensive": _offensive(r, claude, local, w, kind),
        "frames": len(_times(seconds)[0]) + DETAIL_N if claude.get("ran") else 0,
    }
    return look, scan


# --- cutting a stretch out (used by pipeline when it re-renders) ------------------------------

def cut_segments(segments: List[Tuple[float, float]], spans: List[List[float]]) -> List[Tuple[float, float, int]]:
    """Segments (absolute source seconds, in play order) with `spans` taken out.
    Each piece keeps the index of the segment it came from."""
    out = []
    for i, (a, b) in enumerate(segments):
        pieces = [(a, b)]
        for x, y in spans or []:
            nxt = []
            for p, q in pieces:
                if y <= p or x >= q:
                    nxt.append((p, q))
                    continue
                if x > p:
                    nxt.append((p, x))
                if y < q:
                    nxt.append((y, q))
            pieces = nxt
        out += [(round(p, 3), round(q, 3), i) for p, q in pieces if q - p >= 0.05]
    return out


def retime_words(words: List[Dict[str, Any]], segments: List[Tuple[float, float]],
                 pieces: List[Tuple[float, float, int]], length=None) -> List[Dict[str, Any]]:
    """Words on the old clip clock moved onto the clock after the cuts; words inside a cut go.
    `length(a, b)` is how long a stretch plays (frame-exact in the renderer)."""
    length = length or (lambda a, b: b - a)
    old = []
    at = 0.0
    for a, b in segments:
        n = length(a, b)
        old.append((a, at, n))
        at += n
    new = []
    at = 0.0
    for p, q, i in pieces:
        n = length(p, q)
        new.append((p, q, i, at, n))
        at += n

    def move(t: float) -> Optional[float]:
        for i, (a, start, n) in enumerate(old):
            if start - 1e-6 <= t <= start + n + 1e-6:
                src = a + (t - start)
                for p, q, j, nstart, nn in new:
                    if j == i and p - 1e-3 <= src <= q + 1e-3:
                        return round(nstart + min(max(0.0, src - p), nn), 3)
                return None
        return None

    out = []
    for w in words:
        s = move(float(w["start"]))
        if s is None:
            continue
        e = move(float(w["end"]))
        if e is None or e < s:
            e = s + max(0.08, float(w["end"]) - float(w["start"]))
        out.append({**w, "start": s, "end": round(e, 3)})
    return out


def _source_map(clip_words: List[Dict[str, Any]], all_words: List[Dict[str, Any]],
                spans: List[Tuple[float, float]]) -> List[Optional[Dict[str, Any]]]:
    """Each of the clip's words (finished-clip clock) matched to the transcript word it is."""
    pool = []
    for a, b in spans:
        pool += [w for w in all_words if a - 0.1 <= w["start"] < b + 0.1]
    out: List[Optional[Dict[str, Any]]] = []
    j = 0
    for w in clip_words:
        t = _token(w["w"])
        hit = None
        for k in range(j, min(len(pool), j + 10)):
            if _token(pool[k]["w"]) == t:
                hit = k
                break
        if hit is None:
            out.append(None)
            continue
        out.append({**pool[hit], "_k": hit, "_pool": pool})
        j = hit + 1
    return out


def source_span(item: Dict[str, Any], mapped: List[Optional[Dict[str, Any]]]) -> Optional[List[float]]:
    """A finding's words as a stretch of the source, without eating into the words either side."""
    if item.get("i0") is None or item["i1"] >= len(mapped):
        return None
    first, last = mapped[item["i0"]], mapped[item["i1"]]
    if not first or not last:
        return None
    pool = first["_pool"]
    a = first["start"] - 0.04
    b = last["end"] + 0.04
    if first["_k"] > 0:
        a = max(a, (pool[first["_k"] - 1]["end"] + first["start"]) / 2)
    if last["_k"] + 1 < len(pool):
        b = min(b, (last["end"] + pool[last["_k"] + 1]["start"]) / 2)
    return [round(a, 3), round(b, 3)] if b > a else None


def plan_fixes(look: Dict[str, Any], rb: Dict[str, Any], clip: Dict[str, Any], edits: Dict[str, Any],
               all_words: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """The edits that fix what the look found (for one re-render), and a line for each."""
    from . import highlights
    r = rules_of(rb)
    who = look.get("who") or {}
    change: Dict[str, Any] = {}
    notes: List[str] = []
    if r["focus"] and who.get("main") == "other":
        return {}, []                                  # blocked anyway: no point re-rendering
    hook = look.get("hook") or ""
    if who.get("misattributed") and who.get("fixed_hook") and r["hook_ok"]:
        new = who["fixed_hook"][:90]
        cards = [dict(c) for c in edits.get("cards") or []]
        hit = next((c for c in cards if re.sub(r"[\[\]]", "", c.get("text") or "").strip() == hook), None)
        if hit is not None:
            hit["text"] = new
            change["cards"] = cards
        else:
            change["hook"] = new
        notes.append(f"Rewrote the hook so it doesn't credit {r['creator']} with words "
                     f"{who.get('other_name') or 'someone else'} says: “{new}”")
    found = look.get("offensive") or []
    if found and all(f.get("cuttable") for f in found):
        parts = json.loads(clip.get("parts") or "[]") if isinstance(clip.get("parts"), str) else (clip.get("parts") or [])
        spans = [(float(p["start"]), float(p["end"])) for p in parts] if len(parts) > 1 else \
            [(float(clip["start"]), float(clip["end"]))]
        clip_words = norm_words(json.loads(clip.get("words") or "[]") if isinstance(clip.get("words"), str)
                                else clip.get("words") or [])
        mapped = _source_map(clip_words, all_words, spans)
        cuts = [source_span(f, mapped) for f in found]
        seconds = float(look.get("seconds") or 0)
        total = sum(b - a for a, b in cuts if a is not None) if all(cuts) else 0
        lo = float(r["min_len"] or 0)
        if all(cuts) and total <= MAX_CUT_SHARE * max(1.0, seconds) and (not lo or seconds - total >= lo):
            change["cut_out"] = [list(c) for c in (edits.get("cut_out") or [])] + cuts
            for f, c in zip(found, cuts):
                f["src"] = c
            notes.append("Cut out " + ", ".join(
                f"{'an offensive joke' if f['kind'] == 'offensive_joke' else 'an offensive word'} at {mmss(f['from'])}"
                for f in found))
        else:
            for f in found:
                f["cuttable"] = False
                f["why_not"] = f["why_not"] or ("cutting it would take the clip under the brief's minimum length"
                                                if all(cuts) else "couldn't line it up with the source to cut it")
    return change, notes


# --- running it on a clip, an edit, a clip-bank clip ----------------------------------------------

def _on_screen(edits: Dict[str, Any]) -> str:
    """The text a viewer reads first: a card's, or the hook (as the doctor reads it)."""
    for card in edits.get("cards") or []:
        if (card.get("text") or "").strip():
            return re.sub(r"[\[\]]", "", card["text"]).strip()
    return (edits.get("hook") or "").strip() if edits.get("hook_on", True) else ""


def wanted(rb: Optional[Dict[str, Any]]) -> bool:
    return bool(rb)


def review_clip(clip_id: str, rb: Dict[str, Any], fixes: bool = True) -> str:
    """The campaign look on a rendered clip-from-source clip, kept in its edits; with `fixes`,
    the safe fixes made in one re-render. 'rerendered' | 'checked' | 'skipped'. Never raises."""
    from . import store, transcribe
    if getattr(_busy, "on", False):
        return "skipped"
    _busy.on = True
    try:
        clip = store.get_clip(clip_id)
        if not clip or not clip.get("file") or not Path(clip["file"]).exists():
            return "skipped"
        job = store.get_job(clip["job_id"]) or {}
        settings = json.loads(job.get("settings") or "{}")
        edits = json.loads(clip.get("edits") or "{}")
        post = json.loads(clip.get("post") or "{}") or {}
        words = json.loads(clip.get("words") or "[]")
        meta = json.loads(job.get("source_meta") or "{}") if job.get("source_meta") else {}
        title = meta.get("title") or job.get("title") or ""
        cid = (settings.get("campaign") or {}).get("id") or job.get("campaign_id") or ""
        drawn = "the hook text and the word-by-word captions"
        if edits.get("brand_logo"):
            drawn += ", and the campaign's logo at the top"
        hook = _on_screen(edits)
        look, scan = review(Path(clip["file"]), words, rb, title=title, hook=hook,
                            caption=post.get("caption") or clip.get("caption") or "", campaign_id=cid,
                            picker=edits.get("picker_speaker"), drawn=drawn)
        parts = json.loads(clip.get("parts") or "[]")
        look["span"] = [float(clip["start"]), float(clip["end"])]
        look["covered"] = [[float(p["start"]), float(p["end"])] for p in parts] if len(parts) > 1 else [look["span"]]
        if scan.get("solo") and cid:
            try:
                identity.learn(cid, scan, clip["job_id"], clip_id)
            except Exception:
                traceback.print_exc()
        change: Dict[str, Any] = {}
        if fixes:
            all_words = transcribe.in_order(json.loads(job.get("transcript") or "{}").get("words") or [])
            change, notes = plan_fixes(look, rb, clip, edits, all_words)
            look["fixes"] = notes
            if look.get("who", {}).get("misattributed"):
                _fix_caption(clip_id, clip, post, look, rb)
        if change:
            change["campaign_look"] = look
            from . import pipeline
            try:
                pipeline.rerender_clip(clip_id, change)
                return "rerendered"
            except Exception as exc:
                traceback.print_exc()
                look["fix_error"] = f"The fix didn't render: {str(exc)[:140]}"
                for f in look.get("offensive") or []:
                    f.pop("src", None)
        fresh = json.loads((store.get_clip(clip_id) or clip).get("edits") or "{}")
        store.update_clip(clip_id, edits=json.dumps({**fresh, "campaign_look": look}))
        return "checked"
    except Exception:
        traceback.print_exc()
        return "skipped"
    finally:
        _busy.on = False


def _fix_caption(clip_id: str, clip: Dict[str, Any], post: Dict[str, Any], look: Dict[str, Any],
                 rb: Dict[str, Any]) -> None:
    """The post caption gets the same rule as the hook (no re-render needed)."""
    from . import highlights, store
    who = look.get("who") or {}
    creator = rules_of(rb)["creator"]
    old = post.get("caption") or ""
    new = highlights.fix_credit(old, creator, "other", who.get("hook_speaker_name") or who.get("other_name") or "")
    if old and new != old:
        post = {**post, "caption": new, "text": (post.get("text") or "").replace(old, new)}
        store.update_clip(clip_id, post=json.dumps(post), caption=new)
        look.setdefault("fixes", []).append(f"Rewrote the caption the same way: “{new[:80]}”")


def uncovered(look: Optional[Dict[str, Any]], spans: List[Tuple[float, float]]) -> float:
    """Seconds of the clip's source the look never saw (a later trim ran it on)."""
    covered = (look or {}).get("covered") or []
    if not covered:
        return 0.0
    total = 0.0
    for a, b in spans:
        pieces = [(a, b)]
        for x, y in covered:
            nxt = []
            for p, q in pieces:
                if y <= p or x >= q:
                    nxt.append((p, q))
                    continue
                if x > p:
                    nxt.append((p, x))
                if y < q:
                    nxt.append((y, q))
            pieces = nxt
        total += sum(q - p for p, q in pieces)
    return round(total, 2)


def clip_spans(clip: Dict[str, Any]) -> List[Tuple[float, float]]:
    parts = clip.get("parts")
    parts = json.loads(parts or "[]") if isinstance(parts, str) else (parts or [])
    if len(parts) > 1:
        return [(float(p["start"]), float(p["end"])) for p in parts]
    return [(float(clip["start"]), float(clip["end"]))]


def stale(clip: Dict[str, Any], edits: Dict[str, Any]) -> bool:
    """Should the look run (again) before this clip is gated? Never inside a look's own re-render."""
    if getattr(_busy, "on", False):
        return False
    look = edits.get("campaign_look")
    if not look:
        return True
    return uncovered(look, clip_spans(clip)) > UNCHECKED_OK


# Edits and clip-bank clips are judged by compliance directly; the same file is never paid for twice.
_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cached(key: str, make) -> Dict[str, Any]:
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    value = make()
    with _CACHE_LOCK:
        _CACHE[key] = value
        while len(_CACHE) > 64:
            _CACHE.popitem(last=False)
    return value


def _file_key(path: Path, *extra: Any) -> str:
    try:
        st = Path(path).stat()
        base = f"{Path(path).resolve()}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        base = str(path)
    return hashlib.sha1((base + "|" + json.dumps(extra, sort_keys=True, default=str)).encode()).hexdigest()


def campaign_for(rb: Dict[str, Any]) -> str:
    """The campaign a rulebook belongs to (an edit's gate only has the rulebook)."""
    from . import store
    try:
        want = json.dumps(rb, sort_keys=True, default=str)
        for c in store.list_campaigns():
            full = store.get_campaign(c["id"]) or {}
            if json.dumps(full.get("rulebook") or {}, sort_keys=True, default=str) == want:
                return c["id"]
    except Exception:
        traceback.print_exc()
    return ""


def review_edit(rb: Dict[str, Any], timeline: Dict[str, Any], output: Path, hook: str,
                post: Dict[str, Any]) -> Dict[str, Any]:
    """The campaign look on an Edit Maker video (the words come from its timeline)."""
    words = [w for s in timeline.get("segments") or [] for w in (s.get("words") or [])]
    caption = (post or {}).get("caption") or ""

    def make() -> Dict[str, Any]:
        look, _ = review(Path(output), words, rb, hook=hook, caption=caption, campaign_id=campaign_for(rb),
                         drawn="the hook text and any words on screen", kind="edit")
        return look
    return _cached(_file_key(output, "edit", hook, caption, rb), make)


def review_overlay(rb: Dict[str, Any], output: Path, hook: str, title: str = "",
                   logo_drawn: bool = False) -> Optional[Dict[str, Any]]:
    """The look on a clip-bank clip: only what the brief bans in the picture (no words, no identity)."""
    r = rules_of(rb)
    if not (r["no_logos"] or r["no_ai"]):
        return None

    def make() -> Dict[str, Any]:
        look, _ = review(Path(output), [], rb, title=title, hook=hook, kind="overlay", faces=False,
                         drawn="the hook text" + (" and the campaign's logo" if logo_drawn else ""))
        return look
    return _cached(_file_key(output, "overlay", hook, rb), make)
