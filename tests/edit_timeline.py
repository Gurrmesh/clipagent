"""The Edit Maker's timeline (app/edits.py) — offline, no Claude, no video.

Run: python tests/edit_timeline.py
A made-up song (128 BPM, the drop at 20.99 s) and made-up videos. Checks
that beat edits cut exactly on the beat, land a cut and the best moment's
hit on the drop, end on a bar line at the asked length, and that speech
edits keep every sentence whole while still cutting on the beat. Also:
moments off / reordered, plain errors, speed curves, and Claude's picks
being checked (numbers he never said, duplicates, one drop).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="edittl_")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import edits, highlights, store  # noqa: E402

FAILS = []
EPS = 0.002


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def song(bpm, drop, duration=60.0, offset=None):
    period = 60.0 / bpm
    if offset is None:
        offset = drop - int(drop / period) * period
    beats = []
    t = offset
    while t < duration - 0.05:
        beats.append(round(t, 4))
        t += period
    di = min(range(len(beats)), key=lambda i: abs(beats[i] - drop))
    bars = beats[di % 4::4]
    return {"id": "s1", "analysis": {"duration": duration, "bpm": bpm, "beats": beats, "bars": bars,
                                     "drop": beats[di], "energy": []}}


def near_beat(t, beats):
    return min(abs(t - b) for b in beats)


def contiguous(tl):
    segs = tl["segments"]
    ok = abs(segs[0]["at"]) < EPS and all(abs(a["at"] + a["dur"] - b["at"]) < EPS for a, b in zip(segs, segs[1:]))
    return ok and abs(segs[-1]["at"] + segs[-1]["dur"] - tl["length"]) < EPS


# --- made-up videos: sentences every few seconds -------------------------------------------
def words_for(seconds, gap=4.0):
    words, t, n = [], 2.0, 0
    while t < seconds - 5:
        sentence = f"Sentence number {n} has some words here.".split()
        for i, w in enumerate(sentence):
            words.append({"w": w, "start": round(t + i * 0.35, 3), "end": round(t + i * 0.35 + 0.3, 3)})
        t += len(sentence) * 0.35 + gap - 2.5
        n += 1
    return words


WORDS = {"A": words_for(600), "B": words_for(400)}
DURS = {"A": 600.0, "B": 400.0}


def moments(n, span=3.0, drop=None, source_cycle=("A", "B")):
    out = []
    for i in range(n):
        start = 20.0 + i * 25.0
        out.append({"id": f"m{i + 1}", "source": source_cycle[i % 2], "start": start, "end": start + span,
                    "hit": start + span * 0.6, "text": f"PUNCH {i + 1}", "drop": i == (n // 2 if drop is None else drop)})
    return out


S128 = song(128, 20.99)
BEATS128 = S128["analysis"]["beats"]
P128 = 60.0 / 128

# --- speed curves ---------------------------------------------------------------------------
print("== speed curves")
c = edits.ramp(0.9375, 0.46875, 1.7, 0.4)
steps = 20000
numeric = sum(edits.speed_at(c, (k + 0.5) * 0.9375 / steps) for k in range(steps)) * 0.9375 / steps
expect(abs(edits.curve_src(c, 0.9375) - numeric) < 1e-4, "the source time used is the exact integral of the speed")
expect(abs(edits.speed_at(c, 0.46875) - 0.4) < 1e-6 and edits.speed_at(c, 0.0) == 1.7,
       "fast in, slow on the hit, fast out")
expect(abs(edits.curve_time(c, 0.9375, edits.curve_src(c, 0.6)) - 0.6) < 1e-4, "source time maps back to edit time")
expect(all(b[0] > a[0] for a, b in zip(c, c[1:])), "keyframes run forward")

# --- Velocity --------------------------------------------------------------------------------
print("== Velocity, 128 BPM, 20 s")
ms = moments(10, span=3.0, drop=4)
tl = edits.build_timeline(ms, "velocity", 20, S128, {}, WORDS, durations=DURS, hook="He turned 500 into 2 million")
segs = tl["segments"]
s0 = tl["music"]["start"]
expect(contiguous(tl), "segments follow each other with no gaps, up to the exact length")
expect(all(near_beat(s["at"] + s0, BEATS128) < EPS for s in segs), "every cut lands exactly on a beat")
expect(abs(tl["drop_at"] + s0 - 20.99) < EPS, "the drop is where the song drops")
expect(any(abs(s["at"] - tl["drop_at"]) < EPS for s in segs), "a cut lands exactly on the drop")
drop_seg = next(s for s in segs if s["drop"])
drop_m = next(m for m in ms if m["drop"])
expect(drop_seg["moment"] == drop_m["id"] and abs(drop_seg["src_start"] - drop_m["hit"]) < EPS,
       "the drop moment's hit is the frame on the drop")
expect(abs(tl["drop_at"] - 8 * P128) < EPS, "the song starts 8 beats before the drop")
bars = tl["length"] / (4 * P128)
expect(abs(bars - round(bars)) < 1e-3 and abs(tl["length"] - 20) <= 2 * P128 + EPS,
       f"ends on a bar line near the asked length ({tl['length']:.2f} s = {round(bars)} bars)")
expect(abs(tl["music"]["end"] - tl["music"]["start"] - tl["length"]) < EPS and tl["music"]["end"] <= 60,
       "the song section is exactly as long as the edit")
expect(all(s["dur"] >= P128 - EPS for s in segs), "no shot is shorter than a beat")
expect(segs[-1]["dur"] >= 2 * P128 - EPS, "the last shot gets at least two beats")
expect(drop_seg["flashes"] == [0.0] and drop_seg["shakes"] == [0.0] and drop_seg["glitches"] == [0.0],
       "flash, shake and glitch on the drop")
expect(drop_seg["speed"] < 0.8, f"slow-mo on the drop (average speed {drop_seg['speed']})")
built = [s for s in segs if not s["drop"] and s["at"] < tl["drop_at"]]
expect(all(abs(s["dur"] - 2 * P128) < EPS for s in built), "a cut every 2 beats before the drop")
after = [s for s in segs if s["at"] > tl["drop_at"] + EPS]
expect(any(abs(s["dur"] - P128) < EPS for s in after), "single-beat cuts after the drop")
bars_from_drop = [((s["at"] + s0) - 20.99) / (4 * P128) for s in after]
bar_cuts = [s for s, b in zip(after, bars_from_drop) if abs(b - round(b)) < 1e-3]
expect(bar_cuts and all(s["flashes"] for s in bar_cuts), "a flash on every bar-line cut")
expect(all(s["pulses"] == s["beats"] for s in segs), "a zoom punch on every beat")
order = []
for s in segs:
    if not order or order[-1] != s["moment"]:
        order.append(s["moment"])
expect(order == [m["id"] for m in ms if m["id"] in order], "moments play in Claude's order")
expect(len(set(order)) == len(order), "each moment plays as one run")
expect(all(0 <= s["src_start"] and s["src_start"] + s["speed"] * s["dur"] <= DURS[s["source"]] for s in segs),
       "every shot stays inside its video")
for m in ms:
    run = [s for s in segs if s["moment"] == m["id"]]
    if not run or m["drop"]:
        continue
    lo = run[0]["src_start"]
    hi = run[-1]["src_start"] + edits.curve_src(run[-1]["curve"], run[-1]["dur"])
    ok_window = lo >= m["start"] - edits.EXTEND - EPS and hi <= m["end"] + edits.EXTEND + EPS
    if not ok_window:
        break
expect(ok_window, "each moment's shots come from that moment (± a little picture)")
slow_hits = 0
for m in ms:
    for s in segs:
        used = edits.curve_src(s["curve"], s["dur"])
        if s["moment"] == m["id"] and s["src_start"] - EPS <= m["hit"] <= s["src_start"] + used + EPS:
            t = edits.curve_time(s["curve"], s["dur"], m["hit"] - s["src_start"])
            slow_hits += edits.speed_at(s["curve"], t) <= 0.45
expect(slow_hits >= len(ms) - 3, f"the hits play in slow motion ({slow_hits} of {len(ms)})")
texts = [s for s in segs if s["text"]]
expect(texts and all(s["at"] >= edits.HOOK_SECONDS - 0.05 or s["drop"] for s in texts),
       "punch words never cover the hook")
expect(tl["hook"]["text"] and tl["hook"]["end"] == edits.HOOK_SECONDS, "the hook is on screen for the first 2.8 s")
expect(tl["loop"] == edits.LOOP_SECONDS, "the ending loops")

print("== Velocity, other tempos")
for bpm, drop in ((92, 26.457), (150, 12.4), (174, 30.0)):
    sg = song(bpm, drop, 70)
    t2 = edits.build_timeline(moments(9, drop=3), "velocity", 25, sg, {}, WORDS, durations=DURS)
    b2 = sg["analysis"]["beats"]
    ok = contiguous(t2) and all(near_beat(s["at"] + t2["music"]["start"], b2) < EPS for s in t2["segments"])
    ds = next(s for s in t2["segments"] if s["drop"])
    ok = ok and abs(ds["at"] + t2["music"]["start"] - sg["analysis"]["drop"]) < EPS
    expect(ok, f"{bpm} BPM: on the beat, the drop on the drop, {len(t2['segments'])} shots in {t2['length']:.1f} s")

print("== Aura, Flow, Money")
ta = edits.build_timeline(moments(5, span=4.0, drop=2), "aura", 15, S128, {}, WORDS, durations=DURS)
expect(abs(ta["drop_at"] - 4 * P128) < EPS, "Aura: one bar before the drop")
expect(all(abs(s["dur"] - 8 * P128) < EPS for s in ta["segments"][1:-1]), "Aura: a cut every two bars")
expect(all(abs(s["speed"] - 0.6) < 0.01 for s in ta["segments"] if not s["drop"]), "Aura: slow-mo holds")
expect(sum(bool(s["flashes"]) for s in ta["segments"]) == 1, "Aura: a flash on the drop only")
tf = edits.build_timeline(moments(24, span=2.0, drop=6), "flow", 25, S128, {}, WORDS, durations=DURS)
after = [s for s in tf["segments"] if s["at"] > tf["drop_at"] + EPS]
expect(all(abs(s["dur"] - 2 * P128) < EPS for s in after[:-1]), "Flow: about a cut a second")
expect(all(s["blur_in"] for s in tf["segments"][1:]), "Flow: every cut carries a motion blur")
expect(sum(bool(s["flashes"]) for s in tf["segments"]) <= 1 + len(after) // 8, "Flow: flashes only on the big hits")
tm = edits.build_timeline(moments(7, span=4.0, drop=2), "money", 20, S128, {}, WORDS, durations=DURS)
expect(all(abs(s["dur"] - 4 * P128) < EPS for s in tm["segments"][:-1]), "Money: a cut every four beats")
expect(all(abs(s["speed"] - 0.8) < 0.01 for s in tm["segments"] if not s["drop"]), "Money: slowed to 0.8×")
tms = tm["segments"]
expect(all(a["dip_out"] == (a["moment"] != b["moment"] and not b["drop"]) for a, b in zip(tms, tms[1:]))
       and not tms[-1]["dip_out"], "Money: dips between moments, none at the very end (it loops)")
expect(not any(s["dip_in"] for s in tms if s["drop"]), "Money: never a dip into the drop — it hits")

print("== effects switched off")
off = {k: False for k in edits.EFFECTS}
t3 = edits.build_timeline(moments(10, drop=4), "velocity", 20, S128, off, WORDS, durations=DURS)
expect(all(not (s["flashes"] or s["shakes"] or s["glitches"] or s["pulses"]) and s["zoom"] == 1.0
           for s in t3["segments"]), "no flashes, shakes, glitches or zooms when they're off")
expect(all(abs(s["speed"] - 1.0) < 1e-6 for s in t3["segments"]), "no ramps or slow-mo when they're off")
expect(t3["loop"] == 0 and t3["hook"] is None, "no loop and no hook when they're off")

# --- speech pace ----------------------------------------------------------------------------
print("== Cinematic with music: whole sentences, cuts on the beat, the best line on the drop")
sp = [{"id": "c1", "source": "A", "start": 30.0, "end": 37.3, "hit": 34.0, "text": "one", "drop": False},
      {"id": "c2", "source": "B", "start": 80.0, "end": 88.9, "hit": 85.2, "text": "two", "drop": True},
      {"id": "c3", "source": "A", "start": 150.0, "end": 156.1, "hit": 152.0, "text": "three", "drop": False}]
tc = edits.build_timeline(sp, "cinematic", 30, S128, {}, WORDS, durations=DURS)
segs = tc["segments"]
song0 = tc["music"]["start"] - tc["music"]["at"]                   # song time at the edit's start
expect(contiguous(tc), "segments follow each other with no gaps")
expect(all(near_beat(s["at"] + song0, BEATS128) < EPS for s in segs), "every cut lands on a beat")
whole = all(abs(s["src_start"] + s["voice"][0] - m["start"]) < EPS and
            abs(s["voice"][1] - s["voice"][0] - (m["end"] - m["start"])) < EPS and s["voice"][1] <= s["dur"] + EPS
            for s, m in zip(segs, sp))
expect(whole, "every moment plays whole, every word of it")
ds = next(s for s in segs if s["drop"])
expect(abs(ds["at"] + ds["hit"] + song0 - 20.99) < EPS, "the drop moment's hit meets the song's drop")
expect(all(s["dur"] - s["voice"][1] < P128 + 1.3 for s in segs), "the picture holds less than a beat (a bar at the end)")
end_beats = (tc["length"]) / P128
expect(abs(end_beats - round(end_beats)) < 1e-3, "it ends on a beat")
words_ok = all(segs[i]["at"] - EPS <= w["t"] <= segs[i]["at"] + segs[i]["dur"] for i in range(len(segs))
               for w in segs[i]["words"])
expect(words_ok and all(s["words"] for s in segs), "the words sit inside their own shot")
expect(all(s["dip_in"] == (i > 0) for i, s in enumerate(segs)), "dips between the moments")

print("== Motivation with no music")
tn = edits.build_timeline(sp, "motivation", 30, None, {}, WORDS, durations=DURS)
expect(tn["music"] is None and contiguous(tn), "no song: the lines play back to back")
expect(abs(tn["length"] - sum(m["end"] - m["start"] for m in sp) - 0.35) < EPS, "length = the lines + a short hold")
expect(next(s for s in tn["segments"] if s["drop"])["flashes"], "a flash on the strongest line")

print("== Funny: a punch and a shake on every punchline")
tfun = edits.build_timeline(sp, "funny", 30, None, {}, WORDS, durations=DURS)
expect(all(s["pulses"] == [s["hit"]] and s["shakes"] == [s["hit"]] for s in tfun["segments"]),
       "zoom punch + shake at each punchline")

print("== a speech edit too long for the asked length leaves moments out, never the drop")
long_sp = [dict(m, end=m["start"] + 12) for m in sp] + [
    {"id": "c4", "source": "B", "start": 200.0, "end": 212.0, "hit": 205.0, "text": "four", "drop": False}]
tl4 = edits.build_timeline(long_sp, "cinematic", 25, S128, {}, WORDS, durations=DURS)
ids = [s["moment"] for s in tl4["segments"]]
expect("c2" in ids and len(ids) < 4 and tl4["notes"], f"kept {ids}, with a note why")

# --- picking the part of the song ------------------------------------------------------------
print("== picking the part of the song")
part = song(128, 20.99, duration=70.0)
an = part["analysis"]
chorus = min(an["bars"], key=lambda b: abs(b - 45.0))               # a second, bigger kick-in later on
an["energy"] = [0.2 if t / 4 < 20.99 else (0.5 if t / 4 < chorus else 1.0) for t in range(70 * 4)]
tp = edits.build_timeline(moments(9, drop=4), "velocity", 15, part, {}, WORDS, durations=DURS, song_start=37.0)
start = tp["music"]["start"]
expect(min(abs(start - b) for b in an["bars"]) < EPS and abs(start - 37.0) < 2 * 60 / 128 + EPS,
       f"the edit starts on the bar nearest the part picked ({start:.2f} s)")
expect(abs(tp["drop_at"] + start - chorus) < EPS, f"the drop lands on that part's kick-in ({chorus:.2f} s)")
expect(all(near_beat(x["at"] + start, an["beats"]) < EPS for x in tp["segments"]), "cuts still on the beat")
quiet = edits.build_timeline(moments(9, drop=4), "velocity", 15, part, {}, WORDS, durations=DURS, song_start=50.0)
expect(quiet["notes"] and abs(quiet["drop_at"] - 8 * P128) < EPS,
       "a part with no kick-in: the best moment lands on bar 3, and it says so")
ts2 = edits.build_timeline(sp, "cinematic", 30, part, {}, WORDS, durations=DURS, song_start=37.0)
song0 = ts2["music"]["start"] - ts2["music"]["at"]
dseg = next(x for x in ts2["segments"] if x["drop"])
expect(abs(dseg["at"] + dseg["hit"] + song0 - chorus) < EPS, "a speech edit meets the picked part's kick-in")

# --- the editor's controls ------------------------------------------------------------------
print("== moments off, reordered, the drop moved")
ms = moments(8, drop=3)
ms[1]["off"] = True
t5 = edits.build_timeline(ms, "velocity", 20, S128, {}, WORDS, durations=DURS)
expect("m2" not in {s["moment"] for s in t5["segments"]}, "a switched-off moment is gone")
re = [ms[5], ms[0], ms[2], ms[3], ms[4]]
t6 = edits.build_timeline(re, "velocity", 20, S128, {}, WORDS, durations=DURS)
seen = []
for s in t6["segments"]:
    if s["moment"] not in seen:
        seen.append(s["moment"])
expect(seen == ["m6", "m1", "m3", "m4", "m5"], "a new order plays in the new order")
moved = [dict(m, drop=(m["id"] == "m6")) for m in moments(8, drop=3)]
t7 = edits.build_timeline(moved, "velocity", 20, S128, {}, WORDS, durations=DURS)
expect(next(s for s in t7["segments"] if s["drop"])["moment"] == "m6", "the moment you put on the drop lands there")
two_drops = [dict(m, drop=True) for m in moments(6)]
t8 = edits.build_timeline(two_drops, "velocity", 20, S128, {}, WORDS, durations=DURS)
expect(sum(1 for s in t8["segments"] if s["drop"]) == 1, "never two drops")

print("== plain errors")
for args, words in ((("velocity", 20, None), "A Velocity edit is cut to music"),
                    (("aura", 20, {"analysis": {"beats": []}}), "An Aura edit is cut to music")):
    try:
        edits.build_timeline(moments(5), args[0], args[1], args[2], {}, WORDS, durations=DURS)
        expect(False, f"{args[0]} without a song is refused")
    except ValueError as exc:
        expect(words in str(exc), f"{args[0]} without a song: “{exc}”")
try:
    edits.build_timeline([dict(m, off=True) for m in moments(3)], "velocity", 20, S128, {}, WORDS)
    expect(False, "all moments off is refused")
except ValueError as exc:
    expect("switched off" in str(exc), f"all moments off: “{exc}”")
try:
    edits.build_timeline(moments(5), "velocity", 20, song(128, 4.0, duration=6.0), {}, WORDS)
    expect(False, "a too-short song is refused")
except ValueError as exc:
    expect("too short" in str(exc), f"a 6 s song: “{exc}”")
short = song(128, 10.0, duration=15.0)
t9 = edits.build_timeline(moments(6, drop=2), "velocity", 30, short, {}, WORDS, durations=DURS)
expect(t9["length"] <= 15 and t9["notes"], f"a 15 s song makes a {t9['length']:.1f} s edit, and says so")
early = song(128, 1.5, duration=40.0)
t10 = edits.build_timeline(moments(6, drop=2), "velocity", 20, early, {}, WORDS, durations=DURS)
expect(t10["music"]["start"] >= 0 and abs(t10["drop_at"] + t10["music"]["start"] - early["analysis"]["drop"]) < EPS,
       "a drop in the first seconds of the song still lands")
expect(edits.style_key("hype") == "velocity" and edits.style_key("luxury") == "money", "the old style names still work")

# --- Claude's picks are checked -------------------------------------------------------------
print("== Claude's picks are checked")
store.init()


def source_job(title, sentences, duration):
    words, segments = [], []
    for t, sentence in sentences:
        toks = sentence.split()
        for i, w in enumerate(toks):
            words.append({"w": w, "start": t + i * 0.4, "end": t + i * 0.4 + 0.3})
        segments.append({"start": t, "end": t + len(toks) * 0.4, "text": sentence})
    jid = store.create_job(title, "upload", {})
    store.update_job(jid, status="done", duration=duration, transcript=json.dumps({"words": words,
                                                                                    "segments": segments}))
    return store.get_job(jid)


j1 = source_job("Seven years", [(10.0, "I traded for seven years before it worked."),
                                (40.0, "My first account was 500 dollars."), (70.0, "Now I made 2 million this year."),
                                (100.0, "Stick to the plan every single day.")], 300)
j2 = source_job("Q&A", [(5.0, "Discipline beats talent."), (30.0, "Never risk more than one percent.")], 200)
reply = {
    "title": "TJR velocity", "hook": "Bro turned $500 into $2 million",
    "caption": "Seven years.", "hashtags": ["#trading", "tjr"],
    "moments": [
        {"video": 1, "start": 10.0, "end": 13.4, "hit": 11.2, "text": "SEVEN YEARS", "drop": False},
        {"video": 1, "start": 40.0, "end": 42.8, "hit": 41.6, "text": "500 DOLLARS"},
        {"video": 1, "start": 40.2, "end": 42.6, "text": "duplicate"},
        {"video": 1, "start": 70.0, "end": 72.8, "hit": 72.0, "text": "10 MILLION", "drop": True},
        {"video": 2, "start": 5.0, "end": 6.4, "hit": 5.8, "text": "TALENT IS OVERRATED", "drop": True},
        {"video": 2, "start": 30.0, "end": 30.3, "text": "too short"},
        {"video": 3, "start": 1.0, "end": 4.0, "text": "no such video"},
        {"video": 2, "start": 190.0, "end": 260.0, "hit": 999, "text": "RISK"},
    ]}
got = edits.read_pick(reply, [j1, j2], "velocity")
ids = [(m["source"], m["start"]) for m in got["moments"]]
expect(len(got["moments"]) == 5, f"duplicates, too-short bits and missing videos are dropped ({len(ids)} kept)")
expect(sum(m["drop"] for m in got["moments"]) == 1 and got["moments"][2]["drop"], "exactly one drop: the first marked")
expect(got["moments"][2]["text"] == "", "a number he never said ('10 MILLION') is taken off the screen")
expect(got["moments"][3]["text"] == "", "words he didn't say there are taken off the screen")
expect(got["moments"][0]["text"] == "SEVEN YEARS" and got["moments"][1]["text"] == "500 DOLLARS",
       "punch words he really says stay")
expect(got["moments"][4]["end"] == 200.0 and got["moments"][4]["start"] <= got["moments"][4]["hit"] <= 200.0,
       "times are kept inside the video, the hit inside the moment")
expect(got["hook"] == "Bro turned $500 into $2 million", "a hook built from what he said stays")
expect(got["hashtags"] == ["trading", "tjr"] and got["notes"], "hashtags cleaned, notes say what was changed")
bad = edits.read_pick({**reply, "hook": "He made $40M in a week"}, [j1, j2], "velocity")
expect(bad["hook"] != "He made $40M in a week" and any("hook" in n for n in bad["notes"]),
       f"a hook with a made-up number is replaced (now “{bad['hook']}”)")
expect(edits.numbers_in("$20M, twenty million, 20 million, 1,500 and 2.5k") ==
       [20e6, 20e6, 20e6, 1500.0, 2500.0], "numbers are read however they're written")
many = {"hook": "x", "moments": [{"video": 1, "start": 10.0, "end": 13.0, "text": ""}] +
        [{"video": 2, "start": float(t), "end": float(t) + 2, "text": "", "drop": t == 120} for t in range(40, 190, 10)]}
gm = edits.read_pick(many, [j1, j2], "aura")
expect(len(gm["moments"]) == 7 and any(m["drop"] for m in gm["moments"]), "trimmed to the style's 7, the drop kept")

print("== candidates instead of transcripts (Creator Scan)")
PROMPTS = []
ANSWER = {"hook": "Seven years to make it", "moments": [{"candidate": 2, "text": "SEVEN YEARS", "drop": True},
                                                        {"candidate": 1, "text": ""}]}


class FakeMessages:
    def create(self, **kw):
        PROMPTS.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=ANSWER)], stop_reason="tool_use")


highlights._client = lambda: SimpleNamespace(messages=FakeMessages())
cands = [{"source": j2["id"], "start": 5.0, "end": 7.0, "text": "Discipline beats talent.", "score": 80, "kind": "quote"},
         {"source": j1["id"], "start": 10.0, "end": 14.0, "hit": 11.2, "text": "seven years", "score": 92,
          "kind": "story"}]
gc = edits.pick_moments([j1, j2], "velocity", "his story", 20, "", candidates=cands)
prompt = PROMPTS[-1]["messages"][0]["content"]
expect("C1: VIDEO 2" in prompt and "Transcript" not in prompt, "the prompt lists the candidates, not transcripts")
expect(gc["moments"][0]["source"] == j1["id"] and gc["moments"][0]["start"] == 10.0 and
       gc["moments"][0]["hit"] == 11.2, "a candidate number becomes that moment, with its hit")
ANSWER = {"hook": "Discipline beats talent", "moments": [{"video": 2, "start": 5.0, "end": 7.0, "text": "me"}]}
gt = edits.pick_moments([j1, j2], "funny", "", 30, "Never portray TJR negatively.")
p2 = PROMPTS[-1]
expect("Transcript" in p2["messages"][0]["content"] and "Return 4 to 7 moments" in p2["messages"][0]["content"],
       "without candidates the transcripts go in, with the style's moment count")
expect("Never portray TJR negatively" in p2["system"], "campaign rules reach Claude")

print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed")
sys.exit(1 if FAILS else 0)
