"""Whisper transcription with word-level timestamps."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List

from . import media
from .config import AUDIO_CHUNK_SECONDS, WHISPER_API_KEY, WHISPER_BASE_URL, WHISPER_MODEL


def _client():
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(f"openai package not installed: {exc}") from exc

    if not WHISPER_API_KEY:
        raise RuntimeError(
            "No transcription key set. Put OPENAI_API_KEY (or WHISPER_API_KEY) in .env"
        )
    kwargs: Dict[str, Any] = {"api_key": WHISPER_API_KEY}
    if WHISPER_BASE_URL:
        kwargs["base_url"] = WHISPER_BASE_URL
    return OpenAI(**kwargs)


# A long podcast can use up an hourly transcription allowance part-way through.
# The service says how long to wait; waiting beats losing the whole run.
RATE_WAIT_MAX = 20 * 60      # never wait longer than this for one chunk
RATE_TRIES = 6
_WAIT_RE = re.compile(r"try again in\s+((?:\d+(?:\.\d+)?[hms]\s*)+)", re.I)


def _wait_seconds(exc: Exception) -> float:
    """How long the service asked us to wait, from its header or its message."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    try:
        ra = float(headers.get("retry-after"))
        if ra > 0:
            return ra
    except (TypeError, ValueError):
        pass
    m = _WAIT_RE.search(str(exc))
    if not m:
        return 30.0
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)([hms])", m.group(1)):
        total += float(num) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total or 30.0


def _create(client, part: Path, sleep=time.sleep, on_wait=None):
    for attempt in range(RATE_TRIES):
        try:
            with open(part, "rb") as fh:
                return client.audio.transcriptions.create(
                    file=fh,
                    model=WHISPER_MODEL,
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                )
        except Exception as exc:  # noqa: BLE001 — only rate limits and dropped connections are retried
            status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
            dropped = status is None and type(exc).__name__ in ("APIConnectionError", "APITimeoutError")
            if attempt == RATE_TRIES - 1 or not (status == 429 or dropped or (status or 0) >= 500):
                raise
            if status != 429:
                wait = 10.0 * (attempt + 1)
                print(f"transcription {'connection dropped' if dropped else f'server error {status}'}: "
                      f"retrying in {wait:.0f}s (try {attempt + 2}/{RATE_TRIES})", flush=True)
                sleep(wait)
                continue
            wait = _wait_seconds(exc) + 2
            if wait > RATE_WAIT_MAX:
                raise RuntimeError(
                    f"The transcription service's limit is used up for about {wait / 60:.0f} minutes. "
                    "Try this video again later — nothing is lost.") from exc
            print(f"transcription rate limit: waiting {wait:.0f}s (try {attempt + 2}/{RATE_TRIES})", flush=True)
            if on_wait and wait > 60:
                try:
                    on_wait(wait)
                except Exception:   # a heads-up must never stop the transcription
                    pass
            sleep(wait)


def transcribe(wav: Path, progress=None, on_wait=None) -> Dict[str, Any]:
    """Returns {"words": [{w,start,end}], "segments": [{text,start,end}], "text": str}."""
    client = _client()
    chunks = media.split_audio(wav, AUDIO_CHUNK_SECONDS)

    words: List[Dict[str, Any]] = []
    segments: List[Dict[str, Any]] = []

    for i, (part, offset) in enumerate(chunks):
        result = _create(client, part, on_wait=on_wait)
        data = result.model_dump() if hasattr(result, "model_dump") else dict(result)

        for w in data.get("words") or []:
            words.append({
                "w": (w.get("word") or "").strip(),
                "start": round(float(w.get("start", 0)) + offset, 3),
                "end": round(float(w.get("end", 0)) + offset, 3),
            })
        for s in data.get("segments") or []:
            segments.append({
                "text": (s.get("text") or "").strip(),
                "start": round(float(s.get("start", 0)) + offset, 2),
                "end": round(float(s.get("end", 0)) + offset, 2),
            })
        if progress:
            progress(int((i + 1) / len(chunks) * 100))
        if part != wav:
            part.unlink(missing_ok=True)

    # Some endpoints skip word timestamps; fall back to spreading words over
    # their segment so captions still animate.
    if not words and segments:
        for seg in segments:
            tokens = seg["text"].split()
            if not tokens:
                continue
            step = (seg["end"] - seg["start"]) / len(tokens)
            for n, token in enumerate(tokens):
                words.append({
                    "w": token,
                    "start": round(seg["start"] + n * step, 3),
                    "end": round(seg["start"] + (n + 1) * step, 3),
                })

    return {
        "words": in_order(words),
        "segments": segments,
        "text": " ".join(s["text"] for s in segments).strip(),
    }


