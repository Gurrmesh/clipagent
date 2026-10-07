"""On-screen cards drawn as images: the headline label, the title bar and the
viewer-voice comment bubble — the text styles this season's winning clips use
that subtitles can't draw (rounded boxes, colour emoji).

Each card is a transparent PNG at the size it appears on the 1080x1920
frame; the renderer lays it over the video with ffmpeg.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import BASE_DIR, RENDER_W, WORK_DIR

FONTS = BASE_DIR / "fonts"
BOLD = FONTS / "Poppins-Bold.ttf"
REGULAR = FONTS / "Poppins-Regular.ttf"
EMOJI_FONTS = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "seguiemj.ttf",
               Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
               Path("/System/Library/Fonts/Apple Color Emoji.ttc")]
CARD_DIR = WORK_DIR / "cards"

HIGHLIGHT = (255, 212, 0, 255)

_EMOJI = re.compile(
    "([\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\u2300-\u23FF\uFE0F\u200D"
    "\U0001F1E6-\U0001F1FF]+)")


def available() -> bool:
    try:
        import PIL  # noqa: F401
        return BOLD.exists()
    except ImportError:
        return False


def _font(path: Path, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(str(path), size)


def _emoji_font(size: int):
    from PIL import ImageFont
    for p in EMOJI_FONTS:
        if p.exists():
            try:
                # Noto's bitmap emoji only come at 109 px; others scale freely.
                return ImageFont.truetype(str(p), 109 if "Noto" in p.name else size), p
            except OSError:
                continue
    return None, None


def _runs(text: str) -> List[Tuple[str, bool]]:
    """Text split into (piece, is_emoji) runs."""
    out = []
    for piece in _EMOJI.split(text):
        if piece:
            out.append((piece, bool(_EMOJI.fullmatch(piece))))
    return out


class _Writer:
    """Measures and draws text that mixes a text font with colour emoji."""

    def __init__(self, font_path: Path, size: int):
        self.size = size
        self.font = _font(font_path, size)
        self.efont, self.epath = _emoji_font(size)
        self.escale = size / 109 if self.epath and "Noto" in self.epath.name else 1.0

    def _tile(self, piece: str):
        """The emoji run drawn in colour, cropped and scaled to the text size."""
        from PIL import Image, ImageDraw
        piece = piece.strip()
        if not piece or not self.efont:
            return None
        cache = self.__dict__.setdefault("_tiles", {})
        if piece not in cache:
            em = 109 if self.escale != 1.0 else self.size
            tile = Image.new("RGBA", (em * (len(piece) + 1) + 40, em * 2), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((10, 10), piece, font=self.efont, embedded_color=True)
            box = tile.getbbox()
            if box:
                tile = tile.crop(box)
                target = int(self.size * 0.95)
                if tile.height and abs(tile.height - target) > 2:
                    tile = tile.resize((max(1, int(tile.width * target / tile.height)), target),
                                       Image.LANCZOS)
                cache[piece] = tile
            else:
                cache[piece] = None
        return cache[piece]

    def _pad(self) -> int:
        return int(self.size * 0.06)

    def width(self, text: str) -> int:
        w = 0
        for piece, emo in _runs(text):
            if emo:
                tile = self._tile(piece)
                w += tile.width + 2 * self._pad() if tile else 0
            else:
                w += int(self.font.getlength(piece))
        return w

    def draw(self, img, x: int, y: int, text: str, fill, colours: Optional[List[Any]] = None) -> None:
        """Draw at (x, y) = left, top of the line box. `colours` (one per word)
        overrides the fill for highlighted words."""
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        cx = x
        words = text.split(" ")
        for wi, word in enumerate(words):
            colour = (colours[wi] if colours and wi < len(colours) and colours[wi] else fill)
            for piece, emo in _runs(word + (" " if wi < len(words) - 1 else "")):
                if emo:
                    tile = self._tile(piece)
                    if tile:
                        ty = y + max(0, (int(self.size * 1.22) - tile.height) // 2)
                        img.alpha_composite(tile, (cx + self._pad(), ty))
                        cx += tile.width + 2 * self._pad()
                else:
                    draw.text((cx, y + int(self.size * 0.06)), piece, font=self.font, fill=colour)
                    cx += int(self.font.getlength(piece))


def _wrap(writer: _Writer, text: str, max_w: int) -> List[str]:
    lines: List[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            test = (line + " " + word).strip()
            if line and writer.width(test) > max_w:
                lines.append(line)
                line = word
            else:
                line = test
        if line:
            lines.append(line)
    return lines or [""]


def _save(img, kind: str, key: str) -> Path:
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{kind}_{hashlib.md5(key.encode('utf-8')).hexdigest()[:12]}.png"
    path = CARD_DIR / name
    img.save(path)
    return path


def _rounded(size: Tuple[int, int], radius: int, fill) -> Any:
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=fill)
    return img


def label(text: str, max_w: int = 900, size: int = 50) -> Dict[str, Any]:
    """The clip-page headline label: black bold words on a white rounded box,
    emoji in colour. "Fousey was SHOCKED after ... 😳"."""
    text = re.sub(r"\s+", " ", text or "").strip()
    w = _Writer(BOLD, size)
    pad_x, pad_y = 30, 20
    lines = _wrap(w, text, max_w - 2 * pad_x)
    if len(lines) > 4:                                   # too long: smaller, never cut
        return label(text, max_w, int(size * 0.86))
    line_h = int(size * 1.22)
    inner_w = max(w.width(l) for l in lines)
    box = (inner_w + 2 * pad_x, line_h * len(lines) + 2 * pad_y)
    img = _rounded(box, 26, (255, 255, 255, 255))
    for i, line in enumerate(lines):
        x = (box[0] - w.width(line)) // 2
        w.draw(img, x, pad_y + i * line_h, line, (0, 0, 0, 255))
    return {"path": _save(img, "label", f"{text}|{size}|{max_w}"), "w": box[0], "h": box[1]}


def title_bar(text: str, highlight: str = "", size: int = 58) -> Dict[str, Any]:
    """A headline that stays up for the whole clip: white caps on a dark band,
    one word or phrase in yellow."""
    text = re.sub(r"\s+", " ", text or "").strip().upper()
    marks = {m.upper() for m in re.findall(r"\[([^\]]+)\]", text)} | (
        {highlight.upper()} if highlight else set())
    text = re.sub(r"[\[\]]", "", text)
    w = _Writer(BOLD, size)
    pad_x, pad_y = 40, 24
    lines = _wrap(w, text, RENDER_W - 2 * pad_x - 40)
    if len(lines) > 3:
        return title_bar(text, highlight, int(size * 0.86))
    hot = set()
    for m in marks:
        hot.update(m.split())
    line_h = int(size * 1.2)
    box = (RENDER_W - 40, line_h * len(lines) + 2 * pad_y)
    img = _rounded(box, 18, (10, 10, 12, 225))
    for i, line in enumerate(lines):
        x = (box[0] - w.width(line)) // 2
        colours = [HIGHLIGHT if re.sub(r"[^\w$%]", "", word) in {re.sub(r"[^\w$%]", "", h) for h in hot}
                   else None for word in line.split(" ")]
        w.draw(img, x, pad_y + i * line_h, line, (255, 255, 255, 255), colours)
    return {"path": _save(img, "title", f"{text}|{sorted(marks)}|{size}"), "w": box[0], "h": box[1]}


def bubble(text: str, handle: str = "viewer", size: int = 50, max_w: int = 920) -> Dict[str, Any]:
    """A comment in the viewer's voice, like a reply sticker: avatar, handle,
    the comment. "There ain't no way he actually said that 😭"."""
    from PIL import ImageDraw
    text = re.sub(r"\s+", " ", text or "").strip()
    w = _Writer(BOLD, size)
    small = _Writer(REGULAR, int(size * 0.62))
    pad, av = 26, int(size * 1.5)
    text_x = pad + av + 22
    lines = _wrap(w, text, max_w - text_x - pad)
    if len(lines) > 4:
        return bubble(text, handle, int(size * 0.86), max_w)
    line_h = int(size * 1.22)
    head_h = int(small.size * 1.5)
    inner_w = max([w.width(l) for l in lines] + [small.width("@" + handle)])
    box = (min(max_w, text_x + inner_w + pad), pad + head_h + line_h * len(lines) + pad)
    img = _rounded(box, 30, (255, 255, 255, 250))
    d = ImageDraw.Draw(img)
    d.ellipse([pad, pad, pad + av, pad + av], fill=(255, 64, 129, 255))
    d.ellipse([pad + av * 0.3, pad + av * 0.18, pad + av * 0.7, pad + av * 0.58], fill=(255, 255, 255, 230))
    d.pieslice([pad + av * 0.12, pad + av * 0.55, pad + av * 0.88, pad + av * 1.3], 180, 360,
               fill=(255, 255, 255, 230))
    small.draw(img, text_x, pad - 4, "@" + handle, (120, 120, 128, 255))
    for i, line in enumerate(lines):
        w.draw(img, text_x, pad + head_h + i * line_h, line, (17, 17, 17, 255))
    return {"path": _save(img, "bubble", f"{text}|{handle}|{size}"), "w": box[0], "h": box[1]}


KINDS = {"label": label, "title": title_bar, "bubble": bubble}


def render(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One card from the edits ({"kind", "text", ...}) to a PNG, or None."""
    kind = card.get("kind")
    text = (card.get("text") or "").strip()
    if kind not in KINDS or not text or not available():
        return None
    if kind == "title":
        return title_bar(text, card.get("highlight", ""))
    if kind == "bubble":
        return bubble(text, card.get("handle") or "viewer")
    return label(text)
