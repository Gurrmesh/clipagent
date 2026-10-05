"""Score the two versions of each clip — continuous vs stitched — on one scale.

The judge plays a cold viewer: someone scrolling a feed who has never seen the
source video. It only gets what that viewer gets — the on-screen text and what
is said, in order — and scores each version 1-10 on six things. The versions
are shown blind as A and B, and the whole thing runs twice with A and B
swapped, so a preference for "whichever came first" cancels out. Clips that
only have one version (most funny moments) are scored too, so every clip ends
up on the same scale.

This is a proxy. The real judge is the platform: post both versions and
compare how many viewers stay past the first three seconds and how long they
watch. The scores are for deciding which one to post first.
"""
from __future__ import annotations

import random
from typing import Any, Dict, List

from . import highlights, structure, toolio
from .config import CLAUDE_MODEL

CRITERIA = {
    "clarity": ("In the first 3 seconds, do you know who or what this is and why to care?", 0.25),
    "hook": ("Does the opening make you want to keep watching?", 0.20),
    "context": ("By the payoff, do you have what you need to feel it?", 0.15),
    "payoff": ("Does it land — the laugh, the emotion, the reveal, the point?", 0.20),
    "flow": ("Does it feel like one piece? Marked, motivated jumps are fine; confusing ones are not.", 0.10),
    "pace": ("No dead weight; the right length for what it is.", 0.10),
}
TIE = 0.25          # below this margin the simpler continuous version wins

_SCORES = {"type": "object",
           "properties": {k: {"type": "integer", "minimum": 1, "maximum": 10} for k in CRITERIA},
           "required": list(CRITERIA)}

JUDGE_TOOL = {
    "name": "score_versions",
    "description": "Score both versions of every clip.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "A": _SCORES,
                        "B": {**_SCORES, "description": "Only when the clip has a version B."},
                        "prefer": {"type": "string", "enum": ["A", "B", "tie"]},
                        "why": {"type": "string", "description": "One line: the deciding difference, or for a single version its biggest weakness."},
                    },
                    "required": ["id", "A", "prefer", "why"],
                },
            }
        },
        "required": ["clips"],
    },
}

SYSTEM = ("You are a viewer scrolling TikTok, Reels and YouTube Shorts. You have never seen the "
          "long video these clips come from. Each clip below comes in one or two versions (A, and "
          "sometimes B), described exactly as you would experience them: the text on screen and "
          "what is said, in order. Score each version from 1 to 10 on:\n"
          + "\n".join(f"- {k}: {q}" for k, (q, _) in CRITERIA.items())
          + "\n\nBe critical and use the whole scale — most clips are not great. The letters are "
            "assigned at random and mean nothing. Judge only what a viewer gets.")


def overall(scores: Dict[str, float]) -> float:
    return round(sum(scores[k] * w for k, (_, w) in CRITERIA.items()), 2)


def _run(client, clips: List[Dict[str, Any]], words, headline: str, flip: bool) -> Dict[int, Dict[str, Any]]:
    """One judging pass. Returns {clip_index: {"continuous": scores, "stitched": scores?, "prefer": v, "why": s}}."""
    rng = random.Random(7 if not flip else 11)
    blocks, mapping = [], {}
    for i, clip in enumerate(clips):
        variants = clip["variants"]
        shown = clip.get("headline") or headline     # the headline this clip will actually carry
        if variants.get("stitched"):
            a_is_cont = rng.random() < 0.5
            if flip:
                a_is_cont = not a_is_cont
            mapping[i] = ("continuous", "stitched") if a_is_cont else ("stitched", "continuous")
            va, vb = variants[mapping[i][0]], variants[mapping[i][1]]
            blocks.append(f"=== CLIP {i}\n--- VERSION A\n{structure.viewer_view(va, words, shown)}\n"
                          f"--- VERSION B\n{structure.viewer_view(vb, words, shown)}")
        else:
            mapping[i] = ("continuous",)
            blocks.append(f"=== CLIP {i} (one version)\n--- VERSION A\n"
                          f"{structure.viewer_view(variants['continuous'], words, shown)}")
    replies = toolio.ask(
        client, "clips",
        model=CLAUDE_MODEL, max_tokens=8000, system=SYSTEM,
        tools=[JUDGE_TOOL], tool_choice={"type": "tool", "name": "score_versions"},
        messages=[{"role": "user", "content": "\n\n".join(blocks)}],
    )
    out: Dict[int, Dict[str, Any]] = {}
    for item in replies:
        i = highlights._int(item.get("id"), -1)
        if i not in mapping:
            continue
        names = mapping[i]
        try:
            got = {names[0]: {k: float(toolio.as_dict(item.get("A"))[k]) for k in CRITERIA}}
            if len(names) == 2:
                got[names[1]] = {k: float(toolio.as_dict(item.get("B"))[k]) for k in CRITERIA}
        except (KeyError, TypeError, ValueError):
            continue
        if len(names) == 2:
            pref = {"A": names[0], "B": names[1]}.get(item.get("prefer"), "tie")
        else:
            pref = "continuous"
        out[i] = {**got, "prefer": pref, "why": highlights._text(item.get("why"))[:200]}
    return out


def compare(clips: List[Dict[str, Any]], words: List[Dict[str, Any]], headline: str = "") -> List[Dict[str, Any]]:
    """Judge every clip and pick the version to make.

    Sets clip["judge"] = {"continuous": {..., "overall"}, "stitched": {...} (when
    there is one), "winner", "margin", "votes", "why", "passes"} and chooses the
    winning variant. The stitched version has to win clearly (by more than TIE):
    when it is close, the simpler continuous cut is the better bet.
    """
    for c in clips:
        structure.choose(c, "continuous")            # the default, whatever happens below
    if not clips:
        return clips
    try:
        client = highlights._client()
        runs = [_run(client, clips, words, headline, flip=False),
                _run(client, clips, words, headline, flip=True)]
    except Exception as exc:
        highlights.LAST_ERROR = str(exc)[:300]
        return clips

    for i, clip in enumerate(clips):
        got = [r[i] for r in runs if i in r]
        if not got:
            continue
        names = [v for v in ("continuous", "stitched") if all(v in g for g in got)]
        if "continuous" not in names:
            continue
        result: Dict[str, Any] = {"votes": {"continuous": 0, "stitched": 0, "tie": 0}, "why": []}
        for v in names:
            avg = {k: round(sum(g[v][k] for g in got) / len(got), 2) for k in CRITERIA}
            result[v] = {**avg, "overall": overall(avg)}
        for g in got:
            result["votes"][g["prefer"]] = result["votes"].get(g["prefer"], 0) + 1
            if g["why"]:
                result["why"].append(g["why"])
        result["passes"] = len(got)
        if "stitched" in names:
            margin = round(result["stitched"]["overall"] - result["continuous"]["overall"], 2)
            result["margin"] = margin
            result["winner"] = "stitched" if margin > TIE else "continuous"
        else:
            result["margin"] = None
            result["winner"] = "continuous"
        clip["judge"] = result
        structure.choose(clip, result["winner"])
    return clips
