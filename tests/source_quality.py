"""Source-mode quality fixes found on the Lovable podcasts — offline, no video needed.

usage: python tests/source_quality.py
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import captions, motion, transcribe  # noqa: E402

FAILS = []


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


print("== words keep the order they were spoken in")
raw = [("on", 1584.98, 1585.46), ("the", 1585.46, 1585.58), ("AI", 1585.58, 1585.88), ("that", 1585.88, 1586.16),
       ("we", 1586.16, 1586.3), ("did", 1586.3, 1586.42), ("a", 1585.96, 1586.56), ("lot", 1586.56, 1586.64)]
words = [{"w": w, "start": a, "end": b} for w, a, b in raw]
before = " ".join(w["w"] for w in captions.sanitize_words(words))
after = " ".join(w["w"] for w in captions.sanitize_words(transcribe.in_order(words)))
expect(before != after and after == "on the AI that we did a lot", f"captions read '{after}' (were '{before}')")
fixed = transcribe.in_order(words)
expect(all(b["start"] > a["start"] and a["end"] <= b["start"] for a, b in zip(fixed, fixed[1:])),
       "times only move forward and never overlap")

print("\n== misheard words fixed on the captions, timing kept")
ws = [{"w": w, "start": i, "end": i + 0.5} for i, w in enumerate("just a rapper. main workhorse is Anthropics Cloud Model".split())]
out = transcribe.respell(ws, {"rapper": "wrapper", "Anthropics Cloud Model": "Anthropic's Claude model"})
expect(" ".join(w["w"] for w in out) == "just a wrapper. main workhorse is Anthropic's Claude model",
       "'rapper.' -> 'wrapper.', 'Anthropics Cloud Model' -> 'Anthropic's Claude model'")
expect([w["start"] for w in out] == [w["start"] for w in ws], "same timings")

print("\n== the hook: on the first frame, line breaks kept, never over a face's eyes")
ass = Path(captions.build_ass([{"w": "hi", "start": 0.1, "end": 0.5}], 3.0,
                              hook="LOVABLE HIT\n$10M ARR", out_path=Path(tempfile.gettempdir()) / "_sq_test.ass")).read_text()
line = next(l for l in ass.splitlines() if ",Hook," in l and l.startswith("Dialogue"))
expect("\\fad(0," in line and "LOVABLE HIT\\N$10M ARR" in line, "no fade-in, and the typed line break stays")
N = 200
tl = SimpleNamespace(fps=Fraction(50, 1), src_index=lambda: np.arange(1000, 1000 + N))
cam = SimpleNamespace(cx=np.full(N, 960.0), cy=np.full(N, 540.0), zoom=np.ones(N))
block = captions.hook_block("THE BIKE RIDE\nTHAT STARTED\nLOVABLE")
wide = SimpleNamespace(faces={1000 + k: [(0.5, 0.25, 0.08, 0.14, 0.0)] for k in range(0, N, 5)})
close = SimpleNamespace(faces={1000 + k: [(0.5, 0.45, 0.28, 0.5, 0.0)] for k in range(0, N, 5)})
top = motion._hook_clear_top(cam, wide, tl, (1920, 1080), 2.4, captions.HOOK_TOP, block)
expect(top is not None and top > 600 and top + block <= captions.CAPTION_TOP,
       f"wide shot, eyes under the hook: moved below the chin ({top})")
expect(motion._hook_clear_top(cam, close, tl, (1920, 1080), 2.4, captions.HOOK_TOP, block) is None,
       "close-up, hook only over the forehead: left at the top")

print("\n== black bars: a letterboxed shot is zoomed past them")
src = np.arange(100, 300)


class TL:
    fps = Fraction(50, 1)
    segments = [(100, 300)]
    first, last = 100, 299

    def src_index(self):
        return src


an = motion.Analysis(cuts={100}, faces={}, detector="none", bars={f: (0.22, 0.77) for f in range(100, 300, 5)})
cam = motion.plan_camera(TL(), an, (1920, 1080), {"motion": False}, "fill", None)
hh = 1080 / (2 * cam.zoom[0])
expect(cam.cy[0] - hh >= 0.22 * 1080 - 1 and cam.cy[0] + hh <= 0.77 * 1080 + 1,
       f"window {cam.cy[0] - hh:.0f}-{cam.cy[0] + hh:.0f} inside the picture 238-832")
full = motion.Analysis(cuts={100}, faces={}, detector="none", bars={f: (0.0, 1.0) for f in range(100, 300, 5)})
expect(float(motion.plan_camera(TL(), full, (1920, 1080), {"motion": False}, "fill", None).zoom.max()) == 1.0,
       "a full-frame shot is left alone")

print("\nFAILED:" if FAILS else "\nall checks behaved", FAILS or "")
sys.exit(1 if FAILS else 0)
