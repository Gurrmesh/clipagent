"""Clip-bank clips get text written for what's actually in them — offline.

usage: python tests/watch_clips.py <folder with vert.mp4, land.mp4>

A brief like Gamebred FC's rejects "on-screen text that has nothing to do with
the clip" and wants captions that "make sense with the post". So before
writing, ClipAgent shows Claude frames of each clip. Claude is stood in for by
a fake here that answers from the frames it was sent (it tells the clips apart
by their shape), so the test proves the frames get there, each clip gets its
own text, the brief's example captions aren't forced in as required lines, and
the gate still checks everything.
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import os as _os
import tempfile as _tempfile
_os.environ["DATA_DIR"] = _tempfile.mkdtemp(prefix="clipagent_test_")  # never touch the real data folder
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import campaign, main, overlay, pipeline  # noqa: E402
from tests.brand_logo import GAMEBRED_ANSWER, GAMEBRED_BRIEF, make_logo  # noqa: E402

FAILS = []
CALLS = []
GARBLE = {}


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


class FakeClaude:
    """Answers the two calls a clip-bank run makes: watching a clip, and the tone check."""

    class _Messages:
        def create(self, **kw):
            tool = kw["tools"][0]["name"]
            CALLS.append(kw)
            if tool == "submit_clip_text":
                content = kw["messages"][0]["content"]
                images = [c for c in content if c.get("type") == "image"]
                prompt = content[-1]["text"]
                name = "land" if "'land'" in prompt else "vert"
                GARBLE[name] = GARBLE.get(name, 0) + 1
                if GARBLE[name] == 1:
                    # what the real model sent back the first time: a placeholder, not text
                    answer = {"what_happens": "x", "hook": "\n<UNKNOWN>\n", "caption": "\n<UNKNOWN>\n"}
                else:
                    answer = {"what_happens": f"{len(images)} frames of the {name} test clip.",
                              # both clips' best hook is the same brand line
                              "hook": "The ref had to stop it", "hook_alt": f"The {name} clip, seen",
                              "caption": f"{name} caption that fits 🔥 #tag @someone", "required_line": ""}
            elif tool == "submit_checks":
                answer = {"checks": "\n<UNKNOWN>\n"}          # the batch verdict comes back garbled
            else:                                             # submit_check: one post at a time
                answer = {"status": "ok", "reason": ""}
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=answer)], stop_reason="tool_use")

    messages = _Messages()


def main_test(folder: Path) -> None:
    print("== reading: examples aren't requirements")
    answer = {**GAMEBRED_ANSWER, "caption": {
        **GAMEBRED_ANSWER["caption"], "required_one_of": [],
        "examples": ["this knockout ended the whole conversation 😐", "the ref had to save him 😱"]}}
    brief = GAMEBRED_BRIEF + "\nExamples:\nthis knockout ended the whole conversation 😐\nthe ref had to save him 😱\n"
    rb = campaign.normalize(answer, brief)
    r = campaign.resolve(rb)
    expect(r["one_of"] == [] and len(r["caption_examples"]) == 2, "example captions kept as examples, not required lines")
    post = campaign.build_post(rb, 0, "instagram", extra="what a finish 🔥")
    expect(post["text"].startswith("what a finish 🔥") and "@gamebredfightingchampionships" in post["text"],
           "the caption is the clip's own words plus the required tag")
    req = campaign.normalize({**GAMEBRED_ANSWER, "caption": {**GAMEBRED_ANSWER["caption"],
                              "required_one_of": ["the ref had to save him 😱", "this knockout ended the whole conversation 😐"]}}, brief)
    p = campaign.build_post(req, 0, "tiktok", line="this knockout ended the whole conversation 😐")
    expect(p["line"] == "this knockout ended the whole conversation 😐", "a required line is the one that fits, not the next in turn")

    print("\n== a run: Claude looks at each clip")
    campaign._client = lambda: FakeClaude()
    pipeline.ANTHROPIC_API_KEY = "test"
    client = TestClient(main.app)
    saved = client.post("/api/campaigns", json={"brief": brief, "rulebook": rb, "edits": {"name": "Watch test"}}).json()
    cid = saved["id"]
    logo = make_logo(folder / "watch_logo.png")
    client.post(f"/api/campaigns/{cid}/logo", files={"file": ("logo.png", open(logo, "rb"), "image/png")})
    files = [("files", (n, open(folder / n, "rb"), "video/mp4")) for n in ("vert.mp4", "land.mp4")]
    r = client.post(f"/api/campaigns/{cid}/jobs", files=files, data={"versions": "1", "platforms": "instagram"})
    expect(r.status_code == 200, f"run accepted ({r.status_code})")
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    watch_calls = [c for c in CALLS if c["tools"][0]["name"] == "submit_clip_text"]
    images = [sum(1 for b in c["messages"][0]["content"] if b.get("type") == "image") for c in watch_calls]
    expect(len(watch_calls) == 4 and all(n == 6 for n in images),
           f"each clip shown to Claude as 6 frames, asked again after a garbled answer ({images})")
    first = watch_calls[0]["messages"][0]["content"][0]
    expect(first["source"]["media_type"] == "image/jpeg" and len(first["source"]["data"]) > 1000, "real JPEG frames")
    by = {c["title"]: c for c in job["clips"]}
    for name in ("vert", "land"):
        c = by.get(name)
        if not c:
            expect(False, f"{name}: clip made")
            continue
        gate = c["compliance"] or {}
        print(f"  {name}: {gate.get('status')} · hook {c['hook']!r} · {c['post']['text']!r}")
        print(f"     {c['reason']}")
        expect(c["hook"] == ("The ref had to stop it" if name == "vert" else "The land clip, seen"),
               f"{name}: its own hook, from its own frames (the second clip takes its next-best, unused one)")
        expect(c["post"]["text"].startswith(f"{name} caption that fits 🔥") and "#tag" not in c["post"]["text"]
               and "@someone" not in c["post"]["text"], f"{name}: its own caption, stray tags stripped")
        expect("@gamebredfightingchampionships" in c["post"]["text"], f"{name}: the required tag added")
        expect(c["reason"].startswith("Claude watched it:"), f"{name}: the post kit says the text came from watching it")
        checks = {k["id"]: k["status"] for k in gate.get("checks", [])}
        expect(checks.get("brand_logo") == "pass" and checks.get("tone") == "pass" and gate.get("status") == "ready",
               f"{name}: logo, tone and every other check pass")
    expect(by["vert"]["hook"] != by["land"]["hook"], "two clips never share a hook when they have others to choose from")
    tone_call = next(c for c in CALLS if c["tools"][0]["name"] == "submit_checks")
    expect("the clip shows:" in tone_call["messages"][0]["content"], "the tone check is told what each clip shows")
    singles = [c for c in CALLS if c["tools"][0]["name"] == "submit_check"]
    expect(len(singles) == 2 and all("the clip shows:" in c["messages"][0]["content"] for c in singles),
           "a garbled batch verdict is asked again post by post")
    expect("<UNKNOWN>" not in json.dumps(job), "no placeholder text reaches a clip")

    print("\n== no Claude: falls back to the brief")
    pipeline.ANTHROPIC_API_KEY = ""
    CALLS.clear()
    files = [("files", ("vert.mp4", open(folder / "vert.mp4", "rb"), "video/mp4"))]
    r = client.post(f"/api/campaigns/{cid}/jobs", files=files, data={"versions": "1", "platforms": "instagram"})
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    expect(job["status"] == "done" and not any(c["tools"][0]["name"] == "submit_clip_text" for c in CALLS),
           "without a key nothing is sent, and the run still finishes")
    client.delete(f"/api/campaigns/{cid}")

    print("\nFAILED:" if FAILS else "\nall checks behaved", FAILS or "")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main_test(Path(sys.argv[1]))
