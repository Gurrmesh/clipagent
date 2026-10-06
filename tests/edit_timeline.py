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

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
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
ms = moments(10, span=3.0, drop=2)
tl = edits.build_timeline(ms, "velocity", 20, S128, {}, WORDS, durations=DURS, hook="He turned 500 into 2 million")
FIT = {m["id"]: m for m in tl["moments"]}                         # how each moment was cut
segs = tl["segments"]
s0 = tl["music"]["start"]
expect(contiguous(tl), "segments follow each other with no gaps, up to the exact length")
expect(all(near_beat(s["at"] + s0, BEATS128) < EPS for s in segs), "every cut lands exactly on a beat")
expect(abs(tl["drop_at"] + s0 - 20.99) < EPS, "the drop is where the song drops")
expect(any(abs(s["at"] - tl["drop_at"]) < EPS for s in segs), "a cut lands exactly on the drop")
drop_seg = next(s for s in segs if s["drop"])
drop_m = next(m for m in ms if m["drop"])
expect(drop_seg["moment"] == drop_m["id"] and abs(drop_seg["src_start"] - FIT[drop_m["id"]]["hit"]) < EPS
       and abs(FIT[drop_m["id"]]["hit"] - drop_m["hit"]) < EPS, "the drop moment's hit is the frame on the drop")
expect(abs(tl["drop_at"] - 8 * P128) < EPS, "the song starts 8 beats before the drop")
bars = tl["length"] / (4 * P128)
expect(abs(bars - round(bars)) < 1e-3 and abs(tl["length"] - 20) <= 2 * P128 + EPS,
       f"ends on a bar line near the asked length ({tl['length']:.2f} s = {round(bars)} bars)")
expect(abs(tl["music"]["end"] - tl["music"]["start"] - tl["length"]) < EPS and tl["music"]["end"] <= 60,
       "the song section is exactly as long as the edit")
expect(all(s["dur"] >= P128 - EPS for s in segs), "no shot is shorter than a beat")
expect(segs[-1]["dur"] >= 2 * P128 - EPS, "the last shot gets at least two beats")
expect(all(1.5 - EPS <= m["shown"] <= 4.0 + EPS for m in tl["moments"]),
       f"every moment is on screen 1.5–4 s ({sorted(m['shown'] for m in tl['moments'])})")
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
ok_window = True
for m in tl["moments"]:
    run = [s for s in segs if s["moment"] == m["id"]]
    if not run:
        continue
    lo = run[0]["src_start"]
    hi = run[-1]["src_start"] + edits.curve_src(run[-1]["curve"], run[-1]["dur"])
    ok_window = lo >= m["start"] - edits.EXTEND - EPS and hi <= m["end"] + edits.EXTEND + EPS
    if not ok_window:
        break
expect(ok_window, "each moment's shots come from that moment, as cut (± a little picture)")
slow_hits = 0
for m in tl["moments"]:
    for s in segs:
        used = edits.curve_src(s["curve"], s["dur"])
        if s["moment"] == m["id"] and s["src_start"] - EPS <= m["hit"] <= s["src_start"] + used + EPS:
            t = edits.curve_time(s["curve"], s["dur"], m["hit"] - s["src_start"])
            slow_hits += edits.speed_at(s["curve"], t) <= 0.45
expect(slow_hits >= len(tl["moments"]) - 3, f"the hits play in slow motion ({slow_hits} of {len(tl['moments'])})")
texts = [s for s in segs if s["text"]]
expect(texts and all(s["at"] >= edits.HOOK_SECONDS - 0.05 or s["drop"] for s in texts),
       "punch words never cover the hook")
expect(tl["hook"]["text"] and tl["hook"]["end"] == edits.HOOK_SECONDS, "the hook is on screen for the first 2.8 s")
expect(tl["loop"] == edits.LOOP_SECONDS, "the ending loops")

print("== four moments before the drop: the song starts earlier so they keep their order")
t4b = edits.build_timeline(moments(10, span=3.0, drop=4), "velocity", 20, S128, {}, WORDS, durations=DURS)
order4 = []
for s in t4b["segments"]:
    if not order4 or order4[-1] != s["moment"]:
        order4.append(s["moment"])
