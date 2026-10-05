"""A campaign's brand logo on screen.

Some briefs require the brand's own logo in every post ("the logo must appear
somewhere visible on screen", "missing or hidden logo = rejected"). That is a
different thing from your own watermark, which most briefs forbid. This module
prepares the brand's file, decides where it goes, and checks afterwards that
the finished clip really shows it.

The file a brand hands out is often a full 9:16 canvas with the logo floating
in the middle of a transparent sheet, and often a dark logo meant for light
backgrounds. So the logo is cut to what is actually drawn, scaled evenly
(never stretched), and given a soft halo in the opposite tone — the logo's own
pixels are left exactly as they are, the halo only sits behind them, so a
black logo stays readable over a dark arena and a white one over a bright set.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .config import DATA_DIR, WORK_DIR

LOGO_DIR = DATA_DIR / "brand" / "campaigns"
LOGO_DIR.mkdir(parents=True, exist_ok=True)
EXTENSIONS = {".png", ".webp", ".jpg", ".jpeg"}

# How big, as a share of the frame. "Clearly visible — don't make it too small"
# is the usual wording; a wide wordmark gets ~46% of the width, a square badge
# is held back by the height cap so it doesn't swallow the picture.
OVERLAY_MAX_W, OVERLAY_MAX_H = 0.46, 0.085
SOURCE_MAX_W, SOURCE_MAX_H = 0.36, 0.065
# Clip-from-source clips put the logo at the very top as a header, and push
# the hook, headline and top captions down by its height (captions.build_ass).
SOURCE_TOP = 0.072
GAP = 0.010


def path_for(campaign_id: str) -> Path:
    return LOGO_DIR / f"{campaign_id}.png"


def exists(campaign_id: str) -> bool:
    return bool(campaign_id) and path_for(campaign_id).exists()


def save(campaign_id: str, upload_path: Path) -> Path:
    """Store an uploaded logo as the campaign's PNG (whatever it came as)."""
    import cv2
    img = cv2.imread(str(upload_path), cv2.IMREAD_UNCHANGED)
    if img is None or img.ndim < 2:
        raise ValueError("That file isn't an image ClipAgent can read — use a PNG with a transparent background.")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    if img.dtype != np.uint8:                       # 16-bit PNGs
        img = (img / 257).astype(np.uint8)
    if not (img[:, :, 3] > 8).any():
        raise ValueError("That image is completely transparent.")
    # Brands often ship the logo floating on a full 9:16 transparent sheet; keep
    # just what's drawn (the logo's pixels are untouched), so the card's preview shows it.
    ys, xs = np.where(img[:, :, 3] > 8)
    pad = 4
    img = img[max(0, ys.min() - pad):ys.max() + pad + 1, max(0, xs.min() - pad):xs.max() + pad + 1]
    out = path_for(campaign_id)
    cv2.imwrite(str(out), img)
    return out


def remove(campaign_id: str) -> None:
    try:
        path_for(campaign_id).unlink()
    except FileNotFoundError:
        pass


