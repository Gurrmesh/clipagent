"""The clip doctor, on a real rendered file, with Claude's look faked.

usage: python tests/clip_doctor.py <a rendered 1080x1920 clip.mp4>
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="doc_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import doctor, pipeline, store  # noqa: E402

store.init()

FAILS = 0


def expect(ok, label):
    global FAILS
    print(("  ok   " if ok else "  FAIL ") + label)
    FAILS += 0 if ok else 1


clip_file = Path(os.environ["DATA_DIR"]) / "clip.mp4"
shutil.copy(sys.argv[1], clip_file)

print("== measured checks")
words = [{"w": "and", "start": 0.1, "end": 0.3}, {"w": "he", "start": 0.4, "end": 0.6},
         {"w": "paid", "start": 0.7, "end": 1.0}, {"w": "it", "start": 1.1, "end": 1.3}]
checks = doctor.technical(clip_file, {"hook": "He paid $20M", "hook_on": True}, words, "youtube")
by = {c["id"]: c for c in checks}
secs = doctor._probe(clip_file)["duration"]
expect((by["length"]["status"] == "warn") == (not 15 <= secs <= 40) and "s" in by["length"]["detail"],
       f"length judged against YouTube's winning range ({secs:.0f}s)")
expect(by["hook"]["status"] == "pass", "hook on the first frame")
expect(by["start"]["status"] == "warn" and "and" in by["start"]["detail"], "opens on 'and'")
expect(by["end"]["status"] == "warn", "no full stop at the end")
expect("loud" in by and "black" in by and "frozen" in by, "sound, black and frozen checked")
checks2 = doctor.technical(clip_file, {"hook_on": False, "cards": [{"kind": "label", "text": "X was SHOCKED"}]},
                           words, "tiktok")
expect({c["id"]: c for c in checks2}["hook"]["status"] == "pass", "a card counts as first-frame text")

print("\n== fixes")
all_words = [{"w": w, "start": 10 + i * 0.5, "end": 10 + i * 0.5 + 0.4}
             for i, w in enumerate("and he paid twenty million for it then walked away from the deal.".split())]
clip = {"start": 10.0, "end": 14.0, "parts": "[]"}
review = {"postable": 6, "issues": [
    {"problem": "Hook covers his eyes at 0.0s", "severity": "fix", "fix": "move_text", "text_y": 640, "new_text": ""},
    {"problem": "Typo: 'milion'", "severity": "fix", "fix": "rewrite_text", "text_y": None, "new_text": "He paid $20M"},
    {"problem": "slightly dark", "severity": "minor", "fix": "none", "text_y": None, "new_text": ""}]}
change, notes = doctor.plan_fixes(checks, review, {"hook": "He paid $20 milion"}, clip, all_words)
expect(change.get("start") and change["start"] > 10.3, f"trims the leading 'and' (start {change.get('start')})")
expect(change.get("end") and change["end"] > 15.9, f"runs on to the full stop (end {change.get('end')})")
expect(change.get("hook_y") == 640 and change.get("hook") == "He paid $20M", "moves and rewrites the hook")
change2, _ = doctor.plan_fixes(checks, review, {"cards": [{"kind": "label", "text": "old"}]}, clip, all_words,
                               may_trim=False)
expect("start" not in change2 and "end" not in change2, "a brief that forbids trimming: no trims")
change3, _ = doctor.plan_fixes([], {"issues": [{"problem": "captions on his mouth", "severity": "fix",
                                                "fix": "captions_down", "text_y": None, "new_text": ""}]},
                               {"caption_position": "pop"}, clip, all_words)
expect(change3.get("caption_position") == "bottom", "captions moved down a step")
expect(change2["cards"][0]["y"] == 640 and change2["cards"][0]["text"] == "He paid $20M", "cards move and get fixed")

print("\n== the whole treatment, stored on the clip")
job = store.create_job("t", "u", {"platforms": ["tiktok"]})
store.update_job(job, transcript=json.dumps({"words": all_words}))
cid = store.create_clip(job, {"start": 10.0, "end": 14.0, "title": "t", "hook": "He paid $20 milion", "score": 80,
                              "reason": "", "tags": [], "edits": {"hook": "He paid $20 milion"},
                              "words": words})
store.update_clip(cid, status="ready", file=str(clip_file))
doctor.look = lambda *a, **k: review
calls = []
pipeline.rerender_clip = lambda clip_id, edits: calls.append(edits) or {}
result = doctor.treat(cid, None)
expect(len(calls) == 1, "one re-render with all the fixes")
saved = json.loads(store.get_clip(cid)["doctor"])
expect(saved["status"] in ("fixed", "check") and saved["fixed"] and "Fixed:" in saved["summary"],
       f"report kept: {saved['status']} — {saved['summary'][:90]}")
expect(saved["postable"] == 6, "Claude's 1-10 kept")
doctor.look = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))
calls.clear()
result = doctor.treat(cid, None)
expect(result is not None and any(c["id"] == "look" for c in result["checks"]), "Claude down: still a report")

print("\nall checks behaved" if not FAILS else f"\n{FAILS} check(s) failed")
sys.exit(1 if FAILS else 0)