expect(order4 == [f"m{i}" for i in range(1, 11) if f"m{i}" in order4] and order4[4] == "m5",
       f"Claude's order kept, the drop fifth ({order4})")
expect(abs(t4b["drop_at"] - 16 * P128) < EPS and abs(t4b["length"] - 20) <= 3,
       f"the build is 16 beats, the edit still {t4b['length']:.1f} s")
expect(all(1.5 - EPS <= m["shown"] <= 4.0 + EPS for m in t4b["moments"]), "…every moment still 1.5–4 s")

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
ta = edits.build_timeline(moments(5, span=4.0, drop=1), "aura", 15, S128, {}, WORDS, durations=DURS)
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
            for s, m in zip(segs, tc["moments"]))
expect(whole and [m["id"] for m in tc["moments"]] == ["c1", "c2", "c3"],
       "every moment plays whole as cut, every word of it")
sent = edits.word_source(WORDS["A"])
edges_ok = all(sent["sq"][next(k for k, w in enumerate(sent["ws"]) if w["start"] >= m["start"])] == 0
               and sent["eq"][max(k for k, w in enumerate(sent["ws"]) if w["end"] <= m["end"])] == 0
               for m in tc["moments"] if m["id"] != "c2")
expect(edges_ok, "each starts on a sentence start and ends on a sentence end")
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
expect(abs(tn["length"] - sum(m["end"] - m["start"] for m in tn["moments"]) - 0.35) < EPS,
       "length = the lines as cut + a short hold")
expect(abs(tn["length"] - 30) <= 0.15 * 30, f"…and within ±15 % of 30 s ({tn['length']:.1f} s)")
expect(next(s for s in tn["segments"] if s["drop"])["flashes"], "a flash on the strongest line")

print("== Funny: a punch and a shake on every punchline")
tfun = edits.build_timeline(sp, "funny", 30, None, {}, WORDS, durations=DURS)
expect(all(s["pulses"] == [s["hit"]] and s["shakes"] == [s["hit"]] for s in tfun["segments"]),
       "zoom punch + shake at each punchline")

print("== a speech edit too long for the asked length: moments shortened first, then the weakest left out")
long_sp = [dict(m, end=m["start"] + 12) for m in sp] + [
    {"id": "c4", "source": "B", "start": 200.0, "end": 212.0, "hit": 205.0, "text": "four", "drop": False}]
tl4 = edits.build_timeline(long_sp, "cinematic", 25, S128, {}, WORDS, durations=DURS)
ids = [s["moment"] for s in tl4["segments"]]
expect(ids == ["c1", "c2", "c3", "c4"] and abs(tl4["length"] - 25) <= 0.15 * 25,
       f"all four kept, shortened: {tl4['length']:.1f} s for 25 s asked")
many_sp = [{"id": f"x{k}", "source": "AB"[k % 2], "start": 30.0 + 40 * k, "end": 42.0 + 40 * k, "hit": 36.0 + 40 * k,
            "text": "", "drop": k == 2, "strength": 9 if k == 1 else 3} for k in range(7)]
tl5 = edits.build_timeline(many_sp, "cinematic", 20, S128, {}, WORDS, durations=DURS)
kept5 = [m["id"] for m in tl5["moments"]]
expect("x2" in kept5 and "x1" in kept5 and len(kept5) < 7 and abs(tl5["length"] - 20) <= 3.0 + EPS,
       f"seven 12 s moments in a 20 s edit: kept {kept5} ({tl5['length']:.1f} s), the drop and the strongest stay")
expect(len(tl5["left_out"]) == 7 - len(kept5) and sum("Left out" in n for n in tl5["notes"]) == len(tl5["left_out"]),
       "each one left out says why")

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
fresh = moments(8, drop=3)
re = [fresh[5], fresh[0], fresh[2], fresh[3], fresh[4], fresh[1], fresh[6], fresh[7]]
t6 = edits.build_timeline(re, "velocity", 20, S128, {}, WORDS, durations=DURS)
seen = []
for s in t6["segments"]:
    if s["moment"] not in seen:
        seen.append(s["moment"])