def _load(src: Path) -> np.ndarray:
    import cv2
    img = cv2.imread(str(src), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError(f"Couldn't read the logo at {src}")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    return img


def tone(rgba: np.ndarray) -> str:
    """'dark', 'light' or 'mid': how bright the logo's own drawn pixels are."""
    a = rgba[:, :, 3] > 128
    if not a.any():
        return "mid"
    b, g, r = (rgba[:, :, i][a].astype(np.float32) for i in range(3))
    luma = float(np.mean(0.299 * r + 0.587 * g + 0.114 * b))
    return "dark" if luma < 90 else "light" if luma > 170 else "mid"


_CACHE: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def prepare(src: Path, canvas: Tuple[int, int], max_w: float, max_h: float,
            out_dir: Path = WORK_DIR) -> Dict[str, Any]:
    """The logo as it will be drawn: cut to its drawn pixels, scaled evenly to
    fit max_w x max_h of the canvas, with a halo behind it. Returns the PNG and
    its size, plus a mask of where the logo itself (not the halo) is opaque —
    what the gate looks for in the finished clip. Clips render in parallel, so
    each logo is prepared once and shared."""
    src = Path(src)
    W, H = canvas
    stamp = f"{src.stem}_{int(src.stat().st_mtime)}_{src.stat().st_size}_{W}x{H}_{int(max_w * 1000)}_{int(max_h * 1000)}"
    with _LOCK:
        hit = _CACHE.get(stamp)
        if hit and Path(hit["png"]).exists():
            return hit
        made = _prepare(src, canvas, max_w, max_h, out_dir / f"logo_{stamp}.png")
        _CACHE[stamp] = made
        return made


def _prepare(src: Path, canvas: Tuple[int, int], max_w: float, max_h: float, png: Path) -> Dict[str, Any]:
    import cv2
    W, H = canvas
    rgba = _load(src)
    alpha = rgba[:, :, 3]
    ys, xs = np.where(alpha > 8)
    crop = rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    ch, cw = crop.shape[:2]
    scale = min(W * max_w / cw, H * max_h / ch)
    w, h = max(2, int(round(cw * scale))), max(2, int(round(ch * scale)))
    crop = cv2.resize(crop, (w, h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    kind = tone(crop)

    m = max(4, int(round(w * 0.03)))                 # room for the halo
    a_logo = np.zeros((h + 2 * m, w + 2 * m), np.float32)
    a_logo[m:m + h, m:m + w] = crop[:, :, 3] / 255.0
    k = max(3, (m // 2) * 2 + 1)
    glow = cv2.dilate(a_logo, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    strength, colour = (0.85, 255.0) if kind == "dark" else (0.55, 0.0)
    ga = cv2.GaussianBlur(glow, (0, 0), m / 2.2) * strength
    fa = a_logo
    rgb = np.zeros(a_logo.shape + (3,), np.float32)
    rgb[m:m + h, m:m + w] = crop[:, :, :3]
    a_out = fa + ga * (1 - fa)
    col = (rgb * fa[..., None] + colour * (ga * (1 - fa))[..., None]) / np.maximum(a_out, 1e-6)[..., None]
    out = np.dstack([np.clip(col, 0, 255), np.clip(a_out * 255, 0, 255)]).astype(np.uint8)
    # Even sides: 4:2:0 video can't place or cut an odd-sized patch exactly.
    ph, pw = out.shape[0] % 2, out.shape[1] % 2
    if ph or pw:
        out = np.pad(out, ((0, ph), (0, pw), (0, 0)))
        a_logo = np.pad(a_logo, ((0, ph), (0, pw)))
        rgb = np.pad(rgb, ((0, ph), (0, pw), (0, 0)))

    cv2.imwrite(str(png), out)
    solid = (a_logo > 0.95).astype(np.uint8)
    return {"png": png, "w": out.shape[1], "h": out.shape[0], "solid": solid, "tone": kind,
            "logo_rgb": rgb.astype(np.uint8), "rgba": out}


# --- where it goes ---------------------------------------------------------------

def place_overlay(layout: Dict[str, Any], hook_box: Optional[Tuple[int, int, int, int]],
                  hook_position: str, lw: int, lh: int) -> Tuple[int, int, str]:
    """(x, y, why) for a clip-bank clip. Next to the hook rather than somewhere
    new: the hook's spot was already chosen to keep clear of the clip's own
    text and faces, so the logo joins it — under it, or above it when the hook
    sits low (below a low hook is where TikTok's caption covers the picture).
    When the clip is fitted inside bars, the logo takes the bar the hook isn't in."""
    W, H = layout["canvas"]
    x0, y0, fw, fh = layout["content"]
    cx = (W - lw) // 2
    gap = int(H * GAP)
    hook_in_top_bar = hook_box is not None and hook_box[3] <= y0
    if not layout.get("vertical"):
        top_bar, bottom_bar = y0, H - (y0 + fh)
        if hook_box is not None and hook_in_top_bar and bottom_bar >= lh + 2 * gap:
            return cx, y0 + fh + 2 * gap, "in the bar under the footage, so it covers none of it"
        if (hook_box is None or not hook_in_top_bar) and top_bar >= lh + 2 * gap:
            return cx, max(int(H * 0.08), y0 - lh - 2 * gap), "in the bar above the footage, so it covers none of it"
    if hook_box is None:
        return cx, int(H * 0.10), "at the top"
    hx0, hy0, hx1, hy1 = hook_box
    if hook_position == "bottom" or hy1 > H * 0.62:
        return cx, max(int(H * 0.08), hy0 - lh - gap), "right above the hook"
    return cx, min(H - lh - int(H * 0.2), hy1 + gap), "right under the hook"


def source_box(canvas: Tuple[int, int], lw: int, lh: int) -> Tuple[int, int]:
    W, H = canvas
    return (W - lw) // 2, int(H * SOURCE_TOP)


def source_text_offset(canvas: Tuple[int, int], lh: int, headline_margin: int = 70) -> int:
    """How far the top text (headline, hook, labels) moves down to clear the header logo."""
    W, H = canvas
    return max(0, int(H * SOURCE_TOP) + lh + int(H * GAP) - headline_margin)


def hook_box(ass_path: str, canvas: Tuple[int, int], fonts_dir: Path, at: float = 0.6) -> Optional[Tuple[int, int, int, int]]:
    """Where the hook's pixels actually land (x0, y0, x1, y1), drawn alone on black."""
    if not ass_path or not Path(ass_path).exists():
        return None
    W, H = canvas
    esc = lambda p: str(p).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=25:d={at + 0.5:.2f}",
         "-vf", f"subtitles='{esc(ass_path)}':fontsdir='{esc(fonts_dir)}',format=gray",
         "-ss", f"{at:.2f}", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) < W * H:
        return None
    img = np.frombuffer(proc.stdout[:W * H], np.uint8).reshape(H, W)
    ys, xs = np.where(img > 40)
    if not len(ys):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


# --- the check -------------------------------------------------------------------

def visible(output: Path, prepared: Dict[str, Any], box: Tuple[int, int], duration: float,
            frames: int = 0) -> Dict[str, Any]:
    """Is the logo really there, start to end? Frames near the start, middle and
    end of the finished clip are cut at the logo's spot and compared with the
    logo as drawn (halo included). Two tests, because each alone can be fooled:
      pattern  the light/dark shape of logo-plus-halo must show up (correlation);
               footage behind an absent logo doesn't make that shape
      colour   the logo's own solid pixels must come out as its colours — drawn
               on top of everything, the footage behind can't change them"""
    import cv2
    from .overlay import _frames_at
    x, y = box
    img = prepared["rgba"]
    h, w = img.shape[:2]
    alpha = img[:, :, 3].astype(np.float32) / 255.0
    shape = alpha >= 0.6
    solid = prepared["solid"].astype(bool)
    inner = cv2.erode(prepared["solid"], np.ones((5, 5), np.uint8)).astype(bool)
    use = inner if inner.sum() >= 200 else solid
    if use.sum() < 30 or shape.sum() < 60:
        return {"ok": None, "error": "The logo has almost no solid pixels to look for."}
    luma = lambda bgr: 0.114 * bgr[..., 0] + 0.587 * bgr[..., 1] + 0.299 * bgr[..., 2]
    want_l = luma(img[:, :, :3].astype(np.float32))[shape]
    want_c = img[:, :, 2::-1].astype(np.float32)                     # BGR -> RGB
    n = max(3, frames or int(duration * 30))
    idx = sorted({min(n - 1, max(0, int(n * f))) for f in (0.04, 0.5, 0.96)})
    try:
        got = _frames_at(output, idx, f"crop={w}:{h}:{x}:{y},", (w, h))
    except Exception as exc:
        return {"ok": None, "error": str(exc)[:160]}
    if len(got) != len(idx):
        return {"ok": None, "error": f"Read {len(got)} of {len(idx)} frames."}
    corr, diffs = [], []
    for f in got:
        f = f.astype(np.float32)
        o = 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]
        o = o[shape]
        if o.std() < 1e-3 or want_l.std() < 1e-3:
            corr.append(0.0)
        else:
            corr.append(float(np.corrcoef(o, want_l)[0, 1]))
        diffs.append(float(np.mean(np.abs(f[use] - want_c[use]))))
    ok = min(corr) >= 0.7 and max(diffs) < 28.0
    return {"ok": ok, "pattern": [round(c, 2) for c in corr], "diff": [round(d, 1) for d in diffs], "frames": idx}
