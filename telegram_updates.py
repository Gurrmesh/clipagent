"""Telegram updates and plain download errors, without touching the network.

Run: python tests/telegram_updates.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="tg_test_"))
os.environ["DATA_DIR"] = str(TMP)
os.environ["TELEGRAM_BOT_TOKEN"] = "123:test"
os.environ.pop("TELEGRAM_CHAT_ID", None)
os.environ.setdefault("WHISPER_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main, media, notify, store  # noqa: E402

FAILS = 0


def expect(ok, label):
    global FAILS
    print(("  ok   " if ok else "  FAIL ") + label)
    FAILS += 0 if ok else 1


calls = []
notify._call = lambda method, data=None, files=None, timeout=60: calls.append((method, data)) or {}


def drain():
    items = []
    while not notify._out.empty():
        items.append(notify._out.get())
    return items


print("== owner: the first /start connects, everyone else is turned away")
replies = []
handler = lambda text: replies.append(text) or f"echo {text}"
notify.handle_update({"message": {"chat": {"id": 111, "type": "private"}, "text": "hello"}}, handler)
expect(not notify.connected() and not drain(), "a message before /start does nothing")
notify.handle_update({"message": {"chat": {"id": 111, "type": "private"}, "text": "/start"}}, handler)
expect(notify.chat_id() == "111", "/start makes that chat the owner")
expect("Connected" in drain()[0]["text"], "and says so")
notify.handle_update({"message": {"chat": {"id": 999, "type": "private"}, "text": "/status"}}, handler)
expect(calls and calls[-1][1]["text"] == "This bot is private." and not replies,
       "a stranger is told the bot is private, and nothing runs")
notify.handle_update({"message": {"chat": {"id": -5, "type": "group"}, "text": "/status"}}, handler)
expect(not replies, "group chats are ignored")
notify.handle_update({"message": {"chat": {"id": 111, "type": "private"}, "text": "/status"}}, handler)
expect(replies == ["/status"] and drain()[0]["text"] == "echo /status", "the owner gets an answer")

print("\n== a finished job: summary, then the clips best first, blocked ones held back")
job = store.create_job("Lenny podcast", "https://youtu.be/x", {})
store.update_job(job, status="done", stage="Done", title="Lenny podcast")
clip_file = TMP / "c.mp4"
clip_file.write_bytes(b"0" * 1000)


def mk(rank, status, hook, alt_of="", comp=None):
    cid = store.create_clip(job, {"start": 10, "end": 40, "title": "t", "hook": hook, "score": 90 - rank,
                                  "reason": "", "tags": [], "caption": "This changes everything",
                                  "hashtags": ["lovable", "ai"], "alt_of": alt_of, "rank": rank})
    store.update_clip(cid, status="ready", file=str(clip_file), compliance=json.dumps(comp) if comp else None)
    return cid


a = mk(1, "ready", "He made $200M in 8 months", comp={"status": "ready", "summary": "Ready"})
mk(2, "ready", "Second one", comp={"status": "check", "summary": "Check before posting: hook tone"})
mk(3, "ready", "Third one", comp={"status": "blocked", "summary": "Blocked: missing logo"})
mk(1, "ready", "alt version", alt_of=a, comp={"status": "ready", "summary": "Ready"})
notify.job_finished(job)
out = drain()
summary = out[0]["text"]
expect("Lenny podcast" in summary and "3 clips ready" in summary, "summary names the video and counts clips")
expect("1 ready · 1 check · 1 blocked" in summary, "campaign check counts")
expect("1 other version" in summary and "Not sending 1 blocked" in summary, "mentions alternates and held-back clips")
videos = [i for i in out if i["kind"] == "video"]
expect(len(videos) == 2, "sends the two postable clips, not the blocked one or the alternate")
expect("He made $200M" in videos[0]["caption"] and "#1" in videos[0]["caption"], "best first, hook on it")
expect("<code>This changes everything\n\n#lovable #ai</code>" in videos[0]["caption"], "caption + tags to copy")
expect("Check before posting" in videos[1]["caption"], "a 'check' clip says what to check")
expect(all(len(v["caption"]) <= 1024 for v in videos), "captions fit Telegram's limit")

failed = store.create_job("Bad link", "https://youtu.be/y", {})
store.update_job(failed, status="failed", error="YouTube is blocking downloads from this PC right now.")
notify.job_finished(failed)
msg = drain()[0]["text"]
expect("didn't work" in msg and "blocking downloads" in msg and "/retry" in msg, "a failure is explained, with /retry")

print("\n== a long transcription wait is mentioned once per job")
notify.problem(job, "rate", "waiting")
notify.problem(job, "rate", "waiting")
expect(len(drain()) == 1, "only once")

print("\n== plain download errors")
e = media.explain_download_error
expect("bot" in e("ERROR: [youtube] abc: Sign in to confirm you're not a bot.").lower(), "YouTube bot check")
expect("Frame.io" in e("ERROR: Unsupported URL: https://f.io/x"), "unsupported link")
expect("private" in e("ERROR: [youtube] x: Private video. Sign in if you've been granted access"), "private")
expect("unavailable" in e("ERROR: [youtube] x: Video unavailable"), "unavailable")
expect("internet" in e("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>"),
       "no internet")
expect(e("ERROR: something odd happened").startswith("Couldn't download that link: something odd"), "fallback")

print("\n== commands")
ran = []
main.pipeline.run_job = lambda job_id, url=None, upload=None: (ran.append((job_id, url, upload)), time.sleep(0.2))
reply = main.telegram_command("https://www.youtube.com/watch?v=abc 5")
expect("5 clips" in reply and "starting now" in reply, f"a link starts a run ({reply.splitlines()[0]})")
reply2 = main.telegram_command("https://youtu.be/def")
expect("#2 in line" in reply2 or "starting now" in reply2, "a second link queues behind it")
time.sleep(0.8)
expect([r[1] for r in ran] == ["https://www.youtube.com/watch?v=abc", "https://youtu.be/def"],
       "they ran one after another, in order")
jobs = store.list_jobs(5)
expect(json.loads(store.get_job(jobs[1]["id"])["settings"])["max_clips"] == 5, "the clip count was used")
expect("Unknown" not in main.telegram_command("https://youtu.be/zzz nosuchcampaign") and
       "No campaign matched" in main.telegram_command("https://youtu.be/zzz nosuchcampaign"),
       "unknown campaign words fall back to a normal run, and say so")
expect("Working on" in main.telegram_command("/status") or "Nothing running" in main.telegram_command("/status"),
       "/status answers")
expect("No campaigns" in main.telegram_command("/campaigns"), "/campaigns with none")
expect("Trying" in main.telegram_command("/retry"), "/retry restarts the failed link")
expect("What I can do" in main.telegram_command("hi"), "anything else gets the help")

print("\n== jobs cut off by a restart are settled at startup")
j1 = store.create_job("cut off", "u", {}); store.update_job(j1, status="running", stage="Transcribing speech")
j2 = store.create_job("rerendered", "u", {}); store.update_job(j2, status="running", stage="Done — 5 ready to post")
main._settle_interrupted_jobs()
expect(store.get_job(j1)["status"] == "failed" and "restarted" in store.get_job(j1)["error"], "a cut-off job fails, with why")
expect(store.get_job(j2)["status"] == "done", "a finished one just stuck on running becomes done")

print("\nall checks behaved" if not FAILS else f"\n{FAILS} check(s) failed")
sys.exit(1 if FAILS else 0)
