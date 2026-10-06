"""Smart Stitch part 1 — teasers, proof shots, reactions, callbacks (offline, Claude stubbed).

Run: python tests/smart_stitch.py            (~2-3 min: it renders real clips)

Covers docs/SMART_STITCH_PLAN.md 1E and the honesty rules of section 0:
  - the rule checks: quotes verified, whole sentences, reactions after what they
    react to, true time labels
  - a teaser sits inside the payoff, is 1.2-3.5 s, the payoff still plays in full
    after it, the "HOW IT STARTED" label lands when the story starts
  - the judge keeps the clip without its teaser on a tie
  - a funny moment is only stitched when a setup / callback part exists
  - inserts: never in the hook or over the punchline, at most 3, callbacks split
    the payoff, a campaign without stitching gets none, without added sound no
    rewind sound, without speed changes no rewind
  - typed changes: "remove the teaser", "add a teaser", "no inserts", "show the
    chart when he says 50k"
  - REAL renders: a video-only insert leaves the sound bit-for-bit the same while
    every frame of the picture is the expected source frame (frame-number source
    from tools/stitch_check.py), sync stays ~0 ms; the teaser's rewind is silent
    (or has its sound); a drawn talking-head video with a chart stretch made into
    a clip with a teaser and a proof shot by the real pipeline code, re-made from
    the editor (teaser removed, put back, undone). Frames are saved for a look.
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import json
import os
import re
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="smart_")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("WHISPER_API_KEY", "x")
os.environ["RENDER_PROCESSES"] = "0"                # render in this process: simpler to stub, same engine
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import numpy as np  # noqa: E402

from app import (compliance, doctor, highlights, instruct, judge, motion, pipeline, smartstitch,  # noqa: E402
                 stitchrules, store, structure)
from app.config import RENDER_H, RENDER_W  # noqa: E402

FAILS = []
DATA = Path(os.environ["DATA_DIR"])
LOOK = DATA / "frames_to_look_at"
LOOK.mkdir(parents=True, exist_ok=True)


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def sentence_words(sentences, gap=0.6, step=0.45, length=0.35):
    """[(start, "Sentence.")] → word timings; each sentence starts at its time."""
    out = []
    for t, sentence in sentences:
        for i, w in enumerate(sentence.split()):
            out.append({"w": w, "start": round(t + i * step, 3), "end": round(t + i * step + length, 3)})
    return out


def rb_with(**perms):
    """A clip-from-source rulebook with these permissions set by hand."""
    return {"mode": "source", "overrides": {k: ("yes" if v else "no") for k, v in perms.items()}}


# --- stand-in for Claude -----------------------------------------------------------------
ANSWERS = {}
CALLS = []


class FakeMessages:
    def create(self, **kw):
        CALLS.append(kw)
        name = kw["tools"][0]["name"]
        reply = ANSWERS.get(name)
        if callable(reply):
            reply = reply(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=reply or {})], stop_reason="tool_use")


highlights._client = lambda: SimpleNamespace(messages=FakeMessages())

# A story in sentences: 100 s on.
SENT = [(100.0, "So I opened the account with five hundred dollars."),
        (104.4, "Everyone told me it was a stupid idea."),
        (108.2, "I traded every single morning for a year."),
        (112.6, "Look at this chart right here."),
        (115.4, "Then one day it all came together."),
        (119.2, "I made fifty grand in one morning."),
        (122.8, "My wife thought I was joking."),
        (126.0, "Haha"),
        (130.0, "Remember I said I would never sell?"),
        (60.0, "I will never sell this account.")]
WORDS = sorted(sentence_words(SENT), key=lambda w: w["start"])
PAYOFF = [{"start": 99.9, "end": 125.6, "role": "payoff", "label": ""}]

print("== the rule checks (section 0)")
ok, _ = stitchrules.check_quote("Everyone told me it was a stupid idea.", WORDS, 104.3, 107.9)
expect(ok, "a quote that matches what's said there passes")
ok, why = stitchrules.check_quote("Everyone told me it was a brilliant plan", WORDS, 104.3, 107.9)
expect(not ok and "aren't what is said" in why, f"a quote that doesn't match is rejected ({why})")
ok, why = stitchrules.check_quote("So I opened the account … in one morning.", WORDS, 99.9, 122.0)
expect(ok, "a long part quoted by its first and last words passes")
ok, _ = stitchrules.check_sentences(104.3, 107.9, WORDS)
expect(ok, "a span of whole sentences passes")
ok, why = stitchrules.check_sentences(105.2, 107.9, WORDS)
expect(not ok and "middle of a sentence" in why, f"a cut that starts mid-sentence is rejected ({why})")
ok, why = stitchrules.check_sentences(104.3, 106.5, WORDS)
expect(not ok and "middle of a sentence" in why, f"a cut that stops mid-sentence is rejected ({why})")
expect(stitchrules.check_reaction_order(126.0, 125.6)[0], "a laugh right after the line is a real reaction")
expect(not stitchrules.check_reaction_order(110.0, 125.6)[0], "a 'reaction' from before the line is refused")
expect(not stitchrules.check_reaction_order(160.0, 125.6)[0], "a 'reaction' half a minute later is refused")
expect(stitchrules.true_label("5 MONTHS LATER", 40.0, "nothing about months") == "LATER",
       "a made-up number in a time label becomes a neutral word")
expect(stitchrules.true_label("20 MINUTES LATER", 1230.0) == "20 MINUTES LATER", "a true jump keeps its number")
expect(stitchrules.true_label("FIVE MONTHS LATER", None, "it was five months later") == "FIVE MONTHS LATER",
       "a number he says himself is kept")
expect(stitchrules.true_label("BEFORE") == "BEFORE", "a label with no number is left alone")

print("\n== the teaser: checked, then placed")
t, why = smartstitch.check_teaser({"start": 119.0, "end": 121.5, "quote": "I made fifty grand in one morning.",
                                   "why": "the number", "label": "HOW IT STARTED"}, PAYOFF, WORDS)
expect(t is not None and 119.0 <= t["start"] and t["end"] <= 122.6 and 1.2 <= t["end"] - t["start"] <= 3.5,
       f"a teaser inside the payoff, on whole sentences, 1.2-3.5 s ({t and (t['start'], t['end'])})")
bad, why = smartstitch.check_teaser({"start": 60.0, "end": 62.5, "quote": "I will never sell this account.",
                                     "why": "x"}, PAYOFF, WORDS)
expect(bad is None, f"a teaser outside the payoff is refused ({why})")
bad, why = smartstitch.check_teaser({"start": 119.2, "end": 122.0, "quote": "I lost everything that day",
                                     "why": "x"}, PAYOFF, WORDS)
expect(bad is None and "quote" in why, f"a teaser whose quote doesn't match is refused ({why})")
bad, why = smartstitch.check_teaser({"start": 119.2, "end": 122.0, "quote": "I made fifty grand in one morning.",
                                     "why": "the twist would be spoiled", "spoils_surprise": True}, PAYOFF, WORDS)
expect(bad is None and "surprise" in why, "a teaser that would spoil the surprise is skipped")

SETTINGS = {"tighten": False, "platforms": ["tiktok"]}
edits = {"teaser": t, "teaser_on": True, "inserts": [], "inserts_on": True}
plan = smartstitch.plan_render(PAYOFF, edits, WORDS, SETTINGS, Fraction(30), None)
r = plan["receipt"]
t_len, rw_end = r["teaser"]["out_end"], r["rewind"]["out_end"]
expect(plan["extras"] and r["teaser"]["out_start"] == 0 and r["parts"][0]["role"] == "teaser",
       "the teaser is the first part, role teaser")
expect(abs((rw_end - t_len) - 0.4) < 0.02, f"a 0.4 s rewind follows it ({rw_end - t_len:.2f}s)")
story = [w["w"] for w in plan["words"] if w["start"] >= rw_end - 0.01]
payoff_words = [w["w"] for w in WORDS if 99.9 <= w["start"] < 125.55]
expect(story == payoff_words, "the payoff then plays in full — the teaser's words again included")
teaser_words = [w["w"] for w in plan["words"] if w["start"] < t_len]
expect(teaser_words == "I made fifty grand in one morning.".split(), f"the teaser has its captions ({teaser_words})")
expect(any(abs(at - rw_end) < 0.02 and text == "HOW IT STARTED" for at, text in plan["labels"]),
       f"'HOW IT STARTED' shows the moment the story starts ({plan['labels']})")
expect(plan["rewind"]["sound"] is True, "no campaign: the rewind may have its sound")
plan = smartstitch.plan_render(PAYOFF, edits, WORDS, SETTINGS, Fraction(30),
                               rb_with(stitch=True, music=False, speed=True))
expect(plan["rewind"] and plan["rewind"]["sound"] is False, "a campaign without added sound: a silent rewind")
plan = smartstitch.plan_render(PAYOFF, edits, WORDS, SETTINGS, Fraction(30),
                               rb_with(stitch=True, music=True, speed=False))
expect(plan["rewind"] is None and any("speed" in n for n in plan["receipt"]["notes"]),
       "a campaign without speed changes: no rewind, and it says why")
plan = smartstitch.plan_render(PAYOFF, edits, WORDS, SETTINGS, Fraction(30), rb_with(stitch=False))
expect(not plan["extras"] and "joining moments" in plan["receipt"]["notes"][0],
       "a campaign that doesn't allow joining moments: no teaser (a teaser joins two moments)")
plan = smartstitch.plan_render(PAYOFF, edits, WORDS, {**SETTINGS, "platforms": ["youtube"], "max_len": 26.5},
                               Fraction(30), None)
expect(not plan["extras"] and any("under" in n for n in plan["receipt"]["notes"]),
       "a teaser that would push the clip past the length cap is left out")

print("\n== inserts: where they may go")
proof = {"id": "p1", "kind": "proof", "at": 112.6, "start": 300.0, "end": 302.5, "audio": "main", "fit": True}
early = {"id": "p2", "kind": "proof", "at": 100.4, "start": 310.0, "end": 312.0, "audio": "main", "fit": True}
late = {"id": "p3", "kind": "proof", "at": 124.5, "start": 320.0, "end": 322.0, "audio": "main", "fit": True}
base = {"teaser": {}, "teaser_on": False, "inserts_on": True}
plain = smartstitch.plan_render(PAYOFF, {**base, "inserts": [], "teaser_on": True, "teaser": t}, WORDS, SETTINGS,
                                Fraction(30), None)
plan = smartstitch.plan_render(PAYOFF, {**base, "inserts": [proof, early, late], "teaser_on": True, "teaser": t},
                               WORDS, SETTINGS, Fraction(30), None)
ins = plan["receipt"]["inserts"]
expect(len(plan["overrides"]) == 1 and ins[0]["id"] == "p1", "only the proof shot that fits is placed")
expect(plan["words"] == plain["words"] and plan["segments"] == plain["segments"],
       "a video-only insert leaves the sound's timeline and the captions exactly as they were")
o_s, o_e, src, fit = plan["overrides"][0]
at_out = next(w["start"] for w in plan["words"] if w["w"] == "Look" and w["start"] > rw_end)
expect(abs(o_s - (at_out - 0.12)) < 0.05 and fit and src == 300.0,
       f"it starts as he says 'Look at this chart' ({o_s:.2f}s vs {at_out:.2f}s), shown whole")
notes = " ".join(plan["receipt"]["notes"])
expect("hook" in notes and "punchline" in notes, f"the one in the hook and the one over the punchline say why ({notes})")
four = [dict(proof, id=f"p{k}", at=a) for k, a in enumerate((104.4, 108.2, 112.6, 115.4), 1)]
plan = smartstitch.plan_render(PAYOFF, {**base, "inserts": four}, WORDS, SETTINGS, Fraction(30), None)
expect(len(plan["receipt"]["inserts"]) <= 3 and "most a clip gets" in " ".join(plan["receipt"]["notes"]),
       "at most 3 inserts")
plan = smartstitch.plan_render(PAYOFF, {**base, "inserts": [proof]}, WORDS, SETTINGS, Fraction(30),
                               rb_with(stitch=True, borders=False))
expect(not plan["extras"] and "background" in plan["receipt"]["notes"][0],
       "a brief without backgrounds: no proof shot (a chart cropped to a face can't be read)")
short = [{"start": 99.9, "end": 107.9, "role": "payoff", "label": ""}]
plan = smartstitch.plan_render(short, {**base, "inserts": [dict(proof, at=104.4)]}, WORDS, SETTINGS,
                               Fraction(30), None)
expect(plan is not None and not plan["extras"], "a very short clip with no room left gets no insert, and says so")

print("\n== callbacks and reactions from Claude, checked")
cb, why = smartstitch.check_insert({"kind": "callback", "at": 130.5, "refers_to": "Remember I said I would never sell?",
                                    "start": 59.9, "end": 62.9, "quote": "I will never sell this account.",
                                    "why": "he refers back"}, [{"start": 129.9, "end": 135.0, "role": "payoff"}],
                                   WORDS, 600, 1)
expect(cb is not None and cb["audio"] == "own", f"a real callback is accepted, with its own sound ({why})")
fake, why = smartstitch.check_insert({"kind": "callback", "at": 130.5, "refers_to": "Remember I said I would never sell?",
                                      "start": 59.9, "end": 62.9, "quote": "I will always sell everything.",
                                      "why": "x"}, [{"start": 129.9, "end": 135.0, "role": "payoff"}], WORDS, 600, 2)
expect(fake is None and "quote" in why, f"a callback whose words don't match is dropped ({why})")
laugh, why = smartstitch.check_insert({"kind": "reaction", "at": 123.5, "reacts_to": "My wife thought I was joking.",
                                       "start": 125.9, "end": 126.9, "quote": "Haha", "sound": "laugh", "why": "x"},
                                      [{"start": 99.9, "end": 125.5, "role": "payoff"}], WORDS, 600, 3)
expect(laugh is not None and laugh["audio"] == "own" and abs(laugh["at"] - 125.0) < 0.6,
       f"a laugh right after the line it reacts to is accepted ({why})")
wrong, why = smartstitch.check_insert({"kind": "reaction", "at": 105.0, "reacts_to": "Everyone told me it was a stupid idea.",
                                       "start": 125.9, "end": 126.9, "quote": "Haha", "sound": "laugh", "why": "x"},
                                      [{"start": 99.9, "end": 125.5, "role": "payoff"}], WORDS, 600, 4)
expect(wrong is None and "too late" in why, f"a laugh put after a line it didn't react to is refused ({why})")
two_part = [{"start": 99.9, "end": 118.9, "role": "setup", "label": ""}, {"start": 129.9, "end": 135.0, "role": "payoff", "label": ""}]
plan = smartstitch.plan_render(two_part, {**base, "inserts": [cb]}, WORDS, SETTINGS, Fraction(30), None)
roles = [(p["role"], p["start"]) for p in plan["receipt"]["parts"]]
expect([r_[0] for r_ in roles] == ["setup", "payoff", "callback", "payoff"],
       f"the payoff is split around the callback, which plays with its own sound ({roles})")
expect(any(text == "EARLIER" for _, text in plan["labels"]), "the jump back is marked with a neutral label")

print("\n== the judge: with or without the teaser (blind, tie → without)")
clip = {"variants": {"continuous": structure._as_variant(99.9, 125.6, "He made $50k"), "stitched": None},
        "variant": "continuous", "teaser": t, "headline": ""}
structure.choose(clip, "continuous")


def judge_reply(better):
    def reply(kw):
        text = kw["messages"][0]["content"]
        out = []
        for block in text.split("=== CLIP ")[1:]:
            i = int(re.match(r"(\d+)", block).group(1))
            a, b = block.split("--- VERSION B")
            a_has = "flash-forward" in a
            score = lambda has: {k: (9 if has == better else 6) if better is not None else 7 for k in judge.CRITERIA}
            out.append({"id": i, "A": score(a_has), "B": score(not a_has), "prefer": "tie", "why": "test"})
        return {"clips": out}
    return reply


ANSWERS["score_versions"] = judge_reply(None)
judge.compare_teaser([clip], WORDS)
expect(clip["teaser_verdict"]["winner"] == "without", f"a tie keeps the simpler clip ({clip['teaser_verdict']})")
ANSWERS["score_versions"] = judge_reply(True)
judge.compare_teaser([clip], WORDS)
expect(clip["teaser_verdict"]["winner"] == "with", "a clear win keeps the teaser")
blk = CALLS[-1]["messages"][0]["content"]
expect("flash-forward" in blk and "Part 2" in blk, "the judge is told what the teaser is")

print("\n== funny moments: stitched only with a setup or callback")
funny = {"start": 118.0, "end": 127.0, "hook": "h", "type": "funny"}
funny["variants"] = {"continuous": structure._as_variant(119.1, 127.0, "h"), "stitched": None}
item = {"continuous": {"start": 119.1, "end": 127.0, "hook": "h"},
        "stitched": {"parts": [{"start": 59.9, "end": 62.9, "role": "setup", "label": "",
                                "quote": "I will never sell this account."},
                               {"start": 119.1, "end": 127.0, "role": "payoff", "label": "",
                                "quote": "I made fifty grand in one morning. My wife thought I was joking. Haha"}],
                     "hook": "h"}}
structure._apply(funny, item, WORDS, 600.0, max_len=90)
expect(funny["variants"]["stitched"] is not None, f"with a setup from earlier: accepted ({funny.get('stitch_problem')})")
funny2 = {"start": 118.0, "end": 127.0, "hook": "h", "type": "funny",
          "variants": {"continuous": structure._as_variant(119.1, 127.0, "h"), "stitched": None}}
item2 = json.loads(json.dumps(item))
item2["stitched"]["parts"][0]["role"] = "premise"
structure._apply(funny2, item2, WORDS, 600.0, max_len=90)
expect(funny2["variants"]["stitched"] is None and "setup or callback" in funny2["stitch_problem"],
       f"without one: refused ({funny2.get('stitch_problem')})")
story_clip = {"start": 118.0, "end": 127.0, "hook": "h", "type": "story",
              "variants": {"continuous": structure._as_variant(119.1, 127.0, "h"), "stitched": None}}
item3 = json.loads(json.dumps(item))
item3["stitched"]["parts"][0]["quote"] = "I will sell everything tomorrow morning."
structure._apply(story_clip, item3, WORDS, 600.0, max_len=90)
expect(story_clip["variants"]["stitched"] is None and "quoted words" in story_clip["stitch_problem"],
       f"a stitched part whose quote doesn't match is dropped ({story_clip.get('stitch_problem')})")
expect(funny["teaser_raw"] is None and funny["inserts_raw"] == [], "no teaser or inserts proposed: none kept")

print("\n== typed changes")
job_id = store.create_job("Trading story", "upload", {"platforms": ["tiktok"]})
store.update_job(job_id, status="done", duration=600.0,
                 transcript=json.dumps({"words": WORDS, "segments": [{"start": s, "end": s + 3, "text": x} for s, x in SENT]}))
job = store.get_job(job_id)
cl = {"id": "c", "start": 99.9, "end": 125.6, "parts": json.dumps(PAYOFF), "rank": 1}
receipt = {"teaser": {"out_end": 2.5, "start": 119.2, "end": 121.9, "quote": "I made fifty grand"},
           "inserts": [{"id": "p1", "kind": "proof", "name": "Proof shot", "out_start": 9, "out_end": 11, "quote": ""}]}
e = {"teaser": t, "teaser_on": True, "inserts": [proof], "inserts_on": True, "smart": receipt}
got, problems = smartstitch.plan_change(cl, e, {"teaser": False}, WORDS, job)
expect(got == {"teaser_on": False} and not problems, "'remove the teaser' turns it off")
got, _ = smartstitch.plan_change(cl, {**e, "teaser_on": False, "smart": {}}, {"teaser": True}, WORDS, job)
expect(got == {"teaser_on": True}, "'add a teaser' puts the ready one back")
got, problems = smartstitch.plan_change(cl, {"smart": {}}, {"teaser": True}, WORDS, job)
expect(not got and "didn't find" in problems[0], "'add a teaser' with none ready says so plainly")
got, _ = smartstitch.plan_change(cl, e, {"inserts_on": False}, WORDS, job)
expect(got == {"inserts_on": False}, "'no inserts' switches them all off")
got, _ = smartstitch.plan_change(cl, e, {"remove_inserts": ["proof"]}, WORDS, job)
expect(got == {"inserts": []}, "'take out the proof shot' removes just that one")
got, _ = smartstitch.plan_change(cl, e, {"proof_at": "50k"}, WORDS, job)
expect(got.get("proof_request") == {"near": "50k"}, "'show the chart when he says 50k' asks for a proof shot there")
camp_job = {**job, "settings": json.dumps({"campaign": {"rules": rb_with(stitch=False)}})}
got, problems = smartstitch.plan_change(cl, e, {"teaser": True}, WORDS, camp_job)
expect(not got and "joining moments" in problems[0], "a campaign without stitching: refused, with the reason")
props = instruct._tool()["input_schema"]["properties"]["changes"]["items"]["properties"]
expect(all(k in props for k in ("teaser", "inserts_on", "remove_inserts", "proof_at")),
       "the Ask tab's tool has the new controls")
expect("Opens with a teaser" in " ".join(smartstitch.describe_lines(e)), "Claude is told the clip opens with a teaser")

print("\n== a real render: video-only insert, frame by frame (frame-number source)")
import stitch_check  # noqa: E402
from synctest import measure  # noqa: E402

work = DATA / "frames"
work.mkdir(exist_ok=True)
src = work / "numbered.mp4"
stitch_check.make_source(src, seconds=40.0)
fps = Fraction(30000, 1001)
cut = {"motion": False, "captions_on": False, "hook_on": False, "headline_on": False, "labels_on": False,
       "layout": "fill", "auto_frame": True, "normalize_audio": False}


def pcm(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32)


segs = [(10.33, 20.07)]
a = motion.render_clip(source=src, clip_id="plain", start=0, end=0, words=[], edits=cut, has_audio=True,
                       layout="fill", plan=None, source_size=(stitch_check.W, stitch_check.H), segments=segs)
b = motion.render_clip(source=src, clip_id="insert", start=0, end=0, words=[], edits=cut, has_audio=True,
                       layout="fill", plan=None, source_size=(stitch_check.W, stitch_check.H), segments=segs,
                       video_overrides=[(3.0, 5.0, 30.0)])
codes = stitch_check.read_codes(Path(b["file"]), RENDER_W, RENDER_H)
n0 = round(Fraction(3) * fps)
n1 = n0 + round(Fraction(2) * fps)
first = round(Fraction(10.33) * fps)
want = [first + n for n in range(len(codes))]
for n in range(n0, n1):
    want[n] = round(Fraction(30) * fps) + (n - n0)
right = sum(1 for x, y in zip(want, codes) if x == y)
expect(b["frames"] == a["frames"] == len(codes) and right == len(codes),
       f"every frame shows the expected source frame, the insert's from 30 s on ({right} of {len(codes)})")
pa, pb = pcm(a["file"]), pcm(b["file"])
expect(len(pa) == len(pb) and float(np.abs(pa - pb).max()) == 0.0,
       "the sound is bit-for-bit the same as without the insert")
offs = [o for t_, o in measure(Path(b["file"])) if not (3.0 <= t_ < 5.0)]
expect(offs and max(abs(o) for o in offs) < 5.0, f"picture and sound stay locked outside the insert "
                                                 f"(worst {max(abs(o) for o in offs):.1f} ms)")

print("\n== a real render: teaser, rewind, story")
segs2 = [(30.12, 32.55), (10.33, 20.07)]
tl = motion.timeline_from_segments(segs2, fps)
t_frames = tl.segments[0][1] - tl.segments[0][0]
after = float(Fraction(t_frames) / fps)
c = motion.render_clip(source=src, clip_id="teaser", start=0, end=0, words=[], edits=cut, has_audio=True,
                       layout="fill", plan=None, source_size=(stitch_check.W, stitch_check.H), segments=segs2,
                       rewind={"after": after, "seconds": 0.4, "sound": False})
cc = stitch_check.read_codes(Path(c["file"]), RENDER_W, RENDER_H)
rw = round(Fraction(0.4) * fps)
want = list(range(*tl.segments[0])) + [None] * rw + list(range(*tl.segments[1]))
right = sum(1 for x, y in zip(want, cc) if x is not None and x == y)
expect(len(cc) == len(want) and right == len(want) - rw,
       f"teaser frames, then {rw} rewind frames, then the story — every story frame right ({right})")
back = cc[t_frames:t_frames + rw]
expect(all(tl.segments[0][0] <= x < tl.segments[0][1] + 40 for x in back) and back[0] > back[-1],
       f"the rewind plays the teaser's frames backwards ({back[0]} → {back[-1]})")
p = pcm(c["file"])
r0, r1 = int(after * 48000) + 300, int((after + rw / float(fps)) * 48000) - 300
expect(float(np.abs(p[r0:r1]).max()) == 0.0, "the rewind is silent when there's no sound allowed")
offs = [o for _, o in measure(Path(c["file"]))]
expect(max(abs(o) for o in offs) < 5.0, f"still in sync after the rewind (worst {max(abs(o) for o in offs):.1f} ms)")
d = motion.render_clip(source=src, clip_id="teaser_snd", start=0, end=0, words=[], edits=cut, has_audio=True,
                       layout="fill", plan=None, source_size=(stitch_check.W, stitch_check.H), segments=segs2,
                       rewind={"after": after, "seconds": 0.4, "sound": True})
expect(float(np.abs(pcm(d["file"])[r0:r1]).max()) > 2000, "with sound allowed, the rewind has its whirr")

print("\n== the real thing: a talking head with a chart, through the pipeline's own code")
foot = work / "talk.mp4"
subprocess.run([sys.executable, str(ROOT / "tools" / "make_test_footage.py"), "40", str(foot), "--screen", "28-34"],
               check=True)
LINES = ["So I started trading", "Everyone said I was crazy", "I kept going every day", "Look at this chart now",
         "It was a slow climb", "Then it finally clicked", "My account kept growing", "I made fifty grand",
         "Nobody believed me then", "Now they all ask me"]
fw = []
for k, line in enumerate(LINES):                      # the drawn person talks 0.3-2.1 s of every 2.5 s
    toks = line.split()
    for i, w in enumerate(toks):
        fw.append({"w": w + ("." if i == len(toks) - 1 else ""), "start": round(2.5 * k + 0.35 + 0.42 * i, 3),
                   "end": round(2.5 * k + 0.35 + 0.42 * i + 0.36, 3)})
fparts = [{"start": 0.2, "end": 24.6, "role": "payoff", "label": ""}]
fclip = {"parts": fparts, "variants": {"continuous": structure._as_variant(0.2, 24.6, "h")}, "variant": "continuous",
         "teaser_raw": {"start": 17.8, "end": 19.9, "quote": "I made fifty grand.", "why": "the number",
                        "label": "HOW IT STARTED"}, "inserts_raw": []}
ANSWERS["pick_proof"] = {"picks": [{"moment": 1, "frame": "F1", "why": "the chart he means"}]}
settings = {"teaser": "always", "inserts": True, "tighten": True, "platforms": ["tiktok"]}
smartstitch.prepare("talkjob", foot, [fclip], fw, settings, None, "", 40.0)
proofs = [i for i in fclip["inserts"] if i["kind"] == "proof"]
expect(fclip["teaser_on"] and fclip["teaser"]["start"] >= 17.0, "the teaser is checked and on (Always)")
expect(len(proofs) == 1 and 28.0 <= proofs[0]["start"] < proofs[0]["end"] <= 34.0 and abs(proofs[0]["at"] - 7.85) < 0.1,
       f"the proof shot comes from the chart stretch, at 'Look at this chart' ({proofs and proofs[0]})")
look_call = CALLS[-1]
expect(sum(1 for b_ in look_call["messages"][0]["content"] if b_.get("type") == "image") in range(1, 9),
       "Claude was shown 1-8 frames to pick from")
fedits = {"layout": "fill", "motion": True, "captions_on": True, "hook": "He made $50k in a morning",
          "caption_style": "impact", **smartstitch.clip_edits(fclip, fparts, settings, fw)}
job2 = store.create_job("Talk", "upload", settings)
store.update_job(job2, status="done", duration=40.0, source_path=str(foot),
                 transcript=json.dumps({"words": fw, "segments": []}))
cid = store.create_clip(job2, {"start": 0.2, "end": 24.6, "title": "t", "hook": fedits["hook"], "rank": 1,
                               "parts": fparts, "edits": fedits, "words": []})
info = {"has_audio": True, "width": 1920, "height": 1080}
out, words, saved, smart = smartstitch.render(foot, cid, fparts, fedits, fw, settings, Fraction(30), info, None)
store.update_clip(cid, file=str(out["file"]), thumb=str(out["thumb"]), status="ready", words=json.dumps(words),
                  saved=saved, edits=json.dumps({**fedits, "smart": smart}))
dur = float(json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                                       str(out["file"])], capture_output=True, text=True).stdout)["format"]["duration"])
expect(abs(dur - smart["length"]) < 0.08, f"the file is as long as the receipt says ({dur:.2f}s vs {smart['length']}s)")
ins0 = smart["inserts"][0]
shots = {"teaser": 0.8, "rewind": (smart["rewind"]["out_start"] + smart["rewind"]["out_end"]) / 2,
         "story_start": smart["rewind"]["out_end"] + 0.5, "proof": (ins0["out_start"] + ins0["out_end"]) / 2,
         "after_proof": ins0["out_end"] + 0.6}
for name, at in shots.items():
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(out["file"]), "-frames:v", "1",
                    str(LOOK / f"{name}.jpg")])
import cv2  # noqa: E402
pf = cv2.imread(str(LOOK / "proof.jpg"), cv2.IMREAD_GRAYSCALE)
mid = float(cv2.Canny(pf[700:1250], 60, 150).mean())
top = float(cv2.Canny(pf[200:600], 60, 150).mean())
expect(mid > 4 * max(top, 0.5), f"the proof frame shows the chart whole in the middle over a blurred copy "
                                f"(edges middle {mid:.1f} vs top {top:.1f})")
checks = doctor.technical(Path(out["file"]), fedits, words, "tiktok")
st = {ch["id"]: ch["status"] for ch in checks}
expect(st.get("frozen") == "pass" and st.get("black") == "pass" and st.get("file") is None,
       f"the clip doctor's measurements pass (frozen {st.get('frozen')}, black {st.get('black')})")
gate = compliance.check_source(rb_with(stitch=False), {"start": 0.2, "end": 24.6, "parts": fparts}, {**fedits, "smart": smart},
                               Path(out["file"]), {}, fedits["hook"], None)
expect(any(ch["id"] == "stitch" and ch["status"] == "fail" for ch in gate["checks"]),
       "the campaign gate blocks a teaser or insert when the brief forbids joining moments")
gate = compliance.check_source(rb_with(stitch=True, music=False, speed=True), {"start": 0.2, "end": 24.6, "parts": fparts},
                               {**fedits, "smart": smart}, Path(out["file"]), {}, fedits["hook"], None)
expect(any(ch["id"] == "music" and ch["status"] == "fail" for ch in gate["checks"]),
       "…and a rewind sound when it forbids added sound")

print("\n== the editor: remove the teaser, put it back, undo — real re-renders")
before = store.get_clip(cid)
instruct.snapshot(cid)
pipeline.rerender_clip(cid, {"teaser_on": False})
now = store.get_clip(cid)
ne = json.loads(now["edits"])
expect(not ne["smart"].get("teaser") and ne["smart"]["inserts"] and ne["teaser"],
       "teaser removed in one go; the proof shot stays; the teaser is kept ready")
wn = json.loads(now["words"])
expect(wn and wn[0]["w"] == "So", "the clip now opens on the story's first word")
pipeline.rerender_clip(cid, {"inserts_on": False})
ne = json.loads(store.get_clip(cid)["edits"])
expect(not ne["smart"] and ne["inserts"], "'no inserts': a plain clip, inserts kept ready")
pipeline.rerender_clip(cid, {"teaser_on": True, "inserts_on": True})
ne = json.loads(store.get_clip(cid)["edits"])
expect(ne["smart"].get("teaser") and ne["smart"]["inserts"], "both put back")
pipeline.rerender_clip(cid, {"start": 2.4, "end": 24.6})
ne = json.loads(store.get_clip(cid)["edits"])
expect(ne["smart"].get("teaser") and abs(store.get_clip(cid)["start"] - 2.4) < 0.01,
       "a trim keeps the teaser while its moment is still in the clip")
instruct.undo(cid)
back_clip = store.get_clip(cid)
expect(back_clip["edits"] == before["edits"] and back_clip["start"] == before["start"], "Undo puts the version before back")
doctor.treat(cid, None, use_claude=False)
ne = json.loads(store.get_clip(cid)["edits"])
expect(ne["smart"].get("teaser"), "the clip doctor's check (and any fix) keeps the teaser")

print(f"\nFrames to look at: {LOOK}")
print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed")
sys.exit(1 if FAILS else 0)