def in_order(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The words as spoken, with times that never run backwards.

    Whisper (Groq's especially) sometimes starts a word too early — "did"
    1586.30, then "a" 1585.96 — while keeping the words themselves in the
    right order. Anything that sorts by start time then shuffles them, and the
    captions read "that a we did lot of improvements". Trust the order, fix
    the time: a word can't start before the one before it has finished.
    """
    out: List[Dict[str, Any]] = []
    for w in words:
        start, end = float(w["start"]), float(w["end"])
        if out:
            prev = out[-1]
            if start < prev["start"] + 0.01:          # runs backwards: it starts when that one ends
                start = max(prev["end"], prev["start"] + 0.01)
            elif start < prev["end"]:                 # overlaps: that one gives up its tail
                prev["end"] = start
        end = max(end, start + 0.04)
        out.append({**w, "start": round(start, 3), "end": round(end, 3)})
    # A pushed word can only push the next one along so far: settle the ends.
    for a, b in zip(out, out[1:]):
        if a["end"] > b["start"]:
            a["end"] = b["start"]
    return out


def respell(words: List[Dict[str, Any]], fixes: Dict[str, str] | None) -> List[Dict[str, Any]]:
    """Fix what the transcriber mishears ("rapper" for wrapper, "Cloud" for
    Claude) on the captions, keeping each word's timing and punctuation.
    A key may span words ("Anthropics Cloud"), matched case-insensitively."""
    if not fixes:
        return words
    out = [dict(w) for w in words]
    norm = lambda s: re.sub(r"[^\w']", "", s).lower()   # noqa: E731
    for wrong, right in fixes.items():
        keys = [norm(k) for k in str(wrong).split() if norm(k)]
        repl = str(right).split()
        if not keys or not repl:
            continue
        i = 0
        while i + len(keys) <= len(out):
            if [norm(w["w"]) for w in out[i:i + len(keys)]] != keys:
                i += 1
                continue
            first, last = out[i]["w"], out[i + len(keys) - 1]["w"]
            lead = re.match(r"^\W*", first).group(0)
            trail = re.search(r"[^\w']*$", last).group(0)
            span = out[i:i + len(keys)]
            if len(repl) == len(span):
                new = [{**w, "w": r} for w, r in zip(span, repl)]
            else:                                  # different word count: one word over the span
                new = [{**span[0], "w": " ".join(repl), "end": span[-1]["end"]}]
            new[0]["w"] = lead + new[0]["w"]
            new[-1]["w"] = new[-1]["w"] + trail
            out[i:i + len(keys)] = new
            i += len(new)
    return out


def words_between(words: List[Dict[str, Any]], start: float, end: float) -> List[Dict[str, Any]]:
    """Words inside a clip, rebased so the clip starts at 0.

    A word that began before the clip is left out: its sound is mostly cut
    off, and showing it would open the captions on a fragment.
    """
    out = []
    for w in words:
        if w["start"] < start - 0.05 or w["start"] >= end - 0.05:
            continue
        out.append({
            "w": w["w"],
            "start": round(max(0.0, w["start"] - start), 3),
            "end": round(min(end - start, w["end"] - start), 3),
        })
    return out
