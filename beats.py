"""Where the beats are in a song, and where it drops — numpy only.

An edit lands its cuts on the beat and its best moment on the drop, so every
song you add is read once: its tempo, every beat, the bar lines (downbeats),
how loud it is over time, and the moment the energy jumps (the drop).

The method is the classic one: an onset-strength curve from the spectral
flux of a short-time Fourier transform, the tempo from that curve's
autocorrelation (weighted towards the 90-160 BPM most music sits in), the
beats from Ellis' dynamic-programming tracker, the bar phase from where the
strongest onsets fall, and the drop from the largest sustained rise in
loudness.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

SR = 22050
HOP = 512
N_FFT = 2048
FPS = SR / HOP                    # onset-curve frames per second (~43)


def load(path: Path, max_seconds: float = 600.0) -> np.ndarray:
    """The song as mono floats at 22.05 kHz."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-t", str(max_seconds), "-ac", "1", "-ar", str(SR),
           "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("Couldn't read that sound file — is it an audio or video file?")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def _frames(y: np.ndarray) -> np.ndarray:
    pad = np.pad(y, (N_FFT // 2, N_FFT // 2))
    count = 1 + (len(pad) - N_FFT) // HOP
    strides = (pad.strides[0] * HOP, pad.strides[0])
    return np.lib.stride_tricks.as_strided(pad, shape=(count, N_FFT), strides=strides)


def onset_envelope(y: np.ndarray) -> np.ndarray:
    """How much new sound starts in each frame (spectral flux on a log scale)."""
    window = np.hanning(N_FFT).astype(np.float32)
    env = []
    frames = _frames(y)
    for i in range(0, len(frames), 2048):                 # in chunks: a long song stays light on memory
        mag = np.abs(np.fft.rfft(frames[i:i + 2048] * window, axis=1)).astype(np.float32)
        env.append(np.log1p(10.0 * mag))
    spec = np.concatenate(env, axis=0) if env else np.zeros((1, N_FFT // 2 + 1), np.float32)
    flux = np.maximum(0.0, np.diff(spec, axis=0, prepend=spec[:1])).sum(axis=1)
    # take away the slow trend so only the hits stand out
    k = int(FPS)                                           # ~1 s
    trend = np.convolve(flux, np.ones(k) / k, mode="same")
    onset = np.maximum(0.0, flux - trend)
    peak = onset.max() or 1.0
    return (onset / peak).astype(np.float32)


def loudness(y: np.ndarray) -> np.ndarray:
    """RMS loudness per onset frame, smoothed over about half a second."""
    frames = _frames(y)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1)).astype(np.float32)
    k = max(1, int(FPS / 2))
    rms = np.convolve(rms, np.ones(k) / k, mode="same")
    return rms / (rms.max() or 1.0)


def tempo(env: np.ndarray, lo: float = 60.0, hi: float = 190.0) -> float:
    """Beats per minute, from the onset curve's autocorrelation."""
    x = env - env.mean()
    n = len(x)
    if n < FPS * 4:
        return 120.0
    size = 1 << int(np.ceil(np.log2(2 * n)))
    spec = np.fft.rfft(x, size)
    ac = np.fft.irfft(spec * np.conj(spec), size)[:n]
    lags = np.arange(n, dtype=np.float64)
    with np.errstate(divide="ignore"):
        bpm = 60.0 * FPS / lags
    ok = (bpm >= lo) & (bpm <= hi)
    # most songs sit at 90-160: prefer those when the evidence is close
    prior = np.exp(-0.5 * (np.log2(np.where(ok, bpm, 120.0) / 125.0) / 0.9) ** 2)
    score = np.where(ok, ac * prior, -np.inf)
    lag = int(np.argmax(score))
    # the autocorrelation also peaks at twice the beat period (half the tempo):
    # a slow pick with a strong peak at half its lag is really the faster tempo
    while 60.0 * FPS / lag < 88.0 and lag // 2 >= 1 and 60.0 * FPS / (lag / 2) <= hi:
        half = int(round(lag / 2))
        near = ac[max(1, half - 1):half + 2]
        if near.max() >= 0.35 * ac[lag]:
            lag = max(1, half - 1) + int(np.argmax(near))
        else:
            break
    # refine to a fraction of a frame by fitting a parabola through the peak
    if 1 <= lag < n - 1:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        denom = a - 2 * b + c
        if denom:
            lag = lag + 0.5 * (a - c) / denom
    return float(60.0 * FPS / lag)


def track(env: np.ndarray, bpm: float, tightness: float = 100.0) -> List[float]:
    """Beat times in seconds (Ellis 2007: dynamic programming over the onset curve)."""
    period = 60.0 * FPS / bpm
    n = len(env)
    if n < 2:
        return []
    localscore = np.convolve(env, np.exp(-0.5 * (np.arange(-period, period + 1) * 32.0 / period) ** 2),
                             mode="same")
    backlink = np.full(n, -1, dtype=np.int64)
    cumscore = localscore.astype(np.float64).copy()
    window = np.arange(-int(round(2 * period)), -int(round(period / 2)) + 1)
    txcost = -tightness * np.log(-window / period) ** 2
    for i in range(n):
        lo = i + window
        valid = lo >= 0
        if not valid.any():
            continue
        cand = cumscore[lo[valid]] + txcost[valid]
        j = int(np.argmax(cand))
        if cand[j] > 0:
            cumscore[i] = localscore[i] + cand[j]
            backlink[i] = lo[valid][j]
    # start from the best score in the last beat period and walk back
    tail = max(0, n - int(period))
    i = tail + int(np.argmax(cumscore[tail:]))
    beats = []
    while i >= 0:
        beats.append(i)
        i = backlink[i]
    beats = np.array(beats[::-1], dtype=np.float64)
    # drop weak beats at the very start and end (silence, fade)
    strength = localscore[beats.astype(int)]
    keep = strength > 0.15 * np.median(strength) if len(strength) else []
    if len(beats) and keep.any():
        first, last = np.argmax(keep), len(keep) - 1 - np.argmax(keep[::-1])
        beats = beats[first:last + 1]
    return [round(float(b) / FPS, 3) for b in beats]


def downbeats(beats: List[float], env: np.ndarray, per_bar: int = 4) -> List[float]:
    """The first beat of each bar: the phase whose beats carry the strongest hits."""
    if len(beats) < per_bar * 2:
        return beats[::per_bar]
    idx = [min(len(env) - 1, int(round(b * FPS))) for b in beats]
    strengths = np.array([env[max(0, i - 2):i + 3].max() for i in idx])
    best = max(range(per_bar), key=lambda k: strengths[k::per_bar].mean())
    return beats[best::per_bar]


def find_drop(beats: List[float], bars: List[float], level: np.ndarray, duration: float) -> float:
    """The moment the song kicks in: the beat after which it gets — and stays —
    much louder than just before. The first big drop or chorus, not a random
    loud bar near the end."""
    if not beats:
        return 0.0
    def mean(a: float, b: float) -> float:
        i, j = int(max(0.0, a) * FPS), int(min(duration, b) * FPS)
        return float(level[i:j].mean()) if j > i else 0.0
    best, best_score = beats[0], -1e9
    for t in beats:
        if t < 3.0 or t > duration - 6.0:
            continue
        jump = mean(t + 0.1, t + 2.5) - mean(t - 2.5, t - 0.1)        # sharp: where exactly it rises
        sustained = mean(t, t + 8.0) - mean(t - 8.0, t)                 # and it isn't a one-off hit
        score = jump * 2.0 + sustained + 0.5 * mean(t, t + 8.0) - 0.002 * t
        if score > best_score:
            best, best_score = t, score
    period = float(np.median(np.diff(beats))) if len(beats) > 1 else 0.5
    near = [b for b in bars if abs(b - best) <= period * 0.3]
    if near:
        best = min(near, key=lambda b: abs(b - best))
    return round(float(best), 3)


def analyze(path: Path) -> Dict[str, Any]:
    """Everything an edit needs to know about a song."""
    y = load(path)
    duration = len(y) / SR
    if duration < 5:
        raise RuntimeError("That sound is under 5 seconds — too short to cut an edit to")
    env = onset_envelope(y)
    level = loudness(y)
    bpm = tempo(env)
    beats = track(env, bpm)
    if len(beats) > 4:                          # the tracker's beats give a steadier tempo
        bpm = round(60.0 / float(np.median(np.diff(beats))), 1)
    bars = downbeats(beats, env)
    drop = find_drop(beats, bars, level, duration)
    curve = [round(float(v), 3) for v in level[::max(1, int(FPS / 4))]]     # 4 points a second, for the UI
    return {"duration": round(duration, 2), "bpm": round(float(bpm), 1), "beats": beats, "bars": bars,
            "drop": drop, "energy": curve}
