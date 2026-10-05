"""Animated captions, built as an ASS subtitle file that ffmpeg burns in.

One Dialogue event per spoken word gives the word-by-word highlight that
short-form captions live on, without any frame-by-frame rendering.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from .config import RENDER_H, RENDER_W, SAFE_LEFT, SAFE_RIGHT, SAFE_TOP

# name -> look. `font` falls back to Poppins when the TTF is not installed.
STYLES: Dict[str, Dict[str, Any]] = {
    "impact": {
        "label": "Impact", "font": "Anton", "size": 92, "uppercase": True,
        "primary": "#FFFFFF", "active": "#FFD400", "outline": "#000000",
        "outline_w": 7, "shadow": 3, "box": False, "pop": True,
    },
    "karaoke": {
        "label": "Karaoke", "font": "Poppins", "size": 84, "uppercase": False,
        "primary": "#FFFFFF", "active": "#27E67A", "outline": "#000000",
        "outline_w": 6, "shadow": 2, "box": False, "pop": True,
    },
    "clean": {
        "label": "Clean", "font": "Poppins", "size": 74, "uppercase": False,
        "primary": "#FFFFFF", "active": "#FFFFFF", "outline": "#000000",
        "outline_w": 4, "shadow": 2, "box": False, "pop": False,
    },
    "boxed": {
        "label": "Boxed", "font": "Poppins", "size": 76, "uppercase": True,
        "primary": "#FFFFFF", "active": "#FFD400", "outline": "#000000",
        "outline_w": 0, "shadow": 0, "box": True, "pop": False,
    },
    "neon": {
        "label": "Neon", "font": "Poppins", "size": 82, "uppercase": True,
        "primary": "#FFFFFF", "active": "#00E5FF", "outline": "#0A1F2E",
        "outline_w": 6, "shadow": 4, "box": False, "pop": True,
    },
    "streamer": {
        "label": "Streamer", "font": "Poppins", "size": 88, "uppercase": True,
        "primary": "#FFFFFF", "active": "#B06BFF", "outline": "#000000",
        "outline_w": 7, "shadow": 3, "box": False, "pop": True,
    },
}

# alignment, margin. Top captions sit below the hook band so the two never collide.
POSITIONS = {
    "bottom": (2, 480),
    "middle": (5, 0),
    "top": (8, 430),
    # Where this season's word-pop clips put their words: just below the
    # middle, around 60-65% of the height, over the speaker's chest.
    "pop": (2, 680),
}


def hex_to_ass(value: str) -> str:
    """#RRGGBB -> &H00BBGGRR& (ASS stores colours backwards, with alpha first)."""
    v = (value or "#FFFFFF").lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    r, g, b = v[0:2], v[2:4], v[4:6]
    return f"&H00{b}{g}{r}".upper()


