"""Draw an edit: the timeline from edits.build_timeline → a finished 1080×1920 video.

Two stages, one final encode:

A. The picture, frame by frame in Python (OpenCV, BGR): each segment's
   footage is read straight from its source (cropped to what the 9:16 window
   can use), retimed by its speed curve (blended or optical-flow frames in
   slow-mo, a little smear when it runs fast), framed on the face, zoomed,
   shaken, glitched, colour graded (every moment's levels matched first, then
   one grade over everything, so different videos look like one film),
   flashed, dipped to black, and dissolved back into its first frame at the
   end so it loops. Frames are piped straight into stage B.
B. One ffmpeg command: saturation, dark corners, grain, cinema bars and the
   words (ASS, Anton/Poppins), plus the sound — his voice and the song,
   mixed beforehand and brought to -14 LUFS — encoded with x264.
"""
from __future__ import annotations

import math
import re
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import numpy as np

from . import edits, motion
from .config import SAFE_BOTTOM, SAFE_LEFT, SAFE_RIGHT, SAFE_TOP, WORK_DIR

OW, OH = 1080, 1920
FPS = edits.FPS
FONTS_DIR = motion.FONTS_DIR
BAR_H = 230                       # cinema bars, top and bottom
TARGET_LUFS = -14.0

# --- the look, in numbers ---------------------------------------------------------------
PULSE = {"velocity": 0.075, "flow": 0.045}            # zoom punch size per beat
PULSE_K = 9.0                                          # how fast a punch settles
FUNNY_PUNCH = 0.22                                     # Funny: 1.0 → 1.22 in 80 ms, back over 400 ms
SHAKE = {"velocity": 20.0, "flow": 10.0, "funny": 16.0}
PUSH = {"aura": 0.09, "cinematic": 0.06, "motivation": 0.07, "money": 0.07}
FLASH_ALPHA, FLASH_K = 0.85, 14.0
GLITCH_SECONDS = 0.12
DIP_SECONDS = 0.15
VIGNETTE = {"aura": "PI/3.6", "motivation": "PI/3.8"}  # stronger dark corners for these; else PI/4.6
# Film grain: new on every frame, on the brightness only. Colour speckle costs the most
# bits, turns to smudges first under the bitrate ceiling (motion.VIDEO_CAP), and isn't
# how film grain looks — it even put colour dots into the black-and-white Motivation look.
GRAIN = "noise=c0s=7:c0f=t"

# grade: per-channel curves (lift, gamma, gain, as 0..1) for B, G, R + contrast + saturation
GRADES: Dict[str, Dict[str, Any]] = {
    "none": {"sat": 1.0},
    "punchy": {"contrast": 0.30, "lift": (-0.045, -0.045, -0.045), "gain": (1.04, 1.04, 1.05), "sat": 1.28},
    "film": {"contrast": 0.10, "lift": (0.035, 0.012, -0.005), "gain": (0.93, 0.99, 1.04),
             "gamma": (1.0, 1.0, 0.97), "sat": 0.90},
    "tealorange": {"contrast": 0.22, "lift": (0.05, 0.015, -0.03), "gain": (0.88, 0.99, 1.07),
                   "gamma": (1.02, 1.0, 0.95), "sat": 1.12},
    "teal": {"contrast": 0.24, "lift": (0.045, 0.02, -0.03), "gain": (0.93, 0.96, 1.0),
             "gamma": (1.12, 1.14, 1.16), "sat": 0.74},
    "mono": {"contrast": 0.34, "lift": (-0.03, -0.03, -0.03), "gain": (1.05, 1.05, 1.05), "sat": 0.0},
    "gold": {"contrast": 0.14, "lift": (-0.02, 0.01, 0.03), "gain": (0.84, 1.0, 1.08),
             "gamma": (1.08, 0.98, 0.94), "sat": 1.06},
}


# --- sources -------------------------------------------------------------------------------

@dataclass
class Source:
    path: Path
    w: int                     # as decoded (after any halving)
    h: int
    fps: Fraction
    vfr: bool
    has_audio: bool
    duration: float
    scale: Optional[Tuple[int, int]]     # decode at this size (big sources), else None


def probe(path: Path) -> Source:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                          str(path)], capture_output=True, text=True).stdout
    import json
    info = json.loads(out or "{}")
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError(f"“{path.name}” has no picture ClipAgent can read")
    w, h = int(video["width"]), int(video["height"])
    rot = 0
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    rot = rot or int((video.get("tags") or {}).get("rotate") or 0)
    if abs(rot) % 180 == 90:                          # phone video: ffmpeg turns it upright when decoding
        w, h = h, w
    scale = None
    if min(w, h) > 1080:                              # 4K and up: decode at half size (the crop is upscaled anyway)
        k = 1080 / min(w, h)
        scale = (int(w * k) // 2 * 2, int(h * k) // 2 * 2)
    dw, dh = scale or (w - w % 2, h - h % 2)
    return Source(path=path, w=dw, h=dh, fps=motion.frame_rate(path), vfr=motion._is_vfr(path),
                  has_audio=any(s.get("codec_type") == "audio" for s in info.get("streams", [])),
                  duration=float((info.get("format") or {}).get("duration") or 0), scale=scale)


class Reader:
    """Raw BGR frames of one stretch of a source, cropped to the columns the
    9:16 window can reach, read forward with the last few kept for blending."""

    def __init__(self, src: Source, first: int, count: int, crop: Tuple[int, int]):
        self.src, self.first = src, first
        self.x0, self.cw = crop
        self.frame_bytes = self.cw * src.h * 3
        ss = max(0.0, float((Fraction(first) - Fraction(1, 2)) / src.fps))
        filters = []
        if src.vfr:
            filters.append(f"fps={src.fps.numerator}/{src.fps.denominator}")
        if src.scale:
            filters.append(f"scale={src.w}:{src.h}:flags=area")
        if self.cw < src.w:
            filters.append(f"crop={self.cw}:{src.h}:{self.x0}:0")
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{ss:.6f}", "-i", str(src.path),
               "-frames:v", str(count), "-an", "-sn", "-dn", "-fps_mode", "passthrough"]
        if filters:
            cmd += ["-vf", ",".join(filters)]
        cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=self.log, bufsize=self.frame_bytes * 2)
        self.next = first
        self.kept: Deque[Tuple[int, np.ndarray]] = deque(maxlen=4)
        self.last: Optional[np.ndarray] = None

    def _read(self) -> Optional[np.ndarray]:
        buf = bytearray(self.frame_bytes)
        view = memoryview(buf)
        got = 0
        while got < self.frame_bytes:
            n = self.proc.stdout.readinto(view[got:])
            if not n:
                return None
            got += n
        return np.frombuffer(buf, np.uint8).reshape(self.src.h, self.cw, 3)

    def get(self, i: int) -> np.ndarray:
        """Source frame i (absolute index). Past the end of the file: the last frame."""
        i = max(i, self.first)
        for k, fr in self.kept:
            if k == i:
                return fr
        while self.next <= i:
            fr = self._read()
            if fr is None:
                break
            self.kept.append((self.next, fr))
            self.last = fr
            self.next += 1
        for k, fr in self.kept:
            if k == i:
                return fr
        if self.last is None:
            raise RuntimeError(f"Couldn't read the picture of “{self.src.path.name}” at {i / float(self.src.fps):.1f} s")
        return self.last

    def close(self) -> None:
        try:
            self.proc.stdout.close()
        except OSError:
            pass
        self.proc.kill()
        self.proc.wait()
        self.log.close()


