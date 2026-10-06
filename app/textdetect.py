"""Find the big words burned into a video, so a vertical crop never cuts them in half.

Creators put their own text on their videos: a title across the top, a lower
third with their name, their own captions. Cropping a 16:9 video to 9:16
keeps about a third of its width, and a crop edge running through those words
leaves half-cut letters on screen — the clip looks broken.

This finds LARGE text only (capital letters at least ~2.5% of the frame
height), with OpenCV and numpy, no models:

1. Letters: in a downscaled grey frame, pixels clearly brighter (or darker)
   than their surroundings, split into connected shapes. A letter is a shape
   of letter size, made of strokes (thin compared with its height), not solid
   (a book spine, a stripe or an eye is a solid shape; most letters are not).
2. Lines: letters of about the same height and colour, sitting on one line,
   close together. Three or more make a line of text. Evenly repeating shapes
   (stripes, a fence) are not text.
3. Blocks: lines stacked closely make one block (a two-line title) — a block
   is kept whole or left out whole.
4. Over time: frames are sampled sparsely (one every ~2 s, small), and only
   text seen in more than one sample counts (burned-in titles and captions
   stay; a passing sign doesn't).

Then the window-fitting helpers: where a crop window may go so every text
block is either fully inside or fully outside it, keeping the faces it had.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

LARGE_CAP = 0.025        # capital letters at least this share of the frame height count as big text
SAMPLE_EVERY = 2.0       # seconds between sampled frames
WORK_SHORT = 540         # frames are analysed with their short side at this many px
CONTRAST = 24            # letter pixels stand this many grey levels above (or below) the area around them
MIN_CONTRAST = 70        # ...and a letter's average stands this far from the rest of its box
MAX_SAMPLES = 40         # never look at more frames than this for one span

Box = Tuple[float, float, float, float]


@dataclass
class Region:
    """A block of big text, as fractions of the frame, and when it is on screen (source seconds)."""
    x0: float
    y0: float
    x1: float
    y1: float
    t0: float
    t1: float
    hits: int = 1
    cap: float = 0.0          # letter height, share of the frame height

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    def px(self, W: float, H: float) -> Tuple[float, float, float, float]:
        return (self.x0 * W, self.y0 * H, self.x1 * W, self.y1 * H)

    def where(self) -> str:
        """Plain words for where it sits."""
        cy = (self.y0 + self.y1) / 2
        if self.w > 0.6:
            return "across the top" if cy < 0.35 else ("across the bottom" if cy > 0.65 else "across the middle")
        if cy < 0.3:
            return "at the top"
        if cy > 0.7:
            return "at the bottom"
        return "on the left" if (self.x0 + self.x1) / 2 < 0.5 else "on the right"

    def to_json(self) -> Dict[str, Any]:
        return {"box": [round(v, 4) for v in (self.x0, self.y0, self.x1, self.y1)],
                "t": [round(self.t0, 2), round(self.t1, 2)], "hits": self.hits, "cap": round(self.cap, 4)}


# --- one frame ------------------------------------------------------------------------------

def _odd(v: float, lo: int, hi: int) -> int:
    v = int(max(lo, min(hi, v)))
    return v if v % 2 else v + 1


def _work(img: np.ndarray) -> np.ndarray:
    """Grey, short side WORK_SHORT px (never upscaled)."""
    import cv2
    g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = g.shape[:2]
    k = WORK_SHORT / min(h, w)
    if k < 1.0:
        g = cv2.resize(g, (max(1, int(round(w * k))), max(1, int(round(h * k)))), interpolation=cv2.INTER_AREA)
    return g


def _letters(g: np.ndarray, min_h: float, max_h: float, block: int, bright: bool) -> List[Dict[str, float]]:
    """Letter-like shapes of one polarity."""
    import cv2
    src = g if bright else (255 - g)
    bw = cv2.adaptiveThreshold(src, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, block, -CONTRAST)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    if n <= 1:
        return []
    x, y, w, h, area = (stats[:, i] for i in range(5))
    ok = (h >= min_h) & (h <= max_h) & (w >= 2) & (w <= 6 * h) & (area >= 0.12 * w * h)
    ok[0] = False
    idx = np.flatnonzero(ok)
    if idx.size > 800:                      # a wall of noise: it is not text anyway
        idx = idx[np.argsort(-area[idx])[:800]]
    if not idx.size:
        return []
    dt = cv2.distanceTransform(bw, cv2.DIST_L2, 3)
    out = []
    for i in idx:
        x0, y0, ww, hh = int(x[i]), int(y[i]), int(w[i]), int(h[i])
        roi = lab[y0:y0 + hh, x0:x0 + ww] == i
        stroke = 2.0 * float(dt[y0:y0 + hh, x0:x0 + ww][roi].max())
        if stroke > 0.6 * hh or stroke < 1.4:
            continue                                   # a solid blob, or a hairline ("l" and "I" are kept)
        mask = roi.astype(np.uint8)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            continue
        hull_mask = np.zeros_like(mask)
        cv2.fillConvexPoly(hull_mask, cv2.convexHull(np.vstack(cnts)), 1)
        solidity = float(area[i]) / max(1.0, float(np.count_nonzero(hull_mask | mask)))
        # vertical strokes met along the middle row: a wide shape of several merged letters has many
        mid = mask[hh // 2]
        crossings = int(np.count_nonzero(np.diff(np.concatenate([[0], mid, [0]])) == 1))
        patch = g[y0:y0 + hh, x0:x0 + ww]
        fg = float(patch[roi].mean())
        rest = patch[~roi]
        bg = float(rest.mean()) if rest.size else fg
        out.append({"x0": x0, "y0": y0, "x1": x0 + ww, "y1": y0 + hh, "h": hh, "w": ww,
                    "area": float(area[i]), "stroke": stroke, "solid": solidity, "cross": crossings,
                    "grey": fg, "contrast": abs(fg - bg)})
    return out


def _group(cands: List[Dict[str, float]]) -> List[List[Dict[str, float]]]:
    """Letters that sit next to each other on one line, about the same height and colour."""
    if len(cands) < 2:
        return []
    cands = sorted(cands, key=lambda c: c["x0"])
    n = len(cands)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        a = cands[i]
        for j in range(i + 1, n):
            b = cands[j]
            hm = max(a["h"], b["h"])
            if b["x0"] > a["x1"] + 0.9 * hm and b["x0"] > a["x0"] + 3 * hm:
                break
            gap = b["x0"] - a["x1"]
            if gap > 0.9 * hm or gap < -0.35 * hm:
                continue
            if hm / max(1.0, min(a["h"], b["h"])) > 1.6:
                continue
            if abs(a["y1"] - b["y1"]) > 0.22 * hm and abs(a["y0"] - b["y0"]) > 0.22 * hm:
                continue
            if abs(a["grey"] - b["grey"]) > 45:
                continue
            parent[find(j)] = find(i)
    groups: Dict[int, List[Dict[str, float]]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(cands[i])
    return [g for g in groups.values() if len(g) >= 2]


def _features(comps: List[Dict[str, float]]) -> Dict[str, float]:
    comps = sorted(comps, key=lambda c: c["x0"])
    hs = np.array([c["h"] for c in comps], float)
    ws = np.array([c["w"] for c in comps], float)
    cap = float(np.percentile(hs, 75))
    x0 = min(c["x0"] for c in comps)
    x1 = max(c["x1"] for c in comps)
    bots = np.array([c["y1"] for c in comps], float)
    tops = np.array([c["y0"] for c in comps], float)
    gaps = np.array([b["x0"] - a["x1"] for a, b in zip(comps, comps[1:])], float)
    strokes = np.array([c["stroke"] for c in comps], float)
    return {
        "x0": x0, "x1": x1, "y0": float(tops.min()), "y1": float(bots.max()),
        "n": len(comps),
        "letters": sum(max(1, (c["cross"] + 1) // 2) if c["w"] > 1.3 * c["h"] else 1 for c in comps),
        "cap": cap,
        "h_cv": float(hs.std() / max(1.0, hs.mean())),
        "align": float(max(np.mean(np.abs(bots - np.median(bots)) <= 0.12 * cap),
                           np.mean(np.abs(tops - np.median(tops)) <= 0.12 * cap))),
        "solid": float(np.median([c["solid"] for c in comps])),
        "grey_std": float(np.std([c["grey"] for c in comps])),
        "density": float(ws.sum() / max(1.0, x1 - x0)),
        "aspect": float(np.median(ws / np.maximum(1.0, hs))),
        "stroke_cv": float(strokes.std() / max(1e-6, strokes.mean())),
        "stroke_rel": float(np.median(strokes) / max(1.0, cap)),
        "gap": float(np.median(gaps) / max(1.0, cap)) if gaps.size else 0.0,
        "w_cv": float(ws.std() / max(1.0, ws.mean())),
        "gap_cv": float(gaps.std() / max(1.0, abs(gaps.mean()))) if gaps.size > 1 else 1.0,
        "contrast": float(np.median([c["contrast"] for c in comps])),
        "width_caps": float((x1 - x0) / max(1.0, cap)),
    }


def _is_text(f: Dict[str, float]) -> bool:
    if f["letters"] < 3 or f["width_caps"] < 1.2:
        return False
    if f["contrast"] < MIN_CONTRAST:
        return False                                   # burned-in words stand out sharply; texture doesn't
    if f["h_cv"] > 0.35 or f["align"] < 0.7 or f["gap"] > 0.5:
        return False
    if f["aspect"] < 0.3 or f["solid"] > 0.97 or f["grey_std"] > 24 or f["density"] < 0.35:
        return False                                   # thin solid bars: book spines, stripes
    if f["n"] >= 5 and f["w_cv"] < 0.09 and f["h_cv"] < 0.06 and f["gap_cv"] < 0.25 and f["solid"] > 0.88:
        return False                                   # evenly repeating solid shapes: stripes, a fence
    return True


def _lines(cands: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """Groups of letters that read as a line of text."""
    out = []
    for comps in _group(cands):
        f = _features(comps)
        if _is_text(f):
            out.append(f)
    return out


def _passes(g: np.ndarray, min_cap: float):
    H = g.shape[0]
    min_h = max(5.0, 0.6 * min_cap * H)
    max_h = 0.20 * H
    for block in (_odd(min_cap * H * 2.4, 15, 61), _odd(max_h * 0.8, 61, 151)):
        for bright in (True, False):
            yield _letters(g, min_h, max_h, block, bright)


def line_features(img: np.ndarray, min_cap: float = LARGE_CAP) -> List[Dict[str, float]]:
    """Every candidate line's measurements (for tuning)."""
    g = _work(img)
    return [_features(c) for cands in _passes(g, min_cap) for c in _group(cands)]


