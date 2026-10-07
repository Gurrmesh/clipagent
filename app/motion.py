"""Seamless render engine.

What makes the viral car and movie edits feel seamless is mostly motion
discipline rather than effects:

* picture and sound never slip apart,
* the camera never snaps — it holds, then eases (or whips, with motion blur),
* every jump cut changes the framing, so it reads as a choice, not a glitch,
* the frame is never dead still, and the loud moments land with a hit.

The clip is rendered frame by frame here (OpenCV does the warps) and handed to
ffmpeg for captions, audio and encoding.

Frame accuracy is the foundation. Every cut point is snapped to the source's
frame grid and the audio is cut at those same instants, so the two streams
have identical lengths segment by segment and cannot drift apart. The old
renderer cut video and audio with separate select filters that each rounded
to their own frame size; over a dozen cuts that grew to ~0.1 s of lag.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import captions, framing, textdetect
from .config import (BASE_DIR, CLIP_DIR, RENDER_H, RENDER_W, SAFE_BOTTOM, SAFE_LEFT, SAFE_RIGHT,
                     SAFE_TOP, THUMB_DIR, WORK_DIR)
from .framing import FramingPlan

OW, OH = RENDER_W, RENDER_H


def _logo_corners() -> Dict[str, Tuple[Any, Any]]:
    """Watermark spots inside the platforms' safe zone: below the top icons,
    above the bottom caption, left of the right-hand button rail."""
    top = SAFE_TOP - 50
    return {"top-left": (SAFE_LEFT, top), "top-right": (f"W-w-{SAFE_RIGHT}", top),
            "bottom-left": (SAFE_LEFT, f"H-h-{SAFE_BOTTOM}"),
            "bottom-right": (f"W-w-{SAFE_RIGHT}", f"H-h-{SAFE_BOTTOM}")}
BRAND_LOGO = BASE_DIR / "data" / "brand" / "logo.png"
FONTS_DIR = BASE_DIR / "fonts"

# --- the look, in numbers ---------------------------------------------------
PUNCH_LEVEL = 1.10          # alternate framing on jump cuts
PUSH_RATE = 0.007           # slow push-in per second, inside one shot
PUSH_MAX = 0.035
IMPACT_ZOOM = 0.10          # how hard a loud moment punches in
IMPACT_SETTLE = 0.05        # ...and how much of it holds until the next cut
SHAKE_PX = 12.0
DEADZONE_CROP = 0.22        # of the crop width: the subject may drift this far before the camera moves
WHIP_DISTANCE = 0.20        # of source width: a move this big is a whip, not a pan
MAX_ZOOM = 1.22             # beyond this the upscale gets visibly soft
LETTERBOX_LUMA = 28         # a row with nothing brighter than this is a black bar
LETTERBOX_MIN = 0.90        # picture shorter than this share of the frame: zoom past the bars
LETTERBOX_MAX_ZOOM = 1.9    # but never further than this
TEXT_MAX_ZOOM = 1.35        # how far a crop may tighten to leave the creator's burned-in words out
TEXT_EDGE = 26              # output px kept between burned-in words and the frame's edge (shake, rotation)
SPLIT_TOP = 864             # the split layout: facecam panel height; the content gets the rest


# --- small maths ------------------------------------------------------------

def _smootherstep(u: np.ndarray) -> np.ndarray:
    u = np.clip(u, 0.0, 1.0)
    return u * u * u * (u * (u * 6 - 15) + 10)


def _ease_out_cubic(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return 1 - (1 - u) ** 3


def _median_filter(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1 or len(x) < 3:
        return x.copy()
    k = min(k | 1, (len(x) | 1))
    pad = k // 2
    padded = np.pad(x, pad, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, k)
    return np.median(windows, axis=1)


def _fill_nan(x: np.ndarray) -> np.ndarray:
    """Forward/back fill; all-NaN stays NaN."""
    x = x.copy()
    ok = ~np.isnan(x)
    if not ok.any():
        return x
    idx = np.where(ok, np.arange(len(x)), 0)
    np.maximum.accumulate(idx, out=idx)
    x = x[idx]
    first = int(np.argmax(ok))
    x[:first] = x[first]
    return x


def _even(v: float) -> int:
    return max(2, int(round(v / 2.0)) * 2)


# --- probing ----------------------------------------------------------------

def _probe_video(source: Path) -> Dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate,avg_frame_rate", "-of", "json", str(source)],
        capture_output=True, text=True,
    ).stdout
    streams = json.loads(out or "{}").get("streams") or [{}]
    return streams[0]


def _frac(text: Optional[str]) -> Optional[Fraction]:
    try:
        n, d = (text or "").split("/")
        n, d = int(n), int(d)
        return Fraction(n, d) if n > 0 and d > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def frame_rate(source: Path) -> Fraction:
    """The source's frame rate as an exact fraction (30000/1001, not 29.97)."""
    st = _probe_video(source)
    r, a = _frac(st.get("r_frame_rate")), _frac(st.get("avg_frame_rate"))
    fps = r or a or Fraction(30)
    if not 10 <= fps <= 61:
        fps = a if a and 10 <= a <= 61 else Fraction(30)
    return fps.limit_denominator(1001)


def _is_vfr(source: Path) -> bool:
    st = _probe_video(source)
    r, a = _frac(st.get("r_frame_rate")), _frac(st.get("avg_frame_rate"))
    return bool(r and a and abs(float(r) - float(a)) / float(r) > 0.01)


# --- timeline -----------------------------------------------------------------

GAP_SECONDS = 8.0      # parts closer than this are decoded straight through


@dataclass
class Run:
    """Consecutive segments close enough to decode in one pass."""
    first: int
    last: int
    segs: List[int]


@dataclass
class Timeline:
    fps: Fraction
    segments: List[Tuple[int, int]]      # absolute source frames kept, [start, end), in play order

    @property
    def first(self) -> int:
        return min(s for s, _ in self.segments)

    @property
    def last(self) -> int:
        return max(e for _, e in self.segments)

    def runs(self) -> List["Run"]:
        """Group segments for decoding. A stitched clip can jump minutes — or
        backwards — between parts; each such jump starts a new run with its own
        seek, instead of decoding everything in between."""
        gap = int(GAP_SECONDS * float(self.fps))
        out: List[Run] = []
        for i, (s, e) in enumerate(self.segments):
            if out and s >= out[-1].last and s - out[-1].last <= gap:
                out[-1].last = e
                out[-1].segs.append(i)
            else:
                out.append(Run(first=s, last=e, segs=[i]))
        return out

    @property
    def total(self) -> int:
        return sum(e - s for s, e in self.segments)

    def src_index(self) -> np.ndarray:
        return np.concatenate([np.arange(s, e) for s, e in self.segments])

    def seconds(self, frames: float) -> float:
        return float(Fraction(frames) / self.fps) if isinstance(frames, int) else frames / float(self.fps)


def build_timeline(start: float, end: float, keep: Sequence[Tuple[float, float]] | None,
                   fps: Fraction) -> Timeline:
    """Snap the clip and its kept segments onto the source's frame grid."""
    i0 = round(Fraction(start) * fps)
    i1 = max(i0 + 1, round(Fraction(end) * fps))
    spans: List[Tuple[int, int]] = []
    for a, b in (keep or [(0.0, end - start)]):
        s = max(i0, round(Fraction(start + a) * fps))
        e = min(i1, round(Fraction(start + b) * fps))
        if e - s >= 2:
            if spans and s <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], e))
            else:
                spans.append((s, e))
    if not spans:
        spans = [(i0, i1)]
    return Timeline(fps=fps, segments=spans)


def timeline_from_segments(segments: Sequence[Tuple[float, float]], fps: Fraction) -> Timeline:
    """Absolute (start, end) seconds, in play order — for stitched clips."""
    spans: List[Tuple[int, int]] = []
    for a, b in segments:
        s, e = round(Fraction(a) * fps), round(Fraction(b) * fps)
        if e - s < 2:
            continue
        if spans and s == spans[-1][1]:
            spans[-1] = (spans[-1][0], e)          # touching and in order: one segment
        else:
            spans.append((s, e))
    if not spans:
        raise RuntimeError("no usable segments")
    return Timeline(fps=fps, segments=spans)


# --- decoding -------------------------------------------------------------------

class _Decoder:
    """Raw frames from ffmpeg, starting exactly at source frame `first`."""

    def __init__(self, source: Path, first: int, count: int, fps: Fraction,
                 size: Tuple[int, int], pix: str, vfr: bool = False, native: Tuple[int, int] | None = None):
        self.w, self.h = size
        self.pix = pix
        channels = {"gray": 1.0, "bgr24": 3.0, "yuv420p": 1.5}[pix]
        self.frame_bytes = int(self.w * self.h * channels)
        # Seek half a frame early: the first frame at or after that instant is
        # exactly frame `first`, whatever the float rounding of its timestamp.
        ss = max(0.0, float((Fraction(first) - Fraction(1, 2)) / fps))
        filters = []
        if vfr:
            filters.append(f"fps={fps.numerator}/{fps.denominator}")
        if native is None or (self.w, self.h) != tuple(native):
            filters.append(f"scale={self.w}:{self.h}:flags=area")
        # passthrough: hand over exactly the frames decoded. The raw-video
        # muxer otherwise defaults to constant-rate output, which duplicates
        # the first frame whenever the seek lands it half a frame "late" —
        # shifting the whole picture one frame behind its sound.
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{ss:.6f}", "-i", str(source),
               "-frames:v", str(count), "-an", "-sn", "-dn", "-fps_mode", "passthrough"]
        if filters:
            cmd += ["-vf", ",".join(filters)]
        cmd += ["-f", "rawvideo", "-pix_fmt", pix, "-"]
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=self.log,
                                     bufsize=self.frame_bytes * 2)
        self.buf = bytearray(self.frame_bytes)

    def read(self) -> Optional[bytearray]:
        view = memoryview(self.buf)
        got = 0
        while got < self.frame_bytes:
            n = self.proc.stdout.readinto(view[got:])
            if not n:
                return None
            got += n
        return self.buf

    def close(self) -> None:
        try:
            self.proc.stdout.close()
        except OSError:
            pass
        self.proc.wait()
        self.log.close()


# --- analysis: scene cuts and faces ----------------------------------------------

