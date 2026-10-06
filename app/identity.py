"""Who is on screen, and who is talking — measured on the frames, with no new models.

A campaign that pays for one person's clips ("TJR must be the focus") is let
down by a clip where his friend does the talking, and a hook that credits him
with his friend's words gets the post rejected. This module answers, for one
finished video:

* the people in it — faces found with the YuNet model already in models/
  (it also gives 5 points: the eyes, the nose, the mouth corners), followed
  from frame to frame and grouped into people;
* who is talking — each face is lined up on its 5 points, and the mouth area
  is compared frame to frame (~6 a second). A mouth that moves while words are
  being said, and stops when they stop, is the one talking;
* which of them is the campaign's creator — compared with reference faces:
  photos gs adds on the campaign page, plus faces learned from the campaign's
  own "solo" clips (one person in nearly every frame, nobody else).

The face comparison uses no recognition model: the lined-up face is turned
into a texture fingerprint (local binary patterns on a 7x7 grid) and compared
with a chi-square distance, always relative to the other people in the same
clip, with an "unclear" band in between. That's approximate — fine for telling
two people in one video apart, weaker across very different videos — so it is
one signal among three (the others: who talks, and Claude reading the context).

If models/face_recognition_sface_2021dec.onnx is ever added, OpenCV's own face
recogniser (cosine distance) is used instead, automatically. Nothing here
downloads anything.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np

from .config import BASE_DIR, DATA_DIR

MODEL_DIR = BASE_DIR / "models"
YUNET_FILE = MODEL_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_FILE = MODEL_DIR / "face_recognition_sface_2021dec.onnx"
REF_DIR = DATA_DIR / "identity"

SCAN_FPS = 6.0            # frames looked at per second (mouth movement needs ~6-10)
SCAN_WIDTH = 576          # frames are read at this width: a 1080x1920 clip becomes 576x1024
MIN_FACE_PX = 26          # faces narrower than this (at scan width) are counted, not read
MAX_SCAN_SECONDS = 180.0  # a longer video is read at a lower rate so the check stays quick
MAX_PHOTOS = 3
MAX_LEARNED = 60

# The 112x112 face every face is lined up onto (the usual 5-point template).
TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                     [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)
MOUTH = (slice(74, 108), slice(30, 82))    # rows, cols of the mouth area in the lined-up face
EYES = (slice(36, 62), slice(24, 88))      # the eyes: they move with the head, not with speech
UPPER = (slice(16, 72), slice(8, 104))     # forehead to nose: lines up two frames of one head

# Distances, per kind of fingerprint: below `same` two faces are one person;
# a reference match needs `ref` or less and must beat the next person by `margin` —
# or, with nobody else in the clip to compare against, `alone` or less.
# The LBP numbers were set on drawn test faces (same face across sizes ~0.14,
# two different faces ~0.19-0.23): real footage may need them moved.
THRESHOLDS = {
    "lbp": {"same": 0.12, "ref": 0.20, "margin": 0.85, "alone": 0.16},
    "sface": {"same": 0.55, "ref": 0.637, "margin": 0.85, "alone": 0.55},   # 0.637 = OpenCV's cosine 0.363
}

# Who talks: a mouth must move at least this much between two frames 1/12 s apart
# (in face-contrast units, half the eyes' movement taken off), and beat the next
# face by this ratio, before a moment of speech is put down to it.
TALK_MIN = 0.007
TALK_RATIO = 1.6

_lock = threading.Lock()
_local = threading.local()


# --- the face finder and the fingerprint ---------------------------------------------

def available() -> Tuple[bool, str]:
    """(can faces be read here, why not in plain words)."""
    try:
        import cv2
    except Exception:                                    # pragma: no cover - cv2 is a requirement
        return False, "OpenCV isn't installed"
    if not hasattr(cv2, "FaceDetectorYN"):
        return False, f"this OpenCV ({cv2.__version__}) has no YuNet face finder"
    if not YUNET_FILE.exists():
        return False, "the face finder model (models/face_detection_yunet_2023mar.onnx) is missing"
    return True, ""


def kind() -> str:
    """Which fingerprint is in use: 'sface' when that model was added, else 'lbp'."""
    try:
        import cv2
        if SFACE_FILE.exists() and hasattr(cv2, "FaceRecognizerSF"):
            return "sface"
    except Exception:
        pass
    return "lbp"


def _detector():
    """One YuNet per thread (the renders run side by side)."""
    import cv2
    net = getattr(_local, "yunet", None)
    if net is None:
        net = cv2.FaceDetectorYN.create(str(YUNET_FILE), "", (320, 320), 0.6, 0.3, 5000)
        _local.yunet = net
    return net


def _recognizer():
    import cv2
    rec = getattr(_local, "sface", None)
    if rec is None:
        rec = cv2.FaceRecognizerSF.create(str(SFACE_FILE), "")
        _local.sface = rec
    return rec


def detect(img: np.ndarray) -> List[np.ndarray]:
    """YuNet rows: x, y, w, h, 5 points (x, y), score — in this image's pixels."""
    net = _detector()
    h, w = img.shape[:2]
    net.setInputSize((w, h))
    _, faces = net.detect(img)
    return [np.asarray(f, np.float32) for f in (faces if faces is not None else [])]


