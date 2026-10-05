"""Flow edits: chain shots so every cut carries the movement into the next one.

The "infinite transition" edits cut about once a second, and each cut lands
mid-movement into a shot whose motion continues it: a hand sweeping right
cuts to a head turning right, a lean-in cuts to a push-in. Claude can only
read words, so it suggests shots where he's likely moving; this module
measures what really moves:

1. For each shot, a few seconds around it are read small (10 frames a
   second, 256 px wide) and dense optical flow (OpenCV DIS) is measured
   between neighbouring frames: the main direction and speed of what moves
   at the shot's start and at its end, the zoom (divergence), the turn
   (curl), and where the moving subject sits. The 1.2-3 s window with the
   most movement becomes the shot, and its peak of motion becomes the hit
   that lands on the beat.
2. The cost of cutting from shot A into shot B = how differently they move
   (direction, speed, zoom) + how far the subject jumps on screen. A small
   beam search orders the shots to keep that cost low, keeping Claude's
   opener first and the drop moment where the drop is.
3. The motion at each cut also gives its blur direction (see editrender).

The idea follows Netflix's match-cut research (github.com/Netflix/matchcut):
score every pair of shots for how well they cut together, then chain the
best. No heavy model is used — only OpenCV, already installed.
"""
from __future__ import annotations

import math
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

SAMPLE_FPS = 10
WIDTH = 256
SEARCH = 2.0                     # look this far either side of Claude's shot for the real movement
MIN_SHOT, MAX_SHOT = 1.2, 3.0
STILL = 0.04                     # below this (image widths a second) a shot counts as still