class _FaceDetector:
    def __init__(self, width: int, height: int):
        import cv2
        self.cv2 = cv2
        self.w, self.h = width, height
        self.min_w = max(12, int(0.035 * width))
        self.kind = "none"
        model = BASE_DIR / "models" / "face_detection_yunet_2023mar.onnx"
        if model.exists() and hasattr(cv2, "FaceDetectorYN"):
            try:
                self.net = cv2.FaceDetectorYN.create(str(model), "", (width, height), 0.6, 0.3, 50)
                self.kind = "yunet"
            except Exception:
                self.kind = "none"
        if self.kind == "none" and hasattr(cv2, "CascadeClassifier"):
            cascade = Path(getattr(cv2, "data").haarcascades) / "haarcascade_frontalface_default.xml"
            if cascade.exists():
                self.cascade = cv2.CascadeClassifier(str(cascade))
                self.kind = "haar"

    def detect(self, frame: np.ndarray, gray: np.ndarray):
        """Faces as (cx, cy, w, h, mouth), box in fractions of the frame; `mouth`
        is a small normalised patch of the mouth (YuNet only) for spotting who
        is talking, else None."""
        out = []
        if self.kind == "yunet":
            _, faces = self.net.detect(frame)
            for f in (faces if faces is not None else []):
                x, y, w, h = float(f[0]), float(f[1]), float(f[2]), float(f[3])
                if w >= self.min_w:
                    out.append(((x + w / 2) / self.w, (y + h / 2) / self.h, w / self.w, h / self.h,
                                self._mouth(gray, f)))
        elif self.kind == "haar":
            g = self.cv2.equalizeHist(gray)
            found = self.cascade.detectMultiScale(g, scaleFactor=1.1, minNeighbors=6,
                                                  minSize=(self.min_w, self.min_w))
            for (x, y, w, h) in found:
                out.append(((x + w / 2) / self.w, (y + h / 2) / self.h, w / self.w, h / self.h, None))
        return out

    def _mouth(self, gray: np.ndarray, f) -> Optional[np.ndarray]:
        """The lips and jaw, cut out along the mouth corners so head movement
        mostly cancels out; what is left changing is the mouth itself."""
        rx, ry, lx, ly = float(f[10]), float(f[11]), float(f[12]), float(f[13])
        mw = math.hypot(lx - rx, ly - ry)
        if mw < 4:
            return None
        mx, my = (rx + lx) / 2, (ry + ly) / 2 + 0.15 * mw
        ang = math.degrees(math.atan2(ly - ry, lx - rx))
        pw, ph = 32, 24
        scale = pw / (1.7 * mw)
        M = self.cv2.getRotationMatrix2D((mx, my), ang, scale)
        M[0, 2] += pw / 2 - mx
        M[1, 2] += ph / 2 - my
        patch = self.cv2.warpAffine(gray, M, (pw, ph), flags=self.cv2.INTER_LINEAR,
                                    borderMode=self.cv2.BORDER_REPLICATE).astype(np.float32)
        std = float(patch.std())
        if std < 2.0:
            return None
        return (patch - float(patch.mean())) / std


@dataclass
class Analysis:
    cuts: set                                   # source frames that open a new shot
    faces: Dict[int, List[Tuple[float, float, float, float, float]]]   # (cx, cy, w, h, talking)
    detector: str
    # Where the picture is, top to bottom (fractions), when the source has black
    # bars — a call recorded side by side, letterboxed inside a 16:9 frame.
    bars: Dict[int, Tuple[float, float]] = field(default_factory=dict)


def _talking(raw: Dict[int, list], cuts: set, fps: float) -> Dict[int, List[Tuple[float, ...]]]:
    """Give every face a 'talking' score: how much its mouth changes between
    samples, averaged over half a second of the same face."""
    frames = sorted(raw)
    cut_list = sorted(cuts)
    act: Dict[Tuple[int, int], float] = {}
    for a, b in zip(frames, frames[1:]):
        if any(a < c <= b for c in cut_list):
            continue
        for j, fb in enumerate(raw[b]):
            if fb[4] is None:
                continue
            best, dist = None, 0.06
            for fa in raw[a]:
                d = abs(fa[0] - fb[0]) + abs(fa[1] - fb[1])
                if fa[4] is not None and d < dist and 0.7 < fb[2] / max(1e-6, fa[2]) < 1.4:
                    best, dist = fa, d
            if best is not None:
                act[(b, j)] = float(np.abs(fb[4] - best[4]).mean())
    half = max(1, int(0.5 * fps))
    out: Dict[int, List[Tuple[float, ...]]] = {}
    for f in frames:
        faces = []
        for j, face in enumerate(raw[f]):
            vals = []
            for g in frames[max(0, frames.index(f) - 8):frames.index(f) + 9]:
                if abs(g - f) > half or any(min(f, g) < c <= max(f, g) for c in cut_list):
                    continue
                for jj, other in enumerate(raw[g]):
                    if (g, jj) in act and abs(other[0] - face[0]) < 0.08:
                        vals.append(act[(g, jj)])
            faces.append((face[0], face[1], face[2], face[3], float(np.mean(vals)) if vals else 0.0))
        out[f] = faces
    return out


def analyse(source: Path, tl: Timeline, size: Tuple[int, int], want_faces: bool,
            vfr: bool = False) -> Analysis:
    """One quick low-resolution pass: where the source cuts, and where the faces are."""
    import cv2

    W, H = size
    aw = 480
    ah = _even(H * aw / W)
    detector = _FaceDetector(aw, ah) if want_faces else None
    if detector is not None and detector.kind == "none":
        detector = None
    pix = "bgr24" if (detector and detector.kind == "yunet") else "gray"
    step = max(1, round(float(tl.fps) / 10))            # ~10 face checks a second
    faces: Dict[int, List[Tuple[float, float, float, float]]] = {}
    bars: Dict[int, Tuple[float, float]] = {}
    cuts: set = set()
    for r, run in enumerate(tl.runs()):
        count = run.last - run.first
        dec = _Decoder(source, run.first, count, tl.fps, (aw, ah), pix, vfr=vfr, native=size)
        diffs: List[float] = []
        prev = None
        try:
            for k in range(count):
                buf = dec.read()
                if buf is None:
                    break
                if pix == "gray":
                    gray = np.frombuffer(buf, np.uint8).reshape(ah, aw)
                    frame = gray
                else:
                    frame = np.frombuffer(buf, np.uint8).reshape(ah, aw, 3)
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                small = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA).astype(np.int16)
                diffs.append(0.0 if prev is None else float(np.abs(small - prev).mean()) / 255.0)
                prev = small
                if k % step == 0:
                    # a bar row is black all the way across, not just a dark scene
                    rows = np.flatnonzero(np.percentile(gray, 98, axis=1) > LETTERBOX_LUMA)
                    if rows.size:
                        bars[run.first + k] = (rows[0] / ah, (rows[-1] + 1) / ah)
                if detector and k % step == 0:
                    faces[run.first + k] = detector.detect(frame, gray)
        finally:
            dec.close()
        cuts |= _scene_cuts(diffs, run.first, tl.fps)
        cuts.add(run.first)                         # every seek is a cut: nothing links across it
    return Analysis(cuts=cuts, faces=_talking(faces, cuts, float(tl.fps)),
                    detector=detector.kind if detector else "none", bars=bars)


def _scene_cuts(diffs: List[float], first: int, fps: Fraction) -> set:
    """Hard cuts in the source: a frame far more different from its neighbour
    than the frames around it are from theirs."""
    d = np.asarray(diffs, dtype=np.float64)
    cuts = set()
    n = len(d)
    win = 12
    min_gap = max(3, int(float(fps) * 0.3))
    last_cut = -10 ** 9
    for i in range(1, n):
        neigh = np.concatenate([d[max(1, i - win):i], d[i + 1:min(n, i + win + 1)]])
        base = float(np.median(neigh)) if neigh.size else 0.0
        if d[i] > 0.075 and d[i] > 3.0 * base + 0.02 and i - last_cut >= min_gap:
            cuts.add(first + i)
            last_cut = i
    return cuts


def _energy(source: Path, tl: Timeline) -> np.ndarray:
    """Loudness per output frame, 0 when the source is silent."""
    sr = 16000
    spf = sr / float(tl.fps)
    per_seg: Dict[int, np.ndarray] = {}
    for run in tl.runs():
        start = float(Fraction(run.first) / tl.fps)
        dur = float(Fraction(run.last - run.first) / tl.fps)
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.6f}", "-i", str(source),
             "-t", f"{dur:.6f}", "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
            capture_output=True,
        ).stdout
        x = np.frombuffer(raw, np.int16).astype(np.float64) / 32768.0
        csum = np.concatenate([[0.0], np.cumsum(x * x)])
        for i in run.segs:
            s, e = tl.segments[i]
            rel = np.arange(s, e) - run.first
            if x.size == 0:
                per_seg[i] = np.zeros(len(rel))
                continue
            a = np.clip((rel * spf).astype(np.int64), 0, x.size)
            b = np.clip(((rel + 1) * spf).astype(np.int64), 0, x.size)
            per_seg[i] = np.sqrt((csum[b] - csum[a]) / np.maximum(1, b - a))
    return np.concatenate([per_seg[i] for i in range(len(tl.segments))])


# --- the camera -------------------------------------------------------------------

@dataclass
class Camera:
    cx: np.ndarray          # view centre, source px
    cy: np.ndarray
    zoom: np.ndarray        # 1.0 = the widest 9:16 window the source allows
    rot: np.ndarray         # degrees
    sx: np.ndarray          # shake, output px
    sy: np.ndarray
    gain: np.ndarray        # exposure pop on impacts
    blur: np.ndarray        # motion-blur samples (1 = none)
    new_shot: np.ndarray    # a cut lands on this frame: never blur across it
    stats: Dict[str, Any]


TALK_FLOOR = 0.35          # fallback; really measured per clip (the median face)


def _pick_subject(faces, prev, crop_frac, floor=TALK_FLOOR):
    """Where the camera should point for one set of detections: the whole
    group if they fit, else whoever is talking, else whoever is closest."""
    if not faces:
        return None
    big = max(f[2] for f in faces)
    faces = [f for f in faces if f[2] >= 0.45 * big]       # ignore far background faces
    if len(faces) > 1:
        x0 = min(f[0] - f[2] / 2 for f in faces)
        x1 = max(f[0] + f[2] / 2 for f in faces)
        centre = (x0 + x1) / 2
        was_group = prev is not None and abs(prev[0] - centre) < 0.06
        if x1 - x0 <= (0.95 if was_group else 0.8) * crop_frac:   # everyone fits: frame the group
            wsum = sum(f[2] for f in faces)
            return (centre, sum(f[1] * f[2] for f in faces) / wsum, big)

    def score(f):
        return f[2] * (1.0 + 2.5 * max(0.0, f[4] - floor))

    best = max(faces, key=score)
    if prev is not None:                                   # stay on who we were on
        near = min(faces, key=lambda f: abs(f[0] - prev[0]))
        if abs(near[0] - prev[0]) < 0.10 and score(near) >= 0.75 * score(best):
            return (near[0], near[1], near[2])
    return (best[0], best[1], best[2])