def _blocks(lines: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """Lines close together (words on one row, a two-line title) become one block."""
    blocks = [dict(l) for l in lines]
    changed = True
    while changed:
        changed = False
        for i in range(len(blocks)):
            for j in range(i + 1, len(blocks)):
                a, b = blocks[i], blocks[j]
                cap = max(a["cap"], b["cap"])
                # The same words found twice — the fill of outlined letters, and the dark outline
                # around them, which is taller: one block, and the fill has the true letter height.
                ix = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
                iy = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
                small = min((a["x1"] - a["x0"]) * (a["y1"] - a["y0"]), (b["x1"] - b["x0"]) * (b["y1"] - b["y0"]))
                same_text = ix > 0 and iy > 0 and ix * iy > 0.5 * small
                if not same_text and cap / max(1.0, min(a["cap"], b["cap"])) > 1.9:
                    continue
                vgap = max(a["y0"], b["y0"]) - min(a["y1"], b["y1"])
                hgap = max(a["x0"], b["x0"]) - min(a["x1"], b["x1"])
                same_row = vgap < -0.5 * min(a["y1"] - a["y0"], b["y1"] - b["y0"]) and hgap < 1.8 * cap
                stacked = vgap < 0.9 * cap and hgap < 1.0 * cap
                if same_text or same_row or stacked:
                    if same_text:
                        cap = min(a["cap"], b["cap"])
                    blocks[i] = {"x0": min(a["x0"], b["x0"]), "y0": min(a["y0"], b["y0"]),
                                 "x1": max(a["x1"], b["x1"]), "y1": max(a["y1"], b["y1"]),
                                 "cap": cap, "n": a["n"] + b["n"]}
                    del blocks[j]
                    changed = True
                    break
            if changed:
                break
    return blocks


def detect(img: np.ndarray, min_cap: float = LARGE_CAP) -> List[Tuple[float, float, float, float, float]]:
    """Big text in one frame: [(x0, y0, x1, y1, cap)] as fractions of the frame (cap: letter height
    over the frame height). The boxes include a little room for outlines and shadows."""
    g = _work(img)
    H, W = g.shape[:2]
    found: List[Dict[str, float]] = []
    for cands in _passes(g, min_cap):
        found += _lines(cands)
    out = []
    for b in _blocks(found):
        if b["cap"] < min_cap * H * 0.95:
            continue
        px, py = 0.3 * b["cap"], 0.25 * b["cap"]
        out.append((max(0.0, (b["x0"] - px) / W), max(0.0, (b["y0"] - py) / H),
                    min(1.0, (b["x1"] + px) / W), min(1.0, (b["y1"] + py) / H), b["cap"] / H))
    return out


# --- over time --------------------------------------------------------------------------------

_cache: Dict[Tuple[str, float, int], Dict[float, List[Tuple[float, ...]]]] = {}
_cache_lock = threading.Lock()


def _key(source: Path) -> Tuple[str, float, int]:
    try:
        st = Path(source).stat()
        return (str(Path(source).resolve()), st.st_mtime, st.st_size)
    except OSError:
        return (str(source), 0.0, 0)


def sample_frames(source: Path, times: Sequence[float], short: int = WORK_SHORT) -> List[Tuple[float, np.ndarray]]:
    """Small BGR frames at these source times (sorted, unreadable ones skipped)."""
    import cv2
    out: List[Tuple[float, np.ndarray]] = []
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        return out
    try:
        for t in sorted(set(round(float(t), 2) for t in times)):
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h, w = frame.shape[:2]
            k = short / min(h, w)
            if k < 1.0:
                frame = cv2.resize(frame, (int(round(w * k)), int(round(h * k))), interpolation=cv2.INTER_AREA)
            out.append((t, frame))
    finally:
        cap.release()
    return out


def span_times(spans: Iterable[Tuple[float, float]], every: float = SAMPLE_EVERY,
               limit: int = MAX_SAMPLES) -> List[float]:
    """Sample times across one or more (start, end) source spans: one every `every` seconds,
    never fewer than two per span, never more than `limit` in all."""
    spans = [(float(a), float(b)) for a, b in spans if b - a > 0.05]
    total = sum(b - a for a, b in spans) or 1.0
    step = max(every, total / max(1, limit))
    times: List[float] = []
    for a, b in spans:
        k = max(2, int(math.ceil((b - a) / step)))
        times += [a + (b - a) * (i + 0.5) / k for i in range(k)]
    return times[: limit + len(spans)]


def scan(source: Path, times: Sequence[float], frames: Optional[List[Tuple[float, np.ndarray]]] = None
         ) -> Dict[float, List[Tuple[float, ...]]]:
    """Big text at each sampled time (cached per source file)."""
    key = _key(source)
    with _cache_lock:
        known = dict(_cache.get(key, {}))
    want = [round(float(t), 2) for t in times]
    missing = [t for t in want if t not in known]
    if missing:
        got = frames if frames is not None else sample_frames(source, missing)
        for t, frame in got:
            known[round(t, 2)] = detect(frame)
        with _cache_lock:
            _cache.setdefault(key, {}).update(known)
            if len(_cache) > 24:
                _cache.pop(next(iter(_cache)))
    return {t: known[t] for t in want if t in known}


def regions(found: Dict[float, List[Tuple[float, ...]]], every: float = SAMPLE_EVERY,
            min_hits: Optional[int] = None) -> List[Region]:
    """Text that stays: blocks seen in the same band of the frame in more than one sample.
    The creator's own captions change word by word, so a band's region is everything
    they covered there."""
    times = sorted(found)
    if min_hits is None:
        min_hits = 1 if len(times) <= 2 else 2
    clusters: List[Dict[str, Any]] = []
    for t in times:
        for (x0, y0, x1, y1, cap) in found[t]:
            best = None
            for c in clusters:
                ov = min(y1, c["y1"]) - max(y0, c["y0"])
                hgap = max(x0, c["x0"]) - min(x1, c["x1"])
                if ov > 0.5 * min(y1 - y0, c["y1"] - c["y0"]) and hgap < 0.06:
                    best = c
                    break
            if best is None:
                clusters.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "times": {t}, "cap": cap})
            else:
                best.update(x0=min(best["x0"], x0), y0=min(best["y0"], y0), x1=max(best["x1"], x1),
                            y1=max(best["y1"], y1), cap=max(best["cap"], cap))
                best["times"].add(t)
    out = []
    for c in clusters:
        if len(c["times"]) < min_hits:
            continue
        out.append(Region(x0=c["x0"], y0=c["y0"], x1=c["x1"], y1=c["y1"], t0=min(c["times"]) - every,
                          t1=max(c["times"]) + every, hits=len(c["times"]), cap=c["cap"]))
    # a region grown inside another one (a caption band and a word of it) is the same text
    out.sort(key=lambda r: -(r.w * r.h))
    kept: List[Region] = []
    for r in out:
        if any(r.x0 >= k.x0 - 0.01 and r.x1 <= k.x1 + 0.01 and r.y0 >= k.y0 - 0.01 and r.y1 <= k.y1 + 0.01
               for k in kept):
            continue
        kept.append(r)
    return kept


