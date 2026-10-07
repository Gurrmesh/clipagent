"""A/V sync harness.

Builds a 29.97 fps source where a white flash frame and a 1 kHz beep start at
exactly the same instant once per second, cuts it with a many-segment keep
plan whose boundaries are deliberately off the frame grid, renders it with a
given renderer, then measures (beep onset - flash time) for every flash that
survives. A perfect renderer reports ~0 ms everywhere; a drifting one shows
the offset growing with each cut.
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import json
import subprocess
import sys
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np

FPS = Fraction(30000, 1001)
SR = 48000
W, H = 1920, 1080


def make_source(path: Path, seconds: float = 40.0) -> list[float]:
    """Write the test source. Returns the true event times (seconds)."""
    n_frames = int(seconds * FPS)
    flash_frames = [int(round((k + 0.5) * FPS)) for k in range(int(seconds) - 1)]
    events = [f / FPS for f in flash_frames]

    audio = np.zeros(int(seconds * SR), dtype=np.float32)
    beep_len = int(SR / FPS)                      # one frame long
    tone = 0.6 * np.sin(2 * np.pi * 1000 * np.arange(beep_len) / SR).astype(np.float32)
    for t in events:
        s = int(round(float(t) * SR))
        audio[s:s + beep_len] = tone
    wav_path = path.with_suffix(".wav")
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SR)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())

    flash_set = set(flash_frames)
    enc = subprocess.Popen([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}", "-framerate", f"{FPS.numerator}/{FPS.denominator}",
        "-i", "-", "-i", str(wav_path),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest", str(path),
    ], stdin=subprocess.PIPE)
    dark = np.full((H, W), 30, np.uint8)
    # a face-sized grey blob so framing code has something to look at
    dark[400:700, 850:1070] = 90
    white = np.full((H, W), 255, np.uint8)
    for i in range(n_frames):
        enc.stdin.write((white if i in flash_set else dark).tobytes())
    enc.stdin.close(); enc.wait()
    return [float(e) for e in events]


def measure(path: Path) -> list[tuple[float, float]]:
    """(flash_time, beep_onset - flash_time in ms) for each flash in `path`."""
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True).stdout)["streams"][0]
    w, h = info["width"], info["height"]
    num, den = map(int, info["r_frame_rate"].split("/"))
    fps = num / den
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=64:64,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 64, 64)
    lum = frames.reshape(len(frames), -1).mean(axis=1)
    flash_idx = [i for i in range(len(lum)) if lum[i] > 180 and (i == 0 or lum[i - 1] <= 180)]

    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
                          "-f", "s16le", "-"], capture_output=True).stdout
    a = np.abs(np.frombuffer(pcm, np.int16).astype(np.float32))
    # onset = first sample of each burst above threshold, bursts >= 200 ms apart
    thr = 0.25 * a.max() if a.size else 1
    hot = np.flatnonzero(a > thr)
    onsets = []
    for s in hot:
        if not onsets or s - onsets[-1] > SR * 0.2:
            onsets.append(s)
    onsets_t = np.array(onsets) / SR

    out = []
    for i in flash_idx:
        t = i / fps
        if onsets_t.size:
            j = int(np.argmin(np.abs(onsets_t - t)))
            out.append((t, (onsets_t[j] - t) * 1000.0))
    return out


def report(name: str, rows: list[tuple[float, float]]) -> None:
    if not rows:
        print(f"{name}: no flashes found"); return
    offs = np.array([r[1] for r in rows])
    print(f"{name}: {len(rows)} flashes | offset ms  first={offs[0]:+.1f}  last={offs[-1]:+.1f}  "
          f"min={offs.min():+.1f}  max={offs.max():+.1f}  spread={offs.max()-offs.min():.1f}")
    print("   per flash:", " ".join(f"{o:+.0f}" for o in offs))


def keep_plan(total: float) -> list[tuple[float, float]]:
    """Keep segments with off-grid boundaries; each gap is 0.3-0.6 s and never swallows a flash."""
    rng = np.random.default_rng(7)
    keep, t = [], 0.0
    while t < total - 1.5:
        # end each segment somewhere between flashes (flashes sit at k+0.5)
        k = int(t) + int(rng.integers(1, 3))
        end = k + 0.62 + float(rng.uniform(0.0, 0.2))     # after the flash at k+0.5
        gap = float(rng.uniform(0.28, 0.55))              # stays before k+1.5
        keep.append((round(t, 3), round(min(end, total), 3)))
        t = end + gap
    return keep


if __name__ == "__main__":
    work = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sync")
    work.mkdir(parents=True, exist_ok=True)
    src = work / "sync_source.mp4"
    if not src.exists():
        make_source(src)
    report("source", measure(src))