def _uniform_map() -> np.ndarray:
    table = np.full(256, 58, np.uint8)
    n = 0
    for v in range(256):
        bits = [(v >> i) & 1 for i in range(8)]
        if sum(bits[i] != bits[(i + 1) % 8] for i in range(8)) <= 2:
            table[v] = n
            n += 1
    return table


_UNIFORM = _uniform_map()
INNER = (slice(24, 80), slice(16, 96))     # eyes, brows and nose in the lined-up face
LBP_SIZE = (64, 48)                        # width, height the inner face is read at
_GRID = (4, 6)                             # rows, columns of histogram cells


def align(img: np.ndarray, row: np.ndarray) -> Optional[np.ndarray]:
    """The face lined up on its 5 points: 112x112 grey."""
    import cv2
    pts = row[4:14].reshape(5, 2).astype(np.float32)
    m, _ = cv2.estimateAffinePartial2D(pts, TEMPLATE, method=cv2.LMEDS)
    if m is None:
        return None
    grey = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.warpAffine(grey, m, (112, 112), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def lbp_fingerprint(face: np.ndarray) -> np.ndarray:
    """Uniform local binary patterns of the inner face (eyes, brows, nose — not the mouth,
    which changes as they talk, nor hair and background), a 59-bin histogram per grid cell."""
    import cv2
    # every face at one size first: a big face is sharp and a small one blurry, and the
    # patterns would tell the size apart before the person
    small = cv2.resize(face[INNER], LBP_SIZE, interpolation=cv2.INTER_AREA)
    g = cv2.GaussianBlur(small, (3, 3), 0).astype(np.int16)
    c = g[1:-1, 1:-1]
    code = np.zeros(c.shape, np.uint8)
    offsets = [(-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1)]
    h, w = g.shape
    for k, (dy, dx) in enumerate(offsets):
        n = g[1 + dy:h - 1 + dy, 1 + dx:w - 1 + dx]
        code |= ((n >= c).astype(np.uint8) << k)
    lab = _UNIFORM[code]
    rows = np.array_split(np.arange(lab.shape[0]), _GRID[0])
    cols = np.array_split(np.arange(lab.shape[1]), _GRID[1])
    out = []
    for r in rows:
        for cc in cols:
            cell = lab[r[0]:r[-1] + 1, cc[0]:cc[-1] + 1]
            hist = np.bincount(cell.ravel(), minlength=59).astype(np.float32)
            out.append(hist / max(1.0, hist.sum()))
    return np.concatenate(out)


def fingerprint(img: np.ndarray, row: np.ndarray, aligned: Optional[np.ndarray] = None) -> Optional[np.ndarray]:
    if kind() == "sface":
        try:
            rec = _recognizer()
            crop = rec.alignCrop(img, row.reshape(1, -1))
            vec = np.asarray(rec.feature(crop), np.float32).ravel()
            return vec / max(1e-6, float(np.linalg.norm(vec)))
        except Exception:
            return None
    face = aligned if aligned is not None else align(img, row)
    return None if face is None else lbp_fingerprint(face)


def distance(a: np.ndarray, b: np.ndarray, how: str = "") -> float:
    """0 = the same face; LBP: chi-square per cell (0-1); SFace: 1 - cosine."""
    how = how or kind()
    if how == "sface":
        return float(1.0 - np.dot(a, b) / max(1e-6, float(np.linalg.norm(a) * np.linalg.norm(b))))
    return float(0.5 * np.sum((a - b) ** 2 / (a + b + 1e-9)) / (_GRID[0] * _GRID[1]))


def _pack(vec: np.ndarray) -> str:
    return base64.b64encode(np.asarray(vec, np.float32).tobytes()).decode("ascii")


def _unpack(text: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(text), np.float32).copy()


# --- reading a video ---------------------------------------------------------------------

def _probe(path: Path) -> Tuple[int, int, float]:
    from . import media
    info = media.probe(path)
    return int(info.get("width") or 0), int(info.get("height") or 0), float(info.get("duration") or 0)


def frames(path: Path, fps: float = SCAN_FPS, width: int = SCAN_WIDTH) -> Iterator[Tuple[float, np.ndarray]]:
    """(time, BGR frame) at `fps`, scaled to `width`, decoded once by ffmpeg."""
    w0, h0, _ = _probe(path)
    if not w0 or not h0:
        return
    width = min(width, w0)
    height = max(2, int(round(h0 * width / w0 / 2)) * 2)
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-an",
           "-vf", f"fps={fps:g},scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = width * height * 3
    k = 0
    try:
        while True:
            buf = proc.stdout.read(size)
            if not buf or len(buf) < size:
                break
            yield k / fps, np.frombuffer(buf, np.uint8).reshape(height, width, 3)
            k += 1
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def speech_times(words: List[Dict[str, Any]]) -> List[Tuple[float, float]]:
    """When someone is talking, from the words' times ('start'/'end', or an edit's 't'/'end')."""
    spans = []
    for w in words or []:
        try:
            a = float(w["start"] if "start" in w else w["t"])
            b = float(w.get("end", a + 0.2))
        except (KeyError, TypeError, ValueError):
            continue
        if (w.get("w") or "").strip():
            spans.append((a - 0.08, max(b, a + 0.1) + 0.08))
    spans.sort()
    merged: List[List[float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 0.15:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def audio_speech(path: Path, fps: float) -> List[bool]:
    """No words to go by: loud stretches of the sound, one flag per scanned frame."""
    proc = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000",
                           "-f", "s16le", "-"], capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return []
    pcm = np.frombuffer(proc.stdout, np.int16).astype(np.float32)
    hop = int(8000 / fps)
    n = len(pcm) // hop
    if n < 2:
        return []
    rms = np.sqrt(np.mean(pcm[:n * hop].reshape(n, hop) ** 2, axis=1))
    floor = np.percentile(rms, 20)
    return list(rms > max(floor * 3.0, 300.0))


def _iou(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / max(1e-6, aw * ah + bw * bh - inter)


def scan(path: Path, words: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Everyone in a finished video, and how much of it each one is on screen and talking.

    Never raises: trouble comes back as {"ok": False, "note": plain words}."""
    ok, why = available()
    if not ok:
        return {"ok": False, "note": f"Couldn't look for faces: {why}.", "people": []}
    try:
        return _scan(Path(path), words or [])
    except Exception as exc:                               # a check that couldn't run says so
        return {"ok": False, "note": f"Couldn't look for faces: {str(exc)[:160]}", "people": []}


def _window() -> np.ndarray:
    import cv2
    win = getattr(_local, "hann", None)
    if win is None:
        win = cv2.createHanningWindow((UPPER[1].stop - UPPER[1].start, UPPER[0].stop - UPPER[0].start), cv2.CV_32F)
        _local.hann = win
    return win


def _transform(row: np.ndarray) -> Optional[np.ndarray]:
    import cv2
    pts = row[4:14].reshape(5, 2).astype(np.float32)
    m, _ = cv2.estimateAffinePartial2D(pts, TEMPLATE, method=cv2.LMEDS)
    return m


def _mouth_motion(grey0: np.ndarray, grey1: Optional[np.ndarray], m: np.ndarray) -> Tuple[np.ndarray, Optional[float]]:
    """The lined-up face, and how much more its mouth moved than its eyes between two frames
    1/12 s apart. Both frames go through the same lining-up, so the measure doesn't shake
    with the face finder's points; the eyes take out head movement and camera noise."""
    import cv2
    face0 = cv2.warpAffine(grey0, m, (112, 112), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    if grey1 is None:
        return face0, None
    face1 = cv2.warpAffine(grey1, m, (112, 112), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    f0, f1 = face0.astype(np.float32), face1.astype(np.float32)
    # the head moves a little in 1/12 s: line the second frame up on the upper face first,
    # or the sharp edges of eyes and glasses swamp what the mouth does
    (dx, dy), _ = cv2.phaseCorrelate(f0[UPPER], f1[UPPER], _window())
    if abs(dx) < 8 and abs(dy) < 8:
        f1 = cv2.warpAffine(f1, np.float32([[1, 0, -dx], [0, 1, -dy]]), (112, 112),
                            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    spread = float(f0.std()) + 4.0
    # compared as the averages of small patches, not pixel by pixel: what's left of a sub-pixel
    # shift sits on sharp edges (eyes, glasses) and averages out; a mouth opening doesn't
    mouth = float(np.mean(np.abs(_cells(f1[MOUTH]) - _cells(f0[MOUTH])))) / spread
    eyes = float(np.mean(np.abs(_cells(f1[EYES]) - _cells(f0[EYES])))) / spread
    return face0, max(0.0, mouth - 0.5 * eyes)


def _cells(patch: np.ndarray, ny: int = 3, nx: int = 4) -> np.ndarray:
    h, w = patch.shape
    return patch[:h - h % ny, :w - w % nx].reshape(ny, h // ny, nx, w // nx).mean(axis=(1, 3))


def _pairs(path: Path, fps: float) -> Iterator[Tuple[int, float, np.ndarray, Optional[np.ndarray]]]:
    """(k, time, frame, the frame 1/(2*fps) s later or None) — faces are found on the first."""
    held = None
    k = 0
    for i, (t, img) in enumerate(frames(path, fps * 2)):
        if i % 2 == 0:
            held = (t, img)
            continue
        yield k, held[0], held[1], img
        held = None
        k += 1
    if held is not None:
        yield k, held[0], held[1], None


def _scan(path: Path, words: List[Dict[str, Any]]) -> Dict[str, Any]:
    import cv2
    _, _, seconds = _probe(path)
    fps = SCAN_FPS if seconds <= MAX_SCAN_SECONDS else max(1.0, SCAN_FPS * MAX_SCAN_SECONDS / seconds)
    how = kind()
    tracks: List[Dict[str, Any]] = []        # {"id", "samples": [sample], "last": k}
    n = 0
    fw = fh = 0
    for k, t, img, nxt in _pairs(path, fps):
        n = k + 1
        fh, fw = img.shape[:2]
        grey0 = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        grey1 = cv2.cvtColor(nxt, cv2.COLOR_BGR2GRAY) if nxt is not None else None
        samples = []
        for row in detect(img):
            x, y, w, h = (float(v) for v in row[:4])
            s: Dict[str, Any] = {"k": k, "t": round(t, 3), "box": (x, y, w, h), "small": w < MIN_FACE_PX,
                                 "act": None, "vec": None}
            if not s["small"]:
                m = _transform(row)
                if m is not None:
                    face, s["act"] = _mouth_motion(grey0, grey1, m)
                    s["vec"] = fingerprint(img, row, face)
            samples.append(s)
        # follow each face on from the frame before (or the one before that)
        live = [tr for tr in tracks if k - tr["last"] <= 2]
        pairs = sorted(((_iou(tr["samples"][-1]["box"], s["box"]), i, j) for i, tr in enumerate(live)
                        for j, s in enumerate(samples)), reverse=True)
        used_t, used_s = set(), set()
        for score, i, j in pairs:
            if score < 0.25 or i in used_t or j in used_s:
                continue
            used_t.add(i)
            used_s.add(j)
            live[i]["samples"].append(samples[j])
            live[i]["last"] = k
        for j, s in enumerate(samples):
            if j not in used_s:
                tracks.append({"id": len(tracks), "samples": [s], "last": k})
    if not n:
        return {"ok": False, "note": "Couldn't read the video's picture.", "people": []}

    people = _people(tracks, how)
    speaking = _speech_flags(path, words, n, fps)
    return _summarize(people, speaking, n, fps, fw, fh, how, bool(words))


def _people(tracks: List[Dict[str, Any]], how: str) -> List[Dict[str, Any]]:
    """Tracks of the same face, joined into people: never two faces seen in the same frame."""
    th = THRESHOLDS[how]["same"]
    groups = []
    for tr in tracks:
        vecs = [s["vec"] for s in tr["samples"] if s["vec"] is not None]
        groups.append({"tracks": [tr], "frames": {s["k"] for s in tr["samples"]},
                       "vecs": vecs, "mean": np.mean(vecs, axis=0) if vecs else None})
    while True:
        best = None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                a, b = groups[i], groups[j]
                if a["mean"] is None or b["mean"] is None or a["frames"] & b["frames"]:
                    continue
                d = distance(a["mean"], b["mean"], how)
                if d < th and (best is None or d < best[0]):
                    best = (d, i, j)
        if best is None:
            break
        _, i, j = best
        a, b = groups[i], groups.pop(j)
        a["tracks"] += b["tracks"]
        a["frames"] |= b["frames"]
        a["vecs"] += b["vecs"]
        a["mean"] = np.mean(a["vecs"], axis=0)
    # a face seen in one frame and never again is noise unless it is all there is
    keep = [g for g in groups if len(g["frames"]) >= 2] or groups
    keep.sort(key=lambda g: len(g["frames"]), reverse=True)
    out = []
    for pid, g in enumerate(keep, 1):
        samples = sorted((s for tr in g["tracks"] for s in tr["samples"]), key=lambda s: s["k"])
        out.append({"id": pid, "samples": samples, "mean": g["mean"]})
    return out


def _speech_flags(path: Path, words: List[Dict[str, Any]], n: int, fps: float) -> List[bool]:
    if words:
        spans = speech_times(words)
        flags = []
        j = 0
        for k in range(n):
            t = k / fps
            while j < len(spans) and spans[j][1] < t:
                j += 1
            flags.append(j < len(spans) and spans[j][0] <= t <= spans[j][1])
        return flags
    loud = audio_speech(path, fps)
    return (loud + [False] * n)[:n]


def _summarize(people: List[Dict[str, Any]], speaking: List[bool], n: int, fps: float,
               fw: int, fh: int, how: str, had_words: bool) -> Dict[str, Any]:
    by_frame: Dict[int, Dict[int, Dict[str, Any]]] = {}
    for p in people:
        for s in p["samples"]:
            by_frame.setdefault(s["k"], {})[p["id"]] = s

    def act(pid: int, k: int) -> Optional[float]:
        """Mouth movement around frame k, smoothed over its neighbours."""
        vals = [by_frame.get(j, {}).get(pid, {}).get("act") for j in range(k - 2, k + 3)]
        vals = [v for v in vals if v is not None]
        return float(np.mean(vals)) if vals else None

    said = [k for k in range(n) if speaking[k]] if speaking else []
    credit = {p["id"]: 0 for p in people}
    nobody = offcam = unclear = 0
    for k in said:
        here = by_frame.get(k, {})
        readable = {pid: act(pid, k) for pid, s in here.items() if not s["small"]}
        readable = {pid: a for pid, a in readable.items() if a is not None}
        if not here:
            nobody += 1
            continue
        if not readable:
            unclear += 1
            continue
        ranked = sorted(readable.items(), key=lambda kv: kv[1], reverse=True)
        top_pid, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        if top >= TALK_MIN and top >= second * TALK_RATIO + 0.002:
            credit[top_pid] += 1
        elif top < TALK_MIN:
            offcam += 1                    # faces on screen, none of them moving their mouth
        else:
            unclear += 1

    out_people = []
    talk_total = max(1, len(said))
    for p in people:
        frames_seen = sorted({s["k"] for s in p["samples"]})
        big = [s for s in p["samples"] if not s["small"]]
        acts = np.array([np.nan if v is None else v for v in (act(p["id"], k) for k in range(n))], np.float32)
        corr = None
        if speaking and np.isfinite(acts).sum() >= 6:
            ok = np.isfinite(acts)
            sp = np.array(speaking, np.float32)[ok]
            if sp.std() > 0 and acts[ok].std() > 0:
                corr = float(np.corrcoef(acts[ok], sp)[0, 1])
        talk_mean = float(np.nanmean(acts[np.array(speaking, bool)])) if speaking and \
            np.isfinite(acts[np.array(speaking, bool)]).any() else None
        quiet_mask = ~np.array(speaking, bool) if speaking else np.ones(n, bool)
        quiet_mean = float(np.nanmean(acts[quiet_mask])) if np.isfinite(acts[quiet_mask]).any() else None
        boxes = {}
        for s in p["samples"]:
            x, y, w, h = s["box"]
            boxes[s["k"]] = [round(x / fw, 4), round(y / fh, 4), round(w / fw, 4), round(h / fh, 4)]
        widths = sorted(s["box"][2] / fw for s in p["samples"])
        out_people.append({
            "id": p["id"],
            "share": round(len(frames_seen) / n, 3),
            "readable": round(len(big) / max(1, len(p["samples"])), 3),
            "size": round(widths[len(widths) // 2], 3) if widths else 0.0,
            "x": round(float(np.median([(s["box"][0] + s["box"][2] / 2) / fw for s in p["samples"]])), 3),
            "talk": round(credit[p["id"]] / talk_total, 3) if said else 0.0,
            "talk_corr": None if corr is None else round(corr, 3),
            "mouth_talking": None if talk_mean is None else round(talk_mean, 4),
            "mouth_quiet": None if quiet_mean is None else round(quiet_mean, 4),
            "boxes": boxes,
            "vec": p["mean"],
        })
    faces_frames = len(by_frame)
    result = {
        "ok": True, "kind": how, "fps": fps, "frames": n, "frame_w": fw, "frame_h": fh,
        "speech_frames": len(said), "speech_from": "words" if had_words else "sound",
        "nobody": round(nobody / talk_total, 3) if said else 0.0,
        "offcam": round(offcam / talk_total, 3) if said else 0.0,
        "unclear": round(unclear / talk_total, 3) if said else 0.0,
        "face_frames": round(faces_frames / n, 3),
        "people": out_people,
    }
    result["solo"] = is_solo(result)
    result["note"] = describe(result)
    return result


def is_solo(scan_result: Dict[str, Any]) -> Optional[int]:
    """The one person in a 'solo' clip (in >= ~75% of frames, nobody else in > 15%), else None."""
    people = scan_result.get("people") or []
    if not people:
        return None
    top = max(people, key=lambda p: p["share"])
    others = [p for p in people if p["id"] != top["id"]]
    if top["share"] >= 0.75 and top["readable"] >= 0.6 and all(p["share"] <= 0.15 for p in others) \
            and top.get("vec") is not None:
        return top["id"]
    return None


def describe(s: Dict[str, Any]) -> str:
    if not s.get("ok"):
        return s.get("note") or "Couldn't look for faces."
    people = s.get("people") or []
    if not people:
        return "No faces on screen."
    bits = []
    for p in people[:4]:
        bit = f"person {p['id']} on screen {p['share'] * 100:.0f}% of the time"
        if s.get("speech_frames"):
            bit += f", talking in {p['talk'] * 100:.0f}% of the speech"
        if p["readable"] < 0.5:
            bit += " (face small — hard to read)"
        bits.append(bit)
    if s.get("offcam", 0) >= 0.25:
        bits.append(f"{s['offcam'] * 100:.0f}% of the speech comes while nobody on screen moves their mouth "
                    "(someone off camera?)")
    if s.get("nobody", 0) >= 0.25:
        bits.append(f"{s['nobody'] * 100:.0f}% of the speech has no face on screen")
    return "; ".join(bits) + "."


def public(scan_result: Dict[str, Any]) -> Dict[str, Any]:
    """The scan without the fingerprints and per-frame boxes — small enough to keep with a clip."""
    out = {k: v for k, v in scan_result.items() if k != "people"}
    out["people"] = [{k: v for k, v in p.items() if k not in ("vec", "boxes")}
                     for p in scan_result.get("people") or []]
    return out


# --- who is the creator ---------------------------------------------------------------------

def _dir(campaign_id: str) -> Path:
    safe = "".join(ch for ch in str(campaign_id) if ch.isalnum() or ch in "-_")[:64] or "none"
    return REF_DIR / safe


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data: Any) -> None:
    """Write next to it, then swap — Windows can't replace a file something holds open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))
    tmp.unlink(missing_ok=True)


def photo_path(campaign_id: str, name: str) -> Path:
    return _dir(campaign_id) / "photos" / Path(name).name


def photos(campaign_id: str) -> List[Dict[str, Any]]:
    return [p for p in _read(_dir(campaign_id) / "photos.json", []) if isinstance(p, dict)]


def _face_in_photo(img: np.ndarray) -> Tuple[Optional[np.ndarray], str]:
    """The biggest face in a photo, fingerprinted — or why there isn't one."""
    import cv2
    h, w = img.shape[:2]
    scale = min(1.0, 1280.0 / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    faces = detect(img)
    if not faces:
        return None, "Couldn't find a face in that photo — use a clear photo where the face is easy to see."
    row = max(faces, key=lambda r: float(r[2] * r[3]))
    if row[2] < 40:
        return None, "The face in that photo is too small — use a closer photo of the face."
    vec = fingerprint(img, row)
    if vec is None:
        return None, "Couldn't line up the face in that photo — try one looking at the camera."
    return vec, ""


def add_photo(campaign_id: str, file: Path) -> Dict[str, Any]:
    """Keep a reference photo of the creator. Raises ValueError with plain words."""
    import cv2
    ok, why = available()
    if not ok:
        raise ValueError(f"Can't read faces on this PC: {why}.")
    have = photos(campaign_id)
    if len(have) >= MAX_PHOTOS:
        raise ValueError(f"There are already {MAX_PHOTOS} photos — remove one first.")
    data = np.frombuffer(Path(file).read_bytes(), np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("That file isn't a photo ClipAgent can read — use a JPG, PNG or WEBP.")
    vec, why = _face_in_photo(img)
    if vec is None:
        raise ValueError(why)
    name = hashlib.sha1(data.tobytes()).hexdigest()[:12] + ".jpg"
    with _lock:
        folder = _dir(campaign_id) / "photos"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes())
        have = [p for p in photos(campaign_id) if p.get("name") != name]
        have.append({"name": name, "kind": kind(), "vec": _pack(vec), "added": round(time.time(), 1)})
        _write(_dir(campaign_id) / "photos.json", have[-MAX_PHOTOS:])
    return status(campaign_id)


def remove_photo(campaign_id: str, name: str) -> Dict[str, Any]:
    with _lock:
        have = [p for p in photos(campaign_id) if p.get("name") != Path(name).name]
        _write(_dir(campaign_id) / "photos.json", have)
        photo_path(campaign_id, name).unlink(missing_ok=True)
    return status(campaign_id)


def _photo_vectors(campaign_id: str) -> List[np.ndarray]:
    """The photos' fingerprints, re-made from the kept photo when the fingerprint kind changed."""
    import cv2
    how = kind()
    out = []
    for p in photos(campaign_id):
        if p.get("kind") == how and p.get("vec"):
            out.append(_unpack(p["vec"]))
            continue
        pic = photo_path(campaign_id, p.get("name") or "")
        img = cv2.imdecode(np.fromfile(str(pic), np.uint8), cv2.IMREAD_COLOR) if pic.is_file() else None
        if img is not None:
            vec, _ = _face_in_photo(img)
            if vec is not None:
                out.append(vec)
    return out


def learn(campaign_id: str, scan_result: Dict[str, Any], job_id: str, clip_id: str) -> bool:
    """A 'solo' clip of this campaign adds its one face to what ClipAgent has seen.
    Only trusted later when it keeps turning up across different videos (or matches the photos)."""
    if not campaign_id or not scan_result.get("ok"):
        return False
    pid = scan_result.get("solo") or is_solo(scan_result)
    person = next((p for p in scan_result.get("people") or [] if p["id"] == pid), None)
    if not person or person.get("vec") is None:
        return False
    with _lock:
        path = _dir(campaign_id) / "learned.json"
        have = [e for e in _read(path, []) if isinstance(e, dict) and e.get("clip") != clip_id]
        have.append({"job": job_id, "clip": clip_id, "kind": scan_result.get("kind") or kind(),
                     "vec": _pack(person["vec"]), "added": round(time.time(), 1)})
        _write(path, have[-MAX_LEARNED:])
    return True


def references(campaign_id: str) -> Dict[str, Any]:
    """The creator's reference faces: the photos, plus learned faces that are trustworthy —
    the same face in solo clips from at least two different videos, or one that matches the photos."""
    how = kind()
    th = THRESHOLDS[how]
    shots = _photo_vectors(campaign_id) if campaign_id else []
    learned = [e for e in _read(_dir(campaign_id) / "learned.json", []) if isinstance(e, dict)
               and e.get("kind") == how and e.get("vec")] if campaign_id else []
    vecs = [_unpack(e["vec"]) for e in learned]
    trusted: List[int] = []
    if shots:
        trusted = [i for i, v in enumerate(vecs) if min(distance(v, s, how) for s in shots) <= th["ref"]]
    elif vecs:
        # the face that turns up in the most different videos
        best: List[int] = []
        for i, v in enumerate(vecs):
            group = [j for j, u in enumerate(vecs) if distance(v, u, how) <= th["same"]]
            if len({learned[j]["job"] for j in group}) > len({learned[j]["job"] for j in best}):
                best = group
        if len({learned[j]["job"] for j in best}) >= 2:
            trusted = best
    jobs = {learned[i]["job"] for i in trusted}
    return {"kind": how, "vectors": shots + [vecs[i] for i in trusted], "photos": len(shots),
            "learned": len(trusted), "videos": len(jobs), "seen": len(learned)}


def status(campaign_id: str) -> Dict[str, Any]:
    """What the campaign page shows under 'Who is <creator>?'."""
    ok, why = available()
    refs = references(campaign_id) if ok else {"photos": 0, "learned": 0, "videos": 0, "seen": 0}
    pics = [{"name": p["name"], "url": f"/media/identity/{_dir(campaign_id).name}/{p['name']}"}
            for p in photos(campaign_id)]
    if not ok:
        note = f"Face checks can't run on this PC: {why}."
    elif refs["photos"] and refs["learned"]:
        note = (f"Using your {refs['photos']} photo{'s' if refs['photos'] != 1 else ''} and "
                f"{refs['learned']} solo clip{'s' if refs['learned'] != 1 else ''}.")
    elif refs["photos"]:
        note = f"Using your {refs['photos']} photo{'s' if refs['photos'] != 1 else ''}."
    elif refs["learned"]:
        note = (f"Learned from {refs['learned']} solo clip{'s' if refs['learned'] != 1 else ''} in "
                f"{refs['videos']} videos. Photos make it surer.")
    elif refs["seen"]:
        note = ("Seen in a solo clip, but not yet in two different videos — not trusted yet. "
                "Adding photos makes the check work right away.")
    else:
        note = "Doesn't know the face yet. Add photos, or it learns from clips where only that person is on screen."
    return {"photos": pics, "learned": refs["learned"], "videos": refs["videos"], "seen": refs["seen"],
            "ready": bool(refs["photos"] or refs["learned"]), "kind": kind(), "note": note,
            "approximate": kind() != "sface"}


def match(scan_result: Dict[str, Any], refs: Dict[str, Any]) -> Dict[str, Any]:
    """Which person in the clip is the creator: {"person": id | None, "status": clear | unclear |
    no_refs | no_faces, "dist": {id: d}, "why": plain words}."""
    people = [p for p in scan_result.get("people") or [] if p.get("vec") is not None]
    if not scan_result.get("ok") or not people:
        return {"person": None, "status": "no_faces", "dist": {}, "why": "No face clear enough to compare."}
    vecs = refs.get("vectors") or []
    how = scan_result.get("kind") or refs.get("kind") or kind()
    if not vecs or refs.get("kind", how) != how:
        return {"person": None, "status": "no_refs", "dist": {}, "why": "No reference faces for this campaign yet."}
    th = THRESHOLDS[how]
    dist = {}
    for p in people:
        ds = sorted(distance(p["vec"], v, how) for v in vecs)
        dist[p["id"]] = round(float(np.mean(ds[:3])), 4)
    ranked = sorted(dist.items(), key=lambda kv: kv[1])
    pid, d = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else None
    if d > th["ref"]:
        return {"person": None, "status": "unclear", "dist": dist,
                "why": "Nobody in the clip looks clearly like the reference faces."}
    if second is None and d > th["alone"]:
        return {"person": None, "status": "unclear", "dist": dist,
                "why": "The one face in the clip only loosely looks like the reference faces."}
    if second is not None and d > second * th["margin"]:
        return {"person": None, "status": "unclear", "dist": dist,
                "why": f"Person {pid} and person {ranked[1][0]} look about equally like the reference faces."}
    return {"person": pid, "status": "clear", "dist": dist,
            "why": f"Person {pid} matches the reference faces" + (" clearly better than the others." if second
                                                                 else ".")}