# --- framing and matching -------------------------------------------------------------------

def _still(src: Source, t: float, width: int = 640) -> Optional[np.ndarray]:
    """One small frame at time t (a fast keyframe seek, then decode to the instant)."""
    height = int(round(width * src.h / src.w / 2)) * 2
    proc = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", str(src.path),
                           "-frames:v", "1", "-vf", f"scale={width}:{height}:flags=area", "-f", "rawvideo",
                           "-pix_fmt", "bgr24", "-"], capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) < width * height * 3:
        return None
    return np.frombuffer(proc.stdout[:width * height * 3], np.uint8).reshape(height, width, 3)


def look_at(src: Source, start: float, end: float, count: int = 4) -> Dict[str, Any]:
    """Where the face is in this moment (decoded pixels) and how its picture
    looks (per-channel mean, contrast) so every moment can be matched."""
    import cv2
    from . import framing
    detect, _ = framing._detector()
    span = max(0.2, end - start)
    count = 3 if span < 4 else count
    faces, means, stds = [], [], []
    for k in range(count):
        frame = _still(src, start + span * (k + 0.5) / count)
        if frame is None:
            continue
        fh, fw = frame.shape[:2]
        small = cv2.resize(frame, (160, int(160 * fh / fw)), interpolation=cv2.INTER_AREA)
        means.append(small.reshape(-1, 3).mean(axis=0))
        stds.append(float(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).std()))
        found = [(x, y, w, h) for (x, y, w, h) in detect(frame) if w / fw >= framing.MIN_FACE_FRACTION]
        if found:
            x, y, w, h = max(found, key=lambda f: f[2] * f[3])
            faces.append(((x + w / 2) / fw, (y + h / 2) / fh, h / fh))
    if faces:
        fx = float(np.median([f[0] for f in faces]))
        fy = float(np.median([f[1] for f in faces]))
        fs = float(np.median([f[2] for f in faces]))
    else:
        fx, fy, fs = 0.5, 0.42, 0.0
    return {"cx": fx * src.w, "cy": fy * src.h, "face_h": fs * src.h, "found": bool(faces),
            "mean": np.mean(means, axis=0) if means else np.array([110.0, 110.0, 110.0]),
            "std": float(np.mean(stds)) if stds else 50.0}


def _curve(x: np.ndarray, contrast: float) -> np.ndarray:
    """An S-curve around mid-grey: contrast 0 = none."""
    if not contrast:
        return x
    s = 1.0 / (1.0 + np.exp(-(x - 0.5) * (4.0 + 10.0 * contrast)))
    lo = 1.0 / (1.0 + math.exp(0.5 * (4.0 + 10.0 * contrast)))
    s = (s - lo) / (1 - 2 * lo)
    return x + (s - x) * min(1.0, contrast * 2.2)


def match_target(looks: List[Dict[str, Any]]) -> np.ndarray:
    """The colour every moment is matched to: the edit's own average cast, half
    way to neutral, at the average brightness nudged towards a healthy exposure."""
    means = np.array([lk["mean"] for lk in looks], np.float64) / 255.0
    avg = means.mean(axis=0)
    grey = float(avg.mean())
    target = avg * 0.5 + grey * 0.5
    bright = grey * 0.7 + 0.45 * 0.3
    return target * (bright / max(1e-3, float(target.mean())))


def grade_lut(name: str, look: Optional[Dict[str, Any]] = None, target: Optional[np.ndarray] = None) -> np.ndarray:
    """One 256-step lookup per channel (B, G, R): this moment's colour and
    contrast matched to the rest of the edit, then the edit's grade on top."""
    g = GRADES.get(name, GRADES["none"])
    x = np.arange(256, dtype=np.float64) / 255.0
    chans = []
    match = look is not None and target is not None
    if match:
        m = np.maximum(np.asarray(look["mean"], np.float64) / 255.0, 0.02)
        pivot = float(target.mean())
        gain = max(0.85, min(1.3, 0.2 / max(0.04, look["std"] / 255.0)))
    for c in range(3):
        y = x.copy()
        if match:
            y = y * max(0.72, min(1.38, target[c] / m[c]))        # colour cast and exposure
            y = (y - pivot) * gain + pivot                         # contrast, about the middle
        lift = g.get("lift", (0, 0, 0))[c]
        gain_c = g.get("gain", (1, 1, 1))[c]
        gamma = g.get("gamma", (1, 1, 1))[c]
        y = np.clip(y, 0, 1)
        y = np.power(y, gamma)
        y = lift + y * (gain_c - lift)
        y = _curve(np.clip(y, 0, 1), g.get("contrast", 0.0))
        chans.append(np.clip(y * 255.0 + 0.5, 0, 255).astype(np.uint8))
    return np.stack(chans, axis=-1).reshape(1, 256, 3)


# --- per-frame maths ----------------------------------------------------------------------

def _env(t: float, times: List[float], k: float) -> float:
    return sum(math.exp(-k * (t - h)) for h in times if 0 <= t - h < 2.0)


def zoom_at(seg: Dict[str, Any], t: float, style: str, fx: Dict[str, bool]) -> float:
    z = float(seg.get("zoom") or 1.0)
    if fx.get("push"):
        z *= 1.0 + PUSH.get(style, 0.05) * min(1.0, t / max(0.3, seg["dur"]))
    if style == "funny":
        for h in seg["pulses"]:
            d = t - h
            if 0 <= d < 0.08:
                z *= 1.0 + FUNNY_PUNCH * (d / 0.08)
            elif 0.08 <= d < 0.48:
                u = (d - 0.08) / 0.4
                z *= 1.0 + FUNNY_PUNCH * (1 - u * u * (3 - 2 * u))
    else:
        z *= 1.0 + PULSE.get(style, 0.05) * _env(t, seg["pulses"], PULSE_K)
    return z


def shake_at(seg: Dict[str, Any], t: float, style: str) -> Tuple[float, float, float]:
    S = SHAKE.get(style, 14.0)
    sx = sy = rot = 0.0
    for h in seg["shakes"]:
        d = t - h
        if 0 <= d < 0.8:
            e = math.exp(-6.0 * d)
            sx += S * e * math.sin(2 * math.pi * 13.0 * d)
            sy += S * 0.7 * e * math.sin(2 * math.pi * 9.7 * d + 1.1)
            rot += 0.7 * e * math.sin(2 * math.pi * 7.3 * d + 0.4)
    return sx, sy, rot


def affine(src_w: int, src_h: int, cx: float, cy: float, zoom: float, rot: float, sx: float, sy: float) -> np.ndarray:
    """Source px → output px: cover the 1080×1920 frame, zoomed about the subject, kept inside the picture."""
    s = max(OW / src_w, OH / src_h) * zoom
    half_w, half_h = OW / (2 * s), OH / (2 * s)
    cx = min(max(cx, half_w), src_w - half_w) if src_w > 2 * half_w else src_w / 2
    cy = min(max(cy, half_h), src_h - half_h) if src_h > 2 * half_h else src_h / 2
    th = math.radians(rot)
    c, si = math.cos(th), math.sin(th)
    a, b, d, e = s * c, -s * si, s * si, s * c
    return np.array([[a, b, OW / 2 + sx - (a * cx + b * cy)],
                     [d, e, OH / 2 + sy - (d * cx + e * cy)]], np.float64)