def _frames(path: Path, start: float, end: float) -> Tuple[List[np.ndarray], List[float]]:
    """Small grey frames from start to end, SAMPLE_FPS a second."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        w, h = (int(x) for x in probe.stdout.strip().split(",")[:2])
    except ValueError:
        return [], []
    height = max(2, int(round(WIDTH * h / max(1, w) / 2)) * 2)
    start = max(0.0, start)
    proc = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                           "-i", str(path), "-vf", f"fps={SAMPLE_FPS},scale={WIDTH}:{height}:flags=area",
                           "-an", "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
    size = WIDTH * height
    count = len(proc.stdout) // size
    frames = [np.frombuffer(proc.stdout[i * size:(i + 1) * size], np.uint8).reshape(height, WIDTH)
              for i in range(count)]
    return frames, [start + i / SAMPLE_FPS for i in range(count)]


def flow_stats(a: np.ndarray, b: np.ndarray, dis: Any) -> Dict[str, float]:
    """What moves between two frames: the moving parts' mean direction (image
    widths a second), how much moves, the zoom and turn, and where it is."""
    f = dis.calc(a, b, None)
    h, w = a.shape
    fx, fy = f[..., 0], f[..., 1]
    mag = np.hypot(fx, fy)
    moving = mag > max(0.4, float(np.percentile(mag, 75)))
    scale = SAMPLE_FPS / w
    energy = float(mag.mean()) * scale
    if moving.sum() < 0.01 * mag.size:
        return {"vx": 0.0, "vy": 0.0, "energy": energy, "zoom": 0.0, "rot": 0.0, "x": 0.5, "y": 0.5}
    wts = mag[moving]
    vx = float((fx[moving] * wts).sum() / wts.sum()) * scale
    vy = float((fy[moving] * wts).sum() / wts.sum()) * scale
    ys, xs = np.nonzero(moving)
    cx, cy = float((xs * wts).sum() / wts.sum()), float((ys * wts).sum() / wts.sum())
    # zoom and turn: how the moving part spreads away from (and around) its own centre,
    # once its sideways movement is taken out
    rx, ry = xs - cx, ys - cy
    mx, my = fx[moving] - vx / scale, fy[moving] - vy / scale
    rr = float((rx * rx + ry * ry).sum()) or 1.0
    zoom = float((mx * rx + my * ry).sum() / rr) * SAMPLE_FPS
    rot = float((my * rx - mx * ry).sum() / rr) * SAMPLE_FPS
    return {"vx": vx, "vy": vy, "energy": energy, "zoom": zoom, "rot": rot, "x": cx / w, "y": cy / h}


def measure(path: Path, start: float, end: float, src_dur: float) -> Optional[Dict[str, Any]]:
    """The best-moving 1.2-3 s window around a shot, and how it moves."""
    import cv2
    lo, hi = max(0.0, start - SEARCH), min(src_dur, end + SEARCH)
    frames, times = _frames(path, lo, hi)
    if len(frames) < 4:
        return None
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
    steps = [flow_stats(a, b, dis) for a, b in zip(frames, frames[1:])]
    mids = [(t0 + t1) / 2 for t0, t1 in zip(times, times[1:])]
    energy = np.array([s["energy"] for s in steps])
    span = min(MAX_SHOT, max(MIN_SHOT, end - start))
    n = max(2, int(round(span * SAMPLE_FPS)))
    if len(steps) <= n:
        i0 = 0
    else:
        window = np.convolve(energy, np.ones(n), mode="valid")
        # prefer the window nearest Claude's shot when movement is about equal
        centre = np.array([abs(mids[i] + span / 2 - (start + end) / 2) for i in range(len(window))])
        i0 = int(np.argmax(window - 0.002 * centre * window.max()))
    part = steps[i0:i0 + n]
    third = max(1, len(part) // 3)

    def mean(items: List[Dict[str, float]], key: str) -> float:
        return float(np.mean([s[key] for s in items])) if items else 0.0

    peak = i0 + int(np.argmax(energy[i0:i0 + n]))
    s0 = max(lo, mids[i0] - 0.5 / SAMPLE_FPS)
    return {
        "start": round(s0, 2), "end": round(min(hi, s0 + span), 2), "hit": round(mids[peak], 2),
        "v_in": [round(mean(part[:third], "vx"), 4), round(mean(part[:third], "vy"), 4)],
        "v_out": [round(mean(part[-third:], "vx"), 4), round(mean(part[-third:], "vy"), 4)],
        "zoom_in": round(mean(part[:third], "zoom"), 4), "zoom_out": round(mean(part[-third:], "zoom"), 4),
        "rot_in": round(mean(part[:third], "rot"), 4), "rot_out": round(mean(part[-third:], "rot"), 4),
        "pos_in": [round(mean(part[:third], "x"), 3), round(mean(part[:third], "y"), 3)],
        "pos_out": [round(mean(part[-third:], "x"), 3), round(mean(part[-third:], "y"), 3)],
        "energy": round(float(energy[i0:i0 + n].mean()), 4),
    }


def _norm(v: List[float]) -> float:
    return math.hypot(v[0], v[1])


def cost(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    """How badly shot A's ending cuts into shot B's start (0 = the motion carries straight on)."""
    va, vb = a["v_out"], b["v_in"]
    ma, mb = _norm(va), _norm(vb)
    if ma > STILL and mb > STILL:
        direction = 1.0 - (va[0] * vb[0] + va[1] * vb[1]) / (ma * mb)          # 0 same way … 2 opposite
    elif ma > STILL or mb > STILL:
        direction = 0.9                                                         # movement into stillness
    else:
        direction = 0.5                                                         # two still shots
    speed = abs(math.log((ma + 0.02) / (mb + 0.02))) * 0.35
    zoom = min(1.0, abs(a["zoom_out"] - b["zoom_in"]) * 1.5) + min(0.6, abs(a["rot_out"] - b["rot_in"]))
    jump = math.dist(a["pos_out"], b["pos_in"]) * 0.8
    return direction + speed + zoom * 0.5 + jump


def chain(shots: List[Dict[str, Any]], first: int, drop: int, drop_at: int, beam: int = 12) -> List[int]:
    """Order shots so the motion carries from one into the next: shot `first`
    opens, shot `drop` sits at position `drop_at`, the rest as the cost says."""
    n = len(shots)
    if n <= 2:
        return list(range(n))
    drop_at = max(1 if drop != first else 0, min(n - 1, drop_at))
    costs = [[cost(shots[i]["motion"], shots[j]["motion"]) if i != j else 9.0 for j in range(n)] for i in range(n)]
    paths: List[Tuple[float, List[int]]] = [(0.0, [first])] if drop != first else [(0.0, [drop])]
    for pos in range(1, n):
        grown: List[Tuple[float, List[int]]] = []
        for total, path in paths:
            used = set(path)
            if pos == drop_at and drop not in used:
                options = [drop]
            else:
                options = [j for j in range(n) if j not in used and j != drop]
                if not options:
                    options = [drop] if drop not in used else []
            for j in options:
                grown.append((total + costs[path[-1]][j], path + [j]))
        grown.sort(key=lambda x: x[0])
        paths = grown[:beam] or paths
    return paths[0][1]


def order(moments: List[Dict[str, Any]], paths: Dict[str, Path], durations: Dict[str, float],
          keep: Optional[int] = None, drop_at: Optional[int] = None,
          progress: Optional[Callable[[int], None]] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Measure every shot's movement, move each onto its best-moving window,
    and put them in the order that flows. With `keep`, the stillest extra
    shots are switched off (they can be switched back on in the edit page)."""
    notes: List[str] = []
    todo = [m for m in moments if not m.get("off")]
    done = 0

    def one(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        nonlocal done
        got = measure(paths[m["source"]], m["start"], m["end"], durations.get(m["source"], 1e9))
        done += 1
        if progress:
            progress(int(100 * done / max(1, len(todo))))
        return got

    with ThreadPoolExecutor(max_workers=4) as pool:            # each shot is its own ffmpeg read
        measured = list(pool.map(one, todo))
    shots = []
    for m, got in zip(todo, measured):
        m = dict(m)
        if got:
            m.update({"start": got["start"], "end": got["end"], "hit": got["hit"], "motion": got})
        else:
            m["motion"] = {"v_in": [0, 0], "v_out": [0, 0], "zoom_in": 0, "zoom_out": 0, "rot_in": 0, "rot_out": 0,
                           "pos_in": [0.5, 0.5], "pos_out": [0.5, 0.5], "energy": 0.0}
        shots.append(m)
    if not shots:
        return moments, notes
    drop = next((i for i, m in enumerate(shots) if m.get("drop")), len(shots) // 2)
    off: List[Dict[str, Any]] = []
    if keep and len(shots) > keep:
        ranked = sorted((i for i in range(len(shots)) if i not in (0, drop)),
                        key=lambda i: shots[i]["motion"]["energy"], reverse=True)
        stay = {0, drop} | set(ranked[:max(0, keep - 2)])
        off = [dict(m, off=True) for i, m in enumerate(shots) if i not in stay]
        shots = [m for i, m in enumerate(shots) if i in stay]
        drop = next(i for i, m in enumerate(shots) if m.get("drop"))
        notes.append(f"Kept the {len(shots)} shots with the most movement; {len(off)} stiller ones are switched off.")
    still = sum(1 for m in shots if m["motion"]["energy"] < STILL / 2)
    if still > len(shots) // 2:
        notes.append("Most of these shots barely move, so the cuts can't follow much motion — Flow works best with "
                     "footage where he moves (gestures, walking, reactions).")
    sequence = chain(shots, 0, drop, drop if drop_at is None else drop_at)
    return [shots[i] for i in sequence] + off + [m for m in moments if m.get("off")], notes


def blur_vector(prev: Optional[Dict[str, Any]], cur: Optional[Dict[str, Any]]) -> Optional[List[float]]:
    """The streak for a cut, in output pixels: along the motion that carries through it."""
    if not prev and not cur:
        return None
    va = (prev or {}).get("v_out") or [0.0, 0.0]
    vb = (cur or {}).get("v_in") or [0.0, 0.0]
    vx, vy = (va[0] + vb[0]) / 2, (va[1] + vb[1]) / 2
    if math.hypot(vx, vy) < STILL:
        return None
    px = 1080 / 30 * 2.5                       # image widths a second → output px over ~2.5 frames
    dx, dy = vx * px * 1.9, vy * px * 1.9      # the 9:16 crop shows about half the width: motion looks bigger
    k = min(1.0, 70.0 / max(1e-6, math.hypot(dx, dy)))
    return [round(dx * k, 1), round(dy * k, 1)]
