"""The style brain: how each clip should be made.

Every clip gets an edit chosen for it, not one look for the whole video. The
choice is grounded in the Clip Style Database (data/research/
clip_style_db.json): short-form clips that blew up recently, each tagged with
the style family it uses. Claude reads each clip, the families, and real
winning hooks from the database, then picks a recipe and writes the clip's
on-screen text. If Claude isn't reachable, plain rules pick instead.

A recipe is something ClipAgent can actually render. Families the renderer
can't draw yet are never offered.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache
from typing import Any, Dict, List, Optional

from . import cards, highlights, toolio
from .config import CLAUDE_MODEL, DATA_DIR

DB_PATH = DATA_DIR / "research" / "clip_style_db.json"

# Word-pop captions as this season's winners do them: 1-3 words, white caps,
# money and numbers in green, key names and punchlines in yellow, swearing
# starred out, sitting just below the middle of the frame.
WORDPOP_LOOK = {"max_words": 3, "karaoke": False, "auto_colors": True, "censor": True}

RECIPES: Dict[str, Dict[str, Any]] = {
    "wordpop": {
        "family": "F2", "name": "Word-pop captions",
        "what": "Tight crop on whoever is talking, 1-3 big caps words at a time with key words coloured, "
                "hook text at the top for the first seconds.",
        "when": "The default. Anything where the words carry it: hot takes, stories, advice, numbers, debates.",
        "needs": set(),
        "edits": {"caption_style": "impact", "caption_position": "pop", "caption_look": WORDPOP_LOOK,
                  "headline_on": False},
    },
    "label": {
        "family": "F1", "name": "Headline label",
        "what": "A white rounded box with black bold text over the whole clip, written like a story headline: "
                "who + an emotion in CAPS + the specific thing + one emoji. No word captions.",
        "when": "Reactions, shocking or funny moments, stream moments, short clips (10-35 s) where one sentence "
                "explains it all. The clip-page signature look.",
        "needs": {"cards"},
        "edits": {"captions_on": False, "hook_on": False, "headline_on": False},
    },
    "titlebar": {
        "family": "F4", "name": "Title bar",
        "what": "One headline on a dark band at the top for the whole clip (one word in yellow), plus word-pop "
                "captions — so people who join halfway still know what it is about.",
        "when": "Longer clips (45 s+), interviews, explanations, business and money talk, news-style moments.",
        "needs": {"cards"},
        "edits": {"caption_style": "impact", "caption_position": "pop", "caption_look": WORDPOP_LOOK,
                  "hook_on": False, "headline_on": False},
    },
    "bubble": {
        "family": "F6", "name": "Viewer-voice bubble",
        "what": "A comment bubble in a viewer's voice at the top for the whole clip "
                "(\"there ain't no way he actually said that 😭\"), plus word-pop captions. Invites comments.",
        "when": "Wild statements, outrageous takes, cringe or unbelievable moments people will argue about.",
        "needs": {"cards"},
        "edits": {"caption_style": "impact", "caption_position": "pop", "caption_look": WORDPOP_LOOK,
                  "hook_on": False, "headline_on": False},
    },
    "stack": {
        "family": "F3", "name": "Stacked split",
        "what": "The frame split in two, one above the other: one person per panel (a podcast's two seats), "
                "or the speaker on top and the whole scene below. Word-pop captions on the seam.",
        "when": "Back-and-forth between two people on camera: debates, interviews, reactions to each other.",
        "needs": {"two_shot"},
        "edits": {"layout": "stack", "caption_style": "impact", "caption_position": "middle",
                  "caption_look": WORDPOP_LOOK, "headline_on": False},
    },
}

TWO_SHOT_MIN = 0.3      # share of the video with two people side by side before stacking is offered


def capabilities(two_shot: float = 0.0) -> set:
    caps = set()
    if cards.available():
        caps.add("cards")
    if two_shot >= TWO_SHOT_MIN:
        caps.add("two_shot")
    return caps


def available_recipes(two_shot: float = 0.0) -> List[str]:
    caps = capabilities(two_shot)
    return [k for k, r in RECIPES.items() if r["needs"] <= caps]


# --- the database -----------------------------------------------------------------

@lru_cache(maxsize=1)
def database() -> Dict[str, Any]:
    try:
        return json.loads(DB_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"families": {}, "records": []}


def winning_hooks(family: Optional[str] = None, n: int = 8, clip_pages_first: bool = True) -> List[str]:
    """Real hooks from the database, most-viewed first: '"text" (1.2M views)'."""
    recs = [r for r in database().get("records", [])
            if (r.get("hook_text") or "").strip() and (family is None or family in (r.get("families") or []))]
    recs.sort(key=lambda r: ((r.get("account_type") == "clip page") if clip_pages_first else 0,
                             r.get("views") or 0), reverse=True)
    out, seen = [], set()
    for r in recs:
        text = re.sub(r"\s+", " ", r["hook_text"]).strip()[:140]
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        views = r.get("views") or 0
        out.append(f"\"{text}\" ({views / 1e6:.1f}M views)" if views >= 1e6 else f"\"{text}\"")
        if len(out) >= n:
            break
    return out


def family_counts() -> Dict[str, int]:
    return dict(Counter(f for r in database().get("records", []) for f in (r.get("families") or [])))


def research_brief(allowed: Optional[List[str]] = None) -> str:
    """The part of the database Claude needs to choose: how common each
    renderable style is among winners, and real hooks for each."""
    counts = family_counts()
    total = len(database().get("records", [])) or 1
    lines = [f"Reference: {total} short-form clips that blew up in the last two months "
             "(TikTok, Reels, Shorts), tagged by style. Counts show how common a style is among winners."]
    for key in (allowed or available_recipes()):
        r = RECIPES[key]
        fam = r["family"]
        lines.append(f"\n## {key} — {r['name']} ({counts.get(fam, 0)} of {total} winners)\n"
                     f"Looks like: {r['what']}\nUse for: {r['when']}")
        hooks = winning_hooks(fam, 6)
        if hooks:
            lines.append("Winning on-screen text in this style:\n" + "\n".join(f"- {h}" for h in hooks))
    lines.append("\nHook patterns that keep winning on clip pages:\n"
                 "- Name + an emotion in CAPS + the specific detail + one emoji "
                 "(\"Fousey was SHOCKED after he found out N3on got PAID $3,500,000 😳\").\n"
                 "- A big specific number (\"$3,500,000\", \"$20M\", \"$800,000 per year\").\n"
                 "- A question the clip answers (\"MJ or Bron? 🤨\").\n"
                 "- The strongest spoken line itself, no setup.")
    return "\n".join(lines)


def hook_examples(n: int = 10) -> str:
    """For the moment finder: what winning hooks read like right now."""
    hooks = winning_hooks(None, n)
    if not hooks:
        return ""
    return ("\n\nHooks on clips that are winning right now (real, with views) — match their directness "
            "and specificity, never their exact words:\n" + "\n".join(f"- {h}" for h in hooks))


# --- choosing -----------------------------------------------------------------------

def _seconds(clip: Dict[str, Any]) -> float:
    parts = clip.get("parts") or []
    if len(parts) > 1:
        return sum(p["end"] - p["start"] for p in parts)
    return float(clip.get("end", 0)) - float(clip.get("start", 0))


def rule_pick(clip: Dict[str, Any], allowed: List[str]) -> str:
    """The fallback when Claude can't choose."""
    secs = _seconds(clip)
    kind = clip.get("type") or ""
    if "titlebar" in allowed and secs >= 50:
        return "titlebar"
    if "label" in allowed and kind in ("reaction", "funny", "reveal", "hype") and secs <= 35:
        return "label"
    if "bubble" in allowed and kind == "hot_take" and secs <= 45:
        return "bubble"
    if "stack" in allowed and kind in ("reaction", "hot_take", "funny"):
        return "stack"
    return "wordpop"


