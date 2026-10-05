"""Posts, earnings, the planner, reminders, scout and the summary — offline.

Run: python tests/money_side.py
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="money_")
os.environ["TELEGRAM_BOT_TOKEN"] = "1:x"
os.environ["TELEGRAM_CHAT_ID"] = "42"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main, money, notify, store  # noqa: E402

FAILS = 0


def expect(ok, label):
    global FAILS
    print(("  ok   " if ok else "  FAIL ") + label)
    FAILS += 0 if ok else 1


def drain():
    out = []
    while not notify._out.empty():
        out.append(notify._out.get())
    return out


print("== a campaign's pay terms decide the estimate")
camp = store.save_campaign("Lovable", "source", "brief", {"pay": {"per_1k": 2.0, "min_views": 5000, "max_per_post": 300}})
camp_id = camp if isinstance(camp, str) else camp["id"]
job = store.create_job("Lenny podcast", "u", {})
store.update_job(job, status="done", campaign_id=camp_id, title="Lenny podcast")
clip_file = Path(os.environ["DATA_DIR"]) / "c.mp4"
clip_file.write_bytes(b"0" * 100)
clips = []
for rank in (1, 2, 3, 4):
    cid = store.create_clip(job, {"start": 0, "end": 20, "title": "t", "hook": f"Hook {rank}", "score": 90 - rank,
                                  "reason": "", "tags": [], "rank": rank, "edits": {"style": "label" if rank % 2 else "wordpop"}})
    store.update_clip(cid, status="ready", file=str(clip_file))
    clips.append(cid)
p = money.add(clips[0], url="https://www.tiktok.com/@gsclips/video/123")
expect(p["platform"] == "tiktok" and p["account"] == "@gsclips" and p["rate"] == 2.0, "platform, account and rate filled in")
money.update(p["id"], views=4000)
expect(money.estimate(money.get(p["id"])) == 0.0, "under the brief's minimum: pays nothing")
money.update(p["id"], views=60000)
expect(money.estimate(money.get(p["id"])) == 120.0, "60K views x $2/1K = $120")
money.update(p["id"], views=900000)
expect(money.estimate(money.get(p["id"])) == 300.0, "capped at the brief's max per post")

print("\n== views checks and milestones")
money.fetch_stats = lambda url: {"views": 120000, "likes": 5000, "comments": 300}
money.update(p["id"], views=0, milestone=0)
drain()
got = money.check(money.get(p["id"]))
msgs = drain()
expect(got["views"] == 120000 and got["milestone"] == 100000, "views saved, milestone 100K")
expect(msgs and "passed <b>100,000</b>" in msgs[0]["text"] and "$240.00" in msgs[0]["text"],
       "Telegram ping for 100K, with what it has earned")
money.check(money.get(p["id"]))
expect(not drain(), "no repeat ping for the same milestone")
now = time.time()
expect(money.due_for_check({"status": "posted", "url": "x", "posted_at": now - 7200, "checked_at": now - 4000}, now),
       "first day: checked hourly")
expect(not money.due_for_check({"status": "posted", "url": "x", "posted_at": now - 3 * 86400,
                                "checked_at": now - 4000}, now), "later: every 6 hours")

print("\n== the planner")
money.save_settings({"accounts": {"tiktok": ["@gsclips"], "instagram": ["@gs.reels"], "youtube": []}, "per_day": 3})
made = money.plan_job(job)
expect(len(made) == 7, f"4 clips x 2 accounts, minus the one already posted = 7 (got {len(made)})")
by_acc = {}
for m in made:
    by_acc.setdefault(m["account"], []).append(m)
for acc, ps in by_acc.items():
    days = {}
    for m in ps:
        d = datetime.fromtimestamp(m["planned_at"]).date()
        days[d] = days.get(d, 0) + 1
    expect(max(days.values()) <= 3, f"{acc}: at most 3 a day")
    times = sorted(m["planned_at"] for m in ps)
    expect(all(b - a >= 3 * 3600 for a, b in zip(times, times[1:])), f"{acc}: at least 3 h apart")
expect(by_acc["@gs.reels"][0]["clip_id"] == clips[0], "best clip goes out first")
expect(not money.plan_job(job), "planning again adds nothing")

print("\n== reminders")
first = sorted(made, key=lambda m: m["planned_at"])[0]
money.update(first["id"], planned_at=time.time() - 30)
drain()
money.tick()
msgs = drain()
expect(any("Time to post #" in m.get("text", "") for m in msgs) and any(m["kind"] == "video" for m in msgs),
       "reminder with the clip attached")
money.tick()
expect(not [m for m in drain() if "Time to post" in m.get("text", "")], "only once")

print("\n== Telegram commands")
reply = main.telegram_command(f"/posted {first['n']} https://www.instagram.com/reel/ABC/")
expect("is live" in reply and money.by_number(first["n"])["status"] == "posted", "/posted marks it live")
expect("views" in main.telegram_command("/money"), "/money")
expect("@gsclips" in main.telegram_command("/accounts"), "/accounts lists them")
main.telegram_command("/accounts youtube @gs_shorts")
expect(money.settings()["accounts"]["youtube"] == ["@gs_shorts"], "/accounts sets one platform")
money._latest = lambda url, n=6: [{"id": "v1", "title": "Old video", "url": "https://youtu.be/v1", "duration": 600}]
expect("Watching" in main.telegram_command("/watch https://www.youtube.com/@MrBeast"), "/watch")
money._latest = lambda url, n=6: [{"id": "v2", "title": "New video!", "url": "https://youtu.be/v2", "duration": 900},
                                  {"id": "v1", "title": "Old video", "url": "https://youtu.be/v1", "duration": 600}]
drain()
fresh = money.scout()
msgs = drain()
expect([v["id"] for v in fresh] == ["v2"] and "New upload" in msgs[0]["text"], "only the new upload is announced")
expect("Morning summary" in main.telegram_command("/summary"), "/summary")
ig = money.add("", url="https://www.instagram.com/reel/XYZ/")
expect("34,000 views" in main.telegram_command(f"/views {ig['n']} 34k"), "/views 12 34k sets an Instagram post's views")

print("\n== the summary goes out once a morning")
money.save_settings({"summary_sent": ""})
drain()
nine = datetime.now().replace(hour=9, minute=5).timestamp()
money.tick(nine)
money.tick(nine + 60)
expect(len([m for m in drain() if "Morning summary" in m.get("text", "")]) == 1, "exactly once")

print("\n== your results reach the style brain")
for i, cid in enumerate(clips):
    q = money.add(cid, url=f"https://www.tiktok.com/@gsclips/video/9{i}")
    money.update(q["id"], views=50000 if i % 2 == 0 else 5000)
ins = money.style_insights(min_posts=2)
expect("label" in ins and "wordpop" in ins, "average views per style, for the style brain")

print("\nall checks behaved" if not FAILS else f"\n{FAILS} check(s) failed")
sys.exit(1 if FAILS else 0)
