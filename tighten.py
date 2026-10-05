"""Cut the dead air out of a clip, and move the captions with it.

A 30-second clip usually carries two or three seconds of nothing — the pause
while someone thinks, the breath before the punchline, the trailing "uhh".
Removing those keeps the clip the same in content and shorter in time, which
is the whole game in short form.

Two decisions worth knowing about:

- Long pauses are SHORTENED, not deleted. Something is often happening on
  screen during a pause — a reaction, a replay, a save going in — so each gap
  keeps a beat of itself and loses the rest.
- The kept segments and the re-timed captions come out of the same pass,
  because cutting time out of the middle shifts every word after the cut.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# Only true disfluencies. "like", "so" and "you know" carry meaning often
# enough that dropping them mangles real sentences.
FILLERS = {"um", "umm", "ummm", "uh", "uhh", "uhhh", "erm", "er", "mmm", "hmm", "mhm"}

MIN_SEGMENT = 0.25          # never leave a sliver this short
MAX_INTERIOR_SHARE = 0.5    # how much of the middle may go
MIN_WORDS = 3
MIN_SAVED = 0.35            # below this, a second encode is not worth it


def _clean(word: str) -> str:
    return re.sub(r"[^\w']", "", word or "").lower()


def plan_cuts(
    words: List[Dict[str, Any]],
    start: float,
    end: float,
    max_gap: float = 0.6,
    keep_gap: float = 0.35,
    drop_fillers: bool = True,
    lead_in: float = 0.12,
    tail: float = 0.22,
    fps: Any = None,
) -> Tuple[List[Tuple[float, float]], List[Dict[str, Any]], float]:
    """Returns (keep segments relative to `start`, re-timed words, seconds saved).

    An empty segment list means "nothing worth cutting" — render normally.
    With `fps`, every cut lands exactly on a frame boundary, so the renderer
    can cut picture and sound at the same instant and the captions re-timed
    here match the finished clip to the frame.
    """
    duration = end - start
    inside = sorted([w for w in words if w["start"] >= start - 0.05 and w["start"] < end - 0.05],
                    key=lambda w: w["start"])
    if len(inside) < MIN_WORDS or duration <= 2:
        return [], [], 0.0

    cuts: List[Tuple[float, float]] = []          # absolute, in source time
    dropped_words: set = set()

    # 1. Silence before the first word and after the last.
    head = inside[0]["start"] - lead_in
    if head - start > 0.3:
        cuts.append((start, head))
    foot = inside[-1]["end"] + tail
    if end - foot > 0.3:
        cuts.append((foot, end))

    # 2. Fillers, but only where there is a breath around them — cutting one
    #    out of the middle of a fluent sentence sounds like a glitch.
    if drop_fillers:
        for i, w in enumerate(inside):
            if _clean(w["w"]) not in FILLERS:
                continue
            before = w["start"] - inside[i - 1]["end"] if i else 1.0
            after = inside[i + 1]["start"] - w["end"] if i + 1 < len(inside) else 1.0
            if max(before, after) >= 0.1:
                cuts.append((w["start"] - 0.04, w["end"] + 0.04))
                dropped_words.add(id(w))

    # 3. Long pauses, shortened to a beat.
    for prev, nxt in zip(inside, inside[1:]):
        gap = nxt["start"] - prev["end"]
        if gap > max_gap:
            margin = keep_gap / 2
            a, b = prev["end"] + margin, nxt["start"] - margin
            if b - a > 0.1:
                cuts.append((a, b))

    if not cuts:
        return [], [], 0.0

    cuts = _merge(sorted(cuts))
    keep_abs = _complement(cuts, start, end)
    if fps:
        keep_abs = _snap(keep_abs, fps)
    keep_abs = [(a, b) for a, b in keep_abs if b - a >= MIN_SEGMENT]
    if not keep_abs:
        return [], [], 0.0

    kept_total = sum(b - a for a, b in keep_abs)
    saved = duration - kept_total
    if saved < MIN_SAVED:
        return [], [], 0.0

    # Guard against gutting a clip: judge only the middle, since trimming
    # silence off either end is always safe.
    interior_span = max(0.1, foot - head)
    interior_removed = sum(max(0.0, min(b, foot) - max(a, head)) for a, b in cuts)
    if interior_removed > interior_span * MAX_INTERIOR_SHARE:
        return [], [], 0.0

    # Map source time onto the new, shorter timeline.
    offsets: List[Tuple[float, float, float]] = []
    cursor = 0.0
    for a, b in keep_abs:
        offsets.append((a, b, cursor))
        cursor += b - a

    def remap(t: float) -> float:
        for a, b, new_start in offsets:
            if t < a:
                return round(new_start, 3)
            if t <= b:
                return round(new_start + (t - a), 3)
        return round(cursor, 3)

    retimed = []
    for w in inside:
        if id(w) in dropped_words:
            continue
        ws, we = remap(w["start"]), remap(w["end"])
        retimed.append({"w": w["w"], "start": ws, "end": max(we, ws + 0.08)})

    keep = [(round(a - start, 3), round(b - start, 3)) for a, b in keep_abs]
    return keep, retimed, round(saved, 2)


def _snap(spans: List[Tuple[float, float]], fps: Any) -> List[Tuple[float, float]]:
    """Move every boundary onto the source's frame grid (absolute time)."""
    from fractions import Fraction

    rate = Fraction(fps).limit_denominator(1001)
    out: List[Tuple[float, float]] = []
    for a, b in spans:
        fa = float(round(Fraction(a) * rate) / rate)
        fb = float(round(Fraction(b) * rate) / rate)
        if fb <= fa:
            continue
        if out and fa <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], fb))
        else:
            out.append((fa, fb))
    return out


def _merge(spans: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    merged: List[List[float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _complement(cuts: List[Tuple[float, float]], start: float, end: float) -> List[Tuple[float, float]]:
    keep: List[Tuple[float, float]] = []
    cursor = start
    for a, b in cuts:
        if a > cursor:
            keep.append((cursor, min(a, end)))
        cursor = max(cursor, b)
    if cursor < end:
        keep.append((cursor, end))
    return [(a, b) for a, b in keep if b > a]


def select_expression(keep: List[Tuple[float, float]]) -> str:
    """The ffmpeg select/aselect expression for the segments we keep."""
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep)


def describe(keep: List[Tuple[float, float]], saved: float, duration: float) -> str:
    if not keep or saved <= 0:
        return ""
    cuts = max(0, len(keep) - 1)
    return (f"Tightened by {saved:.1f}s over {cuts} cut{'s' if cuts != 1 else ''} "
            f"— {duration - saved:.1f}s final")


def make_remap(keep: List[Tuple[float, float]]):
    """A function from clip-relative source time to output time after the cuts.

    Anything else timed against the source — the crop track, for one — has to
    go through this too, or it drifts out of step with the picture.
    """
    offsets: List[Tuple[float, float, float]] = []
    cursor = 0.0
    for a, b in keep:
        offsets.append((a, b, cursor))
        cursor += b - a

    def remap(t: float) -> float:
        for a, b, new_start in offsets:
            if t < a:
                return new_start
            if t <= b:
                return new_start + (t - a)
        return cursor

    return remap