expect(seen == ["m6", "m1", "m3", "m4", "m5", "m2", "m7", "m8"], f"a new order plays in the new order ({seen})")
few = edits.build_timeline(re[:5], "velocity", 20, S128, {}, WORDS, durations=DURS)
expect(abs(few["length"] - 20) <= 0.15 * 20 + EPS and few["notes"],
       f"only five moments for 20 s: still {few['length']:.1f} s, and it says what it did")
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

# --- moment lengths: every style's window, word boundaries, the length budget -----------------
import bisect  # noqa: E402
import random  # noqa: E402

VOCAB = ("so I told him we are going to win this thing and he laughed at me but look at us now bro the market "
         "opened red and I stayed calm because the plan said wait").split()


def talk(seconds, seed):
    """Made-up speech: sentences of 4-14 words, capitalised, ending in . ! or ?, a few commas, pauses between."""
    rng = random.Random(seed)
    words, t = [], 1.0
    while t < seconds - 3:
        n = rng.randint(4, 14)
        for i in range(n):
            w = rng.choice(VOCAB)
            w = w.capitalize() if i == 0 else w
            w += rng.choice([".", "!", "?"]) if i == n - 1 else ("," if rng.random() < 0.12 else "")
            d = rng.uniform(0.18, 0.4)
            words.append({"w": w, "start": round(t, 3), "end": round(t + d, 3)})
            t += d + rng.uniform(0.02, 0.12)
        t += rng.uniform(0.3, 0.9)
    return words


TALK = {"A": talk(1200, 1), "B": talk(1200, 2)}
TDUR = {"A": 1200.0, "B": 1200.0}
SRC = {k: edits.word_source(v) for k, v in TALK.items()}


def long_moments(n, seed=0):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        span = rng.uniform(12, 28)
        s = 20 + i * 28.0
        out.append({"id": f"m{i + 1}", "source": "AB"[i % 2], "start": round(s, 2), "end": round(s + span, 2),
                    "hit": round(s + span * rng.uniform(0.3, 0.9), 2), "text": "", "drop": i == n // 3,
                    "strength": rng.randint(3, 9)})
    return out


def inside_word(src, t):
    """True when second t falls inside a word (a cut there would chop it)."""
    k = bisect.bisect_right(src["starts"], t) - 1
    return k >= 0 and src["ws"][k]["start"] + 0.03 < t < src["ws"][k]["end"] - 0.03


def first_word(src, t):
    return bisect.bisect_left(src["starts"], t - 0.001)


def last_word(src, t):
    return max(k for k in range(max(0, bisect.bisect_right(src["starts"], t) - 60), len(src["ws"]))
               if src["ws"][k]["end"] <= t + 0.001)


