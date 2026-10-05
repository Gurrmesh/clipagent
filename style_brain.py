"""The style brain, word-pop captions and cards — without calling Claude.

Run: python tests/style_brain.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="style_"))
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import shutil  # noqa: E402

research = Path(os.environ["DATA_DIR"]) / "research"
research.mkdir(parents=True, exist_ok=True)
for name in ("clip_style_db.json",):
    src = ROOT / "data" / "research" / name
    if src.exists():
        shutil.copy(src, research / name)

from app import captions, cards, styles, toolio  # noqa: E402

FAILS = 0


def expect(ok, label):
    global FAILS
    print(("  ok   " if ok else "  FAIL ") + label)
    FAILS += 0 if ok else 1


print("== the database is loaded and summarised")
db = styles.database()
expect(len(db.get("records", [])) > 100, f"{len(db.get('records', []))} winning clips loaded")
brief = styles.research_brief()
expect("wordpop" in brief and "of 141 winners" in brief, "the brief says how common each style is")
expect("views)" in styles.hook_examples(5), "real hooks with their views go to the moment finder")

print("\n== recipes on offer follow what can be drawn")
avail = styles.available_recipes()
expect("wordpop" in avail, "word-pop is always available")
expect(("label" in avail) == cards.available(), "card recipes only when cards can be drawn")

print("\n== rules pick when Claude can't")
allc = ["wordpop", "label", "titlebar", "bubble"]
expect(styles.rule_pick({"start": 0, "end": 70, "type": "info"}, allc) == "titlebar", "a long clip gets the title bar")
expect(styles.rule_pick({"start": 0, "end": 20, "type": "reaction"}, allc) == "label", "a short reaction gets the label")
expect(styles.rule_pick({"start": 0, "end": 30, "type": "story"}, allc) == "wordpop", "a story gets word-pop")
expect(styles.rule_pick({"start": 0, "end": 20, "type": "reaction"}, ["wordpop"]) == "wordpop",
       "never a recipe that isn't allowed")

print("\n== Claude's choices become edits")
clips = [{"start": 0, "end": 20, "type": "reaction", "hook": "old hook", "reason": "r"},
         {"start": 30, "end": 60, "type": "story", "hook": "old", "reason": "r"},
         {"start": 70, "end": 140, "type": "info", "hook": "old", "reason": "r"}]
words = [{"w": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(300)]
fake = [{"id": 0, "recipe": "label", "why": "reaction", "label": "MrBeast was SHOCKED after he saw the $1,000,000 bill 😳",
         "colors": [{"text": "MrBeast", "meaning": "name"}]},
        {"id": 1, "recipe": "wordpop", "why": "story", "hook": "He lost\nEVERYTHING", "colors": "[{\"text\": \"$20M\", \"meaning\": \"money\"}]"},
        {"id": 2, "recipe": "titlebar", "why": "long", "title": ""}]
orig_ask, orig_client = toolio.ask, styles.highlights._client
toolio.ask = lambda *a, **k: fake
styles.highlights._client = lambda: object()
plans = styles.direct("Test video", clips, words, allc)
expect(plans[0]["recipe"] == "label" and "SHOCKED" in plans[0]["label"], "label plan kept")
expect(plans[1]["colors"] == [{"text": "$20M", "meaning": "money"}], "colours read even when sent as a string")
expect(plans[2]["recipe"] == "wordpop", "a title bar with no title falls back to word-pop")
e0 = styles.apply(plans[0], clips[0])
expect(e0["cards"][0]["kind"] == "label" and e0["captions_on"] is False and e0["hook_on"] is False,
       "label: the card, no captions, no separate hook")
expect(styles.on_screen_text(e0).startswith("MrBeast was SHOCKED"), "the label is what's on screen first")
e1 = styles.apply(plans[1], clips[1])
expect(e1["hook"] == "He lost\nEVERYTHING" and e1["caption_position"] == "pop" and e1["cards"] == [],
       "word-pop: new hook, captions at the pop spot, no card")
expect(e1["caption_look"]["colors"] == {"$20M": "money"} and styles.WORDPOP_LOOK.get("colors") is None,
       "colours set on this clip only (the shared recipe isn't changed)")
toolio.ask = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network"))
plans = styles.direct("Test video", clips, words, allc)
expect([p["recipe"] for p in plans] == ["label", "wordpop", "titlebar"], "Claude down: rules plan, no crash")
toolio.ask, styles.highlights._client = orig_ask, orig_client

print("\n== word-pop captions: 1-3 words, colours by meaning, swearing starred")
expect(captions.censor("shit,") == "sh*t," and captions.censor("FUCKING") == "F*CKING", "SH*T / F*CKING")
expect(captions.censor("Hello") == "Hello" and captions.censor("assess") == "assess", "clean words untouched")
out = Path(os.environ["DATA_DIR"]) / "t.ass"
ws = [{"w": w, "start": i * 0.5, "end": i * 0.5 + 0.45} for i, w in
      enumerate("MrBeast paid $20M for this shit and nobody knew".split())]
captions.build_ass(ws, 6.0, position="pop", out_path=out,
                   look={**styles.WORDPOP_LOOK, "colors": {"MrBeast": "name"}})
ass = out.read_text(encoding="utf-8")
events = [l for l in ass.splitlines() if l.startswith("Dialogue: 0")]
import re  # noqa: E402
longest = max(len(re.sub(r"\{[^}]*\}", "", l.split(",,")[-1]).split()) for l in events)
expect(longest <= 3, f"at most 3 words on screen (saw {longest})")
expect("SH*T" in ass and "SHIT" not in ass, "the swear is starred")
green, yellow = captions.hex_to_ass("#3DFF6E"), captions.hex_to_ass("#FFD400")
expect(f"\\c{green}" in ass.split("$20M")[0].splitlines()[-1] or f"{{\\c{green}}}$20M" in ass, "money in green")
expect(f"{{\\c{yellow}}}" in ass and "MRBEAST" in ass, "the name in yellow")
expect(",2,100,140,680," in ass.replace(" ", ""), "pop position, inside the right-hand safe zone")

print("\n== cards draw")
if cards.available():
    lab = cards.label("Fousey was SHOCKED after N3on got PAID $3,500,000 😳")
    expect(lab["path"].exists() and 300 < lab["w"] <= 900 and lab["h"] > 60, f"label {lab['w']}x{lab['h']}")
    bar = cards.title_bar("He made [$1.3M] buying a building")
    expect(bar["w"] == 1040, "title bar spans the frame")
    bub = cards.bubble("there ain't no way he said that 😭", "viewer")
    expect(bub["path"].exists(), "bubble")
else:
    print("  skip Pillow not installed")

print("\nall checks behaved" if not FAILS else f"\n{FAILS} check(s) failed")
sys.exit(1 if FAILS else 0)
