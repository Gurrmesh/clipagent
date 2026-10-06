"""Creator Scan, offline: listing, words, screening, scoring, ranking, sections, the picture
check, pause/resume/restart, the bot check, the estimate, search, and moments → clips / an edit.

Run: python tests/creator_scan.py
Nothing touches the network: yt-dlp is a stand-in that answers from the saved samples in
tests/samples/creator_scan/ (real yt-dlp shapes), Claude and Whisper are stand-ins, and Part 1's
download helpers (media.download_sections / download_audio_only / loudness_envelope /
loud_windows / bot_block) are fakes. The picture check runs for real on a short synthetic video
made with tools/make_test_footage.py.
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="creatorscan_")
os.environ["ANTHROPIC_API_KEY"] = "x"
os.environ["WHISPER_API_KEY"] = "x"
os.environ["TELEGRAM_BOT_TOKEN"] = "1:x"
os.environ["TELEGRAM_CHAT_ID"] = "42"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import (campaign, catalog, creators, editrender, edits, highlights, media, money, notify,  # noqa: E402
                 pipeline, scan, store, transcribe)

FAILS = []
DATA = Path(os.environ["DATA_DIR"])
S = ROOT / "tests" / "samples" / "creator_scan"


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def drain():
    out = []
    while not notify._out.empty():
        out.append(notify._out.get())
    return out


def sample(name):
    return json.loads((S / name).read_text(encoding="utf-8"))


store.init()
creators.init()

# --- the clock: sleeps advance it instantly, so polite gaps cost no real time ------------------------
CLOCK = [1000.0]


def fake_sleep(seconds):
    CLOCK[0] += float(seconds)
    if WAIT_SEEN and not WAIT_VIEW:
        WAIT_VIEW.append(scan.status(WAIT_SEEN[0])["text"])


scan.SLEEP = fake_sleep
scan.CLOCK = lambda: CLOCK[0]

# --- Part 1's download helpers, faked ----------------------------------------------------------------
BOT = {"block": None}
media.bot_block = lambda: BOT["block"]
media.set_bot_block = lambda message, platform="youtube": BOT.update(block={"since": "now", "message": message,
                                                                             "platform": platform})
media.clear_bot_block = lambda: BOT.update(block=None)

FOOTAGE = DATA / "footage.mp4"
subprocess.run([sys.executable, str(ROOT / "tools" / "make_test_footage.py"), "12", str(FOOTAGE)], check=True)
BLACK = DATA / "black.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=640x360:r=30:d=12", "-f", "lavfi",
                "-i", "sine=f=220:d=12", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
                str(BLACK)], check=True)
SECTION_CALLS = []
FAIL_SECTIONS_ONCE = set()
BLACK_VIDEOS = {"aaaaaaaaaa2"}


def fake_sections(url, sections, out_dir, pad=8.0, progress=None):
    SECTION_CALLS.append((url, [tuple(s) for s in sections], Path(out_dir), pad))
    if url in FAIL_SECTIONS_ONCE:
        FAIL_SECTIONS_ONCE.discard(url)
        raise media.DownloadError("Couldn't download that link: HTTP Error 403: Forbidden")
    vid = re.sub(r"\W", "", url.split("=")[-1].split("/")[-1])
    out = []
    for s, e in sections:
        a = max(0.0, s - pad)
        path = Path(out_dir) / f"{vid}_{int(s)}.mp4"
        src = BLACK if vid in BLACK_VIDEOS else FOOTAGE
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-stream_loop", "-1", "-i", str(src), "-t",
                        f"{(e + pad) - a:.2f}", "-c", "copy", str(path)], check=True)
        out.append({"path": path, "start": s, "end": e, "offset": a, "duration": round((e + pad) - a, 3),
                    "index": len(out)})
    return out


AUDIO_FOR = {}


def fake_audio_only(url, out_dir, progress=None):
    out = Path(out_dir) / "audio.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:d=8", "-ac", "1", str(out)],
                   check=True)
    AUDIO_FOR[str(out)] = url
    return out


WINDOW_CALLS = []
media.download_sections = fake_sections
media.download_audio_only = fake_audio_only
media.loudness_envelope = lambda audio, hop=1.0: [-30.0] * 2400
media.loud_windows = lambda env, hop=1.0, **kw: (WINDOW_CALLS.append(kw) or
                                                  [{"start": 3600.0, "end": 3780.0, "score": 1.0, "why": "shouting"},
                                                   {"start": 9000.0, "end": 9200.0, "score": 0.9, "why": "laughter"}])


def fake_cut(audio, start, end, out):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:d=5", "-ac", "1", str(out)],
                   check=True)
    return out


catalog.cut_audio = fake_cut

# --- Whisper, faked: the first call says the free allowance is used up for 20 minutes --------------
WHISPER_CALLS = []
QUOTA = {"left": 1}


class RateLimited(Exception):
    status_code = 429


class FakeTranscriptions:
    def create(self, file, model, response_format, timestamp_granularities):
        WHISPER_CALLS.append(Path(file.name).name)
        if QUOTA["left"] > 0:
            QUOTA["left"] -= 1
            raise RateLimited("Error code: 429 - Rate limit reached for model `whisper-large-v3-turbo` on seconds "
                              "of audio per hour (ASPH): Limit 7200, Used 7190. Please try again in 20m0s.")
        if Path(file.name).name.startswith("section_"):     # a moment's section: the words really said there
            return {"text": SECTION_WORDS["text"],
                    "segments": [{"text": g["text"], "start": g["start"], "end": g["end"]}
                                 for g in SECTION_WORDS["segments"]],
                    "words": [{"word": w["w"], "start": w["start"], "end": w["end"]} for w in SECTION_WORDS["words"]]}
        line = "we are live and the market is crazy today [laughter] I just made 10,000 dollars on this trade"
        words = [{"word": w, "start": 0.5 + i * 0.4, "end": 0.85 + i * 0.4} for i, w in enumerate(line.split())]
        return {"text": line, "words": words, "segments": [{"text": line, "start": 0.5, "end": 0.85 + len(words) * 0.4}]}


SECTION_WORDS = catalog.parse_json3((S / "auto_captions.json3").read_text(encoding="utf-8"))
transcribe._client = lambda: SimpleNamespace(audio=SimpleNamespace(transcriptions=FakeTranscriptions()))

# --- yt-dlp, faked from the saved samples ----------------------------------------------------------
VTT = (S / "auto_captions.vtt").read_text(encoding="utf-8")
JSON3 = (S / "auto_captions.json3").read_text(encoding="utf-8")
VIDEOS = {}
for name, plat in (("yt_videos.json", "youtube"), ("yt_streams.json", "youtube"), ("yt_videos_other.json", "youtube"),
                   ("twitch_vods.json", "twitch")):
    for e in sample(name)["entries"]:
        VIDEOS[e["id"]] = e


def renamed(listing, old, new):
    return json.loads(json.dumps(listing).replace(old, new))


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.fail = {}
        self.hook = None

    def info(self, url, opts):
        self.calls.append(("info", url, CLOCK[0]))
        if self.hook:
            self.hook(url)
        for key, err in list(self.fail.items()):
            if key in url:
                raise Exception(err)
        if url.endswith("/shorts"):
            return sample("yt_shorts.json")
        if "@TJRTrades/videos" in url:
            return sample("yt_videos.json")
        if "@TJRTrades/streams" in url:
            return sample("yt_streams.json")
        if "@TRichesTrades/videos" in url:
            return sample("yt_videos_other.json")
        if "@PauseChannel/videos" in url:
            return renamed(sample("yt_videos_other.json"), "bbbbbbbbbb", "pppppppppp")
        if "@BotChannel/videos" in url:
            return renamed(sample("yt_videos_other.json"), "bbbbbbbbbb", "zzzzzzzzzz")
        if "@EmptyChannel/videos" in url:
            return {"_type": "playlist", "id": "UCempty", "title": "Empty - Videos", "entries": []}
        if url.endswith("/streams"):
            raise Exception("ERROR: [youtube:tab] @X: This channel does not have a streams tab")
        if "twitch.tv/tjr/videos" in url:
            return sample("twitch_vods.json")
        m = re.search(r"watch\?v=([\w-]{11})", url) or re.search(r"twitch\.tv/videos/(\d+)", url)
        if m:
            vid = m.group(1) if "youtube" in url else "v" + m.group(1)
            if vid == "aaaaaaaaaa9":
                raise Exception(f"ERROR: [youtube] {vid}: Video unavailable. This video has been removed by the "
                                "uploader")
            listed = VIDEOS.get(vid) or VIDEOS.get(vid.replace("pppppppppp", "bbbbbbbbbb").replace("zzzzzzzzzz",
                                                                                                    "bbbbbbbbbb")) or {}
            if "twitch" in url:
                return {"id": vid, "title": listed.get("title"), "duration": listed.get("duration"),
                        "timestamp": 1759000000, "view_count": listed.get("view_count"), "live_status": "was_live",
                        "webpage_url": url, "extractor": "twitch:vod"}
            info = copy.deepcopy(sample("video_info.json"))
            info.update(id=vid, title=listed.get("title") or f"New upload {vid}",
                        duration=listed.get("duration") or 1800, webpage_url=url,
                        view_count=listed.get("view_count"))
            if vid == "aaaaaaaaaa3":
                info["upload_date"] = "20250820"
            if vid == "aaaaaaaaaa2":                         # only VTT offered
                info["automatic_captions"] = {k: [t for t in v if t["ext"] != "json3"]
                                              for k, v in info["automatic_captions"].items()}
            if vid == "aaaaaaaaaa7":                         # no captions at all: Whisper
                info["automatic_captions"] = {}
            return info
        raise Exception(f"ERROR: Unsupported URL: {url}")

    def text(self, url):
        self.calls.append(("text", url, CLOCK[0]))
        if "kick.com/api" in url:
            raise Exception("ERROR: HTTP Error 403: Forbidden")
        if "fmt=json3" in url:
            return JSON3
        if "fmt=vtt" in url:
            return VTT
        raise Exception("ERROR: HTTP Error 404: Not Found")


RUNNER = FakeRunner()
catalog.RUNNER = RUNNER

# --- Claude, faked ---------------------------------------------------------------------------------
PROMPTS = []
PICTURE = {"visible": "yes"}


def screen_answer(kw):
    text = kw["messages"][0]["content"]
    if "first time I made a million" in text:
        return {"moments": [
            {"kind": "story", "start": 0.0, "end": 25.0, "hit": 14.0, "score": 9, "reason": "his origin story",
             "quote": "the first time I made a million dollars it was 2019", "speaker": "creator"},
            {"kind": "quote", "start": 25.0, "end": 38.0, "hit": 30.0, "score": 6, "reason": "clear advice",
             "quote": "discipline is the only thing that separates you from the gamblers", "speaker": "creator"},
            {"kind": "funny", "start": 38.0, "end": 52.0, "hit": 42.0, "score": 7, "reason": "roasting a trader",
             "quote": "bro this guy bought the top again", "speaker": "creator"},
            {"kind": "money", "start": 60.0, "end": 82.0, "hit": 66.0, "score": 8, "reason": "big number",
             "quote": "that day I made 40,000 dollars in 10 minutes", "speaker": "creator"},
            {"kind": "crazy", "start": 52.0, "end": 60.0, "hit": 55.0, "score": 9, "reason": "made up",
             "quote": "I quit trading forever and sold my house to buy bitcoin", "speaker": "creator"},
        ]}
    m = re.search(r"\[(\d+(?:\.\d+)?)s [^\]]*\] we are live", text)
    if m:
        t = float(m.group(1))
        return {"moments": [{"kind": "hype", "start": t, "end": t + 12, "hit": t + 3, "score": 7,
                             "reason": "energy", "quote": "the market is crazy today", "speaker": "creator"}]}
    return {"moments": []}


def rank_answer(kw):
    text = kw["messages"][0]["content"]
    kind = text.split(" moments", 1)[0].split()[-1]
    out = []
    for m in re.finditer(r"\[(\d+)\] “([^”]*)”[^\n]*\n\s+([^\n]*)", text):
        i, title, said = int(m.group(1)), m.group(2), m.group(3)
        first = "first time I made a million" in said and "40,000" not in said
        if title == "How I Made My First Million Trading":
            score = {True: 95}.get(first, 99 if "bought the top" in said else (60 if "40,000" in said else 70))
        elif title == "My Trading Routine" and "40,000" in said:
            score = 89
        else:
            score = 60 if first else 50
        hook = "His first million came in 2019" if first else (
            "He made $5 million in one day" if "40,000" in said and title.startswith("How") else "Bro bought the top again")
        out.append({"id": i, "score": score, "story_key": "First Million" if first else f"{kind}-{title}-{i}",
                    "hook": hook, "why": "test"})
    return {"moments": out}


def search_answer(kw):
    text = kw["messages"][0]["content"]
    first = re.search(r"\[(\d+)\] “[^”]*” (\d+)s-(\d+)s", text)
    n = int(first.group(1)) if first else 0
    return {"moments": [
        {"n": n, "start": 82.0, "end": 90.0, "kind": "quote", "score": 6, "why": "the sign-off",
         "quote": "alright guys that's it for today see you tomorrow", "speaker": "creator"},
        {"n": n, "start": 50.0, "end": 60.0, "kind": "crazy", "score": 9, "why": "invented",
         "quote": "this sentence was never said by anyone at all", "speaker": "creator"}]}


class FakeMessages:
    def create(self, **kw):
        name = kw["tool_choice"]["name"]
        PROMPTS.append((name, kw))
        answer = {"submit_moments": screen_answer, "rank_moments": rank_answer, "pick_stretches": search_answer,
                  "rate_picture": lambda kw: {"strong": 8, "person_visible": PICTURE["visible"],
                                              "what": "a man talking at a desk"}}.get(name, lambda kw: {})(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=answer)], stop_reason="tool_use")


highlights._client = lambda: SimpleNamespace(messages=FakeMessages())
campaign.check_text = lambda rb, posts: {p["id"]: {"status": "ok", "reason": "fine"} for p in posts}

# --- watch the scan rows for a "waiting" update ------------------------------------------------------
WAIT_SEEN = []
WAIT_VIEW = []
WAIT_UPDATES = []
_orig_update_scan = creators.update_scan


def recording_update_scan(scan_id, **fields):
    _orig_update_scan(scan_id, **fields)
    if fields.get("status") == "waiting" and fields.get("wait_until"):
        WAIT_UPDATES.append(dict(fields))
        row = creators.get_scan(scan_id)
        if row and not WAIT_SEEN:
            WAIT_SEEN.append(row["creator_id"])


creators.update_scan = recording_update_scan


def wait_scan(cid, statuses=("done", "paused", "failed"), seconds=180):
    end = time.time() + seconds
    while time.time() < end:
        s = creators.latest_scan(cid)
        with scan._lock:
            t = scan._threads.get(cid)
            alive = bool(t and t.is_alive())
        if s and s["status"] in statuses and not alive:
            return s
        time.sleep(0.05)
    return creators.latest_scan(cid)


# =====================================================================================================
print("== subtitles: YouTube's rolling auto-captions read once, with their times")
vtt = catalog.parse_vtt(VTT)
texts = [s["text"] for s in vtt["segments"]]
expect(len(vtt["segments"]) == 13, f"13 caption lines become 13 segments ({len(vtt['segments'])})")
expect(len(texts) == len(set(texts)), "no line is read twice (the rolling repeats and 10 ms settle cues are gone)")
expect(texts[0] == "so the first time I made a million dollars it was 2019" and vtt["segments"][0]["start"] == 0.0,
       f"the first line keeps its words and its time ({vtt['segments'][0]})")
seg_40k = next(s for s in vtt["segments"] if "40,000" in s["text"])
expect(seg_40k["start"] == 60.0 and abs(seg_40k["end"] - 67.99) < 0.02, f"a later line's times are right ({seg_40k})")
all_words = " ".join(w["w"] for w in vtt["words"])
expect(all_words.count("maniac") == 1 and len(vtt["words"]) == 124, f"each word once, with a time ({len(vtt['words'])})")
w = next(w for w in vtt["words"] if w["w"] == "maniac")
expect(abs(w["start"] - 16.462) < 0.01, f"word times come from the inline stamps (maniac at {w['start']})")
expect(all(a["start"] <= b["start"] for a, b in zip(vtt["words"], vtt["words"][1:])), "word times never run backwards")
j3 = catalog.parse_json3(JSON3)
expect([s["text"] for s in j3["segments"]] == texts and len(j3["words"]) == 124,
       "json3 gives the same lines and a time for every word")
man = catalog.parse_vtt((S / "manual_subs.vtt").read_text(encoding="utf-8"))
expect(len(man["segments"]) == 3 and man["segments"][1]["text"] == "Rule number one: never risk more than 1% on a trade."
       and man["words"] == [], "written subtitles: two-line cues joined, cue numbers dropped, segment times only")
expect("That's the & rule." in man["segments"][2]["text"], "HTML entities decoded")
info = catalog.parse_video_info(sample("video_info.json"))
expect(info["subtitles"]["lang"] == "en-orig" and info["subtitles"]["ext"] == "json3",
       "the track picked: the auto-captions in the language spoken, json3 first")
expect(info["upload_date"] == "20250801" and len(info["heatmap"]) == 8 and info["views"] == 912345,
       "one video's date, views and 'most replayed' heat are read")
fr = copy.deepcopy(sample("video_info.json"))
fr["automatic_captions"] = {"es-orig": [{"ext": "vtt", "url": "u1"}], "es": [{"ext": "vtt", "url": "u1"}],
                            "en": [{"ext": "vtt", "url": "u2"}]}
expect(catalog.pick_subtitles(fr)["lang"] == "es-orig", "a Spanish video: its own words, not a machine translation")

print("\n== listing: flat playlists, filters, de-duplication, outliers")
rows_a = catalog.parse_flat(sample("yt_videos.json"), "youtube", "video", "@tjr/videos", "@tjr")
ids = [r["video_id"] for r in rows_a]
expect("aaaaaaaaaa4" not in ids, "a private video is left out of the list")
expect(len(rows_a) == 8 and next(r for r in rows_a if r["video_id"] == "aaaaaaaaaa6")["kind"] == "short",
       "a short in the Videos tab is known as a short")
r3 = next(r for r in rows_a if r["video_id"] == "aaaaaaaaaa3")
expect(r3["upload_date"] == "" and r3["views"] == 150000, "a video with no date in the list keeps an empty date")
r7 = next(r for r in rows_a if r["video_id"] == "aaaaaaaaaa7")
expect(r7["views"] is None, "a video with no views listed keeps 'unknown'")
expect(next(r for r in rows_a if r["video_id"] == "aaaaaaaaaa5").get("status") == "skipped",
       "members-only marked as skipped")
rows_s = catalog.parse_flat(sample("yt_streams.json"), "youtube", "stream", "@tjr/streams", "@tjr")
expect([r["video_id"] for r in rows_s] == ["sssssssss01", "sssssssss02"] and all(r["kind"] == "stream" for r in rows_s),
       "streams listed; the upcoming one left out")
rows_t = catalog.parse_flat(sample("twitch_vods.json"), "twitch", "vod", "twitch/tjr", "twitch/tjr")
expect(len(rows_t) == 2 and rows_t[1]["duration"] == 21600.0 and rows_t[1]["url"].endswith("/videos/2100000002"),
       "Twitch past broadcasts listed, the 6-hour one included")
rows_b = catalog.parse_flat(sample("yt_videos_other.json"), "youtube", "video", "@riches/videos", "@riches")
settings = creators.clean_settings({"since": "2024-01-01"})
filtered = catalog.apply_filters(rows_a + rows_s + rows_b, settings, {"min_upload_date": {"date": "2024-06-01"}})
by = {r["video_id"]: r for r in filtered}
expect(by["aaaaaaaaaa6"]["status"] == "skipped" and "short" in by["aaaaaaaaaa6"]["error"].lower(),
       "shorts skipped by default, with the reason")
expect(by["aaaaaaaaaa8"]["status"] == "skipped" and "2024" in by["aaaaaaaaaa8"]["error"],
       f"a 2019 video skipped by the date set ({by['aaaaaaaaaa8']['error']})")
expect(by["aaaaaaaaaa3"]["status"] == "listed", "no date known: kept, checked again when the video is opened")
expect(by["aaaaaaaaaa7"]["status"] == "listed", "no views known: kept")
mv = catalog.apply_filters(rows_a, creators.clean_settings({"min_views": 100000}))
mvb = {r["video_id"]: r for r in mv}
expect(mvb["aaaaaaaaaa2"]["status"] == "listed" and mvb["aaaaaaaaaa9"]["status"] == "skipped"
       and mvb["aaaaaaaaaa7"]["status"] == "listed", "minimum views: under it skipped, unknown kept")
kept, dups = catalog.dedupe(rows_a + rows_s + rows_b)
expect([d["video_id"] for d in dups] == ["bbbbbbbbbb1"],
       f"the same video on two channels is read once (the one with more views kept): {[d['video_id'] for d in dups]}")
expect({"sssssssss01", "sssssssss02"} <= {r["video_id"] for r in kept},
       "two streams on the SAME channel with the same title and length are both kept")
_, dups2 = catalog.dedupe(rows_b, existing=[{"platform": "youtube", "video_id": "aaaaaaaaaa1",
                                             "title": "How I Made My First Million Trading", "duration": 1255.0}])
expect([d["video_id"] for d in dups2] == ["bbbbbbbbbb1"], "a copy of a video already in the catalog isn't added")
_, dups3 = catalog.dedupe(rows_a, existing=rows_a)
expect(dups3 == [], "listing the same channel again drops nothing")
catalog.outlier_factors(rows_a)
o = {r["video_id"]: r["outlier"] for r in rows_a}
expect(o["aaaaaaaaaa1"] == round(900000 / 120000, 2) and o["aaaaaaaaaa7"] is None,
       f"outlier = views ÷ the channel's median ({o['aaaaaaaaaa1']}×; unknown views → none)")
expect(catalog.classify_link("youtube.com/@TJRTrades/videos")["url"] == "https://www.youtube.com/@TJRTrades"
       and catalog.classify_link("https://kick.com/tjr")["platform"] == "kick"
       and catalog.classify_link("https://www.twitch.tv/videos/123")["type"] == "video",
       "links are understood (channel, Kick, a single Twitch VOD)")
expect(catalog.same_channel("https://www.youtube.com/@TJRTrades", "youtube.com/@tjrtrades/videos"),
       "the watch list's link and the creator's link are the same channel")

print("\n== the score: stable, and better signals only ever raise it")
base = {"text": 0.6, "heat": 0.5, "outlier": 1.5, "loud": 0.4, "age_days": 200, "laugh": 0.5}
expect(scan.merge_score(base) == scan.merge_score(dict(base)), f"same signals, same score ({scan.merge_score(base)})")
for key, lo, hi in (("text", 0.2, 0.9), ("heat", 0.1, 0.95), ("outlier", 0.5, 6.0), ("loud", 0.1, 0.9),
                    ("laugh", 0.0, 1.0), ("visual", 0.2, 0.9), ("motion", 0.0, 0.8), ("face_share", 0.0, 1.0)):
    vals = [lo + (hi - lo) * k / 6 for k in range(7)]
    scores = [scan.merge_score({**base, key: v}) for v in vals]
    expect(all(a <= b for a, b in zip(scores, scores[1:])) and scores[-1] > scores[0],
           f"more {key} → a higher score ({scores[0]} → {scores[-1]})")
ages = [scan.merge_score({**base, "age_days": d}) for d in (2000, 700, 100, 10)]
expect(all(a <= b for a, b in zip(ages, ages[1:])), "newer → higher")
expect(scan.merge_score({"text": 0.6}) == scan.merge_score({"text": 0.6, "heat": None}),
       "an unknown signal counts as middling, the same as a missing one")
expect(0 <= scan.merge_score({}) <= 100 and scan.merge_score({**base, "rank": 1.0, "heat": 1, "loud": 1, "outlier": 9,
                                                              "age_days": 0, "laugh": 1, "visual": 1}) == 100.0,
       "the score stays within 0-100")
expect(scan.merge_score({**base, "rank": 0.9}) > scan.merge_score({**base, "rank": 0.3}),
       "after ranking, the one-scale score replaces the first one")
expect(scan.heat_peak(info["heatmap"], 60, 82) == 0.88 and scan.heat_peak([], 0, 10) is None,
       "most replayed: the highest point inside the moment")

print("\n== the same story, quotes, hooks")
story = [{"id": "m1", "story_key": "first-million", "score": 70, "status": "candidate"},
         {"id": "m2", "story_key": "First Million", "score": 88, "status": "candidate"},
         {"id": "m3", "story_key": "first million!", "score": 60, "status": "used"},
         {"id": "m4", "story_key": "other", "score": 50, "status": "candidate"}]
pairs = scan.dedupe_stories(story)
expect(sorted(pairs) == [("m1", "m3"), ("m2", "m3")], f"a used moment wins its story and is never dropped: {pairs}")
pairs = scan.dedupe_stories([s for s in story if s["id"] != "m3"])
expect(pairs == [("m1", "m2")], "otherwise the best telling is kept")
found, dropped = scan.verify(screen_answer({"messages": [{"content": "first time I made a million"}]})["moments"],
                             j3, 1250)
expect(dropped == 1 and len(found) == 4 and all(f["kind"] != "crazy" for f in found),
       "a quote that was never said drops its moment")
money_m = next(f for f in found if f["kind"] == "money")
expect(money_m["start"] <= 60.0 and money_m["end"] >= 67.5, f"the quote is inside the moment ({money_m})")
expect(scan.clean_hook("He made $5 million in one day", "that day I made 40,000 dollars", "creator", "TJR") == "",
       "a hook with a number nobody said is refused")
expect(scan.clean_hook("He made 40,000 dollars in just 10 short minutes flat today", "I made 40,000 dollars in 10 "
                       "minutes", "creator", "TJR").count(" ") <= 7, "hooks are at most 8 words")
expect(scan.clean_hook("TJR says never trade tired", "never trade tired", "other", "TJR") == "",
       "someone else's words are never put in the creator's mouth")

# =====================================================================================================
print("\n== a whole scan (TJR: two YouTube channels, Twitch, Kick — under a campaign)")
rb = {"mode": "source", "name": "TJR — Reach", "creator": "TJR", "primary_focus": {"name": "TJR", "quote": "x"},
      "min_upload_date": {"date": "2024-06-01", "quote": "x"}, "look_for": ["big wins", "funny reactions"],
      "tone_avoid": ["portraying TJR negatively"]}
camp_id = store.save_campaign("TJR — Reach", "source", "brief", rb)
cid = creators.create_creator("TJR", ["https://www.youtube.com/@TJRTrades", "youtube.com/@TRichesTrades",
                                      "https://www.twitch.tv/tjr", "https://kick.com/tjr"], camp_id,
                              {"since": "2024-01-01", "fetch_top": 3})
FAIL_SECTIONS_ONCE.add("https://www.youtube.com/watch?v=aaaaaaaaaa2")
drain()
sid = scan.start(cid)
final = wait_scan(cid)
st = scan.status(cid)
expect(final["status"] == "done", f"the scan finished ({final['status']}: {final.get('message')} {final.get('error')})")
cc = creators.catalog_counts(cid)
rows = {r["video_id"]: r for r in creators.list_catalog(cid)}
expect("aaaaaaaaaa4" not in rows and "bbbbbbbbbb1" not in rows and "sssssssss03" not in rows,
       "not in the catalog: the private video, the copy on the second channel, the upcoming stream")
expect(rows["aaaaaaaaaa8"]["status"] == "skipped" and rows["aaaaaaaaaa6"]["status"] == "skipped"
       and rows["aaaaaaaaaa5"]["status"] == "skipped", "skipped with reasons: too old, a short, members-only")
expect(rows["aaaaaaaaaa9"]["status"] == "skipped" and "removed" in rows["aaaaaaaaaa9"]["error"].lower(),
       f"removed after listing: skipped in plain words ({rows['aaaaaaaaaa9']['error'][:60]})")
expect(rows["aaaaaaaaaa3"]["upload_date"] == "20250820", "the missing date is filled in when the video is opened")
expect(rows["aaaaaaaaaa1"]["words_source"] == "subs" and rows["aaaaaaaaaa2"]["words_source"] == "subs",
       "words from subtitles (json3, and VTT where that's all there is)")
expect(rows["aaaaaaaaaa7"]["words_source"] == "whisper" and rows["v2100000001"]["words_source"] == "whisper",
       "no subtitles: the sound alone goes to Whisper")
expect(rows["v2100000002"]["words_source"] == "partial", "the 6-hour VOD: only its loud stretches are transcribed")
long_words = creators.get_words(rows["v2100000002"]["id"])
expect([round(w["start"]) for w in long_words["windows"]] == [3600, 9000] and
       all(3600 <= s["start"] <= 9300 for s in long_words["segments"]),
       "…and its words keep the stream's own times")
expect(WINDOW_CALLS and WINDOW_CALLS[0].get("max_total") == 3600 and WINDOW_CALLS[0].get("min_len") == 180,
       f"loud windows asked for with the agreed limits ({WINDOW_CALLS[:1]})")
expect(all(r["status"] in ("done", "skipped") for r in rows.values()), f"every video ends done or skipped ({cc})")
yt = [c for c in RUNNER.calls if "youtube.com" in c[1]]
gaps = [b[2] - a[2] for a, b in zip(yt, yt[1:])]
expect(gaps and min(gaps) >= 4.0 - 1e-6, f"at least 4 s between YouTube requests (smallest gap {min(gaps):.2f} s)")
expect(not any(c[1].endswith("/shorts") for c in RUNNER.calls), "the Shorts tab is never even asked for")
expect(not any("sssssssss03" in c[1] or "aaaaaaaaaa4" in c[1] for c in RUNNER.calls),
       "nothing is requested for videos left out")
expect(any("Kick didn't let ClipAgent list" in n and "paste" in n for n in final["counters"].get("notes", [])),
       "Kick: a plain note asking for VOD links (never a log-in)")
expect(WAIT_UPDATES and "Waiting 20 min for transcription quota" in WAIT_UPDATES[0]["message"]
       and abs(WAIT_UPDATES[0]["wait_until"] - time.time() - 1202) < 120,
       f"the transcription allowance used up: waiting, with the time it carries on ({WAIT_UPDATES[:1]})")
expect(WAIT_VIEW and "Waiting 20 min for transcription quota" in WAIT_VIEW[0],
       f"…and the progress line says so ({WAIT_VIEW[:1]})")
expect(len(WHISPER_CALLS) >= 2 and WHISPER_CALLS[0] == WHISPER_CALLS[1],
       "…then the same chunk is sent again and the scan carries on by itself")
screen_prompts = [kw for n, kw in PROMPTS if n == "submit_moments"]
expect(screen_prompts and "big wins" in screen_prompts[0]["system"] and "TJR must be the main person"
       in screen_prompts[0]["system"], "screening is told what the campaign looks for and who must carry it")
expect(all(kw["model"] == scan.CLAUDE_SCREEN_MODEL for kw in screen_prompts), "screening uses the screening model")
ms = creators.list_moments(cid, status="all", limit=1000)
expect(not any((m.get("signals") or {}).get("quote", "").startswith("I quit trading") for m in ms),
       "the invented quote never became a moment")
stories = [m for m in ms if m["story_key"] == "first-million"]
kept_story = [m for m in stories if m["status"] != "dropped"]
expect(len(stories) > 1 and len(kept_story) == 1 and
       creators.get_catalog(kept_story[0]["catalog_id"])["video_id"] == "aaaaaaaaaa1",
       f"the first-million story told in {len(stories)} videos is kept once — the best telling")
expect(all("same story" in m["drop_reason"].lower() for m in stories if m["status"] == "dropped"),
       "the repeats say why they were dropped")
a1 = rows["aaaaaaaaaa1"]["id"]
a1_money = next(m for m in ms if m["catalog_id"] == a1 and m["kind"] == "money")
expect(a1_money["hook"] == "", "a ranked hook with an unsaid number is left empty")
expect(kept_story[0]["hook"] == "His first million came in 2019", f"hooks written ({kept_story[0]['hook']})")
sig = kept_story[0]["signals"]
expect(sig.get("heat") == 1.0 and sig.get("outlier") and sig.get("rank") == 0.95,
       f"signals stored: most replayed, outlier, rank ({ {k: sig.get(k) for k in ('heat', 'outlier', 'rank')} })")
expect(kept_story[0]["score"] == scan.merge_score(sig), "the stored score is the merged score")
rank_prompts = [kw for n, kw in PROMPTS if n == "rank_moments"]
expect(rank_prompts and all(kw["model"] == scan.CLAUDE_MODEL for kw in rank_prompts), "ranking uses the main model")

print("\n== sections: only the best moments' parts are downloaded")
expect(SECTION_CALLS, "sections were downloaded")
url0, secs0, out0, pad0 = SECTION_CALLS[0]
expect(out0 == DATA / "sections" / cid and pad0 == 8.0, f"into DATA_DIR/sections/<creator>, ±8 s ({out0}, {pad0})")
expect(url0 == "https://www.youtube.com/watch?v=aaaaaaaaaa1" and len(secs0) == 2 and
       all(isinstance(a, float) for s in secs0 for a in s), f"one call per video, its moments' (start, end): {secs0}")
fetched = [m for m in creators.list_moments(cid, status="all", limit=1000) if m.get("section_path")]
expect(len(fetched) == 2 and all(Path(m["section_path"]).is_file() for m in fetched),
       "the top 3 asked; 2 came down (the third video's download failed)")
f0 = fetched[0]
expect(abs(f0["section_offset"] - max(0.0, f0["start"] - 8.0)) < 0.01, "where the section starts is kept")
a2_row = rows["aaaaaaaaaa2"]
a2_m = next(m for m in creators.list_moments(cid, catalog_id=a2_row["id"], status="all")
            if (m.get("signals") or {}).get("fetch_tries"))
expect(a2_m["status"] == "candidate" and a2_m["signals"]["fetch_tries"] == 1 and "403" in a2_m["signals"]["fetch_error"],
       "a failed download is remembered on the moment, which stays a candidate")
n = scan.fetch_moments(catalog.Net(sleep=fake_sleep, clock=lambda: CLOCK[0]), cid, creators.get_catalog(a2_row["id"]),
                       [a2_m])
a2_m = creators.get_moment(a2_m["id"])
expect(n == 1 and a2_m["status"] == "fetched" and Path(a2_m["section_path"]).is_file(),
       "the next try picks it up where it failed")

print("\n== the picture check")
look = scan.look_at_section(FOOTAGE, 1.0, 9.0)
expect(look.get("face_share", 0) >= 0.5 and look.get("face_size", 0) > 0.04,
       f"the drawn person's face is found ({look.get('face_share')}, size {look.get('face_size')})")
expect(look.get("motion_raw", 0) > 0 and look.get("frames", 0) >= 30, f"movement measured ({look.get('motion_raw')})")
dark = scan.look_at_section(BLACK, 1.0, 9.0)
expect(dark.get("face_share") == 0 and dark.get("motion_raw", 1) < 0.005, "a black picture: no face, no movement")
checked = [m for m in creators.list_moments(cid, status="all", limit=1000) if m["status"] == "checked"]
expect(len(checked) == 2 and all((m["signals"] or {}).get("face_share", 0) > 0 for m in checked),
       "the scan checked its downloaded moments (faces found)")
funny = next((m for m in checked if m["kind"] == "funny"), None)
expect(funny and funny["signals"].get("visual") == 0.8 and funny["signals"].get("on_screen") == "yes",
       "a funny moment got Claude's look at its frames")
look_prompts = [kw for n, kw in PROMPTS if n == "rate_picture"]
expect(look_prompts and "don't identify anyone" in look_prompts[0]["messages"][0]["content"][-1]["text"]
       and sum(1 for c in look_prompts[0]["messages"][0]["content"] if c["type"] == "image") == 5,
       "Claude sees 5 frames and is told not to identify anyone from their face")
scan.check_moment(creators.get_moment(a2_m["id"]), rb)
a2_m = creators.get_moment(a2_m["id"])
expect(a2_m["status"] == "dropped" and "TJR in the picture" in a2_m["drop_reason"],
       f"nobody on screen, and the campaign needs TJR: dropped ({a2_m['drop_reason']})")
expect(scan.creator_visible(None, {}, {"face_share": 0.0}, rb) is False and
       scan.creator_visible(None, {}, {"face_share": 0.1, "face_size": 0.02}, rb) is None,
       "creator_visible: no face → no; a small face somewhere → can't tell")

print("\n== Telegram and the progress line")
sent = [m["text"] for m in drain() if m["kind"] == "text"]
expect(any("found" in t and "videos" in t for t in sent), "Telegram: what was found")
expect(any("scan finished" in t and "moments" in t and "open Creators" in t for t in sent),
       f"Telegram: scan finished — N moments — open Creators ({[t for t in sent if 'finished' in t][:1]})")
expect(any("transcription" in t for t in sent), "Telegram: the long wait for the transcription allowance")
expect(st["text"].startswith("Found ") and "Read " in st["text"] and "moments" in st["text"],
       f"status text: {st['text']}")
mj = scan.moment_json(fetched[0])
expect(mj["section_url"] == f"/media/section/{fetched[0]['id']}.mp4" and mj["video"]["title"] and mj["signals"]
       and mj["video"]["link_at"].startswith("https://www.youtube.com/watch?v=") and "&t=" in mj["video"]["link_at"],
       "a moment for the browser: section, video, link at the moment, signals in words")
expect(scan.moment_thumb(fetched[0]["id"]) and scan.section_file(fetched[0]["id"]),
       "a moment's picture and section file are served from disk")

# =====================================================================================================
print("\n== pause, restart, resume: nothing is read twice")
pid = creators.create_creator("Pause test", ["https://www.youtube.com/@PauseChannel"], "", {"fetch_top": 0})
PAUSE_AT = "watch?v=pppppppppp2"
RUNNER.hook = lambda url: scan.pause(pid) if PAUSE_AT in url else None
scan.start(pid)
p1 = wait_scan(pid)
RUNNER.hook = None
prow = {r["video_id"]: r for r in creators.list_catalog(pid)}
expect(p1["status"] == "paused" and "Resume" in p1["message"], f"pause stops the scan ({p1['status']}: {p1['message']})")
done_before = [v for v, r in prow.items() if r["status"] in ("words", "scored", "done")]
expect(done_before and prow["pppppppppp2"]["status"] == "listed", f"read before the pause: {done_before}")
creators.update_scan(p1["id"], status="running")          # as if ClipAgent was closed while it ran
scan.settle_interrupted()
p2 = creators.latest_scan(pid)
expect(p2["status"] == "paused" and "closed" in p2["message"], f"after a restart: paused with a plain note ({p2['message']})")
calls_before = len(RUNNER.calls)
screens_before = sum(1 for n, _ in PROMPTS if n == "submit_moments")
sid2 = scan.start(pid)
p3 = wait_scan(pid)
expect(sid2 == p1["id"] and p3["status"] == "done", "Resume carries on the same scan to the end")
again = [c for c in RUNNER.calls[calls_before:] if any(v in c[1] for v in done_before)]
expect(not again, f"videos read before the pause aren't requested again ({again[:2]})")
expect(not any(c[1].endswith("/videos") for c in RUNNER.calls[calls_before:]), "the channel isn't listed again")
expect(sum(1 for n, _ in PROMPTS if n == "submit_moments") - screens_before <= 2,
       "Claude only reads what wasn't read before")

print("\n== the bot check stops everything")
bid = creators.create_creator("Bot test", ["https://www.youtube.com/@BotChannel"], "", {"fetch_top": 0})
RUNNER.fail["watch?v=zzzzzzzzzz2"] = ("ERROR: [youtube] zzzzzzzzzz2: Sign in to confirm you’re not a bot. Use "
                                      "--cookies-from-browser or --cookies for the authentication.")
drain()
scan.start(bid)
b1 = wait_scan(bid)
expect(b1["status"] == "paused" and "blocking downloads" in b1["message"],
       f"paused with the plain message ({b1['message'][:70]}…)")
expect(BOT["block"] and BOT["block"]["platform"] == "youtube", "the download pause is set for everything else too")
expect(any("paused" in m["text"] for m in drain() if m["kind"] == "text"), "Telegram: the scan paused, and why")
n_calls = len(RUNNER.calls)
scan.start(bid)
b2 = wait_scan(bid)
expect(b2["status"] == "paused" and len(RUNNER.calls) == n_calls,
       "while YouTube is blocking, Resume stops again without a single request")
del RUNNER.fail["watch?v=zzzzzzzzzz2"]
media.clear_bot_block()
scan.start(bid)
b3 = wait_scan(bid)
expect(b3["status"] == "done", "once the block clears, Resume finishes the scan")

print("\n== two creators sharing a channel; a channel with no videos")
words_requests = sum(1 for c in RUNNER.calls if c[0] == "text" and "bbbbbbbbbb2" in c[1])
fan = creators.create_creator("T Riches fan page", ["https://www.youtube.com/@TRichesTrades"], "", {"fetch_top": 0})
scan.start(fan)
f1 = wait_scan(fan)
frows = {r["video_id"]: r for r in creators.list_catalog(fan)}
expect(f1["status"] == "done" and "bbbbbbbbbb1" in frows, "the shared channel is this creator's catalog too")
expect(sum(1 for c in RUNNER.calls if c[0] == "text" and "bbbbbbbbbb2" in c[1]) == words_requests,
       "a video already read for the other creator isn't read again (its words are copied)")
expect(creators.list_moments(fan, catalog_id=frows["bbbbbbbbbb2"]["id"]), "…and gets this creator's own moments")
empty = creators.create_creator("Empty", ["https://www.youtube.com/@EmptyChannel"], "", {})
scan.start(empty)
e1 = wait_scan(empty)
expect(e1["status"] == "done" and "No videos found" in e1["message"], f"no videos: done, in plain words ({e1['message']})")

print("\n== the estimate before a scan")
est_id = creators.create_creator("Estimate", ["https://www.youtube.com/@TJRTrades", "https://www.twitch.tv/tjr"], "",
                                 {"fetch_top": 40})
est = scan.estimate(est_id)
expect(est["videos"] == creators.catalog_counts(est_id)["listed"] and est["videos"] >= 8,
       f"lists first, then counts the videos to read ({est['videos']})")
expect(est["with_subs"] + est["need_whisper"] == est["videos"] and est["need_whisper"] >= 2,
       f"with subtitles {est['with_subs']}, needing transcription {est['need_whisper']}")
expect(0 < est["whisper_hours"] <= est["hours"] and abs(est["whisper_hours"] - (2400 + 3600) / 3600) < 0.6,
       f"Whisper hours: the 6-h stream counts only its loudest hour ({est['whisper_hours']} of {est['hours']} h)")
expect(est["claude_tokens"] > 100000 and 0 < est["claude_cost_usd"] < 50, f"Claude: {est['claude_tokens']:,} tokens, "
       f"${est['claude_cost_usd']}")
expect(est["download_mb"] > 0 and est["minutes"] > 0, f"{est['download_mb']} MB, {est['minutes']} min")
expect("assumption" in est["text"] and "never whole videos" in est["text"], "the assumptions are said in the text")
expect(scan.estimate(cid)["videos"] == 0, "a creator already scanned: nothing left to read")

print("\n== search")
found = scan.search(cid, "see you tomorrow")
expect(len(found) == 1 and found[0]["quote"].startswith("alright guys") and found[0]["status"] == "candidate",
       f"a search returns the stretch that answers it, as a moment ({[f['quote'] for f in found]})")
expect("Found by searching" in " ".join(found[0]["signals"]), "…saying it came from the search")
expect(scan.search(cid, "zebra crossing") == [], "nothing matches: nothing returned")
again = scan.search(cid, "see you tomorrow")
expect(again and again[0]["id"] == found[0]["id"], "the same search again returns the same moment (no duplicate)")

print("\n== moments → clips")
started = []


def fake_run_job(job_id, url=None, upload_path=None):
    started.append((job_id, url, upload_path))
    store.update_job(job_id, status="done", stage="Done", progress=100)


pipeline.run_job = fake_run_job
unfetched = next(m for m in creators.list_moments(cid, status="candidate", limit=100) if not m.get("section_path"))
pick = [funny["id"], unfetched["id"]]
n_sections = len(SECTION_CALLS)
jobs = scan.make_clips(pick, {"platforms": ["tiktok"], "style_recipe": "label"})
end = time.time() + 60
while len(started) < 2 and time.time() < end:
    time.sleep(0.05)
expect(len(jobs) == 2 and len(started) == 2, f"two moments → two jobs, run one after another ({len(started)})")
j0 = store.get_job(jobs[0])
s0 = json.loads(j0["settings"])
fm = creators.get_moment(funny["id"])
win = s0.get("only_window") or {}
expect(abs(win.get("start", -1) - (fm["start"] - fm["section_offset"])) < 0.02 and
       abs(win.get("offset", -1) - fm["section_offset"]) < 0.01 and win.get("type") == "funny",
       f"the job knows the clip wanted, on the section's clock ({win})")
expect(started[0][2] == Path(fm["section_path"]) and started[0][1] is None, "its source is the downloaded section")
expect(j0["campaign_id"] == camp_id and (s0.get("campaign") or {}).get("id") == camp_id and s0.get("style_recipe") == "label"
       and s0.get("platforms") == ["tiktok"], "the creator's campaign comes along, so the gate applies")
expect(j0["source"] == "https://www.youtube.com/watch?v=aaaaaaaaaa1" and "·" in j0["title"],
       "the job keeps the video's link and a readable title")
expect(len(SECTION_CALLS) == n_sections + 1 and creators.get_moment(unfetched["id"]).get("section_path"),
       "a moment not downloaded yet is fetched first")
expect(all(creators.get_moment(i)["status"] == "used" and creators.get_moment(i)["used_in"] for i in pick),
       "the moments are marked used, with their job")
clip = pipeline.window_clip({"start": 8.0, "end": 30.0, "offset": 100.0, "hook": "Bro bought the top", "type": "funny",
                             "score": 80, "kind": "funny"}, 46.0, {"platforms": ["tiktok"]})
expect(clip["start"] == 8.0 and clip["end"] == 30.0 and clip["type"] == "funny" and clip["hook"] == "Bro bought the top",
       "the pipeline's one clip comes from the window")
clip = pipeline.window_clip({"start": 8.0, "end": 30.0, "offset": 100.0}, 3000.0, {}, from_download=True)
expect(clip["start"] == 108.0 and clip["end"] == 130.0, "re-run from the full video: the section's offset is added")
clip = pipeline.window_clip({"start": 0.0, "end": 85.0}, 100.0, {"platforms": ["youtube"]})
expect(clip["end"] - clip["start"] <= highlights.length_window(None, None, ["youtube"])[1],
       "a window longer than the platform allows is trimmed")
from app import main  # noqa: E402
rr = main._settings({**s0})
expect(rr.get("only_window") == s0["only_window"], "Try again keeps the wanted clip")

print("\n== moments → an edit (no Claude pick, the Edit Maker's checks)")


def quick_render(timeline, sources, sound, out, thumb, progress=None):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=gray:s=108x192:d={timeline['length']}",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", str(timeline["length"]),
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(out), "-frames:v", "1", str(thumb)], check=True)
    return {"length": timeline["length"]}


editrender.render = quick_render


def wait_edit(eid, seconds=120):
    end = time.time() + seconds
    while time.time() < end:
        e = store.get_edit(eid)
        if e and e["status"] in ("done", "failed"):
            return e
        time.sleep(0.1)
    return store.get_edit(eid)


picks_before = sum(1 for n, _ in PROMPTS if n == "pick_moments")
ems = [kept_story[0]["id"], funny["id"], next(m["id"] for m in creators.list_moments(cid, status="candidate", limit=100)
                                             if not m.get("section_path") and m["kind"] != "funny")]
try:
    scan.make_edit(ems, {"style": "velocity", "campaign_id": ""})
    expect(False, "a music style without a song is refused")
except ValueError as exc:
    expect("cut to music" in str(exc), f"a music style without a song: plain words ({exc})")
eid = scan.make_edit(ems, {"style": "cinematic", "campaign_id": "", "length": 20})
e = wait_edit(eid)
plan = e.get("plan") or {}
expect(e["status"] == "done", f"the edit is made ({e['status']}: {e.get('error')})")
expect(sum(1 for n, _ in PROMPTS if n == "pick_moments") == picks_before, "Claude wasn't asked to pick moments")
srcs = (e.get("settings") or {}).get("sources") or []
src_jobs = [store.get_job(s) for s in srcs]
expect(len(srcs) >= 2 and all(j and j["status"] == "done" and j["transcript"] and
                                Path(j["source_path"]).parent == DATA / "sections" / cid for j in src_jobs),
       f"its footage is the moments' sections ({len(srcs)} sources)")
expect(len(plan.get("moments") or []) == 3 and sum(1 for m in plan["moments"] if m["drop"]) == 1,
       "every given moment is in it, one on the drop")
expect(plan.get("hook") == kept_story[0]["hook"], f"the best moment's hook leads it ({plan.get('hook')})")
expect(all(creators.get_moment(i)["status"] == "used" and eid in creators.get_moment(i)["used_in"] for i in ems),
       "the moments are marked used in the edit")
eid2 = scan.make_edit([funny["id"], kept_story[0]["id"]], {"style": "cinematic"})
e2 = wait_edit(eid2)
expect(e2["status"] == "failed" and "joining different moments" in (e2.get("error") or ""),
       f"the creator's campaign applies: its brief doesn't allow joining moments ({(e2.get('error') or '')[:70]})")
eid3 = scan.make_edit([funny["id"]], {"style": "cinematic", "campaign_id": "", "length": 15})
e3 = wait_edit(eid3)
expect(e3["status"] == "done" and len(set((e3["settings"] or {}).get("sources") or [])) == 1
       and (e3["settings"]["sources"][0] in srcs), "a section already made into footage is reused")

print("\n== keep watching: a new upload on the creator's channel")
drain()
money.save_settings({"watch": [{"url": "https://www.youtube.com/@TJRTrades", "campaign_id": camp_id, "added": 0}],
                     "seen": []})
money._latest = lambda url, n=6: [{"id": "aaaaaaaaa10", "title": "LIVE: today's session",
                                   "url": "https://www.youtube.com/watch?v=aaaaaaaaa10", "duration": 1800}]
list_calls = sum(1 for c in RUNNER.calls if c[1].endswith("/videos"))
fresh = money.scout()
end = time.time() + 5
while time.time() < end and creators.latest_scan(cid)["status"] == "done" and \
        creators.latest_scan(cid)["id"] == final["id"]:
    time.sleep(0.05)
nu = wait_scan(cid)
new_row = next((r for r in creators.list_catalog(cid) if r["video_id"] == "aaaaaaaaa10"), None)
expect(fresh and new_row and new_row["status"] == "done", "the new upload joined the catalog and was read")
expect(nu["status"] == "done" and (nu["counters"] or {}).get("mode") == "new_uploads", "a short scan of just the new ones")
expect(sum(1 for c in RUNNER.calls if c[1].endswith("/videos")) == list_calls, "the channel isn't listed all over again")
wait_scan(est_id)
est_rows = creators.list_catalog(est_id)
expect(next(r for r in est_rows if r["video_id"] == "aaaaaaaaa10")["status"] == "done" and
       all(r["status"] in ("listed", "skipped") for r in est_rows if r["video_id"] != "aaaaaaaaa10"),
       "a creator sharing the channel but never scanned: only the new video is read, not its whole catalog")
texts = [m["text"] for m in drain() if m["kind"] == "text"]
expect(any("new moment" in t and "want clips? Open Creators" in t for t in texts),
       f"Telegram: N new moments … want clips? ({[t for t in texts if 'new moment' in t][:1]})")

print("\n== restart while waiting for the transcription allowance")
wid = creators.create_creator("Waiter", ["https://www.youtube.com/@EmptyChannel"], "", {})
wsid = creators.create_scan(wid)
creators.update_scan(wsid, status="waiting", stage="words", wait_until=time.time() + 3600,
                     message="Waiting 60 min for transcription quota")
scan.settle_interrupted()
expect(wid in scan._timers and creators.latest_scan(wid)["status"] == "waiting",
       "a scan waiting when ClipAgent closed keeps waiting, and resumes by itself when the time comes")
scan.pause(wid)
expect(wid not in scan._timers and creators.latest_scan(wid)["status"] == "paused", "Pause cancels the wait")

print("\n== the creators' data")
c = creators.get_creator(cid)
expect(c["settings"]["fetch_top"] == 3 and c["settings"]["include_shorts"] is False and c["links"][1] ==
       "youtube.com/@TRichesTrades", "settings kept with defaults filled; links kept")
creators.update_creator(cid, settings={"min_views": 1000})
expect(creators.get_creator(cid)["settings"]["fetch_top"] == 3 and creators.get_creator(cid)["settings"]["min_views"] == 1000,
       "changing one setting keeps the others")
expect(creators.list_moments(cid, kind="story") and all(m["kind"] == "story" for m in creators.list_moments(cid, kind="story")),
       "moments filtered by kind")
expect(all(m["status"] != "dropped" for m in creators.list_moments(cid)), "dropped moments are left out by default")
gm = creators.list_moments(cid, q="gamblers")
expect(gm and all("gamblers" in m["text"].lower() for m in gm), "moments searched by their words")
sec_files = [m["section_path"] for m in creators.list_moments(cid, status="all", limit=1000) if m.get("section_path")]
creators.delete_creator(empty)
expect(creators.get_creator(empty) is None and not creators.list_catalog(empty), "deleting a creator removes its rows")
expect(all(Path(p).is_file() for p in sec_files), "…and never touches files")

if FAILS:
    print(f"\n{len(FAILS)} check(s) FAILED")
    sys.exit(1)
print("\nall checks behaved")
