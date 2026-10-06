"""Downloads: YouTube's robot check pauses the queue, long VODs come in parts, every job keeps its source facts.

Run: python tests/downloads.py
Offline: yt-dlp and the network are replaced by stand-ins (media._ydl_extract and media.RUNNER), except
one real yt-dlp section download from a tiny web server on this PC (127.0.0.1) — still no internet.
Uses a temp DATA_DIR; ~30 s.
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="downloads_"))
os.environ["DATA_DIR"] = str(TMP / "data")
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["WHISPER_API_KEY"] = "x"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ.pop("YTDLP_COOKIES", None)
os.environ.pop("YTDLP_COOKIES_FROM_BROWSER", None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import downloads, highlights, main, media, money, notify, pipeline, store, transcribe  # noqa: E402

FAILS = []


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def ffmpeg(*args):
    proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


# --- stand-ins -----------------------------------------------------------------------------
TINY = TMP / "tiny.mp4"
ffmpeg("-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=4", "-f", "lavfi", "-i", "sine=f=330:r=44100:d=4",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(TINY))

SENT = []
notify.send = lambda text: SENT.append(text)
pause_msgs = lambda: [m for m in SENT if "YouTube downloads are paused" in m]            # noqa: E731
resume_msgs = lambda: [m for m in SENT if "YouTube downloads are working again" in m]    # noqa: E731

BOT = ("ERROR: [youtube] abc123: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies "
       "for the authentication.")
WEB = {"yt_blocked": True, "yt_calls": 0, "calls": []}


def fake_extract(url, opts, download, info=None):
    WEB["calls"].append((url, download))
    site = media.platform_of(url)
    if site == "youtube":
        WEB["yt_calls"] += 1
        if WEB["yt_blocked"]:
            raise Exception(BOT)
    got = {"title": f"{site} video {url[-3:]}", "duration": 4.0, "extractor_key": site.capitalize(),
           "upload_date": "20260901", "uploader": "TJR", "channel": "TJR Trades", "webpage_url": url}
    if download:
        out = Path(opts["outtmpl"].replace("%(ext)s", "mp4"))
        shutil.copy(TINY, out)
    return info or got


media._ydl_extract = fake_extract
transcribe.transcribe = lambda wav, progress=None, on_wait=None: {"words": [], "segments": [], "text": ""}
highlights.find_highlights = lambda **kw: []
store.init()
client = TestClient(main.app)


def job(jid):
    return store.get_job(jid) or {}


# ============================================================================================
print("== 1. YouTube's robot check pauses YouTube links; the rest keep going")
links = ["https://www.youtube.com/watch?v=aa1", "https://youtu.be/aa2", "https://www.youtube.com/watch?v=aa3",
         "https://www.twitch.tv/videos/123"]
r = client.post("/api/batch", json={"urls": links})
ids = r.json()["job_ids"]
block = media.bot_block()
expect(r.status_code == 200 and block is not None and block["platform"] == "youtube",
       "the bot check on link 1 pauses YouTube downloads")
j1, j2, j3, tw = (job(i) for i in ids)
expect(j1["status"] == "queued" and j1["paused"] == "youtube" and "prove it's not a robot" in j1["stage"]
       and "Settings shows how" in j1["stage"], "link 1 stops with the plain message (and is kept)")
expect("Sign in to confirm" in (j1.get("error_raw") or ""), "yt-dlp's own words are kept for Technical details")
expect(all(j["status"] == "queued" and j["paused"] == "youtube" and j["stage"] == downloads.WAITING_STAGE
           for j in (j2, j3)), "YouTube links 2 and 3 stay queued, not failed")
expect(WEB["yt_calls"] == 1, f"only link 1 asked YouTube ({WEB['yt_calls']} YouTube calls)")
expect(tw["status"] == "done", f"the Twitch link still ran ({tw['status']}: {tw['stage']})")
expect(len(pause_msgs()) == 1, f"Telegram told once ({len(pause_msgs())} pause messages)")
expect(not any("aa1" in m or "aa2" in m or "aa3" in m for m in SENT), "no 'finished' report for a waiting link")

vids = client.get("/api/videos").json()
p = vids["pause"]
expect(p["paused"] and p["waiting"] == 3 and "prove it's not a robot" in p["message"] and p["auto_retry_at"],
       f"My videos gets the pause: {p['waiting']} waiting, auto-retry time set")
cards = {v["id"]: v for v in vids["videos"]}
expect([cards[i]["paused"] for i in ids] == ["youtube", "youtube", "youtube", ""], "each card says whether it waits")
d1 = client.get(f"/api/jobs/{ids[0]}").json()
expect(d1["paused"] == "youtube" and d1["pause"]["paused"] and "Sign in to confirm" in d1["error_detail"],
       "the video page: paused, with the raw detail")
page = client.get("/").text
expect(all(f'id="{x}"' in page for x in ("dlpause-make", "dlpause-videos", "set-youtube")),
       "Make clips and My videos have the banner, Settings has the cookies help")

print("\n== 1b. everything else respects the pause")
r = client.post("/api/jobs", data={"url": "https://www.youtube.com/watch?v=single"})
single = r.json()["job_id"]
expect(r.json().get("waits_for_youtube") and job(single)["paused"] == "youtube" and WEB["yt_calls"] == 1,
       "a single YouTube link waits without asking YouTube")
bad = store.create_job("https://www.youtube.com/watch?v=old", "https://www.youtube.com/watch?v=old", {})
store.update_job(bad, status="failed", stage="Failed", error="network")
r = client.post(f"/api/jobs/{bad}/rerun", json={})
expect(r.status_code == 200 and job(r.json()["job_id"])["paused"] == "youtube" and WEB["yt_calls"] == 1,
       "Try again on a failed YouTube link waits too")
reply = main.telegram_command("https://youtu.be/tg1")
for _ in range(50):
    tg = next((j for j in store.list_jobs(50) if j["title"] == "https://youtu.be/tg1"), None)
    if tg and job(tg["id"]).get("paused"):
        break
    time.sleep(0.1)
expect("paused" in reply and tg and job(tg["id"])["paused"] == "youtube" and WEB["yt_calls"] == 1,
       "a Telegram link is saved and waits")
expect("YouTube downloads are paused" in main.telegram_command("/status"), "/status says YouTube is paused")
expect(len(pause_msgs()) == 1, "still only one Telegram message about the pause")

# money's view checks and channel scout leave YouTube alone while paused, and notice the block
import yt_dlp  # noqa: E402
real_ydl = yt_dlp.YoutubeDL


class Boom:
    def __init__(self, *a, **k):
        raise AssertionError("YouTube was asked during the pause")


yt_dlp.YoutubeDL = Boom
expect(money.fetch_stats("https://www.youtube.com/shorts/x1") is None and money._latest("https://www.youtube.com/@x") == [],
       "view checks and channel scouting skip YouTube while it's paused")
yt_dlp.YoutubeDL = real_ydl

print("\n== 1c. Try again now: a second block pauses again")
waiting_before = len(store.waiting_jobs())
out = downloads.try_again_now(background=False)
expect(out["started"] and media.bot_block() is not None, "Try again now ran, YouTube said no again: paused again")
expect(WEB["yt_calls"] == 2, f"only the first saved link was tried ({WEB['yt_calls'] - 1} new YouTube call)")
expect(len(store.waiting_jobs()) == waiting_before and job(ids[0])["paused"] == "youtube",
       "every link is still saved")
expect(len(pause_msgs()) == 1, "no second Telegram message for the same pause")
expect(not media.bot_block()["auto_tried"], "a manual try still leaves the one automatic try")

print("\n== 1d. the one automatic try after ~45 minutes")
st = media.pause_state()
expect(not downloads.tick(now=time.time() + 60, background=False), "not before 45 minutes")
media.update_pause_state(block={**st["block"], "since_ts": time.time() - 46 * 60})
expect(downloads.tick(background=False) and WEB["yt_calls"] == 3, "after 45 minutes: one link tried")
b2 = media.bot_block()
expect(b2 is not None and b2["auto_tried"] and b2["auto_retry_at"] is None, "still blocked: stays paused")
expect(not downloads.tick(now=time.time() + 10 * 3600, background=False) and WEB["yt_calls"] == 3,
       "and doesn't keep trying")

print("\n== 1e. a restart while paused keeps the pause and the links")
kick = store.create_job("https://kick.com/video/k1", "https://kick.com/video/k1", {})
yt_new = store.create_job("https://www.youtube.com/watch?v=aa9", "https://www.youtube.com/watch?v=aa9", {})
up = store.create_job("talk.mp4", "upload", {})
cut = store.create_job("cut", "https://www.twitch.tv/videos/9", {})
store.update_job(cut, status="running", stage="Transcribing speech")
main._settle_interrupted_jobs()
expect(all(job(i)["status"] == "queued" and job(i)["paused"] == "youtube" for i in ids[:3]),
       "links waiting for YouTube are still waiting")
expect(job(kick)["paused"] == "restart" and job(yt_new)["paused"] == "restart",
       "links that hadn't started yet keep their place")
expect(job(up)["status"] == "failed" and job(cut)["status"] == "failed", "a run cut off part-way is failed, as before")
expect(json.loads(media.PAUSE_PATH.read_text(encoding="utf-8"))["block"], "the pause itself is on disk")
downloads._ticker_started = True                    # no clock thread in a test
downloads.start(background=False)
expect(job(kick)["status"] == "done", f"after the restart the Kick link ran ({job(kick)['status']})")
expect(job(yt_new)["paused"] == "youtube" and WEB["yt_calls"] == 3, "the YouTube one waits for YouTube")

print("\n== 1f. YouTube lets up: Try again now runs every saved link")
WEB["yt_blocked"] = False
waiting = [j["id"] for j in store.waiting_jobs()]
downloads.try_again_now(background=False)
expect(media.bot_block() is None and not store.waiting_jobs(), "the pause is gone and nothing waits")
expect(all(job(i)["status"] == "done" for i in waiting), f"all {len(waiting)} saved links ran")
expect(len(resume_msgs()) == 1, f"Telegram told once that it works again ({len(resume_msgs())})")
expect(not client.get("/api/videos").json()["pause"]["paused"], "the banner goes away")

print("\n== 1g. a new block later is a new pause (one message again)")
WEB["yt_blocked"] = True
r = client.post("/api/batch", json={"urls": ["https://youtu.be/bb1", "https://youtu.be/bb2"]})
expect(media.bot_block() is not None and len(pause_msgs()) == 2
       and all(job(i)["paused"] == "youtube" for i in r.json()["job_ids"]), "paused again, told once")
expect(not media.bot_block()["auto_tried"], "and it gets its own one automatic try")
media.clear_bot_block()
WEB["yt_blocked"] = False
downloads.try_again_now(background=False)


class BotSays:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, *a, **k):
        raise Exception(BOT)


yt_dlp.YoutubeDL = BotSays
expect(money.fetch_stats("https://www.youtube.com/shorts/x2") is None and media.bot_block() is not None,
       "a view check that meets the robot check pauses YouTube downloads too")
yt_dlp.YoutubeDL = real_ydl
media.clear_bot_block()
age = "ERROR: [youtube] q1: Sign in to confirm your age. This video may be inappropriate for some users."
expect(not media.noticed_bot_check(age, "https://www.youtube.com/watch?v=q1") and media.bot_block() is None
       and "age-restricted" in media.explain_download_error(age), "YouTube's age check doesn't pause anything")

# ============================================================================================
print("\n== 2. a 6-hour Twitch VOD: listen first, download only the loud parts")
VOD = 6 * 3600 + 20 * 60
BURSTS = [1800, 7300, 7400, 12000, 15000, 20500]
audio = TMP / "vod_sound.wav"
loud = "+".join(f"between(t,{b},{b + 5})" for b in BURSTS)
t0 = time.time()
ffmpeg("-f", "lavfi", "-i", f"sine=f=50:r=1000:d={VOD}",
       "-af", f"volume=eval=frame:volume='0.05*(1+0.3*sin(t/900))',volume=enable='{loud}':volume=12",
       "-c:a", "adpcm_ima_wav", str(audio))
env = media.loudness_envelope(audio)
expect(abs(len(env) - VOD) <= 2, f"one loudness reading per second ({len(env)} for {VOD} s, {time.time() - t0:.1f} s)")
peaks = [max(range(b - 30, b + 30), key=lambda i: env[i]) for b in BURSTS]
expect(all(b <= pk <= b + 5 for b, pk in zip(BURSTS, peaks)), f"the bursts land at their real times ({peaks})")
expect(min(env[b + 2] - env[b - 60] for b in BURSTS) > 15, "and stand well above the sound around them")

wins = media.loud_windows(env, 1.0)
total = sum(w["end"] - w["start"] for w in wins)
expect(all(any(w["start"] <= b and b + 5 <= w["end"] for w in wins) for b in BURSTS),
       "every loud burst is inside a chosen window")
expect(any(w["start"] <= 7300 and 7405 <= w["end"] for w in wins), "the two bursts 100 s apart share one window")
expect(all(a["end"] < b["start"] for a, b in zip(wins, wins[1:])), "windows in order, never overlapping")
expect(total <= 3600 and all(w["end"] - w["start"] >= 180 for w in wins) and len(wins) <= 8,
       f"{len(wins)} windows, {total / 60:.0f} min in all (≤ 60), each ≥ 3 min")
expect(all(re.search(r"\d+:\d\d", w["why"]) for w in wins) and all(0 <= w["score"] <= 100 for w in wins),
       f"each says why in plain words ('{wins[0]['why']}')")
expect(media.loud_windows(env, 1.0, max_total=900, count=3)[-1]["end"] <= VOD
       and sum(w["end"] - w["start"] for w in media.loud_windows(env, 1.0, max_total=900, count=3)) <= 900,
       "a smaller budget is kept")
skip = media.loud_windows(env, 1.0, avoid=[(1700, 1900)])
expect(not any(w["start"] < 1900 and w["end"] > 1700 for w in skip), "avoided stretches stay out")

COMMANDS = []
FAIL_ONCE = {"section": 2}


def fake_runner(cmd, on_line=None):
    COMMANDS.append(list(cmd))
    out = Path(cmd[cmd.index("-o") + 1])
    if "--download-sections" in cmd:
        a, b = map(float, cmd[cmd.index("--download-sections") + 1][1:].split("-"))
        n = sum(1 for c in COMMANDS if "--download-sections" in c)
        if n == FAIL_ONCE.get("section"):
            return 1, "ERROR: ffmpeg exited with code 1 (connection reset)"
        if on_line:
            on_line(f"frame=  10 fps=0.0 time=00:00:{min(59, int(b - a)):02d}.00 bitrate=N/A")
        ffmpeg("-f", "lavfi", "-i", f"color=c=gray:s=32x32:r=1:d={b - a:.2f}", "-f", "lavfi",
               "-i", f"sine=f=220:r=8000:d={b - a:.2f}", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
               "-b:a", "16k", "-shortest", str(out).replace("%(ext)s", "mp4"))
        return 0, "[download] done"
    if "wa/worst" in cmd:
        shutil.copy(audio, str(out).replace("%(ext)s", "wav"))
        if on_line:
            on_line("[sound]  57.0%")
        return 0, "[sound] 100.0%"
    return 1, "unexpected command"


SLEPT = []
media.RUNNER = fake_runner
media._sleep = lambda s: SLEPT.append(s)
url = "https://www.twitch.tv/videos/2468"
info = {"title": "TJR 6 hour stream", "duration": VOD, "extractor_key": "TwitchVod", "upload_date": "20260930",
        "uploader": "tjr", "channel": "TJR", "webpage_url": url, "subtitles": {}}
said = []
src, manifest, note = media.download_long(url, "longjob", info, limit_seconds=240 * 60,
                                          progress=lambda f, text: said.append((f, text)))
secs = [c for c in COMMANDS if "--download-sections" in c]
expect(any("6 h 20 min long — finding its loudest moments first (listening to the sound only)" in t for _, t in said),
       f"progress in plain words ('{said[1][1] if len(said) > 1 else ''}')")
expect(all(c[:3] == [sys.executable, "-m", "yt_dlp"] and c[-1] == url for c in COMMANDS),
       "yt-dlp runs as a program with an argument list, the link last")
expect(COMMANDS[0][COMMANDS[0].index("-f") + 1] == "wa/worst", "sound only, the smallest audio first")
ranges = [c[c.index("--download-sections") + 1] for c in secs]
expect(all(re.fullmatch(r"\*\d+\.\d\d-\d+\.\d\d", x) for x in ranges), f"--download-sections \"*a-b\" ({ranges[0]})")
expect(all("--force-keyframes-at-cuts" in c and c[c.index("--downloader-args") + 1].startswith("ffmpeg_o:")
           for c in secs), "--force-keyframes-at-cuts on every part")
starts = sorted({float(x[1:].split("-")[0]) for x in ranges})
expect(starts == [max(0.0, w["start"] - 8) for w in sorted(wins, key=lambda w: w["start"])],
       "each part padded by 8 s before the window")
expect(len(secs) == len(wins) + 1 and SLEPT, "a part that failed part-way was tried again")
expect(abs(media.probe(src)["duration"] - sum(s["source_end"] - s["source_start"] for s in manifest)) < 1.0,
       f"joined into one source.mp4 ({media.probe(src)['duration'] / 60:.1f} min)")
expect(len(manifest) == len(wins) and all(abs(s["vod_start"] - (w["start"] - 8)) < 0.01
                                         for s, w in zip(manifest, wins)), "the manifest knows each part's place")
expect(all(abs(b["source_start"] - a["source_end"]) < 1e-6 for a, b in zip(manifest, manifest[1:])),
       "parts sit end to end")
expect(all(s["why"] for s in manifest) and "loudest moments" in note and "chat replay isn't available" in note,
       "a plain note says how the parts were chosen")
expect(not (Path(os.environ["DATA_DIR"]) / "sources" / "_long").exists() or
       not any((Path(os.environ["DATA_DIR"]) / "sources" / "_long").iterdir()), "scratch parts are cleaned up")

print("\n== 2b. part downloads: resume, give up on one, the robot check")
COMMANDS.clear()
FAIL_ONCE["section"] = 0
part_dir = TMP / "parts"
calls_before = len(COMMANDS)
real = fake_runner


def flaky(cmd, on_line=None):
    if "--download-sections" in cmd and cmd[cmd.index("--download-sections") + 1].startswith("*492"):
        COMMANDS.append(list(cmd))
        return 1, "ERROR: unable to download video data: HTTP Error 500"
    return real(cmd, on_line)


media.RUNNER = flaky
got = media.download_sections(url, [(100, 200), (500, 600)], part_dir, pad=8)
expect(len(got) == 1 and got[0]["offset"] == 92.0 and got[0]["start"] == 100.0,
       "one part that never comes through is left out, the other kept")
expect(sum(1 for c in COMMANDS if "*492.00-608.00" in c) == 3, "it was tried three times")
media.RUNNER = fake_runner
COMMANDS.clear()
got = media.download_sections(url, [(100, 200), (500, 600)], part_dir, pad=8)
expect(len(got) == 2 and len(COMMANDS) == 1 and "*492.00-608.00" in COMMANDS[0],
       "run again: the part already on disk is kept, only the missing one is fetched")
media.RUNNER = lambda cmd, on_line=None: (1, BOT)
try:
    media.download_sections("https://www.youtube.com/watch?v=long", [(10, 300)], part_dir / "yt")
    expect(False, "the robot check during a part download pauses YouTube")
except media.BotBlocked as exc:
    expect(exc.hit and media.bot_block() is not None, "the robot check during a part download pauses YouTube")
media.clear_bot_block()
chat_info = {**info, "subtitles": {"rechat": [{"url": "x", "ext": "json"}]}}


def chat_runner(cmd, on_line=None):
    out = Path(cmd[cmd.index("-o") + 1].replace("%(ext)s", "rechat.json"))
    comments = [{"content_offset_seconds": 100 + i % 20} for i in range(30)]
    comments += [{"content_offset_seconds": 9000 + (i % 40) / 4} for i in range(400)]
    out.write_text(json.dumps({"comments": comments}), encoding="utf-8")
    return 0, ""


media.RUNNER = chat_runner
counts = media.chat_activity(url, chat_info, TMP / "chat")
cw = media._busy_windows([20 * __import__("math").log10(1 + c) - 60 for c in counts], 1.0, count=2,
                         noun=("burst of chat", "bursts of chat"), unit="far busier than the chat around it")
expect(counts and sum(counts) == 430 and any(w["start"] <= 9000 <= w["end"] for w in cw),
       "a chat replay, when there is one, points at its busiest minute")
expect(media.chat_activity(url, info, TMP / "chat") is None, "no chat replay offered: falls back to the sound")
media.RUNNER = fake_runner

print("\n== 2c. through the pipeline: no clip runs across a join")
COMMANDS.clear()
media._ydl_extract = lambda u, o, download, info=None: dict(info or {**chat_info, "subtitles": {}})
lj = store.create_job(url, url, {})
got = pipeline._get_source(lj, job(lj), url, None)
meta = got[2] if got else {}
expect(got and len(meta.get("sections") or []) >= 2 and meta["upload_date"] == "20260930"
       and "loudest" in meta.get("sections_note", ""), "the job's source_meta has the parts and the note")
pieces = pipeline._pieces(meta)
join = pieces[1][0]
s, e = pipeline._inside_one_piece(join - 10, join + 25, pieces)
expect(s >= join and e <= pieces[1][1] and e - s >= 20, f"a clip across a join loses the unrelated bit, stays in one part ({s}-{e})")
s, e = pipeline._inside_one_piece(join - 30, join + 5, pieces, lo=20)
expect(e <= join and e - s >= 20, f"…into the part holding most of it, still long enough ({s}-{e})")
expect(pipeline._inside_one_piece(10, 40, pieces) == (10, 40), "a clip inside one part is left alone")
marked = pipeline._with_joins([{"text": "hi", "start": 0, "end": 2}], pieces)
expect(sum("JUMP" in m["text"] for m in marked) == len(pieces) - 1, "Claude sees every join marked in the transcript")
clip = {"start": 10, "end": 40, "variants": {
    "continuous": {"parts": [{"start": join - 5, "end": join + 20}], "hook": "h", "start": join - 5, "end": join + 20},
    "stitched": {"parts": [{"start": 20, "end": 30}, {"start": join + 5, "end": join + 30}], "hook": "h"}}}
pipeline._variants_inside_pieces([clip], pieces)
v = clip["variants"]
expect(v["stitched"] is None and v["continuous"]["start"] >= join, "a stitch borrowing from another part is dropped")

# ============================================================================================
print("\n== 3. joining real parts: the times map back exactly")
COLORS = [(90, 240), (240, 90), (128, 128)]          # (Cb, Cr) per part
LENS = [3.0, 2.0, 4.0]
VODS = [100.0, 3000.0, 7000.0]
parts = []
for i, (d, (cb, cr)) in enumerate(zip(LENS, COLORS)):
    f = TMP / f"piece{i}.mp4"
    ffmpeg("-f", "lavfi", "-i", f"color=c=black:s=64x64:r=25:d={d},format=yuv420p,geq=lum='30+40*T':cb={cb}:cr={cr}",
           "-f", "lavfi", "-i", f"sine=f=440:r=48000:d={d}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "8",
           "-g", "25", "-c:a", "aac", "-shortest", str(f))
    parts.append({"path": f, "offset": VODS[i], "why": f"part {i}"})
joined = TMP / "joined.mp4"
man = media.join_sections(parts, joined)
lens = [media.probe(p["path"])["duration"] for p in parts]
expect(abs(media.probe(joined)["duration"] - sum(lens)) < 0.1,
       f"source.mp4 lasts the sum of the parts ({media.probe(joined)['duration']:.2f} vs {sum(lens):.2f})")


def frame_at(path, t):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"], capture_output=True).stdout
    y, u, v = raw[:4096], raw[4096:5120], raw[5120:6144]
    return sum(y) / len(y), sum(u) / len(u), sum(v) / len(v)


good = True
for t in (0.5, 2.6, man[1]["source_start"] + 0.3, man[1]["source_start"] + 1.5, man[2]["source_start"] + 0.2, 7.9):
    k = next(n for n, s in enumerate(man) if s["source_start"] <= t < s["source_end"])
    local = t - man[k]["source_start"]
    y, u, v = frame_at(joined, t)
    want_y = 30 + 40 * local
    ok = abs(y - want_y) <= 6 and abs(u - COLORS[k][0]) <= 8 and abs(v - COLORS[k][1]) <= 8
    back = media.vod_time(t, man)
    ok = ok and abs(back - (VODS[k] + local)) < 1e-6
    good = good and ok
    print(f"       t={t:5.2f}s → part {k}, {local:.2f}s in; luma {y:.0f} (want {want_y:.0f}); stream time {back:.2f}")
expect(good, "every sampled frame is the right part at the right moment, and maps back to the stream's clock")
expect(media.vod_time(man[1]["source_start"], man) == VODS[1] and media.vod_time(99, man) is None,
       "a join maps to the next part's start; outside the parts there's no stream time")
small = TMP / "small.mp4"
ffmpeg("-f", "lavfi", "-i", "color=c=white:s=48x48:r=25:d=2", "-f", "lavfi", "-i", "sine=f=300:r=48000:d=2",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(small))
mixed = media.join_sections([parts[0], {"path": small, "offset": 50.0}], TMP / "mixed.mp4")
expect(abs(media.probe(TMP / "mixed.mp4")["duration"] - (lens[0] + media.probe(small)["duration"])) < 0.25
       and media.probe(TMP / "mixed.mp4")["width"] == 64 and len(mixed) == 2,
       "parts of different sizes are drawn to one size and still line up")

print("\n== 3b. a real yt-dlp part download from this PC (no internet)")
for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
(TMP / "web").mkdir()
ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:r=25:d=20,format=yuv420p,geq=lum='16+10*T':cb=128:cr=128",
       "-f", "lavfi", "-i", "sine=f=440:r=44100:d=20", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "8",
       "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(TMP / "web" / "vod.mp4"))


class RangeFiles(http.server.SimpleHTTPRequestHandler):
    """Serves files with Range support, as video sites do (ffmpeg needs it to jump in)."""

    def send_head(self):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        a = int(m.group(1) or 0) if m else 0
        b = min(int(m.group(2)), size - 1) if m and m.group(2) else size - 1
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.send_header("Content-Length", str(b - a + 1))
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        fh = open(path, "rb")
        fh.seek(a)
        self._left = b - a + 1
        return fh

    def copyfile(self, src, dst):
        while self._left > 0:
            chunk = src.read(min(65536, self._left))
            if not chunk:
                break
            try:
                dst.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            self._left -= len(chunk)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(RangeFiles, directory=str(TMP / "web")))
threading.Thread(target=srv.serve_forever, daemon=True).start()
media.RUNNER = media._run_streaming
try:
    real_parts = media.download_sections(f"http://127.0.0.1:{srv.server_address[1]}/vod.mp4", [(8, 14)],
                                         TMP / "realparts", pad=2)
except media.DownloadError as exc:
    real_parts = []
    print("       yt-dlp said:", (exc.raw or str(exc))[-600:])
srv.shutdown()
rp = real_parts[0] if real_parts else {"duration": 0, "offset": -1, "path": TMP / "missing.mp4"}
expect(abs(rp["duration"] - 10.0) < 0.15 and rp["offset"] == 6.0,
       f"yt-dlp fetched just 6-16 s, padding included ({rp['duration']:.2f} s long)")
y0 = frame_at(rp["path"], 0.02)[0] if real_parts else 0
expect(abs(y0 - (16 + 10 * 6)) <= 4, f"and it starts exactly at 6 s (luma {y0:.0f}, want 76)")

# ============================================================================================
print("\n== 4. every job keeps its source facts")
m = media.source_meta(info)
expect(set(m) == {"upload_date", "uploader", "channel", "title", "duration", "webpage_url", "extractor"}
       and m["upload_date"] == "20260930" and m["duration"] == float(VOD) and m["extractor"] == "TwitchVod",
       "yt-dlp's info → upload date, uploader, channel, title, duration, link, site")
expect(media.source_meta(None, title="talk")["upload_date"] == "" and media.source_meta(None)["extractor"] == "upload",
       "an upload: empty upload date")
twm = store.source_meta(job(ids[3]))
expect(twm.get("upload_date") == "20260901" and twm.get("channel") == "TJR Trades" and twm.get("duration") == 4.0,
       f"the Twitch job saved them ({twm.get('upload_date')}, {twm.get('channel')})")
expect(client.get(f"/api/jobs/{ids[3]}").json()["source_meta"]["webpage_url"] == links[3], "the video page gets them")
with open(TINY, "rb") as fh:
    r = client.post("/api/jobs", files={"file": ("my talk.mp4", fh, "video/mp4")})
um = store.source_meta(job(r.json()["job_id"]))
expect(um.get("upload_date") == "" and um.get("extractor") == "upload" and um.get("duration", 0) > 3,
       f"an uploaded file: no upload date, its length measured ({um})")
store.update_job(lj, status="done", source_path=str(src), source_meta=json.dumps(meta))
again = client.post(f"/api/jobs/{lj}/rerun", json={}).json()["job_id"]
expect(store.source_meta(job(again)).get("sections") == meta["sections"],
       "Run again on a long stream keeps its parts, so clips still stay inside one")
expect(store.source_meta({"source_meta": "not json"}) == {} and store.source_meta(None) == {}, "read defensively")

print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed:\n  " + "\n  ".join(FAILS))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILS else 0)