DIRECT_TOOL = {
    "name": "style_clips",
    "description": "Choose how each clip is made and write its on-screen text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "recipe": {"type": "string", "description": "One of the recipes offered."},
                        "why": {"type": "string", "description": "One line: why this style fits this clip."},
                        "hook": {"type": "string", "description": "wordpop only: on-screen hook for the first 2.4 s, "
                                 "max 8 words, no emoji, no hashtags. Use a newline to break it into 2-3 short lines."},
                        "label": {"type": "string", "description": "label only: the headline label, 8-18 words, "
                                  "who + EMOTION in caps + the specific thing, ending in exactly one emoji."},
                        "title": {"type": "string", "description": "titlebar only: max 8 words; wrap the one word "
                                  "or number to show in yellow in [brackets]."},
                        "bubble": {"type": "string", "description": "bubble only: a viewer's comment, max 14 words, "
                                   "casual, lower case is fine, may end in one emoji."},
                        "colors": {"type": "array", "description": "Words or short phrases actually spoken in the clip "
                                   "to colour in the captions (2-6): names, punchlines, money.",
                                   "items": {"type": "object", "properties": {
                                       "text": {"type": "string"},
                                       "meaning": {"type": "string", "enum": ["money", "name", "emphasis", "danger"]}},
                                       "required": ["text", "meaning"]}},
                    },
                    "required": ["id", "recipe", "why"],
                },
            }
        },
        "required": ["clips"],
    },
}