def find_regions(source: Path, spans: Sequence[Tuple[float, float]], every: float = SAMPLE_EVERY,
                 frames: Optional[List[Tuple[float, np.ndarray]]] = None) -> List[Region]:
    """Burned-in big text in these source spans. Never raises: no frames, no regions."""
    try:
        times = [t for t, _ in frames] if frames is not None else span_times(spans, every)
        return regions(scan(source, times, frames), every)
    except Exception:
        return []


def text_risk(source: Path, start: float, end: float) -> Tuple[float, str]:
    """How much burned-in text would get in the way of cropping this moment to 9:16:
    0 (none, or easy to keep whole) to 1 (wide text most of the time — the clip would
    have to show the whole picture). With a plain reason."""
    regs = find_regions(Path(source), [(start, end)], every=max(SAMPLE_EVERY, (end - start) / 8))
    if not regs:
        return 0.0, ""
    span = max(0.1, end - start)
    worst, why = 0.0, ""
    for r in regs:
        share = max(0.0, min(end, r.t1) - max(start, r.t0)) / span
        # a 9:16 crop of a 16:9 frame is ~0.32 of its width: wider text can never fit inside
        wide = min(1.0, max(0.0, (r.w - 0.30) / 0.30))
        risk = share * (0.35 + 0.65 * wide)
        if risk > worst:
            worst = risk
            why = f"big words burned into the video {r.where()}"
    return round(min(1.0, worst), 2), why


