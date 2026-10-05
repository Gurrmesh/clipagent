"""Make a test song with known beats and a known drop (royalty-free: synthesised here).

usage: python tools/make_test_song.py BPM SECONDS DROP_AT OFFSET out.mp3
   e.g. python tools/make_test_song.py 128 60 20.625 0.37 data/work/test128.mp3
The first loud beat at or after DROP_AT is the true drop. Used by tests/beats_detect.py.
"""
import sys, subprocess
import numpy as np
SR = 44100

def kick(n=0.35, f0=110, f1=45):
    t = np.arange(int(SR * n)) / SR
    f = f1 + (f0 - f1) * np.exp(-t * 25)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(ph) * np.exp(-t * 9)

def snare(n=0.2):
    t = np.arange(int(SR * n)) / SR
    return (np.random.randn(len(t)) * 0.6 + np.sin(2 * np.pi * 190 * t) * 0.4) * np.exp(-t * 22)

def hat(n=0.05):
    t = np.arange(int(SR * n)) / SR
    x = np.random.randn(len(t))
    x = np.diff(x, prepend=0)
    return x * np.exp(-t * 80) * 0.25

def song(bpm, seconds, drop_at, offset=0.0, seed=1):
    np.random.seed(seed)
    y = np.zeros(int(SR * (seconds + 1)))
    beat = 60.0 / bpm
    def add(sig, t, g):
        i = int(t * SR)
        j = min(len(y), i + len(sig))
        if i < len(y):
            y[i:j] += sig[:j - i] * g
    n = int((seconds - offset) / beat)
    for k in range(n):
        t = offset + k * beat
        loud = t >= drop_at
        add(kick(), t, 1.0 if loud else 0.35)
        if k % 2 == 1:
            add(snare(), t, 0.8 if loud else 0.2)
        add(hat(), t + beat / 2, 0.6)
        add(hat(), t, 0.4)
        if loud:  # bass on the drop
            tt = np.arange(int(SR * beat * 0.9)) / SR
            add(np.sin(2 * np.pi * 55 * tt) * np.exp(-tt * 2) * 0.5, t, 1.0)
    y = y[:int(SR * seconds)]
    return y / np.abs(y).max() * 0.9

if __name__ == "__main__":
    bpm, secs, drop, off, out = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
    y = song(bpm, secs, drop, off)
    pcm = (y * 32767).astype(np.int16).tobytes()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "-", "-b:a", "192k", out], input=pcm, check=True)
