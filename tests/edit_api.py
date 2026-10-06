"""The Edit Maker through the real API — offline: Claude is stood in for, the rendering is real.

Run: python tests/edit_api.py
Makes a short video and a test song here (nothing downloaded), then: songs
(add, list, play, refuse junk, take a video's sound), styles, footage, a
Velocity edit made end to end (Telegram gets it), re-make without Claude,
undo, typed changes, versions with another song, the campaign rules (music
and joining moments refused in plain words, speed changes switched off,
#hashtags added, a too-long edit blocked from download), Telegram /edit,
delete, and edits cut off by a restart. Also moment lengths: a Funny edit's
long picks cut to 3–8 s, and typed / by-hand size changes (shorter, longer,
trim the start, end on his words, the whole length) that stay within the
length budget and can be undone.
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
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="editapi_")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("WHISPER_API_KEY", "x")
os.environ["TELEGRAM_BOT_TOKEN"] = "1:x"
os.environ["TELEGRAM_CHAT_ID"] = "42"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import campaign, editrender, edits, highlights, main, notify, store  # noqa: E402

FAILS = []
DATA = Path(os.environ["DATA_DIR"])


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def drain():
    out = []
    while not notify._out.empty():
        out.append(notify._out.get())
    return out


def wait(eid, seconds=240):
    end = time.time() + seconds
    while time.time() < end:
        e = store.get_edit(eid)
        if e and e["status"] in ("done", "failed"):
            return e
        time.sleep(0.3)
    return store.get_edit(eid)


# --- media and a finished video -----------------------------------------------------------
store.init()
song_path = DATA / "beat128.mp3"
subprocess.run([sys.executable, str(ROOT / "tools" / "make_test_song.py"), "128", "40", "10.365", "0.365",
                str(song_path)], check=True)
song2_path = DATA / "beat100.mp3"
subprocess.run([sys.executable, str(ROOT / "tools" / "make_test_song.py"), "100", "40", "9.6", "0.0",
                str(song2_path)], check=True)
src = DATA / "sources" / "talk" / "source.mp4"
src.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=960x540:r=30:d=60", "-f", "lavfi",
                "-i", "sine=f=200:d=60:sample_rate=48000", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                "-c:a", "aac", "-shortest", str(src)], check=True)
words, segments = [], []
for k in range(14):
    t0 = 2.0 + k * 4.0
    line = ("I made 2 million dollars in seven years" if k == 5 else f"this is line number {k} of the talk").split()
    for i, w in enumerate(line):
        words.append({"w": w, "start": round(t0 + i * 0.35, 3), "end": round(t0 + i * 0.35 + 0.3, 3)})
    segments.append({"start": t0, "end": t0 + len(line) * 0.35, "text": " ".join(line)})
job_id = store.create_job("TJR talks money", "upload", {})
store.update_job(job_id, status="done", duration=60.0, source_path=str(src),
                 transcript=json.dumps({"words": words, "segments": segments}))

# --- stand-ins: Claude answers by tool; the campaign tone check says ok --------------------
ANSWERS = {}
PROMPTS = []


class FakeMessages:
    def create(self, **kw):
        PROMPTS.append(kw)
        name = kw["tool_choice"]["name"]
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=ANSWERS.get(name, {}))],
                               stop_reason="tool_use")


highlights._client = lambda: SimpleNamespace(messages=FakeMessages())
campaign.check_text = lambda rb, posts: {p["id"]: {"status": "ok", "reason": "fine"} for p in posts}
ANSWERS["pick_moments"] = {
    "title": "Seven years", "hook": "He made 2 million in seven years", "caption": "Seven years of work.",
    "hashtags": ["trading", "money"],
    "moments": [{"video": 1, "start": 2.0 + k * 4, "end": 4.8 + k * 4, "hit": 3.2 + k * 4,
                 "text": "SEVEN YEARS" if k == 5 else "", "drop": k == 5} for k in range(10)]}
client = TestClient(main.app)

# --- songs ----------------------------------------------------------------------------------
print("== songs")
r = client.post("/api/sounds", files={"file": ("beat128.mp3", song_path.read_bytes(), "audio/mpeg")},
                data={"name": "Montagem test"})
s1 = r.json().get("sound") or {}
expect(r.status_code == 200 and abs((s1.get("bpm") or 0) - 128) < 2 and s1.get("drop"),
       f"a song is added and its beats read ({s1.get('bpm')} BPM, drop {s1.get('drop')} s)")
r = client.post("/api/sounds", files={"file": ("notes.txt", b"hello", "text/plain")})
expect(r.status_code == 400 and "MP3" in r.json()["detail"], f"a text file is refused: “{r.json()['detail']}”")
vid = DATA / "withsong.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=320x240:d=30", "-i",
                str(song2_path), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(vid)],
               check=True)
r = client.post("/api/sounds", files={"file": ("my reel.mp4", vid.read_bytes(), "video/mp4")})
s2 = r.json().get("sound") or {}
expect(r.status_code == 200 and abs((s2.get("bpm") or 0) - 100) < 2, "a video's sound becomes a song")
expect(store.get_sound(s2["id"])["file"].endswith(".m4a"), "only the sound of the video is kept")
lst = client.get("/api/sounds").json()
expect(len(lst["sounds"]) == 2 and "allowed to use" in lst["note"], "songs listed, with the rights note")
expect(client.get(f"/media/sound/{s1['id']}").status_code == 200, "a song plays")

print("== styles and footage")
st = client.get("/api/edit-styles").json()
expect([x["id"] for x in st["styles"]] == ["velocity", "aura", "flow", "cinematic", "motivation", "funny", "money"],
       "the seven styles")
expect("loop" in st["effects"] and "mono" in st["grades"] and st["paces"] == ["slower", "normal", "faster"],
       "effects, colour looks and paces offered")
srcs = client.get("/api/edit-sources").json()["sources"]
expect(len(srcs) == 1 and srcs[0]["id"] == job_id, "the finished video is footage for edits")

# --- a Velocity edit, end to end -------------------------------------------------------------
print("== a Velocity edit")
r = client.post("/api/edits", json={"style": "velocity", "sources": [job_id], "length": 15})
expect(r.status_code == 400 and "cut to music" in r.json()["detail"], f"no song: “{r.json()['detail']}”")
r = client.post("/api/edits", json={"style": "velocity", "sources": [], "sound": s1["id"]})
expect(r.status_code == 400 and "Pick at least one video" in r.json()["detail"], "no footage: plain words")
drain()
r = client.post("/api/edits", json={"style": "velocity", "sources": [job_id], "sound": s1["id"], "length": 10,
                                    "theme": "his money story"})
eid = r.json()["id"]
e = wait(eid)
j = client.get(f"/api/edits/{eid}").json()
expect(j["status"] == "done" and j["video_url"] and j["thumb_url"], f"made ({j['stage']}, {j.get('error')})")
expect(j["hook"] == "He made 2 million in seven years" and len(j["moments"]) == edits.count_for("velocity", 10)[1],
       f"the hook kept, and as many moments as a 10 s edit can use ({len(j['moments'])})")
expect("his money story" in PROMPTS[-1]["messages"][0]["content"], "the theme reached Claude")
expect("every moment plays 1.5 to 4 seconds" in PROMPTS[-1]["messages"][0]["content"] and
       "fill about 10 seconds" in PROMPTS[-1]["messages"][0]["content"], "Claude is told the moment length and the budget")
expect(all(1.5 - 0.01 <= m["length"] <= 4.0 + 0.01 for m in j["moments"] if not m.get("left_out")),
       f"each moment is on screen 1.5–4 s ({[m['length'] for m in j['moments']]})")
expect(client.get(j["video_url"]).status_code == 200 and client.get(j["thumb_url"]).status_code == 200,
       "the video and its poster are served")
expect(client.get(j["moments"][0]["thumb"]).status_code == 200, "each moment has a picture")
d = client.get(f"/api/edits/{eid}/download")
expect(d.status_code == 200 and "attachment" in d.headers.get("content-disposition", ""), "downloads")
sent = drain()
expect(any(m["kind"] == "video" and "Seven years" in m["caption"] for m in sent), "Telegram gets the finished edit")
expect("#trading" in j["post_text"] and j["caption"] == "Seven years of work.", "the caption to paste")

# From here on a stand-in draws a small video of the right length (the renderer has its own test).


def quick_render(timeline, sources, sound, out, thumb, progress=None):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=gray:s=108x192:d={timeline['length']}",
                    "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo", "-t", str(timeline["length"]),
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(out), "-frames:v", "1", str(thumb)], check=True)
    return {"length": timeline["length"]}


editrender.render = quick_render

print("== re-make without Claude, and undo")
calls = len(PROMPTS)
first_cuts = len(j["cuts"])
r = client.post(f"/api/edits/{eid}/remake", json={"pace": "faster", "effects": {"glitch": False}})
e = wait(eid)
j2 = client.get(f"/api/edits/{eid}").json()
expect(j2["status"] == "done" and len(PROMPTS) == calls, "re-made without asking Claude again")
expect(len(j2["cuts"]) > first_cuts and j2["pace"] == "faster" and not j2["effects"]["glitch"],
       f"faster: {first_cuts} → {len(j2['cuts'])} cuts, glitch off")
expect(j2["can_undo"], "undo is offered")
r = client.post(f"/api/edits/{eid}/undo")
j3 = r.json()
expect(r.status_code == 200 and j3["pace"] == "normal" and len(j3["cuts"]) == first_cuts and j3["video_url"],
       "undo brings back the version before")
expect(client.post(f"/api/edits/{eid}/undo").status_code == 400, "nothing more to undo: plain words")
moments = j3["moments"]
new_order = [{"id": m["id"], "off": m["id"] == "m2"} for m in reversed(moments)]
r = client.post(f"/api/edits/{eid}/remake", json={"moments": new_order})
wait(eid)
j4 = client.get(f"/api/edits/{eid}").json()
expect(j4["moments"][0]["id"] == moments[-1]["id"] and next(m for m in j4["moments"] if m["id"] == "m2")["off"],
       "moments reordered and one switched off")
r = client.post(f"/api/edits/{eid}/remake", json={"moments": [{"id": m["id"], "off": True} for m in moments]})
expect(r.status_code == 400 and "switched off" in r.json()["detail"], "all moments off: plain words")

r = client.post(f"/api/edits/{eid}/remake", json={"song_start": 25.0})
wait(eid)
jp = client.get(f"/api/edits/{eid}").json()
expect(jp["song_start"] == 25.0 and jp["song_part"] and abs(jp["song_part"][0] - 25.0) < 2.0,
       f"a part of the song picked by hand ({jp['song_part'][0]:.1f} s)")
client.post(f"/api/edits/{eid}/remake", json={"song_start": None})
wait(eid)
expect(client.get(f"/api/edits/{eid}").json()["song_start"] is None, "…and back to the automatic part")

print("== typed changes")
ANSWERS["change_edit"] = {"understood": "Black and white, with a flash on every cut.", "grade": "mono",
                          "flashes": "many"}
r = client.post(f"/api/edits/{eid}/ask", json={"text": "make it black and white with more flashes"})
rep = r.json()
expect(r.status_code == 200 and rep["reply"]["changed"] and "Black and white" in rep["reply"]["understood"],
       "a typed change is understood")
wait(eid)
j5 = client.get(f"/api/edits/{eid}").json()
expect(j5["grade"] == "mono" and j5["flashes"] == "many" and j5["asks"], "…and made, and remembered")
prompt = PROMPTS[-1]["messages"][0]["content"]
expect("I made 2 million dollars" in prompt and "Montagem test" in prompt,
       "Claude sees what each moment says and the song names")
ANSWERS["change_edit"] = {"understood": "", "question": "Which line do you mean?"}
r = client.post(f"/api/edits/{eid}/ask", json={"text": "put that line on the drop"})
expect(not r.json()["reply"]["changed"] and r.json()["reply"]["question"], "a question changes nothing")
ANSWERS["change_edit"] = {"understood": "New hook.", "hook": "He made 40 million in a week"}
r = client.post(f"/api/edits/{eid}/ask", json={"text": "change the hook"})
expect(r.json()["reply"]["cant"] and not r.json()["reply"]["changed"], "a hook with a made-up number is refused")
target = next(m["id"] for m in j5["moments"] if abs(m["start"] - 22.0) < 0.5)
ANSWERS["change_edit"] = {"understood": "The 2 million line goes on the drop.",
                          "moments": [{"id": m["id"], "drop": m["id"] == target} for m in j5["moments"]]}
client.post(f"/api/edits/{eid}/ask", json={"text": "put the 2 million line on the drop"})
wait(eid)
j6 = client.get(f"/api/edits/{eid}").json()
expect(next(m for m in j6["moments"] if m.get("drop"))["id"] == target, "the line asked for is on the drop")
ANSWERS["change_edit"] = {"understood": "Using your other song.", "song": "my reel"}
client.post(f"/api/edits/{eid}/ask", json={"text": "use a different song"})
wait(eid)
expect(client.get(f"/api/edits/{eid}").json()["sound"]["id"] == s2["id"], "a different song by name")

print("== versions with other songs")
calls = len(PROMPTS)
r = client.post(f"/api/edits/{eid}/versions", json={"sounds": [s1["id"]]})
ids = r.json().get("ids") or []
v = wait(ids[0]) if ids else None
expect(len(ids) == 1 and v and v["status"] == "done" and v["settings"]["sound"] == s1["id"],
       "a version cut to the other song")
expect(len(PROMPTS) == calls and "·" in v["title"], "same moments, no new Claude call, named after the song")

# --- moment lengths: a Funny edit from long moments, then typed and by-hand size changes ------------
print("== a Funny edit from long moments")
LINES = {k: 2.0 + k * 4.0 for k in range(14)}                  # "this is line number k of the talk", 2.8 s each


def word_cut(t):
    """True when second t falls inside a spoken word."""
    return any(w["start"] + 0.03 < t < w["end"] - 0.03 for w in words)


ANSWERS["pick_moments"] = {
    "title": "Funniest bits", "hook": "He made 2 million in seven years", "caption": "Ha.", "hashtags": ["funny"],
    "moments": [{"video": 1, "start": 2.0, "end": 13.0, "hit": 7.0, "text": "bro", "drop": False},
                {"video": 1, "start": 13.5, "end": 26.0, "hit": 23.0, "text": "the 2 million", "drop": True},
                {"video": 1, "start": 26.0, "end": 38.0, "hit": 31.0, "text": "", "drop": False},
                {"video": 1, "start": 38.0, "end": 48.0, "hit": 43.0, "text": "", "drop": False},
                {"video": 1, "start": 48.0, "end": 58.0, "hit": 52.0, "text": "", "drop": False}]}
r = client.post("/api/edits", json={"style": "funny", "sources": [job_id], "sound": "", "length": 20})
fid = r.json()["id"]
wait(fid)
fj = client.get(f"/api/edits/{fid}").json()
fm = [m for m in fj["moments"] if not m.get("left_out")]
expect(fj["status"] == "done" and all(3.0 - 0.06 <= m["end"] - m["start"] <= 8.0 + 0.06 for m in fm),
       f"10–12.5 s picks cut to Funny's 3–8 s ({[round(m['end'] - m['start'], 1) for m in fm]})")
expect(abs(fj["length"] - 20) <= 3.0, f"the edit is {fj['length']:.1f} s for 20 s asked")
expect(not any(word_cut(m["start"]) or word_cut(m["end"]) for m in fm), "every cut between words")
expect(any("down to the punchline" in n for n in fj["notes"]), "it says the long moments were cut down")
expect("every moment plays 3 to 8 seconds" in PROMPTS[-1]["messages"][0]["content"], "Claude was told 3–8 s")

print("== typed size changes")
before = {m["id"]: m for m in fj["moments"]}
second = fj["moments"][1]["id"]
ANSWERS["change_edit"] = {"understood": "The second moment gets shorter.", "resize": [{"id": second, "size": "shorter"}]}
r = client.post(f"/api/edits/{fid}/ask", json={"text": "make the second moment shorter"})
rep = r.json()["reply"]
wait(fid)
fj2 = client.get(f"/api/edits/{fid}").json()
m2 = next(m for m in fj2["moments"] if m["id"] == second)
old = before[second]
expect(rep["changed"] and m2["end"] - m2["start"] < old["end"] - old["start"] - 0.5 and m2.get("manual"),
       f"“make the second moment shorter”: {old['end'] - old['start']:.1f} → {m2['end'] - m2['start']:.1f} s")
expect(not word_cut(m2["start"]) and not word_cut(m2["end"]) and m2["start"] <= m2["hit"] <= m2["end"],
       "…cut between words, the punchline kept")
expect(abs(fj2["length"] - 20) <= 3.0, f"…and the edit still {fj2['length']:.1f} s")
expect("Claude" not in " ".join(rep.get("done") or []) and rep.get("done"), f"it says what it did: {rep.get('done')}")
prompt = PROMPTS[-1]["messages"][0]["content"]
expect("1. m1" in prompt and "2. " + second in prompt and "usually plays 3–8 s" in prompt,
       "Claude sees the moments numbered, with their lengths and the style's usual length")

third = fj2["moments"][2]["id"]
old3 = next(m for m in fj2["moments"] if m["id"] == third)
ANSWERS["change_edit"] = {"understood": "Cutting the first 2 seconds of moment 3.",
                          "resize": [{"id": third, "trim_start": 2}]}
client.post(f"/api/edits/{fid}/ask", json={"text": "cut the first 2 seconds of moment 3"})
wait(fid)
fj3 = client.get(f"/api/edits/{fid}").json()
m3 = next(m for m in fj3["moments"] if m["id"] == third)
expect(1.0 <= m3["start"] - old3["start"] <= 3.2 and abs(m3["end"] - old3["end"]) < 0.05 and not word_cut(m3["start"]),
       f"“cut the first 2 seconds of moment 3”: starts {m3['start'] - old3['start']:.1f} s later, on a word")

first = fj3["moments"][0]
old1 = first
k_line = next(k for k, t0 in LINES.items() if t0 >= first["start"] - 0.2)
ANSWERS["change_edit"] = {"understood": "The first moment ends after that line.",
                          "resize": [{"id": first["id"], "end_words": f"line number {k_line}"}]}
client.post(f"/api/edits/{fid}/ask", json={"text": f"end the first moment right after he says 'line number {k_line}'"})
wait(fid)
fj4 = client.get(f"/api/edits/{fid}").json()
m1 = next(m for m in fj4["moments"] if m["id"] == first["id"])
said_end = LINES[k_line] + 4 * 0.35 + 0.3                       # "…line number k" ends here
expect(said_end - 0.01 <= m1["end"] <= said_end + 0.25 and not word_cut(m1["end"]),
       f"“end right after he says 'line number {k_line}'”: ends at {m1['end']:.2f} s (the word ends {said_end:.2f})")
expect(any("because you asked" in n for n in fj4["notes"]) or 3.0 <= m1["end"] - m1["start"],
       "a moment cut shorter than Funny's 3 s says it's because you asked")

ANSWERS["change_edit"] = {"understood": "The funny one runs longer.", "resize": [{"id": second, "size": "longer"}]}
client.post(f"/api/edits/{fid}/ask", json={"text": "let the funny one run longer"})
wait(fid)
fj5 = client.get(f"/api/edits/{fid}").json()
m2b = next(m for m in fj5["moments"] if m["id"] == second)
m2a = next(m for m in fj4["moments"] if m["id"] == second)
expect(m2b["end"] - m2b["start"] > m2a["end"] - m2a["start"] + 0.5 and m2b["start"] <= m2a["start"] + 0.01
       and m2b["end"] >= m2a["end"] - 0.01, f"“let the funny one run longer”: {m2a['end'] - m2a['start']:.1f} → "
                                            f"{m2b['end'] - m2b['start']:.1f} s, around what it was")
expect(abs(fj5["length"] - 20) <= 3.0, f"…the edit still {fj5['length']:.1f} s")

r = client.post(f"/api/edits/{fid}/undo")
ju = r.json()
mu = next(m for m in ju["moments"] if m["id"] == second)
expect(r.status_code == 200 and abs((mu["end"] - mu["start"]) - (m2a["end"] - m2a["start"])) < 0.02,
       "undo puts the moment back as it was")

ANSWERS["change_edit"] = {"understood": "15 seconds it is.", "length": 15}
client.post(f"/api/edits/{fid}/ask", json={"text": "make the whole thing 15 seconds"})
wait(fid)
fj6 = client.get(f"/api/edits/{fid}").json()
expect(fj6["settings"]["length"] == 15 and abs(fj6["length"] - 15) <= 0.15 * 15 + 0.01,
       f"“make the whole thing 15 seconds”: {fj6['length']:.1f} s")

ANSWERS["change_edit"] = {"understood": "Ending it there.", "resize": [{"id": second, "end_words": "to the moon"}]}
r = client.post(f"/api/edits/{fid}/ask", json={"text": "end it when he says to the moon"})
rep = r.json()["reply"]
expect(not rep["changed"] and any("Couldn't find" in c for c in rep["cant"]),
       f"words he never says: nothing changes, and it says so (“{rep['cant'][-1] if rep['cant'] else ''}”)")

print("== by hand: Shorter / Longer")
cur = client.get(f"/api/edits/{fid}").json()
one = cur["moments"][0]
r = client.post(f"/api/edits/{fid}/remake", json={"resize": [{"id": one["id"], "steps": 1}]})
wait(fid)
after_ = next(m for m in client.get(f"/api/edits/{fid}").json()["moments"] if m["id"] == one["id"])
expect(r.status_code == 200 and after_["end"] - after_["start"] > one["end"] - one["start"] + 0.5,
       f"Longer: {one['end'] - one['start']:.1f} → {after_['end'] - after_['start']:.1f} s")
r = client.post(f"/api/edits/{fid}/remake", json={"resize": [{"id": one["id"], "trim_start": 99}]})
expect(r.status_code == 400 and "stays as it was" in r.json()["detail"], f"an impossible trim: “{r.json()['detail']}”")

# --- campaign rules -------------------------------------------------------------------------
print("== campaign rules")
strict = store.save_campaign("Strict brand", "source", "brief", {"mode": "source", "name": "Strict brand"})
r = client.post("/api/edits", json={"style": "velocity", "sources": [job_id], "sound": s1["id"],
                                    "campaign_id": strict})
expect(r.status_code == 400 and "joining different moments" in r.json()["detail"],
       f"a brief that doesn't allow joining moments: “{r.json()['detail'][:80]}…”")
nomusic = store.save_campaign("TJR — Reach", "source", "brief", {
    "mode": "source", "name": "TJR — Reach", "overrides": {"stitch": "yes"},
    "caption": {"hashtags": [{"text": "TJR", "verified": True}]},
    "look_for": ["Big wins explained calmly", "His trading rules"]})
r = client.post("/api/edits", json={"style": "velocity", "sources": [job_id], "sound": s1["id"],
                                    "campaign_id": nomusic})
expect(r.status_code == 400 and "doesn't allow added music" in r.json()["detail"],
       f"music refused: “{r.json()['detail'][:70]}…”")
r = client.post("/api/edits", json={"style": "cinematic", "sources": [job_id], "sound": "", "length": 15,
                                    "campaign_id": nomusic})
ANSWERS["pick_moments"] = {"title": "His lesson", "hook": "Seven years to make it", "caption": "Patience.",
                           "hashtags": ["mindset"],
                           "moments": [{"video": 1, "start": 22.0, "end": 24.9, "hit": 23.0, "text": "", "drop": True},
                                       {"video": 1, "start": 30.0, "end": 32.9, "hit": 31.0, "text": ""}]}
ce = wait(r.json()["id"])
cj = client.get(f"/api/edits/{ce['id']}").json()
pick_prompt = next(p for p in reversed(PROMPTS) if p["tool_choice"]["name"] == "pick_moments")["messages"][0]["content"]
expect("WHAT THIS CAMPAIGN WANTS TO SEE" in pick_prompt and "- Big wins explained calmly" in pick_prompt
       and "- His trading rules" in pick_prompt, "the brief's “what to look for” reaches Claude, word for word")
expect(cj["status"] == "done" and cj["compliance"] and cj["compliance"]["status"] in ("ready", "check"),
       f"a no-music edit for the campaign passes its check ({(cj['compliance'] or {}).get('summary')})")
expect("#TJR" in cj["post_text"], "the campaign's hashtag is in the caption")
expect(any("speed changes" in n for n in cj["notes"]) or not cj["effects"].get("slowmo"),
       "speed changes the brief doesn't allow stay off, and it says why")
short = store.save_campaign("Short brand", "source", "brief", {
    "mode": "source", "name": "Short brand", "overrides": {"stitch": "yes"}, "length": {"max": 5}})
r = client.post("/api/edits", json={"style": "cinematic", "sources": [job_id], "sound": "", "length": 15,
                                    "campaign_id": short})
be = wait(r.json()["id"])
bj = client.get(f"/api/edits/{be['id']}").json()
expect(bj["compliance"]["status"] == "blocked", f"too long for the brief: blocked ({bj['compliance']['summary']})")
expect(client.get(f"/api/edits/{be['id']}/download").status_code == 409, "a blocked edit doesn't download")
expect(client.get(f"/api/edits/{be['id']}/download?anyway=1").status_code == 200, "…unless you say anyway")
lst = client.get("/api/edits").json()["edits"]
expect(any(x["verdict"] == "blocked" for x in lst), "the list shows the campaign verdict")
r = client.post(f"/api/edits/{ce['id']}/post", json={"caption": "Patience pays.", "hashtags": ["mindset"]})
expect(r.status_code == 200 and "#TJR" in r.json()["post_text"] and "Patience pays." in r.json()["post_text"],
       "a new caption keeps the campaign's hashtag")

# --- Telegram ---------------------------------------------------------------------------------
print("== Telegram")
drain()
ANSWERS["pick_moments"] = {"title": "Wins", "hook": "Seven years", "caption": "Wins.", "hashtags": [],
                           "moments": [{"video": 1, "start": 2.0 + k * 4, "end": 4.8 + k * 4, "text": "",
                                        "drop": k == 3} for k in range(8)]}
reply = main.telegram_command("/edit velocity his biggest wins")
expect(reply and "Making a Velocity edit" in reply, f"/edit velocity …: “{reply}”")
tg = next(x for x in store.list_edits(5) if x["settings"]["theme"] == "his biggest wins")
tg = wait(tg["id"])
expect(tg["status"] == "done" and any(m["kind"] == "video" for m in drain()), "…made and sent to Telegram")
expect("velocity" in (main.telegram_command("/edit") or ""), "/edit alone explains itself")
reply = main.telegram_command("/edit 2 end it sooner")
expect(reply and "Reading that" in reply, "/edit with a clip number still changes clips")

# --- housekeeping -----------------------------------------------------------------------------
print("== delete, restarts")
r = client.delete(f"/api/edits/{eid}")
expect(r.status_code == 200 and client.get(f"/api/edits/{eid}").status_code == 404
       and not (edits.EDIT_DIR / f"{eid}.mp4").exists(), "an edit is deleted with its files")
store.update_edit(tg["id"], status="running", stage="Rendering — 40%")
edits.settle_interrupted()
st = client.get(f"/api/edits/{tg['id']}").json()
expect(st["status"] == "failed" and "restarted" in st["error"], "an edit cut off by a restart says so")
store.update_job(job_id, source_path=str(DATA / "gone.mp4"))
r = client.post(f"/api/edits/{tg['id']}/remake", json={})
expect(r.status_code == 400 and "isn't on this PC" in r.json()["detail"], "missing footage: plain words")
r = client.delete(f"/api/sounds/{s2['id']}")
expect(r.status_code == 200 and len(client.get("/api/sounds").json()["sounds"]) == 1, "a song is deleted")

print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed")
sys.exit(1 if FAILS else 0)