def _hold_path(target: np.ndarray, fps: float, seed: Optional[float], dz: float,
               whips: bool) -> Tuple[np.ndarray, int, int]:
    """Operator-style follow: hold still, then move deliberately.

    Returns (path, reframes, whips). Works offline, so every move starts a beat
    before the subject moves instead of chasing it.
    """
    n = len(target)
    t = _fill_nan(target)
    if np.isnan(t).all():
        return np.full(n, np.nan), 0, 0
    m = _median_filter(t, max(3, int(0.7 * fps)) | 1)
    cur = float(m[0]) if seed is None or abs(float(m[0]) - seed) > dz else float(seed)
    path = np.full(n, cur)
    changes: List[Tuple[int, float]] = []
    i = 0
    while i < n:
        dist = abs(float(m[i]) - cur)
        if dist > dz:
            persist = int((0.5 if dist > WHIP_DISTANCE else 0.4) * fps)
            window = m[i:i + max(2, persist)]
            if len(window) >= 2 and float(np.median(np.abs(window - cur))) > dz:
                value = float(np.median(m[i:i + max(2, int(0.6 * fps))]))
                changes.append((i, value))
                cur = value
                i += max(1, persist)
                continue
        i += 1
    reframes = whip_count = 0
    for c, value in changes:
        base_at = max(0, c - 1)
        dist = abs(value - float(path[base_at]))
        if whips and dist > WHIP_DISTANCE:
            dur, whip_count = 0.30, whip_count + 1
        else:
            dur = min(0.8, 0.4 + 1.6 * dist)
            reframes += 1
        frames = max(3, int(round(dur * fps)))
        start = max(0, c - int(0.35 * frames))
        base = float(path[start])
        u = (np.arange(start, n) - start) / float(frames)
        path[start:] = base + (value - base) * _smootherstep(u)
    return path, reframes, whip_count


def plan_camera(tl: Timeline, an: Analysis, size: Tuple[int, int], edits: Dict[str, Any],
                layout: str, energy: Optional[np.ndarray],
                text: Optional[List[Tuple[float, float, float, float, int, int]]] = None,
                zoom_cap: Optional[float] = None) -> Camera:
    """`text`: the creator's burned-in words as (x0, y0, x1, y1 source px, first, last source frame):
    a fill crop is moved so it never cuts through them (stats["text"] says how, or that it can't).
    `zoom_cap`: the most the camera may punch in (keeps words near a whole-frame layout's edges)."""
    W, H = size
    fps = float(tl.fps)
    src = tl.src_index()
    N = len(src)
    motion = bool(edits.get("motion", True))
    # punchy: whip pans, hard punch-ins, shake and flash on the loud hits (funny,
    # hype, reactions). calm: the same camera without the fireworks, for stories,
    # emotional moments and anything serious — glides instead of whips, gentle
    # punch-ins, a soft push on impacts, no shake, no flash.
    calm = edits.get("motion_style", "punchy") == "calm"
    gap = int(GAP_SECONDS * fps)

    # 1. Where the picture changes: our own jump cuts, and the source's cuts.
    new_shot = np.zeros(N, bool)
    jump = np.zeros(N, bool)
    scene = np.zeros(N, bool)
    new_shot[0] = scene[0] = True
    n = 0
    cuts = an.cuts
    for k, (s, e) in enumerate(tl.segments):
        if k > 0:
            new_shot[n] = True
            prev_e = tl.segments[k - 1][1]
            # Back in time, far ahead, or across a source cut: a new scene.
            # A short skip forward (dead air cut out) is a jump cut.
            if s < prev_e or s - prev_e > gap or any(c in cuts for c in range(prev_e, s + 1)):
                scene[n] = True
            else:
                jump[n] = True
        for c in cuts:
            if s < c < e:
                new_shot[n + (c - s)] = True
                scene[n + (c - s)] = True
        n += e - s
    bounds = list(np.flatnonzero(new_shot)) + [N]

    # 2. The base window: the widest 9:16 rectangle the source allows.
    if W / H >= 9 / 16:
        cw, ch = H * 9 / 16, float(H)
    else:
        cw, ch = float(W), W * 16 / 9
    crop_frac = cw / W

    cx = np.full(N, W / 2.0)
    cy = np.full(N, H / 2.0)
    reframes = whips = 0
    tracked = False

    auto = bool(edits.get("auto_frame", True)) and edits.get("crop_auto", True) is not False
    if layout == "fill" and not auto:
        x = float(edits.get("crop_x", 0.5) if edits.get("crop_x") is not None else 0.5)
        cx[:] = cw / 2 + max(0.0, min(1.0, x)) * (W - cw)
    elif layout == "fill" and an.faces:
        # Subject per detection, in source order, forgetting who it was at each
        # cut. Once on someone, stay at least 1.2 s, and only move to someone
        # else after they have been the better choice for 0.6 s — no ping-pong.
        det_frames = sorted(an.faces)
        cut_list = sorted(cuts)
        talk = [f[4] for v in an.faces.values() for f in v if f[4] > 0]
        floor = float(np.median(talk)) if len(talk) >= 20 else TALK_FLOOR
        picks: Dict[int, Tuple[float, float, float]] = {}
        cur = pending = None
        cur_seen = last_switch = pending_since = -1e9
        ci = 0
        for f in det_frames:
            t = f / fps
            while ci < len(cut_list) and cut_list[ci] <= f:
                cur = pending = None
                last_switch = -1e9
                ci += 1
            faces_f = an.faces[f]
            p = _pick_subject(faces_f, cur, crop_frac, floor)
            if p is None:
                if cur is not None and t - cur_seen <= 0.8:
                    picks[f] = cur
                continue
            if cur is None:
                cur, cur_seen, last_switch, pending = p, t, t, None
            elif abs(p[0] - cur[0]) < 0.10:
                cur, cur_seen, pending = p, t, None
            else:
                if pending is None or abs(p[0] - pending[0]) >= 0.10:
                    pending, pending_since = p, t
                if (t - pending_since >= 0.6 and t - last_switch >= 1.2) or t - cur_seen > 0.8:
                    cur, cur_seen, last_switch, pending = p, t, t, None
                else:
                    same = [g for g in faces_f if abs(g[0] - cur[0]) < 0.10]
                    if same:
                        g = max(same, key=lambda g: g[2])
                        cur, cur_seen = (g[0], g[1], g[2]), t
            picks[f] = cur
        shot_edges = [tl.first] + cut_list + [tl.last + 1]
        pick_frames = np.array(sorted(picks), dtype=np.int64)
        px = np.array([picks[f][0] for f in pick_frames]) if len(pick_frames) else np.array([])
        py = np.array([picks[f][1] for f in pick_frames]) if len(pick_frames) else np.array([])
        head = 0.10                                        # face sits above centre
        prev_end_x = prev_end_y = None
        for a, b in zip(bounds[:-1], bounds[1:]):
            seg_src = src[a:b]
            lo = max(e for e in shot_edges if e <= seg_src[0])
            hi = min(e for e in shot_edges if e > seg_src[0])
            sel = (pick_frames >= lo) & (pick_frames < hi) if len(pick_frames) else np.array([], bool)
            if sel.any():
                tx = np.interp(seg_src, pick_frames[sel], px[sel])
                ty = np.interp(seg_src, pick_frames[sel], py[sel]) + head
            else:
                tx = np.full(b - a, np.nan)
                ty = np.full(b - a, np.nan)
            seed_x = prev_end_x if (jump[a] and prev_end_x is not None) else None
            seed_y = prev_end_y if (jump[a] and prev_end_y is not None) else None
            path_x, r, w = _hold_path(tx, fps, seed_x, DEADZONE_CROP * crop_frac, whips=motion and not calm)
            path_y, _, _ = _hold_path(ty, fps, seed_y, 0.05, whips=False)
            reframes += r
            whips += w
            if np.isnan(path_x).all():
                path_x = np.full(b - a, prev_end_x if (jump[a] and prev_end_x is not None) else 0.5)
                path_y = np.full(b - a, 0.5)
            else:
                tracked = True
            cx[a:b] = path_x * W
            cy[a:b] = path_y * H
            prev_end_x, prev_end_y = float(path_x[-1]), float(path_y[-1])

    # 3. Zoom: alternate on jump cuts, creep in inside a shot, punch on impacts.
    zoom = np.ones(N)
    level = 1.0
    shot_t0 = 0
    if calm:
        level_up = 1.05 if layout == "fill" else 1.04
    else:
        level_up = PUNCH_LEVEL if layout == "fill" else 1.06
    for n_ in range(N):
        if scene[n_]:
            level, shot_t0 = 1.0, n_
        elif jump[n_]:
            level = level_up if level == 1.0 else 1.0
        push = 1.0
        if layout == "fill":
            push = 1.0 + min(PUSH_MAX, PUSH_RATE * (n_ - shot_t0) / fps)
        zoom[n_] = level * push if motion else 1.0

    sx = np.zeros(N)
    sy = np.zeros(N)
    rot = np.zeros(N)
    gain = np.ones(N)
    impacts: List[int] = []
    if motion and energy is not None and len(energy) == N and N > fps * 3:
        impacts = _impacts(energy, new_shot, fps, N)
        next_bound = np.empty(N, dtype=np.int64)
        nb = N
        for n_ in range(N - 1, -1, -1):
            next_bound[n_] = nb
            if new_shot[n_]:
                nb = n_
        punch = np.ones(N)
        hit, rest = (IMPACT_ZOOM / 2, IMPACT_SETTLE / 2) if calm else (IMPACT_ZOOM, IMPACT_SETTLE)
        attack = 0.25 if calm else 0.09                      # calm eases in instead of snapping
        for p in impacts:
            a0 = max(0, p - int(0.05 * fps))
            end = next_bound[p]
            for n_ in range(a0, end):
                tau = (n_ - a0) / fps
                if tau < attack:
                    mult = 1 + hit * _ease_out_cubic(tau / attack)
                else:
                    settle = float(_smootherstep(np.array((tau - attack) / 0.5)))
                    mult = 1 + hit - (hit - rest) * settle
                punch[n_] = max(punch[n_], mult)
                if calm:
                    continue
                if tau < 0.6:
                    amp = SHAKE_PX * math.exp(-tau / 0.12)
                    sx[n_] += amp * math.sin(2 * math.pi * 12 * tau)
                    sy[n_] += amp * 0.7 * math.sin(2 * math.pi * 9 * tau + 1.1)
                    rot[n_] += 0.35 * math.exp(-tau / 0.12) * math.sin(2 * math.pi * 7 * tau + 0.5)
            if calm:
                continue
            if p < N:
                gain[p] = max(gain[p], 1.10)
            if p + 1 < N:
                gain[p + 1] = max(gain[p + 1], 1.04)
        zoom *= punch
        shake_room = 1 + (np.abs(sx) + np.abs(sy)) * 1.5 / OW
        zoom *= shake_room
    zoom = np.clip(zoom, 1.0, MAX_ZOOM)

    # 3b. Black bars: a shot letterboxed inside the frame (a video call laid out
    # side by side) would leave them across the top and bottom of a 9:16 crop.
    # Zoom just far enough that the window fits inside the picture.
    pic_top = np.zeros(N)
    pic_bot = np.full(N, float(H))
    if layout == "fill" and an.bars and ch >= H - 1:
        bar_keys = np.array(sorted(an.bars), dtype=np.int64)
        for a, b in zip(bounds[:-1], bounds[1:]):
            seg_src = src[a:b]
            lo_i = int(np.searchsorted(bar_keys, seg_src.min(), side="left"))
            hi_i = int(np.searchsorted(bar_keys, seg_src.max(), side="right"))
            seen = [an.bars[int(k)] for k in bar_keys[lo_i:hi_i]]
            if not seen:
                continue
            top = float(np.median([t for t, _ in seen]))
            bot = float(np.median([e for _, e in seen]))
            tall = bot - top
            if 0.3 < tall < LETTERBOX_MIN and top > 0.04 and bot < 0.96:
                floor = min(LETTERBOX_MAX_ZOOM, 1.0 / tall)
                zoom[a:b] = np.maximum(zoom[a:b] * floor / max(1.0, float(zoom[a:b].min())), floor)
                pic_top[a:b], pic_bot[a:b] = top * H, bot * H
        zoom = np.clip(zoom, 1.0, LETTERBOX_MAX_ZOOM * 1.05)

    if zoom_cap is not None:
        zoom = np.minimum(zoom, max(1.0, float(zoom_cap)))
        if zoom_cap < 1.05:
            # no room to hide a shake's edges either: the hits stay, the shake goes
            sx[:] = 0.0
            sy[:] = 0.0
            rot[:] = 0.0

    # 4. Keep the window inside the picture at every zoom level.
    half_w = cw / (2 * zoom)
    half_h = ch / (2 * zoom)
    cx = np.clip(cx, half_w, W - half_w)
    cy = np.clip(cy, np.maximum(half_h, pic_top + half_h), np.maximum(np.maximum(half_h, pic_top + half_h),
                                                                     np.minimum(H - half_h, pic_bot - half_h)))

    # 4b. The creator's own words burned into the picture: never cut through them.
    text_stats: Dict[str, Any] = {}
    if text and layout == "fill" and auto:
        text_stats = _fit_text(tl, an, size, cw, ch, cx, cy, zoom, bounds, src, text,
                               allow_zoom=motion)

    # 5. Motion blur where the camera moves fast (180-degree shutter).
    blur = np.ones(N, dtype=np.int32)
    s = OW * zoom / cw
    for n_ in range(1, N):
        if new_shot[n_]:
            continue
        d = (abs(cx[n_] - cx[n_ - 1]) * s[n_] + abs(cy[n_] - cy[n_ - 1]) * s[n_]
             + abs(math.log(zoom[n_] / zoom[n_ - 1])) * OH / 2
             + abs(sx[n_] - sx[n_ - 1]) + abs(sy[n_] - sy[n_ - 1])
             + abs(math.radians(rot[n_] - rot[n_ - 1])) * OH / 2)
        blur[n_] = int(min(16, max(1, math.ceil(d / 5.0))))

    stats = {
        "tracked": tracked, "reframes": reframes, "whips": whips,
        "impacts": [round(p / fps, 2) for p in impacts],
        "jump_cuts": int(jump.sum()), "scene_cuts": int(scene[1:].sum()),
        "detector": an.detector, "blurred_frames": int((blur > 1).sum()),
        "style": "calm" if calm else "punchy",
        "text": text_stats,
    }
    return Camera(cx=cx, cy=cy, zoom=zoom, rot=rot, sx=sx, sy=sy, gain=gain,
                  blur=blur, new_shot=new_shot, stats=stats)