print("== every style × 15/20/30/60 s with long moments (12–28 s each)")
S96 = song(96, 30.0, duration=120.0)
S128L = song(128, 30.0, duration=120.0)
for style, st in edits.STYLES.items():
    lo_w, hi_w = st["window"]
    for L in (15, 20, 30, 60):
        for snd in ((S128L, S96) if st["pace"] == "beat" else (S128L, None)):
            ms = long_moments(edits.count_for(style, L)[1], seed=L)
            t = edits.build_timeline(ms, style, L, snd, {}, TALK, durations=TDUR, hook="Hook")
            tag = f"{style} {L} s {'%d BPM' % snd['analysis']['bpm'] if snd else 'no music'}"
            within = abs(t["length"] - L) <= 0.15 * L + EPS
            if st["pace"] == "speech":
                lens = [m["end"] - m["start"] for m in t["moments"]]
            else:
                lens = [m["shown"] for m in t["moments"]]
            in_win = all(lo_w - 0.06 <= x <= hi_w + 0.06 for x in lens)
            edges = all(not inside_word(SRC[m["source"]], m["start"]) and not inside_word(SRC[m["source"]], m["end"])
                        for m in t["moments"])
            whole = True
            if st["pace"] == "speech":
                for m in t["moments"]:
                    src = SRC[m["source"]]
                    i, j = first_word(src, m["start"]), last_word(src, m["end"])
                    whole = whole and src["sq"][i] <= 1 and src["eq"][j] <= 1
                segs_ok = all(abs(s["voice"][1] - s["voice"][0] - (m["end"] - m["start"])) < EPS
                              for s, m in zip(t["segments"], t["moments"]))
                whole = whole and segs_ok
            hits = all(m["start"] - EPS <= m["hit"] <= m["end"] + EPS for m in t["moments"])
            told = len(t["left_out"]) == len(ms) - len(t["moments"]) and \
                sum("Left out" in n for n in t["notes"]) >= len(t["left_out"])
            expect(within and in_win and edges and whole and hits and told,
                   f"{tag}: {t['length']:.1f} s, {len(t['moments'])}/{len(ms)} moments of "
                   f"{min(lens):.1f}–{max(lens):.1f} s (window {lo_w:g}–{hi_w:g}), clean edges"
                   + ("" if within else " [LENGTH]") + ("" if in_win else " [WINDOW]") + ("" if edges else " [EDGE]")
                   + ("" if whole else " [THOUGHT]") + ("" if hits else " [HIT]") + ("" if told else " [NOTE]"))

print("== pace faster / slower keeps the windows")
for style in ("velocity", "flow", "money", "aura"):
    lo_w, hi_w = edits.STYLES[style]["window"]
    for pace in ("faster", "slower"):
        t = edits.build_timeline(long_moments(edits.count_for(style, 30)[1], 3), style, 30, S128L, {}, TALK,
                                 durations=TDUR, pace=pace)
        expect(all(lo_w - 0.06 <= m["shown"] <= hi_w + 0.06 for m in t["moments"]) and abs(t["length"] - 30) <= 4.5,
               f"{style} {pace}: {t['length']:.1f} s, moments {min(m['shown'] for m in t['moments']):.1f}–"
               f"{max(m['shown'] for m in t['moments']):.1f} s")

print("== a moment too short to cut cleanly is left out, with a note")
tiny_words = [{"w": w, "start": 0.2 + i * 0.3, "end": 0.45 + i * 0.3} for i, w in enumerate("No way bro that's crazy.".split())]
tiny = [{"id": "t1", "source": "T", "start": 0.3, "end": 1.4, "hit": 1.0, "text": "", "drop": False},
        {"id": "t2", "source": "A", "start": 100.0, "end": 104.0, "hit": 102.0, "text": "", "drop": True}]
tt = edits.build_timeline(tiny, "funny", 15, None, {}, {"T": tiny_words, "A": TALK["A"]},
                          durations={"T": 2.0, "A": 1200.0})
expect([x["id"] for x in tt["left_out"]] == ["t1"] and any("Left out" in n and "3 s" in n for n in tt["notes"]),
       f"a 1.1 s quip in a 2 s video can't make Funny's 3 s: “{next((n for n in tt['notes'] if 'Left out' in n), '')}”")
expect(abs(tt["length"] - 15) > 0.15 * 15 and any("less than the 15 s" in n for n in tt["notes"]),
       "one moment can't fill 15 s — and it says so instead of pretending")
try:
    edits.build_timeline(tiny[:1], "funny", 15, None, {}, {"T": tiny_words}, durations={"T": 2.0})
    expect(False, "nothing left to play is refused")
except ValueError as exc:
    expect("cut cleanly" in str(exc), f"nothing left: “{exc}”")

print("== no words: kept, cut around the hit to the window")
nw = [{"id": f"n{i}", "source": "S", "start": 5.0 + 20 * i, "end": 25.0 + 20 * i, "hit": 12.0 + 20 * i, "text": "",
       "drop": i == 1} for i in range(6)]
tnw = edits.build_timeline(nw, "funny", 30, None, {}, {}, durations={"S": 200.0})
expect(all(3.0 - EPS <= m["end"] - m["start"] <= 8.0 + EPS and m["start"] <= m["hit"] <= m["end"]
           for m in tnw["moments"]) and abs(tnw["length"] - 30) <= 4.5,
       f"Funny with no transcript: {len(tnw['moments'])} moments of 3–8 s, {tnw['length']:.1f} s")