def _t(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


HOOK_TOP = SAFE_TOP + 10   # px from the top of a 1080x1920 frame, before any brand logo offset —
                           # below the apps' own tabs and icons
CAPTION_TOP = 1150      # bottom captions never start above this


def caption_top(position: str, style_name: str = "impact", size_scale: float = 1.0) -> int:
    """The highest a two-line caption reaches at this position, in px."""
    style = STYLES.get(style_name, STYLES["impact"])
    two = int(2 * style["size"] * max(0.6, min(1.6, size_scale)) * 1.2)
    align, margin = POSITIONS.get(position, POSITIONS["bottom"])
    if align == 2:
        return min(CAPTION_TOP, RENDER_H - margin - two)
    if align == 5:
        return RENDER_H // 2 - two // 2
    return RENDER_H                     # top captions: below the hook's band anyway


def hook_block(hook: str, style_name: str = "impact", size_scale: float = 1.0) -> int:
    """About how tall the hook is on screen, in px: its lines times the line height."""
    style = STYLES.get(style_name, STYLES["impact"])
    size = int(style["size"] * max(0.6, min(1.6, size_scale)) * 1.05)
    text = (hook or "").strip()
    if not text:
        return 0
    lines = text.count("\n") + 1 if "\n" in text else max(1, -(-len(text) // 16))
    return int(lines * size * 1.18) + 24


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


MIN_WORD = 0.06   # shortest time a word may hold the highlight


# --- the caption "look": colour by meaning, censoring ---------------------------

MEANING_COLOURS = {"money": "#3DFF6E", "number": "#3DFF6E", "name": "#FFD400",
                   "emphasis": "#FFD400", "danger": "#FF3B3B"}
_MONEY = re.compile(r"^[\"'(]*[$€£]\s*\d|\d[\d,.]*\s*(?:k|m|b|bn|million|billion|thousand)?\b", re.I)
_SWEARS = re.compile(
    r"^(motherfuck\w*|fuck\w*|shit\w*|bullshit|bitch\w*|asshole\w*|dick\w*|pussy|cunt\w*|"
    r"nigg\w*|bastard\w*|damn|goddamn\w*|whore\w*|slut\w*)$", re.I)
_SLURS = re.compile(r"^(nigg\w*|cunt\w*)$", re.I)


def censor(token: str) -> str:
    """SH*T, F*CKING, B*TCH: the first vowel starred, the way clip pages do it,
    so the word still reads. Slurs lose everything but the first letter."""
    m = re.match(r"^([^A-Za-z]*)([A-Za-z]+)(.*)$", token)
    if not m:
        return token
    lead, word, tail = m.groups()
    if not _SWEARS.match(word):
        return token
    if _SLURS.match(word):
        return lead + word[0] + "*" * (len(word) - 1) + tail
    for i, ch in enumerate(word):
        if i and ch.lower() in "aeiou":
            return lead + word[:i] + "*" + word[i + 1:] + tail
    return lead + word[0] + "*" * (len(word) - 1) + tail


def _key(token: str) -> str:
    return re.sub(r"[^\w$€£%]", "", token.lower())


def word_colours(tokens: List[str], look: Dict[str, Any]) -> List[str | None]:
    """The meaning colour of each token (ASS colour), or None for plain white.
    `look["colors"]` maps words or short phrases to a meaning or a #hex."""
    wanted: Dict[str, str] = {}
    for phrase, colour in (look.get("colors") or {}).items():
        hexv = MEANING_COLOURS.get(str(colour).lower(), colour)
        if not re.match(r"^#[0-9A-Fa-f]{6}$", str(hexv)):
            continue
        for part in str(phrase).split():
            if _key(part):
                wanted[_key(part)] = hex_to_ass(hexv)
    money = hex_to_ass(MEANING_COLOURS["money"]) if look.get("auto_colors", True) else None
    out: List[str | None] = []
    for tok in tokens:
        k = _key(tok)
        if k in wanted:
            out.append(wanted[k])
        elif money and _MONEY.search(tok):
            out.append(money)
        else:
            out.append(None)
    return out


def sanitize_words(words: List[Dict[str, Any]], duration: float | None = None) -> List[Dict[str, Any]]:
    """Words in strict time order, none overlapping the next.

    Whisper has no speaker separation. When two people talk over each other it
    returns both voices' words with timings that overlap ("thank" 6.88-7.02
    and "Wait," 6.88-7.60). Captions built straight from that put two lines on
    screen at once, stacked on top of each other. Here each word gives up its
    tail the moment the next one starts, so exactly one word is ever active.
    """
    clean: List[Dict[str, Any]] = []
    ordered = sorted(
        (w for w in words if (w.get("w") or "").strip()),
        key=lambda w: (float(w.get("start", 0)), float(w.get("end", 0))),
    )
    for w in ordered:
        start = max(0.0, float(w["start"]))
        end = float(w["end"])
        if clean:
            prev = clean[-1]
            start = max(start, prev["start"] + MIN_WORD)   # never two words at once
            prev["end"] = min(prev["end"], start)
        end = max(end, start + MIN_WORD)
        if duration is not None:
            if start >= duration:
                break
            end = min(end, duration)
        clean.append({**w, "w": w["w"], "start": round(start, 3), "end": round(end, 3)})
    return clean


_LAUGH = re.compile(r"^[\"'(]*(?:h+a+|a*h+a+h*)+[.!,?)\"']*$", re.I)


def collapse_laughter(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """'HAAAHA HAAAHA HAAAHA HAAAHA' is how the transcriber hears a room
    laughing. One is enough on screen: a run of laughter words becomes a
    single word spanning the run."""
    out: List[Dict[str, Any]] = []
    for w in words:
        token = (w.get("w") or "").strip()
        if out and _LAUGH.match(token) and _LAUGH.match((out[-1].get("w") or "").strip()):
            out[-1] = {**out[-1], "end": max(out[-1]["end"], w["end"])}
            continue
        out.append(w)
    return out


def group_words(
    words: List[Dict[str, Any]],
    max_words: int = 4,
    max_chars: int = 26,
    max_gap: float = 0.7,
) -> List[List[Dict[str, Any]]]:
    """Break the word stream into short caption lines.

    A line breaks on a pause, on sentence punctuation, or when it gets long —
    which is what keeps captions readable instead of a wall of text.
    """
    lines: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for word in words:
        if not word.get("w"):
            continue
        if current:
            gap = word["start"] - current[-1]["end"]
            too_long = len(" ".join(w["w"] for w in current)) + len(word["w"]) > max_chars
            ended = bool(re.search(r"[.!?]$", current[-1]["w"]))
            if gap > max_gap or too_long or len(current) >= max_words or ended:
                lines.append(current)
                current = []
        current.append(word)
    if current:
        lines.append(current)
    return lines


def build_ass(
    words: List[Dict[str, Any]],
    duration: float,
    style_name: str = "impact",
    position: str = "bottom",
    size_scale: float = 1.0,
    hook: str = "",
    hook_seconds: float = 2.4,
    accent: str = "",
    out_path: Path | None = None,
    labels: List[tuple] | None = None,
    headline: str = "",
    top_offset: int = 0,
    hook_top: int | None = None,
    look: Dict[str, Any] | None = None,
) -> Path:
    """`top_offset` moves everything that sits at the top (hook, headline,
    stitch labels, top captions) down by that many pixels — room for a
    campaign's brand logo drawn above them."""
    style = dict(STYLES.get(style_name, STYLES["impact"]))
    look = look or {}
    if accent:
        style["active"] = accent          # brand kit overrides the style's highlight
    if look.get("karaoke") is False:
        style["active"] = style["primary"]   # no running highlight: only meaning colours
    align, margin_v = POSITIONS.get(position, POSITIONS["bottom"])
    off = max(0, int(top_offset or 0))
    if align == 8:
        margin_v += off
    size = int(style["size"] * max(0.6, min(1.6, size_scale)))

    primary = hex_to_ass(style["primary"])
    active = hex_to_ass(style["active"])
    outline = hex_to_ass(style["outline"])
    border_style = 4 if style["box"] else 1
    back = "&H99000000" if style["box"] else "&H80000000"
    outline_w = 12 if style["box"] else style["outline_w"]

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {RENDER_W}
PlayResY: {RENDER_H}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{style['font']},{size},{primary},{primary},{outline},{back},-1,0,0,0,100,100,1,0,{border_style},{outline_w},{style['shadow']},{align},{SAFE_LEFT + 40},{SAFE_RIGHT},{margin_v},1
Style: Hook,{style['font']},{int(size * 1.05)},&H00FFFFFF,&H00FFFFFF,&H00000000,&HB0000000,-1,0,0,0,100,100,1,0,3,10,0,8,80,80,{HOOK_TOP + off},1
Style: Headline,{style['font']},44,&H00FFFFFF,&H00FFFFFF,&H70000000,&H70000000,-1,0,0,0,100,100,1,0,3,12,0,8,60,60,{HOOK_TOP + off},1
Style: Label,{style['font']},66,&H00FFFFFF,&H00FFFFFF,{active},{active},-1,0,0,0,100,100,2,0,3,16,0,8,80,80,{330 + off},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events: List[str] = []

    # The premise, small at the top for the whole clip once the hook has gone:
    # a viewer who lands mid-clip still knows what this is.
    if headline.strip():
        text = _escape(headline.strip().upper())
        begin = min(hook_seconds, duration) if hook.strip() else 0.0
        if duration - begin > 0.5:
            events.append(f"Dialogue: 2,{_t(begin)},{_t(duration)},Headline,,0,0,0,,"
                          f"{{\\fad(250,0)}}{text}")

    # A jump in time between stitched parts, marked the moment it happens.
    for at, label in (labels or []):
        label = (label or "").strip()
        if not label or at >= duration - 0.3:
            continue
        begin = max(at, min(hook_seconds, duration) if hook.strip() and at < hook_seconds else at)
        events.append(f"Dialogue: 3,{_t(begin)},{_t(min(duration, begin + 1.6))},Label,,0,0,0,,"
                      f"{{\\fad(120,200)\\t(0,140,\\fscx108\\fscy108)\\t(140,260,\\fscx100\\fscy100)}}{_escape(label.upper())}")

    if hook.strip():
        text = _escape(hook.strip().upper() if style["uppercase"] else hook.strip())
        # A line break typed into the hook is kept, so "$10M ARR" never splits.
        text = re.sub(r"\s*\n\s*", r"\\N", text)
        # No fade in: the very first frame is the one the feed shows before the
        # video plays (and the default cover), so the hook is already there.
        events.append(
            f"Dialogue: 1,{_t(0)},{_t(min(hook_seconds, duration))},Hook,,0,0,{int(hook_top or 0)},,"
            f"{{\\fad(0,220)}}{text}"
        )

    max_words = int(look.get("max_words") or 4)
    lines = group_words(collapse_laughter(sanitize_words(words, duration)),
                        max_words=max(1, min(6, max_words)), max_chars=26 if max_words > 2 else 18)
    for n, line in enumerate(lines):
        tokens = [w["w"] for w in line]
        if look.get("censor", True):
            tokens = [censor(t) for t in tokens]
        colours = word_colours(tokens, look)
        if style["uppercase"]:
            tokens = [t.upper() for t in tokens]
        # A line may linger after its last word, but never into the next line —
        # two caption events alive at once is what stacked them on screen.
        next_start = lines[n + 1][0]["start"] if n + 1 < len(lines) else duration
        for i, word in enumerate(line):
            start = word["start"]
            end = line[i + 1]["start"] if i + 1 < len(line) else max(word["end"], start + 0.12)
            end = min(end, next_start, duration)
            if end <= start:
                continue
            parts = []
            for j, token in enumerate(tokens):
                token = _escape(token)
                own = colours[j]
                if j == i:
                    pop = "{\\t(0,90,\\fscx112\\fscy112)}" if style["pop"] else ""
                    parts.append(f"{{\\c{own or active}}}{pop}{token}{{\\c{primary}\\fscx100\\fscy100}}")
                elif own:
                    parts.append(f"{{\\c{own}}}{token}{{\\c{primary}}}")
                else:
                    parts.append(token)
            events.append(
                f"Dialogue: 0,{_t(start)},{_t(end)},Main,,0,0,0,, " + " ".join(parts)
            )

    out_path = out_path or Path("captions.ass")
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


def style_catalogue() -> List[Dict[str, Any]]:
    """The full style definitions, so the editor can mirror them in the browser."""
    return [{"id": key, **value} for key, value in STYLES.items()]


def grouping_rules() -> Dict[str, float]:
    """The line-breaking numbers, shared with the editor's live preview."""
    return {"max_words": 4, "max_chars": 26, "max_gap": 0.7}