def _fit_text(tl: Timeline, an: Analysis, size: Tuple[int, int], cw: float, ch: float,
              cx: np.ndarray, cy: np.ndarray, zoom: np.ndarray, bounds: List[int], src: np.ndarray,
              text: List[Tuple[float, float, float, float, int, int]], allow_zoom: bool) -> Dict[str, Any]:
    """Shot by shot, move the crop (in place) so it never cuts through the creator's burned-in words:
    each block either whole inside the frame (first choice, when the face stays well framed) or
    wholly outside it — shifted aside, or a slightly tighter crop. A shot where neither works is
    counted in "failed": the caller then shows the whole picture instead."""
    W, H = size
    margin = textdetect.MARGIN * W
    shots = failed = 0
    modes: List[str] = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = src[a:b]
        lo, hi = int(seg.min()), int(seg.max())
        boxes = [r[:4] for r in text if r[4] <= hi and r[5] >= lo]
        if not boxes:
            continue
        hw, hh = cw / (2 * zoom[a:b]), ch / (2 * zoom[a:b])
        risky = np.zeros(b - a, bool)
        for box in boxes:
            risky |= textdetect.cuts(cx[a:b], cy[a:b], hw, hh, box, margin)
        if not risky.any():
            continue                                         # already whole, or already out of the frame
        rows = []
        for k in range(b - a):
            for f in an.faces.get(int(seg[k]), ()):
                rows.append((k, (f[0] - f[2] / 2) * W, (f[1] - f[3] / 2) * H,
                             (f[0] + f[2] / 2) * W, (f[1] + f[3] / 2) * H))
        got = textdetect.fit_window(cx[a:b], cy[a:b], zoom[a:b], cw, ch, W, H, boxes,
                                    np.array(rows) if rows else None, max_zoom=TEXT_MAX_ZOOM,
                                    margin=margin, allow_zoom=allow_zoom)
        if got is None:
            failed += b - a
            continue
        cx[a:b], cy[a:b], zoom[a:b] = got["cx"], got["cy"], got["zoom"]
        shots += 1
        modes += got["modes"]
    return {"shots_moved": shots, "modes": modes, "failed_frames": failed}


def _zoom_cap(boxes_out: List[Tuple[float, float, float, float]]) -> Optional[float]:
    """The most the camera may punch in (about the frame's centre) and still show every one of
    these output-px boxes whole, TEXT_EDGE px from the edge. None: no limit needed."""
    cap = None
    for (x0, y0, x1, y1) in boxes_out:
        for u, c in ((x0, OW / 2), (x1, OW / 2), (y0, OH / 2), (y1, OH / 2)):
            d = abs(u - c)
            if d > 1e-6:
                z = (c - TEXT_EDGE) / d
                cap = z if cap is None else min(cap, z)
    return None if cap is None else max(1.0, cap)


def _impacts(energy: np.ndarray, new_shot: np.ndarray, fps: float, N: int) -> List[int]:
    """The loud moments worth hitting: sharp rises to a local peak."""
    ref = float(np.percentile(energy, 95)) or 1.0
    r = energy / ref
    k = 3
    smooth = np.convolve(r, np.ones(k) / k, mode="same")
    look = max(2, int(0.4 * fps))
    half = max(2, int(0.5 * fps))
    shots = np.flatnonzero(new_shot)
    cands = []
    for n in range(int(0.8 * fps), N - int(0.6 * fps)):
        lo, hi = max(0, n - half), min(N, n + half + 1)
        if smooth[n] < 0.8 or smooth[n] < smooth[lo:hi].max():
            continue
        before = smooth[max(0, n - look):max(1, n - 2)]
        rise = smooth[n] - (float(before.mean()) if before.size else 0.0)
        if rise < 0.35:
            continue
        # a cut already hits here; do not stack a punch on top of it
        if any(0 <= n - c < int(0.25 * fps) for c in shots):
            continue
        cands.append((float(smooth[n] * rise), n))
    cands.sort(reverse=True)
    limit = max(1, min(4, int(N / fps / 10)))
    spacing = int(4.5 * fps)
    chosen: List[int] = []
    for _, n in cands:
        if all(abs(n - c) >= spacing for c in chosen):
            chosen.append(n)
        if len(chosen) >= limit:
            break
    return sorted(chosen)


# --- frame composition ------------------------------------------------------------

def _affine(cx, cy, zoom, rot, sx, sy, cw) -> np.ndarray:
    """Source px -> output px for the fill layout."""
    s = OW * zoom / cw
    th = math.radians(rot)
    c, si = math.cos(th), math.sin(th)
    a, b, d, e = s * c, -s * si, s * si, s * c
    return np.array([[a, b, OW / 2 + sx - (a * cx + b * cy)],
                     [d, e, OH / 2 + sy - (d * cx + e * cy)]], np.float64)


def _camera_affine(zoom, rot, sx, sy) -> np.ndarray:
    """Output px -> output px: punch/shake about the frame centre (blur/split)."""
    th = math.radians(rot)
    c, si = math.cos(th), math.sin(th)
    a, b, d, e = zoom * c, -zoom * si, zoom * si, zoom * c
    return np.array([[a, b, OW / 2 + sx - (a * OW / 2 + b * OH / 2)],
                     [d, e, OH / 2 + sy - (d * OW / 2 + e * OH / 2)]], np.float64)


def _then(outer: np.ndarray, inner: np.ndarray) -> np.ndarray:
    """Compose 2x3 affines: apply `inner`, then `outer`."""
    o = np.vstack([outer, [0, 0, 1]])
    i = np.vstack([inner, [0, 0, 1]])
    return (o @ i)[:2]


class _Frame:
    """A yuv420p frame as three planes."""
    __slots__ = ("y", "u", "v")

    def __init__(self, y, u, v):
        self.y, self.u, self.v = y, u, v


