"""The endpoints the redesigned interface leans on — offline, no Claude, no downloads.

Run: python tests/ui_api.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ui_api_")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("WHISPER_API_KEY", "x")
os.environ["TELEGRAM_BOT_TOKEN"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import main, money, pipeline, store  # noqa: E402

FAILS = []


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


started = []
pipeline.run_job = lambda job_id, url=None, upload_path=None: started.append((job_id, url, upload_path))
main.pipeline.run_job = pipeline.run_job
store.init()
client = TestClient(main.app)

print("== the page and its config")
page = client.get("/").text
for el in ("page-make", "page-videos", "page-video", "page-campaigns", "page-money", "page-settings",
           "looks", "platforms", "theme-toggle", "err-detail", "cu-srcpick", "m-chart"):
    expect(f'id="{el}"' in page, f"page has #{el}")
cfg = client.get("/api/config").json()
ids = {r["id"]: r for r in cfg.get("recipes", [])}
expect({"wordpop", "label", "titlebar", "bubble", "stack"} <= set(ids), "config lists the five looks")
expect(all("available" in r and r["name"] and r["what"] for r in ids.values()), "each look has a name, a line and availability")

print("\n== making clips: the look and the platforms reach the run")
r = client.post("/api/jobs", data={"url": "https://www.youtube.com/watch?v=abc", "style_recipe": "label",
                                   "platforms": "tiktok,instagram", "max_clips": "5"})
expect(r.status_code == 200, f"job accepted ({r.status_code})")
jid = r.json()["job_id"]
saved = json.loads(store.get_job(jid)["settings"])
expect(saved.get("style_recipe") == "label", "the picked look is saved with the run")
expect(saved.get("platforms") == ["tiktok", "instagram"], f"platforms saved as a list ({saved.get('platforms')})")
expect(started and started[-1][1] == "https://www.youtube.com/watch?v=abc", "the run starts with the link")
r = client.post("/api/batch", json={"urls": ["https://youtu.be/x", "https://youtu.be/y"],
                                    "settings": {"platforms": ["youtube", "tiktok"], "style_recipe": "auto"}})
expect(r.status_code == 200, f"batch accepted ({r.status_code} {r.text[:80]})")
batch_jobs = [j for j in store.list_jobs(10) if j["title"] in ("https://youtu.be/x", "https://youtu.be/y")]
saved = json.loads(store.get_job(batch_jobs[0]["id"])["settings"]) if batch_jobs else {}
expect(len(batch_jobs) == 2 and saved.get("platforms") == ["youtube", "tiktok"], "a batch keeps the platform list")
expect(saved.get("style_recipe") == "auto", "no look picked: ClipAgent chooses")

bcamp = store.save_campaign("Batch brand", "source", "brief", {"mode": "source", "caption": {"hashtags": [{"text": "#BB"}]}})
bcamp_id = bcamp if isinstance(bcamp, str) else bcamp["id"]
r = client.post("/api/batch", json={"urls": ["https://youtu.be/c1", "https://youtu.be/c2"], "campaign_id": bcamp_id,
                                    "settings": {"platforms": ["tiktok"]}})
cjobs = [store.get_job(j) for j in r.json().get("job_ids", [])]
expect(r.status_code == 200 and len(cjobs) == 2 and all(j["campaign_id"] == bcamp_id for j in cjobs)
       and all((json.loads(j["settings"]).get("campaign") or {}).get("id") == bcamp_id for j in cjobs),
       "several links for a campaign queue together, each under the brief")

print("\n== My videos: readable errors, never a bare link")
ansi = "\x1b[0;31mERROR:\x1b[0m [youtube] plN7JMbadRg: Sign in to confirm you’re not a bot. Use --cookies"
store.update_job(jid, status="failed", stage="Failed at: Downloading video", error=ansi)
j2 = store.create_job("Long talk", "upload", {})
store.update_job(j2, status="failed", stage="Failed", error="Error code: 413 - {'error': {'message': 'Request Entity Too Large'}}")
j3 = store.create_job("Podcast", "upload", {})
store.update_job(j3, status="failed", stage="Failed", error="'str' object has no attribute 'get'")
vids = {v["id"]: v for v in client.get("/api/videos?limit=50").json()["videos"]}
expect("\x1b" not in vids[jid]["error"] and "[0;31m" not in vids[jid]["error"], "no terminal colour codes")
expect("blocking downloads" in vids[jid]["error"], f"bot check explained ({vids[jid]['error'][:60]}…)")
expect(vids[jid]["source"].startswith("https://www.youtube.com"), "the source link comes along for the card title")
expect("too big" in vids[j2]["error"], "a 413 explained")
expect("inside ClipAgent" in vids[j3]["error"] and "attribute" not in vids[j3]["error"], "a code error explained")
detail = client.get(f"/api/jobs/{jid}").json()
expect("blocking downloads" in detail["error"] and "Sign in to confirm" in detail["error_detail"]
       and "\x1b" not in detail["error_detail"], "the video page gets both: plain words and the raw detail")
expect(detail["can_refetch"] and not detail["can_retry"], "a failed download can be fetched again")

print("\n== Try again on a failed download re-downloads it")
started.clear()
r = client.post(f"/api/jobs/{jid}/rerun", json={})
expect(r.status_code == 200, f"rerun accepted ({r.status_code} {r.text[:80]})")
expect(started and started[-1][1] == "https://www.youtube.com/watch?v=abc" and started[-1][2] is None,
       "it starts from the link, not a missing file")
r = client.post(f"/api/jobs/{j2}/rerun", json={})
expect(r.status_code == 400 and "no longer on this PC" in r.json()["detail"], "an upload that's gone says so")

print("\n== the editor knows when the original is gone")
cid = store.create_clip(j2, {"start": 10, "end": 30, "title": "t", "hook": "h", "score": 80, "reason": "", "tags": [], "rank": 1})
wave = client.get(f"/api/clips/{cid}/waveform").json()
expect(wave.get("missing") is True and wave["points"] == [], "waveform says the source is missing")

print("\n== a failed run remembers where it stopped")
j4 = store.create_job("x", "upload", {})
store.update_job(j4, stage="Transcribing speech", status="running")
expect(pipeline._failed_stage(j4) == "Failed at: Transcribing speech", "Failed at: <step>")
store.update_job(j4, stage="Failed")
expect(pipeline._failed_stage(j4) == "Failed", "no double 'Failed at'")

print("\n== campaigns carry their numbers; money carries the chart")
camp = store.save_campaign("Brand X", "source", "brief", {"pay": {"per_1k": 1.5}})
camp_id = camp if isinstance(camp, str) else camp["id"]
store.update_job(j3, campaign_id=camp_id, status="done")     # only finished videos count as made
c3 = store.create_clip(j3, {"start": 0, "end": 20, "title": "t", "hook": "h", "score": 80, "reason": "", "tags": [], "rank": 1})
p = money.add(c3, url="https://www.tiktok.com/@me/video/1")
money.update(p["id"], views=20000, status="posted", posted_at=time.time() - 3600, rate=None,
             history=[[time.time(), 20000]])      # now, so it is today even just after midnight
camps = {c["id"]: c for c in client.get("/api/campaigns").json()["campaigns"]}
st = camps[camp_id].get("stats") or {}
expect(st.get("videos") == 1 and st.get("posts") == 1 and st.get("views") == 20000, f"campaign stats ({st})")
expect(abs(st.get("earned", 0) - 30.0) < 0.01, "a post with no saved rate uses the campaign's $/1K")
m = client.get("/api/money").json()
daily = m["dashboard"].get("daily") or []
expect(len(daily) == 14 and daily[-1]["views"] == 20000, "14 days of views, today's gain counted")
vids = {v["id"]: v for v in client.get("/api/videos").json()["videos"]}
expect(vids[j3]["campaign"]["name"] == "Brand X", "a video card names its campaign")

print("\n== editor re-renders: a failure keeps the clip, a success refreshes the check")
from app import doctor, highlights  # noqa: E402
clip_file = Path(os.environ["DATA_DIR"]) / "ok.mp4"
clip_file.write_bytes(b"0" * 10)
store.update_clip(c3, status="ready", file=str(clip_file), reason="Why this moment",
                  doctor=json.dumps({"status": "check", "summary": "Look at: length (89s)", "checks": [], "issues": []}))
real_rerender = pipeline.rerender_clip
pipeline.rerender_clip = lambda cid, edits: (_ for _ in ()).throw(RuntimeError("The source video has been cleaned up"))
client.post(f"/api/clips/{c3}/render", json={"start": 0, "end": 10})
got = client.get(f"/api/clips/{c3}").json()
expect(got["status"] == "ready" and "cleaned up" in got["render_error"] and got["reason"] == "Why this moment",
       "a failed re-render leaves the working clip and its 'why', and says what went wrong")
pipeline.rerender_clip = lambda cid, edits: store.update_clip(cid, status="ready")
doctor.technical = lambda path, edits, words, platform: [{"id": "length", "label": "Length", "status": "pass", "detail": ""}]
client.post(f"/api/clips/{c3}/render", json={"start": 0, "end": 10})
got = client.get(f"/api/clips/{c3}").json()
expect(got["render_error"] == "" and got["doctor"]["status"] == "good" and got["doctor"].get("rechecked"),
       "after a good re-render the doctor's report is measured again")
pipeline.rerender_clip = real_rerender

print("\n== where you post caps how long clips run")
expect(highlights.length_window(None, None, ["youtube"])[1] <= 55, "Shorts only: nothing past ~50 s")
expect(highlights.length_window(None, None, ["tiktok"])[1] == 90, "TikTok keeps room for longer stories")
expect(highlights.length_window(None, 75, ["youtube"])[1] == 75, "a campaign's own maximum wins")
expect(highlights.length_window(None, None, None)[1] == 90, "no platform picked: the usual limit")

r = client.post(f"/api/clips/{c3}/text", json={"caption": "New caption", "hashtags": "#one two  #three"})
got = client.get(f"/api/clips/{c3}").json()
expect(r.status_code == 200 and got["caption"] == "New caption" and got["hashtags"] == ["one", "two", "three"],
       "the editor's caption and hashtags save")

from app import structure  # noqa: E402
words = [{"w": f"w{i}.", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(400)]
clip = {"start": 100.0, "end": 130.0, "hook": "h"}
clip["variants"] = {"continuous": structure._as_variant(100, 130, "h"), "stitched": None}
structure._apply(clip, {"continuous": {"start": 50, "end": 130}, "stitched": {"parts": [
    {"start": 20, "end": 50, "role": "setup"}, {"start": 100, "end": 140, "role": "payoff"}]}}, words, 200, max_len=52)
cont = clip["variants"]["continuous"]
expect(cont["end"] - cont["start"] <= 52.5, f"pulling in setup can't stretch a Shorts clip past the ceiling ({cont['end'] - cont['start']:.0f}s)")
expect(clip["variants"]["stitched"] is None and "allowed" in clip.get("stitch_problem", ""),
       f"a 70 s stitch is refused for Shorts ({clip.get('stitch_problem')})")

print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed")
sys.exit(1 if FAILS else 0)