# --- fitting a crop window around text ------------------------------------------------------------

MARGIN = 0.012           # of the frame width: room left between a text block and a crop edge


def cuts(cx: np.ndarray, cy: np.ndarray, hw: np.ndarray, hh: np.ndarray, box: Box, margin: float) -> np.ndarray:
    """Frames where the window (centre cx, cy; half sizes hw, hh) cuts through the box:
    not fully inside it (with `margin` to spare), not fully outside it."""
    x0, y0, x1, y1 = box
    inside = (cx - hw <= x0 - margin) & (cx + hw >= x1 + margin) & (cy - hh <= y0 - margin) & (cy + hh >= y1 + margin)
    outside = (cx + hw <= x0 + margin * 0.25) | (cx - hw >= x1 - margin * 0.25) | \
        (cy + hh <= y0 + margin * 0.25) | (cy - hh >= y1 - margin * 0.25)
    return ~(inside | outside)


MODES = ("in", "left", "right", "up", "down")


def fit_window(cx: np.ndarray, cy: np.ndarray, zoom: np.ndarray, cw: float, ch: float, W: float, H: float,
               boxes: Sequence[Box], faces: Optional[np.ndarray] = None,
               max_zoom: float = 1.4, margin: Optional[float] = None, allow_zoom: bool = True,
               ) -> Optional[Dict[str, Any]]:
    """Move (and if need be zoom) a crop window so no text box is cut.

    cx, cy, zoom: the planned window centre and zoom per frame (source px; zoom 1 = a cw x ch window).
    boxes: text blocks in source px. faces: rows (frame index, x0, y0, x1, y1) in source px of the
    faces the window must keep well framed — only those wholly inside the planned window count.
    Tries, per box: keep it whole inside (preferred), else keep the window clear of it to its left,
    right, above or below (zooming in up to `max_zoom` when there is no room otherwise). Returns
    {"cx", "cy", "zoom", "modes", "moved"} for the way that keeps the most text whole and moves the
    least, or None when nothing works (the caller shows the whole picture instead).
    """
    import itertools
    cx = np.asarray(cx, float)
    cy = np.asarray(cy, float)
    zoom = np.asarray(zoom, float)
    n = len(cx)
    m = (MARGIN * W) if margin is None else float(margin)
    boxes = [tuple(map(float, b)) for b in boxes][:3]
    if not boxes:
        return {"cx": cx, "cy": cy, "zoom": zoom, "modes": [], "moved": 0.0}
    top_zoom = max_zoom if allow_zoom else 1.0
    hw0, hh0 = cw / (2 * zoom), ch / (2 * zoom)

    F = np.zeros((0, 5))
    if faces is not None and len(faces):
        F = np.asarray(faces, float).reshape(-1, 5)
        F = F[(F[:, 0] >= 0) & (F[:, 0] < n)]
        k = F[:, 0].astype(int)
        inside = ((cx[k] - hw0[k] <= F[:, 1]) & (F[:, 3] <= cx[k] + hw0[k])
                  & (cy[k] - hh0[k] <= F[:, 2] + 0.25 * (F[:, 4] - F[:, 2])) & (F[:, 4] <= cy[k] + hh0[k] + 1))
        F = F[inside]
    fk = F[:, 0].astype(int)
    # a face whose forehead the planned crop already trimmed may keep that trim; any other keeps its top
    allow = np.where(F[:, 2] < cy[fk] - hh0[fk], 0.25, 0.0) if len(F) else np.zeros(0)
    checked = len(np.unique(fk))

    best = None
    for combo in itertools.product(MODES, repeat=len(boxes)):
        zmin = np.ones(n)
        zmax = np.maximum(np.full(n, top_zoom), np.minimum(zoom, max_zoom))   # planned punch-ins stay where they fit
        ok = True
        for mode, (x0, y0, x1, y1) in zip(combo, boxes):
            if mode == "in":
                zmax = np.minimum(zmax, min(cw / max(1.0, x1 - x0 + 2 * m), ch / max(1.0, y1 - y0 + 2 * m)))
                continue
            room = {"left": x0 - m, "right": W - x1 - m, "up": y0 - m, "down": H - y1 - m}[mode]
            if room <= 0:
                ok = False
                break
            zmin = np.maximum(zmin, (cw if mode in ("left", "right") else ch) / room)
        if not ok or (zmin > zmax + 1e-9).any():
            continue
        z = np.clip(zoom, zmin, zmax)
        hw, hh = cw / (2 * z), ch / (2 * z)
        lox, hix = hw.copy(), W - hw
        loy, hiy = hh.copy(), H - hh
        for mode, (x0, y0, x1, y1) in zip(combo, boxes):
            if mode == "in":
                lox = np.maximum(lox, x1 + m - hw)
                hix = np.minimum(hix, x0 - m + hw)
                loy = np.maximum(loy, y1 + m - hh)
                hiy = np.minimum(hiy, y0 - m + hh)
            elif mode == "left":
                hix = np.minimum(hix, x0 - m - hw)
            elif mode == "right":
                lox = np.maximum(lox, x1 + m + hw)
            elif mode == "up":
                hiy = np.minimum(hiy, y0 - m - hh)
            else:
                loy = np.maximum(loy, y1 + m + hh)
        if (lox > hix + 0.5).any() or (loy > hiy + 0.5).any():
            continue
        nx = np.clip(cx, lox, np.maximum(lox, hix))
        ny = np.clip(cy, loy, np.maximum(loy, hiy))
        if checked:
            # the faces it had must stay well framed: wholly inside, not jammed against a side
            pad = 0.04 * 2 * hw[fk]
            out = ((F[:, 1] < nx[fk] - hw[fk] + pad) | (F[:, 3] > nx[fk] + hw[fk] - pad)
                   | (F[:, 2] + allow * (F[:, 4] - F[:, 2]) < ny[fk] - hh[fk]) | (F[:, 4] > ny[fk] + hh[fk] + 1))
            if len(np.unique(fk[out])) > 0.1 * checked:
                continue
        moved = float(np.mean(np.abs(nx - cx)) / cw + np.mean(np.abs(ny - cy)) / ch
                      + 2.0 * np.mean(np.maximum(0.0, z - zoom)))
        rank = (sum(1 for mode in combo if mode != "in"), moved)
        if best is None or rank < best[0]:
            best = (rank, {"cx": nx, "cy": ny, "zoom": z, "modes": list(combo), "moved": moved})
    return best[1] if best else None