tnb = edits.build_timeline(nw, "velocity", 15, S128L, {}, {}, durations={"S": 200.0})
expect(all(1.5 - EPS <= m["shown"] <= 4.0 + EPS for m in tnb["moments"]), "Velocity with no transcript: 1.5–4 s each")

print("== the key line stays, and a line longer than the window ends on its punchline")
kl = [{"w": w, "start": 50.0 + i * 0.4, "end": 50.3 + i * 0.4} for i, w in enumerate(
      "Listen. Most people quit too early, they never see it work. Discipline is the whole game. "
      "Then we went to get food and talked about cars for an hour.".split())]
line_m = [{"id": "k1", "source": "K", "start": 49.0, "end": 72.0, "hit": 60.0, "text": "Discipline is the whole game",
           "key": "game", "drop": True}]
tk = edits.build_timeline(line_m, "motivation", 15, None, {}, {"K": kl}, durations={"K": 120.0})
k = tk["moments"][0]
said = [w["w"] for w in kl if k["start"] - 0.01 <= w["start"] and w["end"] <= k["end"] + 0.01]
expect("Discipline" in said and "game." in said and "cars" not in said and 4.0 - EPS <= k["end"] - k["start"] <= 10 + EPS,
       f"Motivation keeps “Discipline is the whole game.” and stops there: {' '.join(said)}")
long_line = [{"w": w, "start": 10.0 + i * 0.45, "end": 10.35 + i * 0.45} for i, w in enumerate(
    ("Every single morning I wake up, I check the plan, I check my risk, I sit on my hands, I wait for the "
     "setup, and when it finally comes I take it without fear.").split())]
ll = [{"id": "l1", "source": "L", "start": 9.5, "end": 24.0, "hit": 23.5, "text": "", "key": "fear", "drop": True}]
tll = edits.build_timeline(ll, "motivation", 15, None, {}, {"L": long_line}, durations={"L": 40.0})
c = tll["moments"][0]
lsrc = edits.word_source(long_line)
fi = first_word(lsrc, c["start"])
expect(c["end"] - c["start"] <= 10 + EPS and [w["w"] for w in long_line if w["end"] <= c["end"] + 0.01][-1] == "fear."
       and lsrc["sq"][fi] <= 1, f"a 14 s sentence: the last {c['end'] - c['start']:.1f} s, from a comma to "
                                f"“fear.” (starts on “{long_line[fi]['w']}”)")

print("== sizes set by hand win over the window; the edit still keeps its length")
base = long_moments(6, 11)
cut = edits.build_timeline(base, "funny", 30, None, {}, TALK, durations=TDUR)
first = next(m for m in cut["moments"] if m["id"] == "m2")
m2 = dict(base[1], start=first["start"], end=first["end"], hit=first["hit"], pick=first["pick"])
longer, note = edits.resize_moment(m2, {"seconds": 11}, "funny", TALK["B"], 1200.0)
expect(longer and longer["manual"] and 10.0 <= longer["end"] - longer["start"] <= 12.6 and
       longer["start"] <= m2["start"] + EPS and longer["end"] >= m2["end"] - EPS and "because you asked" in note,
       f"“make it 11 seconds”: {note}")
expect(not inside_word(SRC["B"], longer["start"]) and not inside_word(SRC["B"], longer["end"]), "…on word boundaries")
withm = [longer if m["id"] == "m2" else m for m in base]
tw = edits.build_timeline(withm, "funny", 30, None, {}, TALK, durations=TDUR)
mm = next(m for m in tw["moments"] if m["id"] == "m2")
expect(abs((mm["end"] - mm["start"]) - (longer["end"] - longer["start"])) < 0.02 and abs(tw["length"] - 30) <= 4.5
       and any("because you asked" in n for n in tw["notes"]),
       f"the 11 s moment plays as set, the edit is still {tw['length']:.1f} s, and it says why")
