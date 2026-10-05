"""Flow edits' motion matching (app/motionmatch.py) — offline, on made-up shots with known movement.

Run: python tests/motion_match.py
Draws short videos where a textured shape slides right, left, up or down,
one that zooms in and one that holds still. Checks the measured direction,
zoom and stillness, that a cut into the same movement costs less than into
the opposite one, that the chain keeps movements flowing (and the opener
and drop where they belong), that the stillest extra shots are switched
off, and that each cut's blur follows the motion.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="motion_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app import edits, motionmatch  # noqa: E402

FAILS = []
TMP = Path(os.environ["DATA_DIR"])


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


W, H, FPS, SECONDS = 640, 360, 30, 8
rng = np.random.default_rng(4)
backdrop = cv2.GaussianBlur((rng.random((H, W, 3)) * 120 + 40).astype(np.uint8), (0, 0), 3)
texture = cv2.GaussianBlur((rng.random((140, 140, 3)) * 255).astype(np.uint8), (0, 0), 1.5)


def shot(name, kind, speed=120.0):
    """A shape moving `kind` (right/left/up/down/zoom/still) between seconds 2 and 6; still before and after."""
    path = TMP / f"{name}.mp4"
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                             "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                             "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)
    for i in range(SECONDS * FPS):
        t = i / FPS
        u = min(max(t - 2.0, 0.0), 4.0)                     # moving from 2 s to 6 s
        img = backdrop.copy()
        cx, cy, scale = W / 2 - 120 * (kind in ("right",)) + 120 * (kind in ("left",)), H / 2, 1.0
        cy += -60 * (kind == "down") + 60 * (kind == "up")
        if kind == "right":
            cx += speed * u / 4 * 2
        elif kind == "left":
            cx -= speed * u / 4 * 2
        elif kind == "down":
            cy += speed * u / 4
        elif kind == "up":
            cy -= speed * u / 4
        elif kind == "zoom":
            scale = 1.0 + 1.2 * u / 4
        tex = cv2.resize(texture, None, fx=scale, fy=scale)
        th, tw = tex.shape[:2]
        x0, y0 = int(cx - tw / 2), int(cy - th / 2)
        xa, ya, xb, yb = max(0, x0), max(0, y0), min(W, x0 + tw), min(H, y0 + th)
        img[ya:yb, xa:xb] = tex[ya - y0:yb - y0, xa - x0:xb - x0]
        proc.stdin.write(img.tobytes())
    proc.stdin.close()
    proc.wait()
    return path


print("== made-up shots")
paths = {k: shot(k, k) for k in ("right", "left", "up", "down", "zoom", "still")}
paths["right2"] = shot("right2", "right", 90.0)
paths["left2"] = shot("left2", "left", 90.0)

print("== measuring movement")
m = {k: motionmatch.measure(p, 3.0, 5.0, SECONDS) for k, p in paths.items()}
expect(m["right"]["v_in"][0] > 0.05 and abs(m["right"]["v_in"][1]) < 0.03, f"right moves right {m['right']['v_in']}")
expect(m["left"]["v_out"][0] < -0.05, f"left moves left {m['left']['v_out']}")
expect(m["up"]["v_in"][1] < -0.03 and m["down"]["v_in"][1] > 0.03,
       f"up moves up, down moves down {m['up']['v_in']} {m['down']['v_in']}")
expect(m["zoom"]["zoom_in"] > 0.02, f"a zoom-in spreads outwards ({m['zoom']['zoom_in']})")
expect(m["still"]["energy"] < m["right"]["energy"] / 4, "a still shot has little movement")
expect(2.0 - 0.3 <= m["right"]["start"] and m["right"]["end"] <= 6.0 + 0.3,
       f"the shot moves onto where the movement is ({m['right']['start']}-{m['right']['end']})")
expect(m["right"]["start"] <= m["right"]["hit"] <= m["right"]["end"], "the hit sits at the peak of the movement")

print("== what a cut costs")
expect(motionmatch.cost(m["right"], m["right2"]) < motionmatch.cost(m["right"], m["left"]),
       "right into right costs less than right into left")
expect(motionmatch.cost(m["up"], m["up"]) < motionmatch.cost(m["up"], m["down"]), "up into up beats up into down")
expect(motionmatch.cost(m["right"], m["right2"]) < motionmatch.cost(m["right"], m["still"]),
       "a movement carried on beats a cut into stillness")

print("== the chain")
names = ["right", "left", "right2", "up", "left2", "down"]
shots = [{"motion": m[n]} for n in names]
seq = motionmatch.chain(shots, first=0, drop=3, drop_at=3)
expect(seq[0] == 0 and seq[3] == 3 and sorted(seq) == list(range(len(names))), "opener first, the drop in its place")
claude_cost = sum(motionmatch.cost(shots[a]["motion"], shots[b]["motion"]) for a, b in zip(range(5), range(1, 6)))
chain_cost = sum(motionmatch.cost(shots[a]["motion"], shots[b]["motion"]) for a, b in zip(seq, seq[1:]))
expect(chain_cost < claude_cost, f"the chain flows better than the picked order ({chain_cost:.2f} vs {claude_cost:.2f})")
pairs = [(names[a], names[b]) for a, b in zip(seq, seq[1:])]
expect(("right", "right2") in pairs or ("left", "left2") in pairs or ("left2", "left") in pairs
       or ("right2", "right") in pairs, f"movements continue across cuts {pairs}")

print("== order(): measure, switch off the stillest, chain")
moments = [{"id": f"s{i}", "source": n, "start": 3.0, "end": 4.5, "hit": 3.5, "text": "", "drop": n == "up"}
           for i, n in enumerate(["right", "still", "left", "right2", "up", "left2", "zoom", "down"])]
got, notes = motionmatch.order(moments, paths, {k: float(SECONDS) for k in paths}, keep=6, drop_at=2)
on = [x for x in got if not x.get("off")]
expect(len(on) == 6 and any(x["source"] == "still" and x.get("off") for x in got) and notes,
       "with room for 6 shots, the still one is switched off (and it says so)")
expect(on[0]["source"] == "right" and on[2].get("drop"), "Claude's opener still opens, the drop is third")
expect(all("motion" in x for x in on), "every shot carries its movement")

print("== the cut's blur follows the motion")
vec = motionmatch.blur_vector(m["right"], m["right2"])
expect(vec and vec[0] > 5 and abs(vec[1]) < vec[0] / 3, f"a rightward cut blurs sideways {vec}")
expect(motionmatch.blur_vector(m["still"], m["still"]) is None, "no streak between still shots (a zoom blur instead)")
beats_ = [round(0.365 + k * 0.46875, 4) for k in range(120)]
song = {"id": "s", "analysis": {"duration": 60.0, "bpm": 128, "beats": beats_, "bars": beats_[0::4],
                                "drop": beats_[44]}}
tl = edits.build_timeline(on, "flow", 20, song, {}, {}, durations={k: float(SECONDS) for k in paths})
cuts = [s for s in tl["segments"][1:] if s.get("blur_vec")]
expect(cuts, f"Flow cuts carry blur directions ({len(cuts)} of {len(tl['segments']) - 1})")

print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed")
sys.exit(1 if FAILS else 0)
