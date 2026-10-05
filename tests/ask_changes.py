"""'Tell ClipAgent what to change' — offline, with a stand-in for Claude and the renderer.

Run: python tests/ask_changes.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ask_")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("WHISPER_API_KEY", "x")
os.environ["TELEGRAM_BOT_TOKEN"] = "1:x"
os.environ["TELEGRAM_CHAT_ID"] = "42"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import doctor, highlights, instruct, main, notify, pipeline, store  # noqa: E402

FAILS = []


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def drain():
    out = []
    while not notify._out.empty():
        out.append(notify._out.get())
    return out


# --- a finished video with three clips ---------------------------------------------------
store.init()
data = Path(os.environ["DATA_DIR"])
(data / "clips").mkdir(parents=True, exist_ok=True)
(data / "thumbs").mkdir(parents=True, exist_ok=True)
sentences = [(100.0, "So I got a call from Mick."), (104.0, "He wanted to borrow my car."),
             (108.0, "I said no way."), (111.0, "Then he laughed for ten minutes."),
             (116.0, "That is the whole story."), (120.0, "Anyway, next question.")]
words, segments = [], []
for t, sentence in sentences:
    toks = sentence.split()
    for i, w in enumerate(toks):
        words.append({"w": w, "start": t + i * 0.5, "end": t + i * 0.5 + 0.4})
    segments.append({"start": t, "end": t + len(toks) * 0.5, "text": sentence})
job_id = store.create_job("Conan on Mick", "upload", {"platforms": ["youtube"]})
src = data / "source.mp4"
src.write_bytes(b"src")
store.update_job(job_id, status="done", duration=600.0, source_path=str(src),
                 transcript=json.dumps({"words": words, "segments": segments}),
                 framing=json.dumps({"kind": "single", "two_shot": 0.0}))


def make_clip(rank, edits, start=100.0, end=118.0, **extra):
    cid = store.create_clip(job_id, {"start": start, "end": end, "title": f"t{rank}", "hook": "Old hook",
                                     "score": 80, "reason": "why", "tags": [], "rank": rank, "edits": edits,
                                     "words": words[:6], **extra})
    f = data / "clips" / f"{cid}.mp4"
    f.write_bytes(f"version-1 of {rank}".encode())
    t = data / "thumbs" / f"{cid}.jpg"
    t.write_bytes(b"thumb-1")
    store.update_clip(cid, status="ready", file=str(f), thumb=str(t),
                      doctor=json.dumps({"status": "good", "summary": "fine", "checks": [], "issues": []}))
    return cid


c_label = make_clip(1, {"style": "label", "cards": [{"kind": "label", "text": "Conan SHOCKED by Mick 😳"}],
                        "captions_on": False, "hook_on": False})
c_pop = make_clip(2, {"style": "wordpop", "hook": "Mick wanted his car", "caption_style": "impact",
                      "caption_position": "pop", "caption_look": {"max_words": 3}})
c_plain = make_clip(3, {"caption_style": "clean"})

# --- stand-ins: Claude answers what each test sets; the renderer writes a new file -------
ANSWER = {}
PROMPTS = []


class FakeMessages:
    def create(self, **kw):
        PROMPTS.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=ANSWER)], stop_reason="tool_use")


highlights._client = lambda: SimpleNamespace(messages=FakeMessages())
RENDERED = []


def fake_rerender(clip_id, edits):
    RENDERED.append((clip_id, edits))
    clip = store.get_clip(clip_id)
    Path(clip["file"]).write_bytes(b"version-2")
    merged = {**json.loads(clip["edits"] or "{}"), **{k: v for k, v in edits.items() if k not in ("start", "end")}}
    store.update_clip(clip_id, status="ready", edits=json.dumps(merged),
                      start=edits.get("start", clip["start"]), end=edits.get("end", clip["end"]))
    return store.get_clip(clip_id)


pipeline.rerender_clip = fake_rerender
doctor.recheck = lambda cid: None
client = TestClient(main.app)


def ask(text, **kw):
    RENDERED.clear()
    r = client.post(f"/api/jobs/{job_id}/ask", json={"text": text, **kw})
    out = r.json()
    if r.status_code == 200 and out.get("status") == "working":
        for _ in range(100):
            if store.get_request(out["id"])["status"] == "done":
                break
            time.sleep(0.05)
    return r.status_code, out


print("== a trim, by clip number")
ANSWER = {"understood": "I'll end clip 2 right after “I said no way.”",
          "changes": [{"clip": "2", "end": 109.6, "note": "ends after the punchline"}]}
code, out = ask("clip 2: end it right after he says no way")
expect(code == 200 and out["understood"].startswith("I'll end clip 2"), "answers with what it understood")
expect(len(RENDERED) == 1 and RENDERED[0][0] == c_pop, "only clip 2 is re-made")
end = RENDERED[0][1].get("end")
expect(end is not None and 109.0 <= end <= 110.5, f"the end lands on the sentence's end ({end})")
prompt = PROMPTS[-1]["messages"][0]["content"]
expect("[108.0] I said no way." in prompt and "CLIP 2" in prompt, "Claude sees the clip's lines with their times")
expect(store.get_request(out["id"])["status"] == "done", "the request is marked done")
c = client.get(f"/api/clips/{c_pop}").json()
expect(c["can_undo"] and c["status"] == "ready", "the old version is kept for undo")
expect(Path(store.get_clip(c_pop)["file"]).read_bytes() == b"version-2", "the clip file is the new one")

print("\n== undo puts the old one back")
r = client.post(f"/api/clips/{c_pop}/undo")
clip = store.get_clip(c_pop)
expect(r.status_code == 200 and Path(clip["file"]).read_bytes() == b"version-1 of 2", "file restored")
expect(abs(clip["end"] - 118.0) < 0.01 and not r.json()["can_undo"], "the trim is undone too, and undo is used up")
expect(client.post(f"/api/clips/{c_pop}/undo").status_code == 400, "nothing left to undo says so")

print("\n== a new look, new top text, all clips")
ANSWER = {"understood": "Clip 1 becomes word-pop captions; every clip gets karaoke, a bit bigger.",
          "changes": [{"clip": "1", "look": "wordpop", "hook": "Mick asked to borrow his car"},
                      {"clip": "2", "caption_style": "karaoke", "caption_size": 3.0},
                      {"clip": "3", "caption_style": "karaoke", "caption_size": 1.2, "highlight_color": "blue"},
                      {"clip": "9", "caption_style": "karaoke"}]}
code, out = ask("all clips karaoke and bigger, and make 1 word-pop")
by = {cid: e for cid, e in RENDERED}
e1 = by.get(c_label, {})
expect(e1.get("style") == "wordpop" and e1.get("cards") == [] and e1.get("captions_on") is True
       and e1.get("hook_on") is True and e1.get("hook") == "Mick asked to borrow his car",
       "switching a label clip to word-pop turns captions and the hook on, drops the card")
expect(by.get(c_pop, {}).get("caption_size") == 1.5, "a size past the slider's range is held at 1.5")
expect(by.get(c_plain, {}).get("caption_size") == 1.2 and "accent" not in by.get(c_plain, {}),
       "a colour that isn't a colour is ignored")
expect(len(RENDERED) == 3, "a clip number that doesn't exist is ignored")

print("\n== top text on a card look; words fixed; post caption only")
ANSWER = {"understood": "New label text on 1… ", "changes": []}
store.update_clip(c_label, edits=json.dumps({"style": "label", "cards": [{"kind": "label", "text": "Old label"}],
                                             "captions_on": False, "hook_on": False}))
ANSWER = {"understood": "Done.", "changes": [
    {"clip": "1", "hook": "Conan CAN'T believe what Mick asked 😳"},
    {"clip": "2", "fix_words": [{"wrong": "Mik", "right": "Mick"}]},
    {"clip": "3", "post_caption": "Mick Jagger called Conan 😂", "hashtags": ["#conan", "mick"]}]}
code, out = ask("1: change the top text; 2: it's Mick not Mik; 3: new caption")
by = {cid: e for cid, e in RENDERED}
expect(by.get(c_label, {}).get("cards", [{}])[0].get("text") == "Conan CAN'T believe what Mick asked 😳",
       "on a card look, 'the hook' means the card's text")
expect(by.get(c_pop, {}).get("spell") == {"Mik": "Mick"}, "a misheard word becomes a word fix")
clip3 = client.get(f"/api/clips/{c_plain}").json()
expect(c_plain not in by and clip3["caption"] == "Mick Jagger called Conan 😂" and clip3["hashtags"] == ["conan", "mick"],
       "a new post caption saves without re-making the clip")

print("\n== questions, can'ts and limits")
ANSWER = {"understood": "", "question": "Which clip do you mean — 1 or 3?", "changes": []}
code, out = ask("make that one better")
expect(code == 200 and out["question"].startswith("Which clip") and not RENDERED, "unclear → one question, no changes")
ANSWER = {"understood": "I can't add music.", "cant": ["Adding music — ClipAgent can't add audio. It can turn the "
                                                       "camera moves up instead."], "changes": [{"clip": "2", "look": "stack"}]}
code, out = ask("add music and split screen on 2")
item = next(c for c in out["clips"] if c["label"] == "2")
expect(out["cant"] and "music" in out["cant"][0].lower(), "what it can't do is said plainly")
expect(not RENDERED and item["problems"] and "doesn't fit" in item["problems"][0],
       "the two-person split is refused when the video has no two-shot")

print("\n== the editor's clip is the focus; Claude still sees the others")
ANSWER = {"understood": "Bigger captions on clip 3.", "changes": [{"clip": "3", "caption_size": 1.3}]}
code, out = ask("bigger captions", focus=c_plain)
prompt = PROMPTS[-1]["messages"][0]["content"]
expect("FOCUS: clip 3" in prompt and "CLIP 1" in prompt and "CLIP 2" in prompt, "focus is named, other clips listed")
expect([cid for cid, _ in RENDERED] == [c_plain], "only the focus clip changes")

print("\n== a re-make that fails keeps the clip")
pipeline.rerender_clip = lambda cid, edits: (_ for _ in ()).throw(RuntimeError("ffmpeg fell over"))
ANSWER = {"understood": "Boxed captions on 3.", "changes": [{"clip": "3", "caption_style": "boxed"}]}
code, out = ask("boxed captions on 3")
clip3 = store.get_clip(c_plain)
req = client.get(f"/api/asks/{out['id']}").json()
item = next(c for c in req["clips"] if c["label"] == "3")
expect(clip3["status"] == "ready" and "ffmpeg" in clip3["render_error"] and item["status"] == "failed",
       "the old clip stays, the request says it failed and why")
pipeline.rerender_clip = fake_rerender

print("\n== while the video is still being made, it waits")
store.update_job(job_id, status="running")
code, out = ask("bigger captions")
expect(code == 400 and "Wait until" in out["detail"], "asks are turned away mid-run")
store.update_job(job_id, status="done")

print("\n== history")
hist = client.get(f"/api/jobs/{job_id}/asks").json()["asks"]
expect(len(hist) >= 6 and hist[0]["text"] == "boxed captions on 3", "every request is kept, newest first")

print("\n== Telegram: /change")
drain()
ANSWER = {"understood": "I'll end clip 2 after the punchline.", "changes": [{"clip": "2", "end": 109.6}]}
reply = main.telegram_command("/change 2 end it after the punchline")
expect("Reading that" in reply and "Conan on Mick" in reply, "answers at once, naming the video")
for _ in range(100):
    msgs = drain()
    if any(m["kind"] == "video" for m in msgs) or len(PROMPTS) and any("I'll end clip 2" in (m.get("text") or "") for m in msgs):
        got = msgs
        break
    time.sleep(0.05)
else:
    got = []
time.sleep(0.3)
got += drain()
expect(any("I'll end clip 2" in (m.get("text") or "") for m in got), "then says what it understood")
expect(any(m["kind"] == "video" for m in got), "and sends the new version")
expect(PROMPTS[-1]["messages"][0]["content"].rstrip().endswith("Clip 2: end it after the punchline"),
       "'/change 2 …' is read as clip 2")
expect("change" in main.telegram_command("/change").lower(), "/change alone explains itself")

print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed")
sys.exit(1 if FAILS else 0)