shorter, note = edits.resize_moment(m2, {"size": "shorter"}, "funny", TALK["B"], 1200.0)
expect(shorter and shorter["end"] - shorter["start"] < first["end"] - first["start"] - 0.5 and
       shorter["start"] - EPS <= shorter["hit"] <= shorter["end"] + EPS and
       m2["start"] - EPS <= shorter["start"] and shorter["end"] <= m2["end"] + EPS,
       f"“shorter”: {note}")
trimmed, note = edits.resize_moment(m2, {"trim_start": 2}, "funny", TALK["B"], 1200.0)
expect(trimmed and 1.0 <= trimmed["start"] - m2["start"] <= 3.2 and abs(trimmed["end"] - m2["end"]) < EPS
       and not inside_word(SRC["B"], trimmed["start"]), f"“cut the first 2 seconds”: {note}")
lets = [{"w": w, "start": 5.0 + i * 0.4, "end": 5.3 + i * 0.4} for i, w in enumerate(
    "Alright we are live. Let's go! Today we trade the open and we keep it simple.".split())]
lm = {"id": "g1", "source": "G", "start": 4.9, "end": 11.0, "hit": 6.0, "text": "", "drop": True}
ended, note = edits.resize_moment(lm, {"end_words": "let's go"}, "funny", lets, 60.0)
expect(ended and lets[5]["end"] - 0.01 <= ended["end"] < lets[6]["start"] and ended["hit"] <= ended["end"],
       f"“end right after he says let's go” (before “Today”): {note}")
nope, why = edits.resize_moment(lm, {"end_words": "to the moon"}, "funny", lets, 60.0)
expect(nope is None and "Couldn't find" in why, f"words he never says there: “{why}”")
nope, why = edits.resize_moment(lm, {"trim_start": 30}, "funny", lets, 60.0)
expect(nope is None and why, f"cutting more than the moment: “{why}”")

print("== a campaign's own length limits")
camp = edits.build_timeline(long_moments(7, 5), "funny", 30, None, {}, TALK, durations=TDUR,
                            limits={"max": 20, "name": "TJR — Reach"})
expect(camp["length"] <= 20 + EPS and any("TJR — Reach allows at most 20 s" in n for n in camp["notes"]),
       f"asked 30 s, the brief allows 20 s: {camp['length']:.1f} s, and it says why")
campb = edits.build_timeline(long_moments(12, 5), "velocity", 30, S128L, {}, TALK, durations=TDUR,
                             limits={"max": 20, "name": "TJR — Reach"})
expect(campb["length"] <= 20 + EPS, f"…a beat edit too ({campb['length']:.1f} s)")
tiny_camp = edits.build_timeline(long_moments(7, 5), "funny", 30, None, {}, TALK, durations=TDUR,
                                 limits={"max": 5, "name": "Short brand"})
expect(any("block" in n for n in tiny_camp["notes"]), "a brief allowing only 5 s: it says the check will block it")
mincamp = edits.build_timeline(long_moments(7, 5), "funny", 15, None, {}, TALK, durations=TDUR,
                               limits={"min": 25, "name": "Long brand"})
expect(mincamp["length"] >= 25 - EPS, f"a brief asking for at least 25 s: {mincamp['length']:.1f} s")

print("== a song that drops in its first second")
early_s = song(128, 0.9, duration=60.0)
te = edits.build_timeline(long_moments(9, 2), "velocity", 20, early_s, {}, TALK, durations=TDUR)
expect(all(1.5 - EPS <= m["shown"] <= 4.0 + EPS for m in te["moments"]) and abs(te["length"] - 20) <= 3 and
       abs(te["drop_at"] + te["music"]["start"] - early_s["analysis"]["drop"]) < EPS,
       f"the windows hold and the drop still lands ({te['length']:.1f} s)")

print("== moment counts and what Claude is told")
expect(edits.count_for("funny", 30) == (4, 7) and edits.count_for("motivation", 60)[0] >= 6 and
       edits.count_for("velocity", 60)[0] >= 15, f"counts scale with the length {edits.count_for('motivation', 60)}")

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