def glitch(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """RGB split: blue and red pulled 8-14 px apart in opposite directions, sometimes a torn band."""
    import cv2
    d = int(rng.integers(8, 15)) * (1 if rng.random() < 0.5 else -1)
    b, g, r = cv2.split(img)
    out = cv2.merge([np.roll(b, d, axis=1), g, np.roll(r, -d, axis=1)])
    if rng.random() < 0.6:
        y0 = int(rng.integers(0, img.shape[0] - 140))
        out[y0:y0 + 90] = np.roll(out[y0:y0 + 90], int(rng.integers(-40, 40)), axis=1)
    return out


class Interp:
    """Optical-flow in-between frames for smooth slow-mo (DIS, computed at half size)."""

    def __init__(self):
        import cv2
        self.cv2 = cv2
        self.dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
        self.key: Any = None
        self.flows: Any = None

    def between(self, a: np.ndarray, b: np.ndarray, u: float, key: Any) -> np.ndarray:
        cv2 = self.cv2
        h, w = a.shape[:2]
        if key != self.key:
            ga = cv2.resize(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), (w // 2, h // 2), interpolation=cv2.INTER_AREA)
            gb = cv2.resize(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), (w // 2, h // 2), interpolation=cv2.INTER_AREA)
            fab = cv2.resize(self.dis.calc(ga, gb, None), (w, h)) * 2.0
            fba = cv2.resize(self.dis.calc(gb, ga, None), (w, h)) * 2.0
            gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
            self.key, self.flows = key, (fab, fba, gx, gy)
        fab, fba, gx, gy = self.flows
        wa = cv2.remap(a, gx - u * fab[..., 0], gy - u * fab[..., 1], cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
        wb = cv2.remap(b, gx - (1 - u) * fba[..., 0], gy - (1 - u) * fba[..., 1], cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
        return cv2.addWeighted(wa, 1 - u, wb, u, 0)


# --- the words (ASS) -----------------------------------------------------------------------

def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    cs = int(round((s - int(s)) * 100))
    if cs == 100:
        s, cs = s + 1, 0
    return f"{int(h)}:{int(m):02d}:{int(s):02d}.{cs:02d}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\\", "").replace("{", "(").replace("}", ")")).strip()


_SCALE_WORD = re.compile(r"^(k|m|b|bn|million|billion|thousand|grand|percent|%|dollars?|years?|days?|"
                         r"months?|weeks?|hours?|minutes?)$", re.I)


def _units(text: str) -> List[str]:
    """Words, with a number kept together with what it counts ("$2 MILLION", "7 YEARS")."""
    out: List[str] = []
    for w in text.split():
        if out and re.search(r"\d", out[-1]) and _SCALE_WORD.match(re.sub(r"[^\w%]", "", w)):
            out[-1] = out[-1] + "\u00a0" + w
        else:
            out.append(w)
    return out


def wrap(text: str, per_line: int) -> List[str]:
    """Balanced lines: the fewest lines of at most `per_line` characters, as even as possible."""
    units = _units(text)
    if not units:
        return []
    n = len(units)

    def length(i: int, j: int) -> int:
        return sum(len(u) for u in units[i:j]) + (j - i - 1)

    best: Optional[List[str]] = None
    for lines in range(1, min(n, 5) + 1):
        # dynamic programming: split units into `lines` lines, smallest longest line
        INF = 10 ** 9
        cost = [[INF] * (n + 1) for _ in range(lines + 1)]
        cut = [[0] * (n + 1) for _ in range(lines + 1)]
        cost[0][0] = 0
        for k in range(1, lines + 1):
            for j in range(1, n + 1):
                for i in range(k - 1, j):
                    c = max(cost[k - 1][i], length(i, j))
                    if c < cost[k][j]:
                        cost[k][j], cut[k][j] = c, i
        if cost[lines][n] <= per_line or lines == min(n, 5):
            out, j = [], n
            for k in range(lines, 0, -1):
                i = cut[k][j]
                out.append(" ".join(units[i:j]))
                j = i
            best = out[::-1]
            break
    return [b.replace("\u00a0", " ") for b in best or [text]]


YELLOW = "&H0000D4FF&"         # #FFD400 in ASS order
GOLD = "&H007AD2FF&"           # #FFD27A
_NUMBERISH = re.compile(r"[$€£]?\d[\d,.]*\s*[kmb%]?$|^(million|billion|thousand|k)$", re.I)


def _colour_words(line: str, key: str, colour: str) -> str:
    out = []
    for w in line.split():
        bare = re.sub(r"[^\w$%]", "", w).lower()
        hot = (key and bare == re.sub(r"[^\w$%]", "", key).lower()) or bool(_NUMBERISH.match(re.sub(r"[^\w$%.,]", "", w)))
        out.append(f"{{\\c{colour}}}{w}{{\\c&H00FFFFFF&}}" if hot else w)
    return " ".join(out)


# Anton as libass draws it, as fractions of the font size: capitals are half
# the size tall, start 0.307 below the anchor, and average 0.27 wide.
ANTON_CAP, ANTON_TOP, ANTON_W = 0.5, 0.307, 0.27
TEXT_LEFT, TEXT_RIGHT = SAFE_LEFT + 30, SAFE_RIGHT + 30
TEXT_W = OW - TEXT_LEFT - TEXT_RIGHT
TEXT_CX = (TEXT_LEFT + OW - TEXT_RIGHT) // 2
LEADING = 1.32                                   # line pitch, in capital heights


def fit(text: str, size: int, max_lines: int, min_size: int) -> Tuple[List[str], int]:
    """Wrap uppercase Anton text, shrinking the size until it fits `max_lines` (never cutting words)."""
    while True:
        lines = wrap(text, max(6, int(TEXT_W / (ANTON_W * size))))
        if len(lines) <= max_lines or size <= min_size:
            return lines, size
        size = max(min_size, int(size * 0.9))


def block_height(lines: int, size: int) -> float:
    return size * ANTON_CAP * (1 + LEADING * (lines - 1))


# --- where the words may go: never over his face -------------------------------------------

# How far libass's ink reaches past the capitals (outline, shadow, a Q's tail, a comma), as
# fractions of the size, measured: a little above the first line's capitals, more below the last.
INK_ABOVE, INK_BELOW = 0.05, 0.14
# Poppins subtitles sit on their anchor (\an2); their ink runs from 0.85 to 0.14 of the size above it.
SUB_SIZE, SUB_INK_TOP, SUB_INK_BOTTOM = 96, 0.85, 0.14
SUB_BOTTOM = 1500                                # where a subtitle line sits when the face allows
TEXT_MIN_SIZE = 64                               # words never shrink below this to get out of the way
# The head around a face the finder saw, in face heights: hair above, chin and neck below, the
# sides — with room for him to move, since a moment's face is found on a few stills, not followed.
HEAD_ABOVE, HEAD_BELOW, HEAD_SIDE = 0.95, 0.75, 0.65
FACE_ABOVE, FACE_BELOW, FACE_SIDE = 0.30, 0.60, 0.45      # just the eyes, nose and mouth
FACE_MARGIN = 36                                          # output px kept clear around the head


def text_zone(letterbox: bool) -> Tuple[float, float]:
    """Where words' ink may go, top to bottom: under the apps' top bar (or the cinema bar), above their caption area."""
    return float((BAR_H + 30) if letterbox else SAFE_TOP + 30), float(OH - SAFE_BOTTOM - 20)


def _ink(size: float, pop: float = 1.0) -> Tuple[float, float]:
    """How far the ink reaches (above, below) a block of Anton capitals. `pop`: how big the words
    start before settling (\\fscy118 → 1.18); they grow from each line's top, so only downwards."""
    return INK_ABOVE * size, INK_BELOW * size + (pop - 1.0) * (ANTON_TOP + ANTON_CAP + INK_BELOW) * size


def place(want: float, height: float, ink: Tuple[float, float], avoid: List[Tuple[float, float]],
          lo: float, hi: float) -> Optional[float]:
    """The top for a block `height` tall (its ink reaching `ink` = (above, below) further) as near
    `want` as it can be: all of it between lo and hi, none of it over any (top, bottom) band in
    `avoid`. None when there's no such place."""
    up, down = ink

    def clear(top: float) -> bool:
        a, b = top - up, top + height + down
        return a >= lo - 0.5 and b <= hi + 0.5 and all(b <= t0 or a >= t1 for t0, t1 in avoid)

    tries = [want, min(max(want, lo + up), hi - down - height)]
    for t0, t1 in avoid:
        tries += [t0 - down - height, t1 + up]                 # just above the band, just below it
    good = [t for t in tries if clear(t)]
    return min(good, key=lambda t: (abs(t - want), t)) if good else None


def _box(M: np.ndarray, cx: float, cy: float, fh: float, above: float, below: float, side: float,
         margin: float) -> Tuple[float, float, float, float]:
    xs, ys = (cx - side * fh, cx + side * fh), (cy - above * fh, cy + below * fh)
    pts = np.array([[x, y, 1.0] for x in xs for y in ys]) @ M.T
    return (float(pts[:, 0].min() - margin), float(pts[:, 1].min() - margin),
            float(pts[:, 0].max() + margin), float(pts[:, 1].max() + margin))


class FaceTrack:
    """Where the face is on screen in every frame of the edit (output px), worked out the way
    render() draws it: the moment's crop window, its zoom (push-ins, punches), its shake.

    faces(a, b) → {left, top, right, bottom}: everything the head covers in any frame from a to
    b seconds, or None when no face was found there. faces(a, b, tight=True): just the eyes to
    the chin — the last thing words may ever cover."""

    def __init__(self, tl: Dict[str, Any], sources: Dict[str, "Source"], looks: Dict[str, Dict[str, Any]]):
        segs, style, fx = tl["segments"], tl["style"], tl.get("effects") or {}
        N = int(round(float(tl["length"]) * FPS))
        self.head = np.full((N, 4), np.nan)
        self.core = np.full((N, 4), np.nan)
        for i, s in enumerate(segs):
            look = looks.get(s["moment"]) or {}
            if not look.get("found") or not look.get("face_h"):
                continue
            src = sources[s["source"]]
            # the same window render() reads for this moment
            win = int(min(src.w, src.h * 9 / 16 * 1.12 + 40))
            win -= win % 2
            x0 = int(min(max(0, look["cx"] - win / 2), src.w - win))
            x0 -= x0 % 2
            cx, cy, fh = look["cx"] - x0, look["cy"], float(look["face_h"])
            n0 = int(round(s["at"] * FPS))
            n1 = N if i == len(segs) - 1 else int(round((s["at"] + s["dur"]) * FPS))
            for n in range(max(0, n0), min(n1, N)):
                t = (n - n0) / FPS
                z = zoom_at(s, t, style, fx)
                sx, sy, rot = shake_at(s, t, style)
                if sx or sy:
                    z *= 1.0 + 2.2 * (abs(sx) + abs(sy)) / OW
                if s.get("blur_in") and t < 3 / FPS:
                    z *= 1.045                                   # the zoom blur's widest copy
                M = affine(win, src.h, cx, cy, z, rot, sx, sy)
                self.head[n] = _box(M, cx, cy, fh, HEAD_ABOVE, HEAD_BELOW, HEAD_SIDE, FACE_MARGIN)
                self.core[n] = _box(M, cx, cy, fh, FACE_ABOVE, FACE_BELOW, FACE_SIDE, 12)

    def __call__(self, a: float, b: float, tight: bool = False) -> Optional[Dict[str, float]]:
        boxes = self.core if tight else self.head
        # the frames the words are on: shown from a to b, as the ASS file says it (to 1/100 s)
        n0, n1 = (int(math.ceil(round(x, 2) * FPS - 1e-6)) for x in (a, b))
        part = boxes[max(0, n0):max(0, min(len(boxes), max(n1, n0 + 1)))]
        part = part[~np.isnan(part[:, 0])]
        if not len(part):
            return None
        return {"left": float(part[:, 0].min()), "top": float(part[:, 1].min()),
                "right": float(part[:, 2].max()), "bottom": float(part[:, 3].max())}


def _faces_fn(face: Any) -> Callable[..., Optional[Dict[str, float]]]:
    """A FaceTrack as it is; one fixed box ({top, bottom}) for every moment; or no face at all."""
    if callable(face):
        return face
    return lambda a, b, tight=False: face or None


def fit_clear(text: str, size: int, max_lines: int, min_size: int, want: Callable[[float], float],
              faces: Callable[..., Optional[Dict[str, float]]], a: float, b: float, lo: float, hi: float,
              pop: float = 1.0, avoid: Tuple[Tuple[float, float], ...] = (),
              strict: bool = False) -> Optional[Tuple[List[str], int, float]]:
    """Wrap uppercase Anton words and find their place for the time they're up (a..b s): as near
    `want(block height)` as they can be, never over his head, smaller when the head leaves too little
    room. When even small words can't clear the whole head they keep off his eyes and mouth; failing
    that (a face filling the screen) they go to the end of the screen furthest from his mouth.
    Returns (lines, size, top of the capitals). strict: only a place clear of his whole head at a
    comfortable size will do — None otherwise."""
    comfy = max(TEXT_MIN_SIZE, int(min_size * 0.75))
    tiers = ((False, comfy),) if strict else \
        ((False, comfy), (True, comfy), (False, TEXT_MIN_SIZE), (True, TEXT_MIN_SIZE))
    for tight, floor in tiers:
        box = faces(a, b, tight=tight)
        if tight and box is None:
            continue                                         # no face: the tier before tried exactly this
        bands = list(avoid) + ([(box["top"], box["bottom"])] if box else [])
        s = size
        while True:
            lines = wrap(text, max(6, int(TEXT_W / (ANTON_W * s))))
            if len(lines) <= max_lines or s <= min(min_size, floor):
                h = block_height(len(lines), s)
                top = place(want(h), h, _ink(s, pop), bands, lo, hi)
                if top is not None:
                    return lines, s, top
            if s <= floor:
                break
            s = max(floor, int(s * 0.9))
    if strict:
        return None
    s = TEXT_MIN_SIZE
    lines = wrap(text, max(6, int(TEXT_W / (ANTON_W * s))))
    h = block_height(len(lines), s)
    up, down = _ink(s, pop)
    core = faces(a, b, tight=True) or {"top": 0.0, "bottom": 0.0}
    mouth = core["top"] + 0.8 * (core["bottom"] - core["top"])
    top = lo + up if mouth > (lo + hi) / 2 else hi - down - h
    return lines, s, top


def sub_spot(faces: Callable[..., Optional[Dict[str, float]]], a: float, b: float, lo: float, hi: float,
             avoid: Tuple[Tuple[float, float], ...] = ()) -> Tuple[float, int]:
    """Where a subtitle line sits (its bottom anchor, \\an2) and its size: low on the screen as usual
    unless his face is there, then just above or below his head — smaller if it must, off his eyes
    and mouth at the very least, at the end of the screen furthest from his mouth if nothing else fits."""
    for tight in (False, True):
        box = faces(a, b, tight=tight)
        if tight and box is None:
            break
        bands = list(avoid) + ([(box["top"], box["bottom"])] if box else [])
        for fs in (SUB_SIZE, 84, 72):
            h = (SUB_INK_TOP - SUB_INK_BOTTOM) * fs
            top = place(SUB_BOTTOM - SUB_INK_TOP * fs, h, (3.0, 4.0), bands, lo, hi)
            if top is not None:
                return top + SUB_INK_TOP * fs, fs
    core = faces(a, b, tight=True) or {"top": 0.0, "bottom": 0.0}
    mouth = core["top"] + 0.8 * (core["bottom"] - core["top"])
    return (lo + 3 + SUB_INK_TOP * 72 if mouth > (lo + hi) / 2 else hi - 4 + SUB_INK_BOTTOM * 72), 72


def hook_top(lines: int, size: int, face: Optional[Dict[str, float]], letterbox: bool) -> int:
    """Where the hook's capitals start: in the top third, never over the face —
    under the chin when there's no room above the head."""
    h = block_height(lines, size)
    lo, hi = text_zone(letterbox)
    ink = _ink(size, 1.18)
    top = place(400.0, h, ink, [(face["top"], face["bottom"])] if face else [], lo, hi)
    return int(round(top if top is not None else max(400.0, lo + ink[0])))


def build_ass(tl: Dict[str, Any], out: Path, face: Any = None) -> bool:
    """All the words of an edit. Returns False when there are none. `face` is where his face is
    on screen: a FaceTrack (every moment, frame by frame), one fixed box (output px: top,
    bottom) for the whole edit, or None. No words ever go over it."""
    L = tl["length"]
    loop = tl.get("loop") or 0.0
    stop = L - loop - 0.03 if loop else L
    segs = tl["segments"]
    mode = tl.get("text")
    letterbox = bool((tl.get("effects") or {}).get("letterbox"))
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OW}", f"PlayResY: {OH}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Hook,Anton,140,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,7,3,8,{TEXT_LEFT},{TEXT_RIGHT},0,1",
        f"Style: Punch,Anton,236,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,2,0,1,9,5,8,{TEXT_LEFT},{TEXT_RIGHT},0,1",
        f"Style: Quote,Anton,168,&H00FFFFFF,&H00FFFFFF,&H00101010,&H78000000,0,0,0,0,100,100,2,0,1,6,4,8,{TEXT_LEFT},{TEXT_RIGHT},0,1",
        f"Style: Sub,Poppins,96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0.5,0,1,3,3,2,{TEXT_LEFT},{TEXT_RIGHT},{OH - 1500},1",
        f"Style: Build,Anton,188,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,8,4,8,{TEXT_LEFT},{TEXT_RIGHT},0,1",
        f"Style: Meme,Anton,132,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,7,3,8,{TEXT_LEFT},{TEXT_RIGHT},0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events: List[str] = []
    faces = _faces_fn(face)
    lo, hi = text_zone(letterbox)

    def add(a: float, b: float, style: str, text: str, layer: int = 0) -> None:
        b = min(b, stop)
        if b - a >= 0.12 and text:
            events.append(f"Dialogue: {layer},{_ass_time(a)},{_ass_time(b)},{style},,0,0,0,,{text}")

    def stack(a: float, b: float, style: str, lines: List[str], size: int, top: float, tags: str = "",
              layer: int = 1) -> None:
        """Lines placed one by one, so the line spacing is ours, not the font's."""
        pitch = size * ANTON_CAP * LEADING
        for i, line in enumerate(lines):
            y = top + i * pitch - ANTON_TOP * size
            add(a, b, style, f"{{\\an8\\pos({TEXT_CX},{y:.0f})\\fs{size}{tags}}}{line}", layer)

    def shots(a: float, b: float) -> List[Tuple[float, float]]:
        """a..b cut where the picture moves to another moment (a short leftover joins its neighbour) —
        on the first frame of the new shot, in the 1/100 s steps of the ASS file."""
        cuts = [math.floor(round(s["at"] * FPS) / FPS * 100 + 1e-6) / 100 for k, s in enumerate(segs)
                if k and s["moment"] != segs[k - 1]["moment"]]
        pts = [a] + [c for c in cuts if a + 0.3 < c < b - 0.3] + [b]
        return list(zip(pts, pts[1:]))

    hook = tl.get("hook") or {}
    hook_end = 0.0
    top_text = None                                          # where top captions go (the hook's place)
    hook_bands: List[Tuple[float, float, Tuple[float, float]]] = []
    if hook.get("text"):
        hook_end = hook["end"]
        text = _clean(hook["text"]).upper()
        # one place for the whole hook when one clears his head; else a place per shot (it moves on the cut)
        whole = fit_clear(text, 140, 3, 92, lambda h: 400.0, faces, 0.0, hook_end, lo, hi, pop=1.18, strict=True)
        pieces = [(0.0, hook_end)] if whole else shots(0.0, hook_end)
        spots = [whole] if whole else [fit_clear(text, 140, 3, 92, lambda h: 400.0, faces, a, b, lo, hi, pop=1.18)
                                       for a, b in pieces]
        if len(spots) > 1:                                   # the same size in every shot
            size = min(sp[1] for sp in spots)
            spots = [fit_clear(text, size, 3, min(92, size), lambda h: 400.0, faces, a, b, lo, hi, pop=1.18)
                     for a, b in pieces]
        for k, ((a, b), (lines, size, top)) in enumerate(zip(pieces, spots)):
            up, down = _ink(size, 1.18)
            hook_bands.append((a, b, (top - up, top + block_height(len(lines), size) + down)))
            tags = ("\\fscx118\\fscy118\\t(0,110,\\fscx100\\fscy100)" if k == 0 else "") + \
                ("\\fad(0,160)" if k == len(pieces) - 1 else "")
            stack(a, b, "Hook", [_colour_words(l, "", YELLOW) for l in lines], size, top, tags, 2)
            top_text = top

    def run_end(i: int) -> float:
        j = i + 1
        while j < len(segs) and segs[j]["moment"] == segs[i]["moment"] and not segs[j].get("first"):
            j += 1
        return segs[j - 1]["at"] + segs[j - 1]["dur"]

    if mode in ("punch", "quote"):
        for i, s in enumerate(segs):
            if not s.get("text"):
                continue
            a = max(s["at"], hook_end)                      # never over the hook, the drop's line included
            b = min(run_end(i), a + (1.7 if mode == "punch" else 2.6)) - 0.04
            if b - a < 0.6:
                continue
            if mode == "punch":
                lines, size, top = fit_clear(_clean(s["text"]).upper(), 236, 2, 150, lambda h: OH * 0.62 - h / 2,
                                             faces, a, b, lo, hi, pop=1.12)
                tags, colour, style = "\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)", YELLOW, "Punch"
            else:
                lines, size, top = fit_clear(_clean(s["text"]).upper(), 168, 3, 110, lambda h: OH * 0.62 - h / 2,
                                             faces, a, b, lo, hi)
                tags, colour, style = "\\fad(140,140)", GOLD, "Quote"
            stack(a, b, style, [_colour_words(l, s.get("key", ""), colour) for l in lines], size, top, tags)
    elif mode == "subtitle":
        for s in segs:
            phrases: List[List[Dict[str, Any]]] = []
            for w in s["words"]:
                cur = phrases[-1] if phrases else None
                gap = w["t"] - cur[-1]["end"] if cur else 0
                chars = sum(len(x["w"]) + 1 for x in cur) if cur else 0
                if not cur or len(cur) >= 6 or chars + len(w["w"]) > 24 or gap > 0.45 or \
                        re.search(r"[.!?]$", cur[-1]["w"]):
                    phrases.append([w])
                else:
                    cur.append(w)
            for k, ph in enumerate(phrases):
                nxt = phrases[k + 1][0]["t"] if k + 1 < len(phrases) else s["at"] + s["dur"]
                a, b = ph[0]["t"], min(nxt, ph[-1]["end"] + 0.35)
                anchor, fs = sub_spot(faces, a, b, lo, hi, tuple(band for h0, h1, band in hook_bands
                                                                   if h0 < b and a < h1))
                add(a, b, "Sub", f"{{\\an2\\pos({TEXT_CX},{anchor:.0f})\\fs{fs}\\fad(70,70)}}"
                    + _clean(" ".join(x["w"] for x in ph)))
    elif mode == "build":
        for s in segs:
            # one screen per phrase: a new one at a sentence end, a pause, or 7 words
            pages: List[List[Dict[str, Any]]] = []
            for w in s["words"]:
                cur = pages[-1] if pages else None
                if not cur or len(cur) >= 7 or w["t"] - cur[-1]["end"] > 0.5 or re.search(r"[.!?]$", cur[-1]["w"]):
                    pages.append([w])
                else:
                    cur.append(w)
            key = re.sub(r"[^\w]", "", (s.get("key") or "")).lower()
            for p, page in enumerate(pages):
                end_page = pages[p + 1][0]["t"] if p + 1 < len(pages) else s["at"] + s["dur"] - 0.05
                end_page = min(end_page, page[-1]["end"] + 1.2)
                shown_from = [w["t"] for w in page if w["t"] >= hook_end]
                if not shown_from:
                    continue
                # one place for the whole page, clear of his face for as long as the page is up
                lines, size, top = fit_clear(" ".join(_clean(w["w"]).upper() for w in page), 188, 3, 120,
                                             lambda h: OH * 0.60 - h / 2, faces, shown_from[0], end_page, lo, hi)
                for k, w in enumerate(page):
                    if w["t"] < hook_end:                 # the hook has the screen for its first seconds
                        continue
                    b = page[k + 1]["t"] if k + 1 < len(page) else end_page
                    shown, idx = [], 0
                    for line in lines:
                        parts = []
                        for tok in line.split():
                            hot = key and re.sub(r"[^\w]", "", tok).lower() == key
                            colour = f"\\c{YELLOW}" if hot else "\\c&H00FFFFFF&"
                            if idx < k:
                                alpha = "\\alpha&H00&"
                            elif idx == k:                  # the word being said fades in; nothing moves
                                alpha = "\\alpha&HFF&\\t(0,90,\\alpha&H00&)"
                            else:
                                alpha = "\\alpha&HFF&"
                            parts.append(f"{{{alpha}{colour}}}{tok}")
                            idx += 1
                        shown.append(" ".join(parts))
                    stack(w["t"], b, "Build", shown, size, top)
    elif mode == "meme":
        for s in segs:
            if not s.get("text"):
                continue
            a, b = max(s["at"], hook_end), s["at"] + s["dur"] - 0.05
            want = top_text if top_text is not None else 400.0
            lines, size, top = fit_clear(_clean(s["text"]).upper(), 132, 3, 96, lambda h: want, faces, a, b, lo, hi)
            stack(a, b, "Meme", lines, size, top, "\\fad(60,60)")
    if not events:
        return False
    out.write_text("\n".join(head + events) + "\n", encoding="utf-8")
    return True


# --- sound ----------------------------------------------------------------------------------

def _atempo(speed: float) -> str:
    parts = []
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    parts.append(f"atempo={speed:.5f}")
    return ",".join(parts)


def build_audio(tl: Dict[str, Any], sources: Dict[str, Source], song: Optional[Path], out: Path) -> Dict[str, Any]:
    """His voice (each shot's own sound, kept to its words) + the song section → one WAV, then its loudness."""
    L = tl["length"]
    inputs: List[str] = []
    graph: List[str] = []
    voice_labels: List[str] = []
    voice_level = float(tl.get("voice") or 0.0)
    if voice_level > 0.001:
        for k, s in enumerate(tl["segments"]):
            src = sources[s["source"]]
            if not src.has_audio:
                continue
            used = edits.curve_src(s["curve"], s["dur"])
            if s.get("voice"):
                v0, v1 = s["voice"]
                a0 = s["src_start"] + edits.curve_src(s["curve"], v0)
                a1 = s["src_start"] + edits.curve_src(s["curve"], v1)
                at, length, tempo = s["at"] + v0, v1 - v0, (a1 - a0) / max(0.01, v1 - v0)
            else:
                a0, at, length, tempo = s["src_start"], s["at"], s["dur"], used / max(0.01, s["dur"])
            if length < 0.05:
                continue
            src_len = length * tempo
            n = sum(1 for x in inputs if x == "-i")
            inputs += ["-ss", f"{max(0.0, a0):.4f}", "-t", f"{src_len + 0.05:.4f}", "-i", str(src.path)]
            fade_in, fade_out = min(0.02, length / 4), min(0.06, length / 4)
            chain = f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{src_len:.5f},asetpts=PTS-STARTPTS"
            if abs(tempo - 1.0) > 0.01:
                chain += "," + _atempo(tempo)
            chain += (f",apad=whole_dur={length:.5f},atrim=0:{length:.5f},afade=t=in:d={fade_in:.3f},"
                      f"afade=t=out:st={length - fade_out:.4f}:d={fade_out:.3f},"
                      f"adelay={int(round(at * 1000))}:all=1[v{k}]")
            graph.append(chain)
            voice_labels.append(f"[v{k}]")
    voice = None
    if voice_labels:
        graph.append(f"{''.join(voice_labels)}amix=inputs={len(voice_labels)}:normalize=0:dropout_transition=0,"
                     f"volume={voice_level:.3f},apad=whole_dur={L:.4f},atrim=0:{L:.4f}[voice]")
        voice = "[voice]"
    music = tl.get("music")
    mus = None
    if music and song and float(music.get("level") or 0) > 0.001:
        n = sum(1 for x in inputs if x == "-i")
        span = max(0.1, music["end"] - music["start"])
        inputs += ["-ss", f"{music['start']:.4f}", "-t", f"{span + 0.05:.4f}", "-i", str(song)]
        chain = (f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{span:.5f},asetpts=PTS-STARTPTS,"
                 f"afade=t=in:d=0.02,afade=t=out:st={max(0.0, span - (music.get('fade_out') or 0.03)):.4f}:"
                 f"d={music.get('fade_out') or 0.03:.3f},volume={float(music['level']):.3f}")
        if music.get("at"):
            chain += f",adelay={int(round(music['at'] * 1000))}:all=1"
        chain += f",apad=whole_dur={L:.4f},atrim=0:{L:.4f}[music]"
        graph.append(chain)
        mus = "[music]"
    if voice and mus:
        # the song ducks a little under his voice and swells back between lines
        graph.append("[voice]asplit=2[vmix][vkey]")
        graph.append("[music][vkey]sidechaincompress=threshold=0.04:ratio=3:attack=20:release=380:makeup=1[mduck]")
        graph.append("[vmix][mduck]amix=inputs=2:normalize=0:dropout_transition=0[mix]")
    elif voice or mus:
        graph.append(f"{voice or mus}anull[mix]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{L:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]
        n = sum(1 for x in inputs if x == "-i") - 1
        graph.append(f"[{n}:a]anull[mix]")
    graph.append(f"[mix]atrim=0:{L:.4f},asetpts=PTS-STARTPTS[aout]")
    raw = out.with_name("raw.wav")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin", *inputs,
           "-filter_complex", ";".join(graph), "-map", "[aout]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_f32le",
           str(raw)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not raw.exists():
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
        raise RuntimeError(f"Couldn't mix the sound:\n{tail}")
    loud = normalize(raw, out)
    return {"lufs": loud, "silent": not (voice or mus)}


def measure(wav: Path) -> Tuple[float, float]:
    """(integrated loudness in LUFS, true peak in dBFS)."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(wav), "-af", "ebur128=peak=true",
                           "-f", "null", "-"], capture_output=True, text=True)
    text = proc.stderr or ""
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else text
    m_i = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summary)
    m_p = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary)
    try:
        return (float(m_i.group(1)) if m_i else float("nan")), (float(m_p.group(1)) if m_p else 0.0)
    except ValueError:
        return float("nan"), 0.0


def normalize(raw: Path, out: Path, target: float = TARGET_LUFS) -> float:
    """Bring the mix to `target` LUFS with peaks held under -1.5 dBFS: gain into a
    limiter, measured again and corrected once (the limiter takes a little back)."""
    loud, _ = measure(raw)
    if not math.isfinite(loud) or loud < -70:             # silence: nothing to bring up
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-c:a", "pcm_s16le", str(out)], check=True)
        return loud
    gain = max(-30.0, min(30.0, target - loud))
    for _ in range(3):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-nostdin", "-i", str(raw), "-af",
                        f"aresample=192000,volume={gain:.2f}dB,alimiter=limit=0.78:attack=2:release=80:level=false:"
                        "latency=true,aresample=48000",
                        "-c:a", "pcm_s16le", str(out)], check=True)
        got, _ = measure(out)
        if not math.isfinite(got) or abs(got - target) <= 0.4:
            return got
        gain = max(-30.0, min(30.0, gain + (target - got) * 1.1))
    return got


# --- the render -----------------------------------------------------------------------------

def swap_in(tmp: Path, out: Path) -> None:
    """Put the new video in place of the old one. Windows refuses to replace a file another program has open
    (the browser playing the edit): wait a little, then copy over it instead."""
    import shutil
    from . import postready
    for _ in range(12):
        try:
            tmp.replace(out)
            break
        except PermissionError:
            time.sleep(0.4)
    else:
        try:
            shutil.copyfile(tmp, out)
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError("The new version is made but couldn't replace the old one — it's open somewhere. "
                               "Close the video and press Re-make.") from exc
    if out.suffix.lower() == ".mp4":
        postready.refresh(out)              # the old phone copy goes; a new one when it's over 50 MB

def _groups(segs: List[Dict[str, Any]]) -> List[List[int]]:
    """Segments read with one decoder: same moment, footage running on."""
    groups: List[List[int]] = []
    for i, s in enumerate(segs):
        if groups:
            p = segs[groups[-1][-1]]
            p_end = p["src_start"] + edits.curve_src(p["curve"], p["dur"])
            if p["moment"] == s["moment"] and p["source"] == s["source"] and -0.05 <= s["src_start"] - p_end < 1.5:
                groups[-1].append(i)
                continue
        groups.append([i])
    return groups


def render(timeline: Dict[str, Any], sources_by_id: Dict[str, Dict[str, Any]], sound: Optional[Dict[str, Any]],
           out_path: Path, thumb_path: Path, progress: Optional[Callable[[int], None]] = None) -> Dict[str, Any]:
    """Render the edit. Raises RuntimeError in plain words when something can't be done."""
    import cv2
    started = time.time()
    tl = timeline
    style, fx, segs = tl["style"], tl["effects"], tl["segments"]
    L = float(tl["length"])
    N = int(round(L * FPS))
    sources: Dict[str, Source] = {}
    for sid in {s["source"] for s in segs}:
        job = sources_by_id.get(sid) or {}
        path = Path(job.get("source_path") or "")
        if not path.is_file():
            raise RuntimeError(f"“{job.get('title') or sid}” isn't on this PC any more — make clips from it again first")
        sources[sid] = probe(path)
    song = Path(sound["file"]) if sound and sound.get("file") else None
    if tl.get("music") and (not song or not song.is_file()):
        raise RuntimeError("The song file is missing — add the song again")
    work = Path(tempfile.mkdtemp(prefix="edit_", dir=str(WORK_DIR)))
    try:
        mix = work / "mix.wav"
        audio = build_audio(tl, sources, song, mix)

        # how each moment looks: where the face is, and its levels for matching
        spans: Dict[str, Tuple[str, float, float]] = {}
        for s in segs:
            a = s["src_start"]
            b = a + edits.curve_src(s["curve"], s["dur"])
            if s["moment"] in spans:
                _, a0, b0 = spans[s["moment"]]
                a, b = min(a, a0), max(b, b0)
            spans[s["moment"]] = (s["source"], a, b)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:             # ffmpeg does the work: several at once
            futures = {m: pool.submit(look_at, sources[sid], a, b) for m, (sid, a, b) in spans.items()}
            looks = {m: f.result() for m, f in futures.items()}
        target = match_target(list(looks.values()))
        luts = {m: grade_lut(tl.get("grade") or "none", lk, target) for m, lk in looks.items()}

        # where his face is on screen in every frame, so no words ever go over it
        ass = work / "words.ass"
        has_words = build_ass(tl, ass, FaceTrack(tl, sources, looks))

        sat = GRADES.get(tl.get("grade") or "none", GRADES["none"]).get("sat", 1.0)
        vf = []
        if abs(sat - 1.0) > 0.01:
            vf.append(f"eq=saturation={sat:.3f}")
        if fx.get("vignette"):
            vf.append(f"vignette=angle={VIGNETTE.get(style, 'PI/4.6')}")
        if fx.get("grain"):
            vf.append(GRAIN)
        if fx.get("letterbox"):
            vf.append(f"drawbox=x=0:y=0:w=iw:h={BAR_H}:color=black:t=fill,"
                      f"drawbox=x=0:y=ih-{BAR_H}:w=iw:h={BAR_H}:color=black:t=fill")
        if has_words:
            vf.append(f"subtitles='{motion._escape(ass)}':fontsdir='{motion._escape(FONTS_DIR)}'")
        vf.append("format=yuv420p")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out_path.with_name(out_path.stem + ".part.mp4")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OW}x{OH}", "-framerate", str(FPS), "-i", "-",
               "-i", str(mix), "-filter_complex",
               f"[0:v]{','.join(vf)}[v];[1:a]anull[a]",
               "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
               *motion.VIDEO_CAP, "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(2 * FPS), "-c:a", "aac", "-b:a", "192k",
               "-ar", "48000", "-t", f"{N / FPS:.4f}", "-movflags", "+faststart", str(tmp_out)]
        enc_log = tempfile.TemporaryFile()
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=enc_log)
        interp = Interp() if fx.get("smooth") else None
        rng = np.random.default_rng(7)
        loop_n = int(round((tl.get("loop") or 0.0) * FPS))
        frame0: Optional[np.ndarray] = None
        written = 0
        last_pct = -1

        def emit(img: np.ndarray) -> None:
            nonlocal written, last_pct
            enc.stdin.write(np.ascontiguousarray(img).data)
            written += 1
            pct = int(100 * written / max(1, N))
            if progress and pct != last_pct and pct % 5 == 0:
                last_pct = pct
                progress(pct)

        try:
            for group in _groups(segs):
                g0 = segs[group[0]]
                src = sources[g0["source"]]
                fpsf = float(src.fps)
                total_frames = max(1, int(src.duration * fpsf) - 1) if src.duration else 10 ** 9
                a = g0["src_start"]
                last = segs[group[-1]]
                b = last["src_start"] + edits.curve_src(last["curve"], last["dur"])
                f_first = max(0, int(math.floor(a * fpsf)) - 2)
                count = int(math.ceil((b - a) * fpsf)) + 6
                look = looks[g0["moment"]]
                # read only the columns the 9:16 window can reach (with room for zoom-outs and shake)
                win = int(min(src.w, src.h * 9 / 16 * 1.12 + 40))
                win -= win % 2
                x0 = int(min(max(0, look["cx"] - win / 2), src.w - win))
                x0 -= x0 % 2
                reader = Reader(src, f_first, count, (x0, win))
                cx, cy = look["cx"] - x0, look["cy"]
                lut = luts[g0["moment"]]
                try:
                    for si in group:
                        s = segs[si]
                        n0 = int(round(s["at"] * FPS))
                        n1 = N if si == len(segs) - 1 else int(round((s["at"] + s["dur"]) * FPS))
                        for n in range(n0, n1):
                            t = (n - n0) / FPS
                            sp = edits.speed_at(s["curve"], t)
                            f = (s["src_start"] + edits.curve_src(s["curve"], t)) * fpsf
                            f = min(max(f, 0.0), float(total_frames))
                            i0 = int(math.floor(f))
                            u = f - i0
                            if sp < 0.92 and u > 0.04:
                                fa, fb = reader.get(i0), reader.get(i0 + 1)
                                img = interp.between(fa, fb, u, (id(reader), i0)) if interp else \
                                    cv2.addWeighted(fa, 1 - u, fb, u, 0)
                            elif sp > 1.45:
                                k = int(round(f))
                                img = cv2.addWeighted(reader.get(k - 1), 0.5, reader.get(k), 0.5, 0)
                            else:
                                img = reader.get(int(round(f)))
                            z = zoom_at(s, t, style, fx)
                            sx, sy, rot = shake_at(s, t, style)
                            if sx or sy:
                                z *= 1.0 + 2.2 * (abs(sx) + abs(sy)) / OW      # never show past the picture's edge
                            vec = s.get("blur_vec") or None
                            frames_blur = 3 if vec else 2           # a streak along the motion, or a light zoom blur
                            if s.get("blur_in") and t < frames_blur / FPS:
                                strength = 1.0 - t * FPS / frames_blur
                                mats = []
                                for j in range(5):
                                    v = (j / 4 - 0.5) * strength
                                    if vec:
                                        M = affine(win, src.h, cx, cy, z, rot, sx + vec[0] * v * 1.6,
                                                   sy + vec[1] * v * 1.6)
                                    else:
                                        M = affine(win, src.h, cx, cy, z * (1 + 0.045 * (v + 0.5)), rot, sx, sy)
                                    mats.append(M)
                                acc = np.zeros((OH // 2, OW // 2, 3), np.float32)
                                for M in mats:
                                    Mh = M * 0.5
                                    cv2.accumulate(cv2.warpAffine(img, Mh, (OW // 2, OH // 2), flags=cv2.INTER_LINEAR,
                                                                  borderMode=cv2.BORDER_REFLECT101), acc)
                                out = cv2.resize(cv2.convertScaleAbs(acc, alpha=1 / len(mats)), (OW, OH),
                                                 interpolation=cv2.INTER_LINEAR)
                            else:
                                M = affine(win, src.h, cx, cy, z, rot, sx, sy)
                                out = cv2.warpAffine(img, M, (OW, OH), flags=cv2.INTER_LINEAR,
                                                     borderMode=cv2.BORDER_REFLECT101)
                            if any(0 <= t - g < GLITCH_SECONDS for g in s["glitches"]):
                                out = glitch(out, rng)
                            out = cv2.LUT(out, lut)
                            flash = sum(FLASH_ALPHA * math.exp(-FLASH_K * (t - f0)) for f0 in s["flashes"]
                                        if 0 <= t - f0 < 0.6)
                            if flash > 0.01:
                                flash = min(0.95, flash)
                                out = cv2.convertScaleAbs(out, alpha=1 - flash, beta=255 * flash)
                            dim = 1.0
                            if s.get("dip_in") and t < DIP_SECONDS:
                                dim = min(dim, t / DIP_SECONDS)
                            left = s["dur"] - t
                            if s.get("dip_out") and left < DIP_SECONDS:
                                dim = min(dim, max(0.0, left - 1 / FPS) / DIP_SECONDS)
                            if dim < 0.999:
                                out = cv2.convertScaleAbs(out, alpha=dim)
                            if n == 0:
                                frame0 = out.copy()
                            if loop_n and frame0 is not None and n >= N - loop_n:
                                w = (n - (N - loop_n) + 1) / (loop_n + 1)
                                out = cv2.addWeighted(out, 1 - w, frame0, w, 0)
                            emit(out)
                finally:
                    reader.close()
            enc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        code = enc.wait()
        enc_log.seek(0)
        err = enc_log.read().decode("utf-8", "replace").strip()
        enc_log.close()
        if code != 0 or not tmp_out.exists():
            tail = "\n".join(err.splitlines()[-8:])
            raise RuntimeError(f"The video couldn't be written ({code}):\n{tail}")
        swap_in(tmp_out, out_path)
        at = min(L - 0.1, (tl.get("drop_at") or L / 3) + 0.15)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, at):.2f}",
                        "-i", str(out_path), "-frames:v", "1", "-q:v", "3", str(thumb_path)], capture_output=True)
        return {"length": N / FPS, "frames": written, "seconds": round(time.time() - started, 1)}
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
