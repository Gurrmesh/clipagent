"""Stitched-clip harness: right frames, right order, sound locked to picture.

The source is 29.97 fps. Every frame carries its own frame number as a 16-bit
code of black/white blocks in the middle of the picture, and once a second a
white flash frame and a 1 kHz beep start at the same instant. The clip is
stitched from parts that jump backwards, far forwards, and a little forwards
(one decode run), with every boundary off the frame grid.

Checks:
  order  every output frame shows exactly the source frame the timeline says
  sync   every flash that survives still starts with its beep (~0 ms)
  length the output has exactly as many frames as the parts add up to
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import subprocess
import sys
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synctest import measure, report  # noqa: E402

FPS = Fraction(30000, 1001)
SR = 48000
W, H = 1280, 720
BITS = 16
BLOCK = 40
X0 = W // 2 - 2 * BLOCK            # 4x4 grid centred horizontally, near the top
Y0 = 120


def make_source(path: Path, seconds: float = 120.0) -> None:
    n_frames = int(seconds * FPS)
    flash_frames = {int(round((k + 0.5) * FPS)) for k in range(int(seconds) - 1)}
    audio = np.zeros(int(seconds * SR), dtype=np.float32)
    beep_len = int(SR / FPS)
    tone = 0.6 * np.sin(2 * np.pi * 1000 * np.arange(beep_len) / SR).astype(np.float32)
    for f in flash_frames:
        s = int(round(float(Fraction(f) / FPS) * SR))
        audio[s:s + beep_len] = tone
    wav_path = path.with_suffix(".wav")
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SR)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())

    enc = subprocess.Popen([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}",
        "-framerate", f"{FPS.numerator}/{FPS.denominator}", "-i", "-", "-i", str(wav_path),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-g", "60", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest", str(path),
    ], stdin=subprocess.PIPE)
    for i in range(n_frames):
        frame = np.full((H, W), 255 if i in flash_frames else 30, np.uint8)
        for b in range(BITS):
            r, c = divmod(b, 4)
            frame[Y0 + r * BLOCK:Y0 + (r + 1) * BLOCK, X0 + c * BLOCK:X0 + (c + 1) * BLOCK] = \
                235 if (i >> b) & 1 else 16
        enc.stdin.write(frame.tobytes())
    enc.stdin.close()
    enc.wait()


def read_codes(path: Path, out_w: int, out_h: int) -> list[int]:
    """The frame number shown in every frame of the rendered 9:16 clip."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, out_h, out_w)
    crop_w = H * 9 / 16                        # the static centre crop (no faces, no motion)
    scale = out_w / crop_w
    left = W / 2 - crop_w / 2
    codes = []
    for fr in frames:
        v = 0
        for b in range(BITS):
            r, c = divmod(b, 4)
            sx = (X0 + (c + 0.5) * BLOCK - left) * scale
            sy = (Y0 + (r + 0.5) * BLOCK) * scale
            patch = fr[int(sy) - 8:int(sy) + 8, int(sx) - 8:int(sx) + 8]
            if patch.mean() > 128:
                v |= 1 << b
        codes.append(v)
    return codes


if __name__ == "__main__":
    from app import motion
    from app.config import RENDER_H, RENDER_W

    work = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/stitch")
    work.mkdir(parents=True, exist_ok=True)
    src = work / "stitch_source.mp4"
    if not src.exists():
        make_source(src)

    segments = [
        (60.21, 64.93),      # part 1
        (10.33, 14.07),      # back in time: a new run
        (90.05, 97.33),      # far ahead: a new run
        (97.91, 101.18),     # short skip: same run, a jump cut
        (106.02, 108.44),    # 5 s ahead: still the same run
        (30.12, 33.71),      # back again
    ]
    edits = {"motion": False, "captions_on": False, "hook_on": False, "headline_on": False,
             "labels_on": False, "layout": "fill", "auto_frame": True, "normalize_audio": False}
    out = motion.render_clip(source=src, clip_id="stitch_test", start=0, end=0, words=[],
                             edits=edits, has_audio=True, layout="fill", plan=None,
                             source_size=(W, H), segments=segments, debug=True)
    tl = out["timeline"]
    print("runs:", [(r.first, r.last, r.segs) for r in tl.runs()])
    expected = tl.src_index()
    codes = read_codes(Path(out["file"]), RENDER_W, RENDER_H)
    print(f"frames: rendered {len(codes)}, expected {len(expected)}")
    wrong = [(n, int(e), c) for n, (e, c) in enumerate(zip(expected, codes)) if int(e) != c]
    print(f"order: {len(expected) - len(wrong)} of {len(expected)} frames exactly right"
          + (f"; first wrong (out, want, got): {wrong[:6]}" if wrong else ""))
    report("stitched sync", measure(Path(out["file"])))
    print("stats:", out["stats"])