def _planes(buf, w, h) -> _Frame:
    ys = w * h
    cs = (w // 2) * (h // 2)
    y = np.frombuffer(buf, np.uint8, ys, 0).reshape(h, w)
    u = np.frombuffer(buf, np.uint8, cs, ys).reshape(h // 2, w // 2)
    v = np.frombuffer(buf, np.uint8, cs, ys + cs).reshape(h // 2, w // 2)
    return _Frame(y, u, v)


class _Warper:
    def __init__(self):
        import cv2
        self.cv2 = cv2

    def warp(self, fr: _Frame, M: np.ndarray, size=(OW, OH), dst: Optional[_Frame] = None,
             transparent: bool = False, fast: bool = False) -> _Frame:
        cv2 = self.cv2
        border = cv2.BORDER_TRANSPARENT if transparent else cv2.BORDER_REFLECT101
        Mc = M.copy()
        Mc[:, 2] /= 2.0
        w, h = size
        luma_flags = cv2.INTER_LINEAR if fast else cv2.INTER_CUBIC
        if dst is None:
            return _Frame(
                cv2.warpAffine(fr.y, M, (w, h), flags=luma_flags, borderMode=border),
                cv2.warpAffine(fr.u, Mc, (w // 2, h // 2), flags=cv2.INTER_LINEAR, borderMode=border),
                cv2.warpAffine(fr.v, Mc, (w // 2, h // 2), flags=cv2.INTER_LINEAR, borderMode=border),
            )
        cv2.warpAffine(fr.y, M, (w, h), dst=dst.y, flags=luma_flags, borderMode=border)
        cv2.warpAffine(fr.u, Mc, (w // 2, h // 2), dst=dst.u, flags=cv2.INTER_LINEAR, borderMode=border)
        cv2.warpAffine(fr.v, Mc, (w // 2, h // 2), dst=dst.v, flags=cv2.INTER_LINEAR, borderMode=border)
        return dst

    def blurred(self, src: _Frame, matrices: List[np.ndarray]) -> _Frame:
        """Average several warps: motion blur. Many samples are done at half size,
        since the result is smeared anyway."""
        cv2 = self.cv2
        half = len(matrices) >= 4
        scale = 0.5 if half else 1.0
        w, h = int(OW * scale), int(OH * scale)
        acc_y = np.zeros((h, w), np.float32)
        acc_u = np.zeros((h // 2, w // 2), np.float32)
        acc_v = np.zeros((h // 2, w // 2), np.float32)
        for M in matrices:
            Ms = M * scale
            fr = self.warp(src, Ms, (w, h), fast=True)
            cv2.accumulate(fr.y, acc_y)
            cv2.accumulate(fr.u, acc_u)
            cv2.accumulate(fr.v, acc_v)
        k = 1.0 / len(matrices)
        out = _Frame(cv2.convertScaleAbs(acc_y, alpha=k), cv2.convertScaleAbs(acc_u, alpha=k),
                     cv2.convertScaleAbs(acc_v, alpha=k))
        if half:
            out = _Frame(cv2.resize(out.y, (OW, OH), interpolation=cv2.INTER_LINEAR),
                         cv2.resize(out.u, (OW // 2, OH // 2), interpolation=cv2.INTER_LINEAR),
                         cv2.resize(out.v, (OW // 2, OH // 2), interpolation=cv2.INTER_LINEAR))
        return out


def _layout_base(layout: str, size: Tuple[int, int], cam_box: Dict[str, float]):
    """For blur/split: fixed source->output affines for each panel."""
    W, H = size
    if layout == "split":
        top_h, bot_h = SPLIT_TOP, OH - SPLIT_TOP
        bx, by = cam_box["x"] * W, cam_box["y"] * H
        bw, bh = max(8.0, cam_box["w"] * W), max(8.0, cam_box["h"] * H)
        sc = max(OW / bw, top_h / bh)
        top = np.array([[sc, 0, OW / 2 - sc * (bx + bw / 2)], [0, sc, top_h / 2 - sc * (by + bh / 2)]])
        sc2 = max(OW / W, bot_h / H)
        bot = np.array([[sc2, 0, OW / 2 - sc2 * W / 2], [0, sc2, bot_h / 2 - sc2 * H / 2]])
        return {"top": top, "bottom": bot, "top_h": top_h, "bot_h": bot_h}
    # blur: soft cover background + full-width foreground
    sb = max(OW / W, OH / H)
    bg = np.array([[sb, 0, OW / 2 - sb * W / 2], [0, sb, OH / 2 - sb * H / 2]])
    sf = OW / W
    fg = np.array([[sf, 0, 0.0], [0, sf, (OH - H * sf) / 2]])
    return {"bg": bg, "fg": fg}


STACK_H = OH // 2          # each panel of the stacked split


def _panel_affine(face: Optional[Tuple[float, float, float, float]], size: Tuple[int, int],
                  ph: int = STACK_H) -> np.ndarray:
    """Source -> panel (OW x ph) affine: a crop around one face, the face a
    little below the panel's middle so text along the top stays off it.
    No face: the whole frame, covering the panel."""
    W, H = size
    aspect = OW / ph
    if not face:
        s = max(OW / W, ph / H)
        return np.array([[s, 0, OW / 2 - s * W / 2], [0, s, ph / 2 - s * H / 2]])
    fx, fy, _, fh = face
    ch = min(float(H), max(fh * H * 2.6, H * 0.42))
    cw = ch * aspect
    if cw > W:
        cw, ch = float(W), W / aspect
    cx = min(max(fx * W, cw / 2), W - cw / 2)
    cy = min(max(fy * H - 0.10 * ch, ch / 2), H - ch / 2)
    s = OW / cw
    return np.array([[s, 0, OW / 2 - s * cx], [0, s, ph / 2 - s * cy]])


def _stack_base(an: "Analysis", size: Tuple[int, int]) -> Optional[Dict[str, Any]]:
    """The stacked split: two people side by side become one panel each, left
    person on top. One person in a wide shot goes on top with the whole scene
    below. None when the clip doesn't suit it (close-ups, cutting between
    angles) — a stack of the same close-up twice looks broken."""
    pairs, singles = [], []
    for faces in (an.faces or {}).values():
        fs = sorted((f for f in faces if f[3] > 0.06), key=lambda f: f[0])
        if len(fs) >= 2:
            a, b = fs[0], fs[-1]
            if b[0] - a[0] > 0.22 and 0.5 < a[3] / max(b[3], 1e-6) < 2.0:
                pairs.append((a[:4], b[:4]))
                continue
        if fs:
            singles.append(max(fs, key=lambda f: f[3])[:4])
    med = lambda boxes: tuple(float(np.median([b[i] for b in boxes])) for i in range(4))
    total = len(pairs) + len(singles)
    if total and len(pairs) >= 0.6 * total:
        top, bottom = med([p[0] for p in pairs]), med([p[1] for p in pairs])
        kind = "two"
    elif total and len(singles) >= 0.8 * total and med(singles)[3] < 0.2:
        top, bottom = med(singles), None
        kind = "one"
    else:
        return None
    return {"top": _panel_affine(top, size), "bottom": _panel_affine(bottom, size),
            "top_h": STACK_H, "bot_h": OH - STACK_H, "kind": kind}


def _window(M: np.ndarray, ph: float) -> Tuple[float, float, float, float]:
    """The source window (x0, y0, w, h) an unrotated panel affine shows in an OW x ph panel."""
    s = float(M[0, 0])
    return (-float(M[0, 2]) / s, -float(M[1, 2]) / s, OW / s, ph / s)


def _cover_affine(win: Tuple[float, float, float, float], ph: float) -> np.ndarray:
    """Source window -> an OW x ph panel, filling it (the window has the panel's shape)."""
    x0, y0, w, h = win
    s = OW / w
    return np.array([[s, 0, OW / 2 - s * (x0 + w / 2)], [0, s, ph / 2 - s * (y0 + h / 2)]])


def _fit_panel(win: Tuple[float, float, float, float], size: Tuple[int, int], boxes: List[Tuple[float, ...]],
               faces: List[Tuple[float, float, float, float]], allow_zoom: bool
               ) -> Tuple[Optional[Tuple[float, float, float, float]], List[str]]:
    """A fixed panel window moved (or tightened) so no burned-in text block is cut by it.
    Returns (window, modes); (None, []) when that can't be done with the faces kept framed."""
    W, H = size
    x0, y0, w, h = win
    cx, cy = np.array([x0 + w / 2]), np.array([y0 + h / 2])
    hw, hh = np.array([w / 2]), np.array([h / 2])
    m = textdetect.MARGIN * W
    if not any(textdetect.cuts(cx, cy, hw, hh, b[:4], m).any() for b in boxes):
        return win, []
    # every block near the window has to come out whole too, not just the ones cut now
    near = [b[:4] for b in boxes if b[2] > x0 - w and b[0] < x0 + 2 * w and b[3] > y0 - h and b[1] < y0 + 2 * h]
    rows = np.array([(0, *f) for f in faces]) if faces else None
    got = textdetect.fit_window(cx, cy, np.array([1.0]), w, h, W, H, near[:3], rows,
                                max_zoom=TEXT_MAX_ZOOM, margin=m, allow_zoom=allow_zoom)
    if got is None:
        return None, []
    z = float(got["zoom"][0])
    nw, nh = w / z, h / z
    return (float(got["cx"][0]) - nw / 2, float(got["cy"][0]) - nh / 2, nw, nh), got["modes"]


def _boxes_out(M: np.ndarray, ph: float, y_off: float, boxes: List[Tuple[float, ...]]) -> List[Tuple[float, ...]]:
    """Text blocks wholly shown in a panel, in output px."""
    out = []
    for b in boxes:
        x0, y0 = M[0, 0] * b[0] + M[0, 2], M[1, 1] * b[1] + M[1, 2]
        x1, y1 = M[0, 0] * b[2] + M[0, 2], M[1, 1] * b[3] + M[1, 2]
        if x0 >= -1 and x1 <= OW + 1 and y0 >= -1 and y1 <= ph + 1:
            out.append((x0, y0 + y_off, x1, y1 + y_off))
    return out


def _split_setup(size: Tuple[int, int], cam_box: Dict[str, float], boxes: List[Tuple[float, ...]],
                 faces: List[Tuple[float, float, float, float]], allow_zoom: bool) -> Dict[str, Any]:
    """The facecam split: the facecam scaled big in the top panel, the content (game, the video
    being reacted to, the charts) in the bottom one, centred on the content rather than on the
    facecam's corner. Neither panel's edge may cut through burned-in words: the content window
    moves or tightens, and when it can't, the content panel shows the whole picture instead."""
    W, H = size
    top_h, bot_h = SPLIT_TOP, OH - SPLIT_TOP
    bx, by = cam_box["x"] * W, cam_box["y"] * H
    bw, bh = max(8.0, cam_box["w"] * W), max(8.0, cam_box["h"] * H)
    sc = max(OW / bw, top_h / bh)
    top_win = (bx + bw / 2 - OW / sc / 2, by + bh / 2 - top_h / sc / 2, OW / sc, top_h / sc)
    in_cam = [f for f in faces if bx <= (f[0] + f[2]) / 2 <= bx + bw and by <= (f[1] + f[3]) / 2 <= by + bh]
    others = [f for f in faces if f not in in_cam]
    fitted, top_modes = _fit_panel(top_win, size, boxes, in_cam, allow_zoom)
    top = _cover_affine(fitted or top_win, top_h)

    # the content: the widest window of the bottom panel's shape, centred on what isn't the facecam
    asp = OW / bot_h
    ww, wh = (H * asp, float(H)) if W / H >= asp else (float(W), W / asp)
    cxc = W / 2
    if bx > W * 0.45 and bx > 0.5 * ww:                 # facecam on the right: centre on what's left of it
        cxc = bx / 2
    elif bx + bw < W * 0.55 and W - (bx + bw) > 0.5 * ww:
        cxc = (bx + bw + W) / 2
    cxc = min(max(cxc, ww / 2), W - ww / 2)
    fitted, bot_modes = _fit_panel((cxc - ww / 2, (H - wh) / 2, ww, wh), size, boxes, others, allow_zoom)
    out: Dict[str, Any] = {"top_h": top_h, "bot_h": bot_h, "top": top, "content": "crop",
                           "modes": top_modes + bot_modes}
    if fitted is None:
        # the whole picture, full width, over a soft blurred copy of itself
        sb, sf = max(OW / W, bot_h / H), OW / W
        out["bottom"] = np.array([[sb, 0, OW / 2 - sb * W / 2], [0, sb, bot_h / 2 - sb * H / 2]])
        out["bottom_fit"] = np.array([[sf, 0, 0.0], [0, sf, (bot_h - H * sf) / 2]])
        out["content"] = "whole"
        shown = out["bottom_fit"]
    else:
        out["bottom"] = _cover_affine(fitted, bot_h)
        shown = out["bottom"]
    out["boxes_out"] = _boxes_out(top, top_h, 0, boxes) + _boxes_out(shown, bot_h, top_h, boxes)
    return out


def _blurred_cover(warper: "_Warper", fr: _Frame, M: np.ndarray, w: int, h: int) -> _Frame:
    """A soft, slightly darkened copy of the picture filling a w x h area (behind a whole-frame picture)."""
    cv2 = warper.cv2
    small = warper.warp(fr, M / 8.0, (w // 8, h // 8), fast=True)
    sizes = ((w, h), (w // 2, h // 2), (w // 2, h // 2))
    bg = _Frame(*(np.ascontiguousarray(cv2.resize(cv2.GaussianBlur(p, (0, 0), 5), sz,
                                                  interpolation=cv2.INTER_LINEAR))
                  for p, sz in zip((small.y, small.u, small.v), sizes)))
    bg.y = cv2.convertScaleAbs(bg.y, alpha=0.82)
    return bg


def _compose(warper: _Warper, layout: str, base, fr: _Frame) -> _Frame:
    if layout in ("split", "stack"):
        top = warper.warp(fr, base["top"], (OW, base["top_h"]))
        if base.get("bottom_fit") is not None:
            bg = _blurred_cover(warper, fr, base["bottom"], OW, base["bot_h"])
            bot = warper.warp(fr, base["bottom_fit"], (OW, base["bot_h"]), dst=bg, transparent=True)
        else:
            bot = warper.warp(fr, base["bottom"], (OW, base["bot_h"]))
        return _Frame(np.vstack([top.y, bot.y]), np.vstack([top.u, bot.u]), np.vstack([top.v, bot.v]))
    # blur
    bg = _blurred_cover(warper, fr, base["bg"], OW, OH)
    return warper.warp(fr, base["fg"], dst=bg, transparent=True)


# --- audio --------------------------------------------------------------------------

def _audio_graph(tl: Timeline, labels: Sequence[str]) -> str:
    """Cut the source audio at exactly the video's frame boundaries.

    `labels` holds one audio input per decode run, each opened with its own
    seek to that run's first frame, so a stitched clip never has to read the
    minutes between its parts."""
    parts = []
    names = []
    fade = 0.008
    for r, run in enumerate(tl.runs()):
        label = labels[r]
        for k in run.segs:
            s, e = tl.segments[k]
            a = float(Fraction(s - run.first) / tl.fps)
            b = float(Fraction(e - run.first) / tl.fps)
            length = b - a
            f = min(fade, length / 4)
            parts.append(
                f"[{label}]atrim=start={a:.6f}:end={b:.6f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d={f:.4f},afade=t=out:st={max(0.0, length - f):.6f}:d={f:.4f}[s{k}]"
            )
            names.append(f"[s{k}]")
    names.sort(key=lambda x: int(x[2:-1]))              # play order
    if len(names) == 1:
        parts.append(f"{names[0]}anull[acut]")
    else:
        parts.append(f"{''.join(names)}concat=n={len(names)}:v=0:a=1[acut]")
    return ";".join(parts)


def _audio_inputs(source: Path, tl: Timeline, first_index: int) -> Tuple[List[str], List[str]]:
    """ffmpeg input args and stream labels: one seeked input per decode run."""
    args: List[str] = []
    labels: List[str] = []
    for r, run in enumerate(tl.runs()):
        args += ["-ss", f"{float(Fraction(run.first) / tl.fps):.6f}", "-i", str(source)]
        labels.append(f"{first_index + r}:a")
    return args, labels


def _loudness_gain(source: Path, tl: Timeline, target: float = -14.0) -> float:
    """dB of gain to bring the cut audio to `target` LUFS without clipping.
    Measured first, then applied as plain gain: no lookahead, no timing shift."""
    args, labels = _audio_inputs(source, tl, 0)
    graph = _audio_graph(tl, labels)
    if graph.endswith("anull[acut]"):
        graph = graph[:-len("anull[acut]")] + "ebur128=peak=true[aout]"
    else:
        graph = graph.replace("[acut]", "[acut];[acut]ebur128=peak=true[aout]")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", *args,
         "-filter_complex", graph, "-map", "[aout]", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    text = proc.stderr or ""
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else text
    m_i = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summary)
    m_p = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary)
    try:
        loud = float(m_i.group(1)) if m_i else float("nan")
        peak = float(m_p.group(1)) if m_p else 0.0
    except ValueError:
        return 0.0
    if not math.isfinite(loud) or loud < -60:
        return 0.0
    gain = target - loud
    # allow the limiter a few dB of work, never more
    return float(max(-20.0, min(gain, (-1.0 - peak) + 4.0, 18.0)))


# --- the render -----------------------------------------------------------------------

# x264 is led by quality (CRF) with a ceiling on top. Without one, busy pictures —
# film grain above all, new noise every frame — made x264 spend 50-60 Mbps (a 28 s
# edit came out at 180 MB). 11 Mbps on average, with a 22 Mbit buffer for the busy
# seconds, keeps a 1080×1920 picture clean; TikTok, Reels and Shorts re-encode to
# far less anyway. Ordinary talking-head clips stay well under it.
VIDEO_CAP = ("-maxrate", "11M", "-bufsize", "22M")


def _escape(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", r"\:")


def _to_output(layout: str, base: Optional[Dict[str, Any]], cam: "Camera", size: Tuple[int, int], n: int,
               box: Tuple[float, float, float, float]) -> List[Tuple[float, float, float, float]]:
    """Where a source box (px) lands on output frame n, as output px boxes — one per panel that
    shows its centre (a facecam's face can show in both halves of the split). [] if off screen."""
    W, H = size
    x0, y0, x1, y1 = box
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    z = float(cam.zoom[n]) or 1.0
    if layout == "fill":
        cw, ch = (H * 9 / 16, float(H)) if W / H >= 9 / 16 else (float(W), W * 16 / 9)
        hw, hh = cw / (2 * z), ch / (2 * z)
        left = min(max(float(cam.cx[n]) - hw, 0.0), W - 2 * hw)
        top = min(max(float(cam.cy[n]) - hh, 0.0), H - 2 * hh)
        if not (left <= mx <= left + 2 * hw):
            return []
        s = OH / (2 * hh)
        return [((x0 - left) * s, (y0 - top) * s, (x1 - left) * s, (y1 - top) * s)]
    if not base:
        return []
    if layout == "blur":
        panels = [(base["fg"], 0.0, float(OH))]
    else:
        lower = base["bottom_fit"] if base.get("bottom_fit") is not None else base["bottom"]
        panels = [(base["top"], 0.0, float(base["top_h"])), (lower, float(base["top_h"]), float(base["bot_h"]))]
    out = []
    for M, off, ph in panels:
        if not (0 <= M[0, 0] * mx + M[0, 2] <= OW and 0 <= M[1, 1] * my + M[1, 2] <= ph):
            continue
        b = [M[0, 0] * x0 + M[0, 2], M[1, 1] * y0 + M[1, 2] + off, M[0, 0] * x1 + M[0, 2], M[1, 1] * y1 + M[1, 2] + off]
        # the camera's punch-in, about the frame's centre
        out.append((OW / 2 + z * (b[0] - OW / 2), OH / 2 + z * (b[1] - OH / 2),
                    OW / 2 + z * (b[2] - OW / 2), OH / 2 + z * (b[3] - OH / 2)))
    return out


def _hook_clear_top(cam: "Camera", an: Analysis, tl: Timeline, size: Tuple[int, int],
                    seconds: float, top: int, block: int,
                    limit: Optional[int] = None, layout: str = "fill",
                    base: Optional[Dict[str, Any]] = None,
                    text: Optional[List[Tuple[float, ...]]] = None) -> Optional[int]:
    """Where the hook should sit so it never covers a face's eyes while it is up.

    At the top of the frame by default; when a face on screen in those first
    seconds would be under it (a wide stage shot puts the speaker's eyes right
    there), it moves to just below the chin — the first frame is the one the
    feed shows, and a hook over someone's eyes is the worst one. The creator's own
    burned-in words (`text`, source px boxes) shown in the frame are kept clear the
    same way, so two titles never sit on top of each other. None: leave it."""
    if block <= 0 or not (an.faces or text):
        return None
    W, H = size
    keys = np.array(sorted(an.faces), dtype=np.int64)
    src = tl.src_index()
    n_max = min(len(src), max(1, int(seconds * float(tl.fps))))
    eyes, chins, words = [], [], []
    for n in range(0, n_max, 2):
        i = int(np.searchsorted(keys, src[n], side="right")) - 1 if keys.size else -1
        if i >= 0 and src[n] - keys[i] <= float(tl.fps) * 0.5:
            for f in an.faces[int(keys[i])]:
                face = ((f[0] - f[2] / 2) * W, (f[1] - f[3] / 2) * H, (f[0] + f[2] / 2) * W, (f[1] + f[3] / 2) * H)
                for (_, y0, _, y1) in _to_output(layout, base, cam, size, n, face):
                    if y1 - y0 < 0.05 * OH:                  # a face in the far background
                        continue
                    eyes.append(y0 + 0.30 * (y1 - y0))
                    chins.append(y1)
        for b in text or []:
            if len(b) > 5 and not (b[4] <= src[n] <= b[5]):
                continue
            for (bx0, y0, bx1, y1) in _to_output(layout, base, cam, size, n, tuple(b[:4])):
                if bx0 >= -2 and bx1 <= OW + 2 and y0 >= -2 and y1 <= OH + 2:
                    words.append((y0, y1))
    if not eyes and not words:
        return None

    def cost(t: float) -> float:
        """How much it covers: eyes count three times, the creator's own words once."""
        over = lambda a, b: max(0.0, min(t + block, b) - max(t, a))  # noqa: E731
        return (3 * max([over(e, c) for e, c in zip(eyes, chins)] or [0.0])
                + max([over(y0 - 10, y1 + 10) for y0, y1 in words] or [0.0]))

    if cost(top) == 0:                                   # it only covers hair and forehead: fine
        return None
    stop = captions.CAPTION_TOP if limit is None else limit
    spots = sorted({int(c + 40) for c in chins} | {int(y1 + 24) for _, y1 in words})
    spots = [t for t in spots if t > top and t + block <= stop]
    for t in spots:
        if cost(t) == 0:
            return t
    # nowhere is clear: the spot covering least, if it beats where it is
    best = min(spots, key=cost, default=None)
    return best if best is not None and cost(best) < cost(top) else None


def _split_hook_top(cam: "Camera", an: Analysis, tl: Timeline, size: Tuple[int, int], base: Dict[str, Any],
                    block: int, limit: int, floor: int) -> int:
    """The split layout's hook: just above the seam between the face and the content, under the
    chin — near the split line, off both the face and the content."""
    W, H = size
    keys = np.array(sorted(an.faces), dtype=np.int64)
    src = tl.src_index()
    chin = None
    for n in range(0, min(len(src), max(1, int(2.4 * float(tl.fps)))), 2):
        i = int(np.searchsorted(keys, src[n], side="right")) - 1 if keys.size else -1
        if i < 0 or src[n] - keys[i] > float(tl.fps) * 0.5:
            continue
        for f in an.faces[int(keys[i])]:
            face = ((f[0] - f[2] / 2) * W, (f[1] - f[3] / 2) * H, (f[0] + f[2] / 2) * W, (f[1] + f[3] / 2) * H)
            for (_, y0, _, y1) in _to_output("split", base, cam, size, n, face):
                if y1 <= base["top_h"] + 4 and y1 - y0 >= 0.05 * OH:
                    chin = y1 if chin is None else max(chin, y1)
    top = limit - block
    if chin is not None and top < chin + 12:
        top = int(chin + 12)              # a long hook runs a little past the seam rather than over his mouth
    return int(max(floor, top))


# --- one sparse look at the clip: burned-in words, a small facecam ---------------------------------

def _look(source: Path, tl: Timeline, size: Tuple[int, int]) -> Tuple[List[Tuple[float, np.ndarray]],
                                                                      List[Tuple[float, float]]]:
    """Small frames every ~2 s of what the clip shows, and its spans in source seconds. Frames
    that don't match the source's shape (a rotated phone video read two ways) are dropped."""
    spans = [(float(Fraction(s) / tl.fps), float(Fraction(e) / tl.fps)) for s, e in tl.segments]
    try:
        frames = textdetect.sample_frames(source, textdetect.span_times(spans))
    except Exception:
        return [], spans
    W, H = size
    want = H / max(1, W)
    return [(t, f) for t, f in frames if abs(f.shape[0] / max(1, f.shape[1]) - want) <= 0.03 * want], spans


def _sample_faces(frames: List[Tuple[float, np.ndarray]]) -> List[framing.Face]:
    """Faces in the sampled frames, small ones too (a facecam), as fractions of the frame."""
    if not frames:
        return []
    try:
        detect, _ = framing._detector()
    except Exception:
        return []
    out: List[framing.Face] = []
    for t, fr in frames:
        h, w = fr.shape[:2]
        for (x, y, fw, fh) in detect(fr):
            if fw / w >= framing.FACECAM_MIN_FACE:
                out.append(framing.Face(t=t, x=float(x) / w, y=float(y) / h, w=float(fw) / w, h=float(fh) / h))
    return out


def _choose_layout(source: Path, tl: Timeline, size: Tuple[int, int], edits: Dict[str, Any], layout: str,
                   plan: Optional[FramingPlan]):
    """Which layout this clip really gets, from one sparse look at its own frames.

    Returns (layout, facecam box for the split or None, burned-in text regions, faces seen as
    source px boxes, notes). With the layout on Auto, a small facecam in THIS clip means the
    split (a facecam that shows in only part of a stream still gets it where it shows), and a
    clip where the camera is full screen gets the crop even if the rest of the video had a
    facecam. A split nobody can find a face for shows the whole picture instead of guessing."""
    W, H = size
    notes: List[str] = []
    frames, spans = _look(source, tl, size)
    regions = textdetect.find_regions(source, spans, frames=frames) if frames else []
    faces = _sample_faces(frames) if layout in ("fill", "split") else []
    face_px = [(f.x * W, f.y * H, (f.x + f.w) * W, (f.y + f.h) * H) for f in faces]
    count = max(1, len(frames))
    found = framing.find_facecam(faces, count, W / H, min_hits=0.45) if faces else None
    cam_box = found if framing.is_small_facecam(found) else None
    if (edits.get("layout") or "auto") == "auto" and layout in ("fill", "split") and frames:
        if cam_box is not None:
            layout = "split"
        elif layout == "split":
            full = {round(f.t, 2) for f in faces if f.w >= framing.MIN_FACE_FRACTION}
            if len(full) >= 0.4 * count:
                layout = "fill"                 # here the camera is full screen, not a facecam
    if layout == "split":
        if edits.get("facecam_manual") and edits.get("facecam"):
            cam_box = dict(edits["facecam"])
        elif cam_box is None:
            cam_box = found or (dict(plan.facecam) if plan and plan.facecam else None)
            if cam_box is None and faces:
                cam_box = framing._facecam_box(framing._cluster(faces)[0], W / H)
        if cam_box is None:
            layout = "blur"
            notes.append("Couldn't find a face for the facecam split, so the whole picture is shown — "
                         "set the facecam box under Framing to split it anyway.")
    if cam_box is not None:
        cam_box = {k: float(cam_box.get(k, d)) for k, d in (("x", 0.0), ("y", 0.0), ("w", 0.28), ("h", 0.30))}
    return layout, cam_box, regions, face_px, notes


def _what(r: "textdetect.Region") -> Tuple[str, str]:
    """Plain words for a block of burned-in text: ("the title at the top", "isn't")."""
    cy = (r.y0 + r.y1) / 2
    name = "title" if cy < 0.35 else ("captions" if cy > 0.65 else "words")
    return f"the {name} {r.where()}", ("aren't" if name in ("captions", "words") else "isn't")


def _stack_text(base: Dict[str, Any], size: Tuple[int, int], boxes: List[Tuple[float, ...]],
                allow_zoom: bool) -> Optional[Dict[str, Any]]:
    """The stacked split's two panels, moved so neither cuts burned-in words. None: they can't be."""
    out = dict(base)
    for key, ph in (("top", base["top_h"]), ("bottom", base["bot_h"])):
        win = _window(base[key], ph)
        x0, y0, w, h = win
        face = (x0 + w * 0.3, y0 + h * 0.25, x0 + w * 0.7, y0 + h * 0.75)   # whoever the panel was framed on
        fitted, _ = _fit_panel(win, size, boxes, [face], allow_zoom)
        if fitted is None:
            return None
        out[key] = _cover_affine(fitted, ph)
    out["boxes_out"] = (_boxes_out(out["top"], base["top_h"], 0, boxes)
                        + _boxes_out(out["bottom"], base["bot_h"], base["top_h"], boxes))
    return out


def render_clip(
    source: Path,
    clip_id: str,
    start: float,
    end: float,
    words: List[Dict[str, Any]],
    edits: Dict[str, Any],
    has_audio: bool,
    layout: str,
    plan: Optional[FramingPlan],
    source_size: Tuple[int, int],
    keep: Optional[List[Tuple[float, float]]] = None,
    debug: bool = False,
    segments: Optional[List[Tuple[float, float]]] = None,
    labels: Optional[List[Tuple[float, str]]] = None,
) -> Dict[str, Any]:
    """Render one clip. `segments` (absolute seconds, in play order) replaces
    start/end/keep for a stitched clip; `labels` are (output seconds, text)
    marks for its jumps in time."""
    import cv2

    W, H = int(source_size[0]), int(source_size[1])
    if W < 16 or H < 16:
        raise RuntimeError("source has no usable video stream")
    W, H = W - W % 2, H - H % 2
    fps = frame_rate(source)
    vfr = _is_vfr(source)
    tl = timeline_from_segments(segments, fps) if segments else build_timeline(start, end, keep, fps)
    runs = tl.runs()
    N = tl.total
    fps_str = f"{fps.numerator}/{fps.denominator}"

    auto_crop = bool(edits.get("auto_frame", True)) and edits.get("crop_auto", True) is not False
    # Faces are found in every layout: they also keep the hook and cards off people's eyes.
    an = analyse(source, tl, (W, H), want_faces=True, vfr=vfr)
    stack_base = _stack_base(an, (W, H)) if layout == "stack" else None
    stack_refused = layout == "stack" and stack_base is None
    if stack_refused:
        layout = "fill"                    # this clip doesn't suit a stack: follow the speaker instead
    layout, cam_box, regions, seen_faces, notes = _choose_layout(source, tl, (W, H), edits, layout, plan)
    text_px = [(r.x0 * W, r.y0 * H, r.x1 * W, r.y1 * H, int(math.floor(r.t0 * float(fps))),
                int(math.ceil(r.t1 * float(fps)))) for r in regions]
    energy = _energy(source, tl) if (has_audio and edits.get("motion", True)) else None
    allow_zoom = bool(edits.get("motion", True))
    cam = plan_camera(tl, an, (W, H), edits, layout, energy, text=text_px if auto_crop else None)
    cw = H * 9 / 16 if W / H >= 9 / 16 else float(W)
    widest = max(regions, key=lambda r: r.w) if regions else None
    base: Optional[Dict[str, Any]] = None
    if layout == "fill":
        tstats = cam.stats.get("text") or {}
        if tstats.get("failed_frames"):
            # No crop keeps those words whole and his face framed: show the whole picture —
            # under his facecam when there is one, else full width over a blurred copy.
            what, _ = _what(widest)
            if cam_box is not None:
                layout = "split"
                notes.append(f"Used the facecam split because of {what} — a vertical crop would cut its words in half.")
            else:
                layout = "blur"
                notes.append(f"Showed the whole picture because of {what} — a vertical crop would cut its words in half.")
        elif tstats.get("shots_moved"):
            what, verb = _what(widest)
            if all(m == "in" for m in tstats.get("modes") or []):
                notes.append(f"Moved the frame so {what} {verb} cut off.")
            else:
                notes.append(f"Kept {what} out of the frame, so no half-cut words show.")
        elif regions and not auto_crop:
            m = textdetect.MARGIN * W
            ch_ = float(H) if W / H >= 9 / 16 else W * 16 / 9
            hw, hh = cw / (2 * cam.zoom), ch_ / (2 * cam.zoom)
            if any(textdetect.cuts(cam.cx, cam.cy, hw, hh, b[:4], m).any() for b in text_px):
                what, _ = _what(widest)
                notes.append(f"Your crop cuts through {what} — drag Crop position a little, "
                             "or pick Blurred bars to show it whole.")
    if layout != "fill":
        if layout == "split":
            base = _split_setup((W, H), cam_box, text_px, seen_faces, allow_zoom)
            if base["content"] == "whole" and widest is not None:
                what, _ = _what(widest)
                notes.append(f"Showed the whole picture under the facecam because of {what}.")
            elif base["modes"] and widest is not None:
                what, verb = _what(widest)
                notes.append(f"Moved the content half so {what} {verb} cut off." if "in" in base["modes"]
                             else f"Kept {what} out of the content half, so no half-cut words show.")
        elif layout == "stack":
            base = stack_base
            if text_px and auto_crop:
                fitted = _stack_text(stack_base, (W, H), text_px, allow_zoom)
                if fitted is None:
                    layout, base = "blur", None
                    what, _ = _what(widest)
                    notes.append(f"Showed the whole picture because of {what} — the stacked split would cut its words.")
                else:
                    base = fitted
        if layout == "blur":
            base = _layout_base("blur", (W, H), {})
            base["boxes_out"] = _boxes_out(base["fg"], OH, 0, text_px)
        cap = _zoom_cap(base.get("boxes_out") or []) if base else None
        cam = plan_camera(tl, an, (W, H), edits, layout, energy, zoom_cap=cap)

    # --- captions and hook -------------------------------------------------------
    out_duration = float(Fraction(N) / fps)
    show_captions = bool(edits.get("captions_on", True)) and bool(words)
    hook_text = edits.get("hook", "") if edits.get("hook_on", True) else ""
    headline = edits.get("headline", "") if edits.get("headline_on", True) else ""
    marks = [(t, x) for t, x in (labels or []) if (x or "").strip()] if edits.get("labels_on", True) else []
    # A campaign's brand logo heads the frame; the text at the top moves down for it.
    brand = None
    if edits.get("brand_logo") and Path(edits["brand_logo"]).exists():
        from . import brandlogo
        brand = brandlogo.prepare(Path(edits["brand_logo"]), (OW, OH), brandlogo.SOURCE_MAX_W, brandlogo.SOURCE_MAX_H)
        brand["at"] = brandlogo.source_box((OW, OH), brand["w"], brand["h"])
    vchain = "[0:v]"
    caption_position = edits.get("caption_position", "bottom")
    if layout == "split" and caption_position in ("bottom", "pop"):
        # on the seam between his face and the content: low down they would cover the content
        caption_position = "middle"
    cap_style, cap_size = edits.get("caption_style", "impact"), float(edits.get("caption_size", 1.0))

    def text_top(default: int, block: int) -> Optional[int]:
        """Where a hook or card goes: clear of eyes and of the creator's own words on screen;
        in the split, just above the seam under his chin."""
        limit = captions.caption_top(caption_position, cap_style, cap_size) - 20
        if layout == "split" and base:
            return _split_hook_top(cam, an, tl, (W, H), base, block, min(limit, base["top_h"] - 8), default)
        return _hook_clear_top(cam, an, tl, (W, H), 2.4, default, block, limit, layout=layout, base=base,
                               text=text_px)

    if show_captions or hook_text.strip() or headline.strip() or marks:
        ass_path = WORK_DIR / f"{clip_id}.ass"
        offset = brandlogo.source_text_offset((OW, OH), brand["h"]) if brand else 0
        hook_top = None
        if edits.get("hook_y") is not None:
            hook_top = int(edits["hook_y"])          # placed by hand, or by the clip doctor
        elif hook_text.strip():
            hook_top = text_top(captions.HOOK_TOP + offset, captions.hook_block(hook_text, cap_style, cap_size))
        captions.build_ass(
            words=words if show_captions else [], duration=out_duration,
            style_name=edits.get("caption_style", "impact"),
            position=caption_position,
            size_scale=float(edits.get("caption_size", 1.0)), hook=hook_text,
            accent=edits.get("accent", ""), out_path=ass_path,
            labels=marks, headline=headline,
            top_offset=offset, hook_top=hook_top,
            look=edits.get("caption_look") or None,
        )
        vchain += f"subtitles='{_escape(ass_path)}':fontsdir='{_escape(FONTS_DIR)}',"
    vchain += "format=yuv420p[vcap]"
    graph = [vchain]
    vout = "[vcap]"

    inputs = ["-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{OW}x{OH}",
              "-framerate", fps_str, "-i", "-"]
    next_input = 1
    audio_labels: List[str] = []
    if has_audio:
        args, audio_labels = _audio_inputs(source, tl, next_input)
        inputs += args
        next_input += len(audio_labels)
    # Cards — the headline label, title bar, comment bubble — over the video,
    # under the logos. Each is a still PNG; overlay holds it for its time.
    for card in [c for c in (edits.get("cards") or []) if (c.get("text") or "").strip()]:
        from . import cards as card_images
        made = card_images.render(card)
        if not made:
            continue
        top_off = brandlogo.source_text_offset((OW, OH), brand["h"]) if brand else 0
        y = int(card["y"]) if card.get("y") is not None else captions.HOOK_TOP + top_off
        if card.get("y") is None:
            # Same rule as the hook: never over a face's eyes in the opening.
            moved = text_top(y, made["h"])
            y = moved if moved is not None else y
        a = max(0.0, float(card.get("start") or 0.0))
        b = min(out_duration, float(card.get("end") or out_duration))
        if b - a < 0.2:
            continue
        inputs += ["-i", str(made["path"])]
        x = (OW - made["w"]) // 2
        tag = f"[vcard{next_input}]"
        graph.append(f"{vout}[{next_input}:v]overlay={x}:{y}:enable='between(t,{a:.3f},{b:.3f})'"
                     f":format=yuv420,format=yuv420p{tag}")
        vout = tag
        next_input += 1
    if edits.get("logo") and BRAND_LOGO.exists():
        inputs += ["-i", str(BRAND_LOGO)]
        scale = max(0.05, min(0.4, float(edits.get("logo_scale", 0.16))))
        corners = _logo_corners()
        x, y = corners.get(edits.get("logo_corner", "top-right"), corners["top-right"])
        graph.append(f"[{next_input}:v]scale={int(OW * scale)}:-1[logo];"
                     f"{vout}[logo]overlay={x}:{y}[vbrand]")
        vout = "[vbrand]"
        next_input += 1
    if brand:
        inputs += ["-i", str(brand["png"])]
        graph.append(f"{vout}[{next_input}:v]overlay={brand['at'][0]}:{brand['at'][1]}:format=yuv420,format=yuv420p[vcamp]")
        vout = "[vcamp]"
        next_input += 1
    if audio_labels:
        graph.append(_audio_graph(tl, audio_labels))
        if edits.get("normalize_audio", True):
            gain = _loudness_gain(source, tl)
            # level=false: no automatic make-up gain; latency=true: the limiter's
            # lookahead is compensated, so it cannot delay the sound.
            graph.append(f"[acut]volume={gain:.2f}dB,alimiter=limit=0.891:attack=3:release=60:"
                         f"level=false:latency=true,aresample=48000[aout]")
        else:
            # Levels left exactly as recorded (a campaign that forbids touching
            # the audio): no gain and no limiter either.
            graph.append("[acut]aresample=48000[aout]")

    out_file = CLIP_DIR / f"{clip_id}.mp4"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-filter_complex", ";".join(graph), "-map", vout]
    if audio_labels:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *VIDEO_CAP, "-pix_fmt", "yuv420p",
            "-r", fps_str, "-g", str(2 * round(float(fps))), "-movflags", "+faststart",
            str(out_file)]

    enc_log = tempfile.TemporaryFile()
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=enc_log)
    warper = _Warper()

    clean_at = min(N - 1, int(min(1.0, out_duration / 3) * float(fps)))
    clean_path = THUMB_DIR / f"{clip_id}_clean.jpg"
    written = 0
    last_frame: Optional[_Frame] = None

    def state(n: int) -> Tuple[float, ...]:
        return (cam.cx[n], cam.cy[n], cam.zoom[n], cam.rot[n], cam.sx[n], cam.sy[n])

    def matrix(st) -> np.ndarray:
        cx, cy, z, r, sx, sy = st
        if layout == "fill":
            return _affine(cx, cy, z, r, sx, sy, cw)
        return _camera_affine(z, r, sx, sy)

    def emit(out: _Frame) -> None:
        for plane in (out.y, out.u, out.v):
            enc.stdin.write(np.ascontiguousarray(plane).data)

    dec: Optional[_Decoder] = None
    try:
        due = 0
        for run in runs:
            count = run.last - run.first
            keep_mask = np.zeros(count, bool)
            for i in run.segs:
                s_, e_ = tl.segments[i]
                keep_mask[s_ - run.first:e_ - run.first] = True
            due += int(keep_mask.sum())
            dec = _Decoder(source, run.first, count, fps, (W, H), "yuv420p", vfr=vfr, native=(W, H))
            for k in range(count):
                if written >= due:
                    break
                buf = dec.read()
                if buf is None:
                    break
                if not keep_mask[k]:
                    continue
                n = written
                src = _planes(buf, W, H)
                if layout != "fill":
                    src = _compose(warper, layout, base, src)
                cur = state(n)
                if cam.blur[n] > 1 and n > 0 and not cam.new_shot[n]:
                    prev = state(n - 1)
                    K = int(cam.blur[n])
                    mats = []
                    for j in range(K):
                        u = 0.5 + 0.5 * (j + 0.5) / K
                        mats.append(matrix(tuple(p + (c - p) * u for p, c in zip(prev, cur))))
                    out = warper.blurred(src, mats)
                else:
                    M = matrix(cur)
                    if layout != "fill" and abs(cur[2] - 1) < 1e-4 and not any(cur[3:]):
                        out = src
                    else:
                        out = warper.warp(src, M)
                if cam.gain[n] != 1.0:
                    out = _Frame(cv2.convertScaleAbs(out.y, alpha=float(cam.gain[n])), out.u, out.v)
                if n == clean_at:
                    i420 = np.concatenate([out.y.ravel(), out.u.ravel(), out.v.ravel()]).reshape(OH * 3 // 2, OW)
                    cv2.imwrite(str(clean_path), cv2.cvtColor(i420, cv2.COLOR_YUV2BGR_I420),
                                [cv2.IMWRITE_JPEG_QUALITY, 88])
                emit(out)
                last_frame = out          # only reused before the next decode, so no copy
                written += 1
            dec.close()
            dec = None
            # A part that ends early in the source: hold its last frame, so the
            # picture of every later part still lines up with its sound.
            while last_frame is not None and written < due:
                emit(last_frame)
                written += 1
        enc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    finally:
        if dec is not None:
            dec.close()
    code = enc.wait()
    enc_log.seek(0)
    err = enc_log.read().decode("utf-8", "replace").strip()
    enc_log.close()
    if code != 0 or not out_file.exists():
        tail = "\n".join(err.splitlines()[-12:])
        raise RuntimeError(f"encode failed ({code}):\n{tail}")

    thumb = THUMB_DIR / f"{clip_id}.jpg"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{min(1.0, out_duration / 3):.2f}", "-i", str(out_file),
                    "-frames:v", "1", "-q:v", "4", str(thumb)], capture_output=True)
    if not clean_path.exists():
        clean_path = thumb

    st = cam.stats
    st["parts"] = len(runs)
    manual = edits.get("crop_auto", True) is False or not edits.get("auto_frame", True)
    if layout == "stack":
        kind = "static"
        note = ("Stacked split — one person per panel." if base and base.get("kind") == "two"
                else "Stacked split — the speaker on top, the whole scene below.")
    elif layout == "split":
        # "facecam" keeps the split on every re-render, doctor fix and undo while the layout is Auto
        kind, note = "facecam", "Facecam split — the face big on top, the content below it."
    elif layout != "fill":
        kind, note = "static", "The whole picture over a blurred copy of itself, with seamless cuts."
    elif manual:
        kind, note = "static", "Fixed crop where you set it, with seamless motion."
    elif st["tracked"]:
        def many(n: int, word: str) -> str:
            return f"{n} {word}{'' if n == 1 else 's'}"
        calm = st.get("style") == "calm"
        bits = [many(st["reframes"], "smooth reframe")]
        if st["whips"]:
            bits.append(many(st["whips"], "whip pan"))
        if st["jump_cuts"]:
            bits.append(many(st["jump_cuts"], "gentle punch-in" if calm else "punch-in cut"))
        if st["impacts"]:
            bits.append(many(len(st["impacts"]), "soft push-in" if calm else "impact zoom"))
        who = "calm camera for a story moment" if calm else "punchy camera"
        kind, note = "track", f"Following the speaker, {who} — " + ", ".join(bits) + "."
    else:
        kind, note = "static", "No faces to follow — centred, with seamless motion."
    if stack_refused:
        note = "Stacked split didn't suit this clip (close-ups or changing angles) — " + note[0].lower() + note[1:]
    if notes:
        note = " ".join([note] + notes)
    step = max(1, int(float(fps) / 2))
    src_idx = tl.src_index()
    track = [(float(Fraction(int(src_idx[n])) / fps), float(cam.cx[n] / W)) for n in range(0, N, step)]
    result_plan = FramingPlan(kind=kind, facecam=cam_box or (plan.facecam if plan else None), track=track,
                              confidence=1.0 if st["tracked"] else 0.0, note=note, layout=layout,
                              text=[r.to_json() for r in regions])
    st["layout"] = layout
    if base and layout == "split":
        st["split_content"] = base.get("content")
    result: Dict[str, Any] = {"file": out_file, "thumb": thumb, "clean": clean_path,
                              "plan": result_plan, "stats": st, "frames": N}
    if debug:
        result["camera"] = cam
        result["timeline"] = tl
        result["analysis"] = an
        result["base"] = base
    return result