SYSTEM = """You are the editor at a clip page that makes money from views. For each clip you choose \
how it is styled and write the text that goes on screen. You know what is winning right now from the \
reference below — use it, but fit each clip on its own merits; a run of clips should not all look the same \
unless they truly are the same kind of moment.

Rules for the words:
- Only facts that are in the clip's own words or the video title. Never invent names, numbers or events.
- Name who it is when the transcript or title says so ("Anton Osika", "MrBeast"); otherwise describe them \
("This founder", "He").
- Make a stranger scrolling with the sound off want to stop: specific beats vague. Never a teaser that \
hides the thing ("wait till you see what's next", "they go on…", "you won't believe") — say who and what.
- Campaign rules, when given, override everything else here.

"""


def _clip_block(i: int, clip: Dict[str, Any], words: List[Dict[str, Any]]) -> str:
    from . import transcribe
    parts = clip.get("parts") or [{"start": clip["start"], "end": clip["end"]}]
    said = " ".join(w["w"] for p in parts for w in transcribe.words_between(words, p["start"], p["end"]))
    said = re.sub(r"\s+", " ", said).strip()
    if len(said) > 1400:
        said = said[:1000] + " … " + said[-380:]
    return (f"=== CLIP {i} — {clip.get('type') or 'moment'}, {_seconds(clip):.0f} s\n"
            f"Picker's hook: {clip.get('hook', '')}\nWhy it was picked: {clip.get('reason', '')}\n"
            f"What is said: {said}")


def direct(title: str, clips: List[Dict[str, Any]], words: List[Dict[str, Any]],
           allowed: Optional[List[str]] = None, guidance: str = "") -> List[Dict[str, Any]]:
    """A style plan for every clip: [{"recipe", "why", "hook"?, "label"?, ...}],
    in the same order as `clips`. Never raises."""
    allowed = [r for r in (allowed or available_recipes()) if r in RECIPES] or ["wordpop"]
    plans: List[Dict[str, Any]] = [{"recipe": rule_pick(c, allowed), "why": "rules (Claude unavailable)"}
                                   for c in clips]
    if not clips:
        return plans
    try:
        client = highlights._client()
        offered = "Recipes you may use: " + ", ".join(allowed)
        prompt = (f"Video: {title}\n{offered}\n\n" + "\n\n".join(_clip_block(i, c, words) for i, c in enumerate(clips)))
        try:
            from . import money
            mine = money.style_insights()
        except Exception:
            mine = ""
        system = SYSTEM + research_brief(allowed) + mine + (f"\n\nCAMPAIGN RULES:\n{guidance}" if guidance else "")
        replies = toolio.ask(client, "clips", model=CLAUDE_MODEL, max_tokens=6000, system=system,
                             tools=[DIRECT_TOOL], tool_choice={"type": "tool", "name": "style_clips"},
                             messages=[{"role": "user", "content": prompt}])
    except Exception as exc:                                  # the rules plan stands
        highlights.LAST_ERROR = highlights.LAST_ERROR or f"style step: {exc}"[:300]
        return plans
    for item in replies:
        i = highlights._int(item.get("id"), -1)
        if not 0 <= i < len(clips):
            continue
        recipe = item.get("recipe") if item.get("recipe") in allowed else plans[i]["recipe"]
        plan = {"recipe": recipe, "why": highlights._text(item.get("why"))[:200]}
        for key in ("hook", "label", "title", "bubble"):
            text = highlights._text(item.get(key)).strip()
            if text:
                plan[key] = text[:200]
        colors = []
        for c in toolio.as_list(item.get("colors")):
            c = toolio.as_dict(c)
            if c.get("text") and c.get("meaning") in ("money", "name", "emphasis", "danger"):
                colors.append({"text": str(c["text"])[:40], "meaning": c["meaning"]})
        plan["colors"] = colors[:8]
        # A recipe whose text didn't come back can't be drawn: word-pop instead.
        need = {"label": "label", "titlebar": "title", "bubble": "bubble"}.get(recipe)
        if need and not plan.get(need):
            plan["recipe"] = "wordpop"
        plans[i] = plan
    return plans


def apply(plan: Dict[str, Any], clip: Dict[str, Any]) -> Dict[str, Any]:
    """The edits a style plan turns into (merged over the job's base edits)."""
    recipe = plan.get("recipe") if plan.get("recipe") in RECIPES else "wordpop"
    edits: Dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v)
                             for k, v in RECIPES[recipe]["edits"].items()}
    edits["style"] = recipe
    if "caption_look" in edits:
        edits["caption_look"]["colors"] = {c["text"]: c["meaning"] for c in plan.get("colors") or []}
    if recipe == "wordpop" and plan.get("hook"):
        edits["hook"] = plan["hook"]
    card = {"label": ("label", plan.get("label")), "titlebar": ("title", plan.get("title")),
            "bubble": ("bubble", plan.get("bubble"))}.get(recipe)
    edits["cards"] = [{"kind": card[0], "text": card[1]}] if card and card[1] else []
    edits["style_why"] = plan.get("why", "")
    return edits


def on_screen_text(edits: Dict[str, Any]) -> str:
    """What a viewer reads first: the card's text, or the hook."""
    for card in edits.get("cards") or []:
        if (card.get("text") or "").strip():
            return re.sub(r"[\[\]]", "", card["text"]).strip()
    return (edits.get("hook") or "").strip()


def campaign_recipes(rb: Dict[str, Any], two_shot: float = 0.0) -> List[str]:
    """The recipes a campaign's brief leaves open. Every card is on-screen text,
    so a brief that forbids hook text leaves only plain word-pop captions; one
    that requires captions rules out the label, which has none."""
    from . import campaign
    allowed = available_recipes(two_shot)
    if not campaign.allowed(rb, "crop"):
        allowed = [r for r in allowed if r != "stack"]
    if not campaign.allowed(rb, "hook"):
        return ["wordpop"]
    if campaign.resolve(rb).get("captions_required"):
        allowed = [r for r in allowed if r != "label"]
    return allowed or ["wordpop"]
