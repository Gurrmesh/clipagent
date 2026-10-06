"""The money side: what got posted where, how it's doing, what it's earning,
when to post next, and fresh videos worth clipping.

* Posts — every clip you post, with its link. Views are checked by ClipAgent
  itself (yt-dlp reads the public view count of a TikTok, Short or Reel), so
  the numbers fill in without you typing them.
* Earnings — views x the campaign's rate per 1,000, minus what the brief says
  doesn't pay (a minimum, a cap per post).
* What works for YOU — average views per style, so the style brain can lean
  on your own results, not just the internet's.
* Planner — 2-3 posts per account a day, a few hours apart, best clip first,
  never the same clip twice on one account; a Telegram reminder at each time.
* Scout — channels you clip from, checked for new uploads.
* Morning summary — yesterday's views and money, today's plan.
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from . import notify, store
from .config import DATA_DIR

SETTINGS_PATH = DATA_DIR / "money.json"
MILESTONES = [1_000, 10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000, 5_000_000]
SLOTS = [(12, 0), (16, 30), (20, 30)]        # local posting times: lunch, after school/work, prime time
CHECK_EVERY = 6 * 3600                       # view checks per post
TRACK_DAYS = 30                              # stop checking a post after this
SUMMARY_AT = (9, 0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id          TEXT PRIMARY KEY,
    n           INTEGER,
    clip_id     TEXT,
    job_id      TEXT,
    campaign_id TEXT,
    platform    TEXT,
    account     TEXT,
    url         TEXT,
    status      TEXT,
    planned_at  REAL,
    posted_at   REAL,
    reminded    INTEGER DEFAULT 0,
    views       INTEGER,
    likes       INTEGER,
    comments    INTEGER,
    checked_at  REAL,
    history     TEXT,
    milestone   INTEGER DEFAULT 0,
    rate        REAL,
    paid        REAL,
    style       TEXT,
    hook        TEXT,
    title       TEXT,
    created_at  REAL
);
"""


def init() -> None:
    with store.connect() as conn:
        conn.executescript(SCHEMA)


# --- settings (accounts, watched channels, summary) --------------------------------

def settings() -> Dict[str, Any]:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("accounts", {"tiktok": [], "instagram": [], "youtube": []})
    data.setdefault("per_day", 3)
    data.setdefault("watch", [])
    data.setdefault("seen", [])
    data.setdefault("summary_sent", "")
    return data


def save_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    current = settings()
    current.update({k: v for k, v in data.items() if v is not None})
    current["seen"] = current["seen"][-500:]
    SETTINGS_PATH.write_text(json.dumps(current, indent=1), encoding="utf-8")
    return current


# --- posts -------------------------------------------------------------------------

PLATFORM_OF = [("tiktok.com", "tiktok"), ("instagram.com", "instagram"), ("youtube.com", "youtube"),
               ("youtu.be", "youtube"), ("x.com", "x"), ("twitter.com", "x"), ("facebook.com", "facebook"),
               ("snapchat.com", "snapchat")]


def platform_of(url: str) -> str:
    u = (url or "").lower()
    return next((p for host, p in PLATFORM_OF if host in u), "")


def account_of(url: str) -> str:
    m = re.search(r"tiktok\.com/@([\w.]+)", url or "") or re.search(r"youtube\.com/@([\w.-]+)", url or "")
    return "@" + m.group(1) if m else ""


def _row(r) -> Dict[str, Any]:
    d = dict(r)
    d["history"] = json.loads(d.get("history") or "[]")
    return d


def get(post_id: str) -> Optional[Dict[str, Any]]:
    with store.connect() as conn:
        r = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    return _row(r) if r else None


def by_number(n: int) -> Optional[Dict[str, Any]]:
    with store.connect() as conn:
        r = conn.execute("SELECT * FROM posts WHERE n=?", (n,)).fetchone()
    return _row(r) if r else None


def list_posts(limit: int = 200, status: Optional[str] = None) -> List[Dict[str, Any]]:
    q = "SELECT * FROM posts" + (" WHERE status=?" if status else "") + \
        " ORDER BY COALESCE(posted_at, planned_at, created_at) DESC LIMIT ?"
    with store.connect() as conn:
        rows = conn.execute(q, ((status, limit) if status else (limit,))).fetchall()
    return [_row(r) for r in rows]


def _clip_facts(clip_id: str) -> Dict[str, Any]:
    clip = store.get_clip(clip_id) if clip_id else None
    if not clip:
        return {}
    job = store.get_job(clip["job_id"]) or {}
    edits = json.loads(clip.get("edits") or "{}")
    camp_id = job.get("campaign_id") or ""
    rate = None
    if camp_id:
        camp = store.get_campaign(camp_id)
        if camp:
            rate = ((camp.get("rulebook") or {}).get("pay") or {}).get("per_1k")
    return {"job_id": clip["job_id"], "campaign_id": camp_id, "style": edits.get("style") or "",
            "hook": clip.get("hook") or "", "title": job.get("title") or "", "rate": rate}


def _next_n(conn) -> int:
    r = conn.execute("SELECT COALESCE(MAX(n), 0) + 1 AS n FROM posts").fetchone()
    return int(r["n"])


def add(clip_id: str = "", platform: str = "", account: str = "", url: str = "",
        status: str = "posted", planned_at: Optional[float] = None, rate: Optional[float] = None) -> Dict[str, Any]:
    url = (url or "").strip()
    platform = platform or platform_of(url)
    account = account or account_of(url)
    facts = _clip_facts(clip_id)
    now = time.time()
    with store.connect() as conn:
        n = _next_n(conn)
        pid = store.new_id()
        conn.execute(
            "INSERT INTO posts (id,n,clip_id,job_id,campaign_id,platform,account,url,status,planned_at,posted_at,"
            "rate,style,hook,title,history,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, n, clip_id, facts.get("job_id", ""), facts.get("campaign_id", ""), platform, account, url,
             status, planned_at, now if status == "posted" else None,
             rate if rate is not None else facts.get("rate"), facts.get("style", ""), facts.get("hook", ""),
             facts.get("title", ""), "[]", now))
    return get(pid)


def update(post_id: str, **fields) -> Optional[Dict[str, Any]]:
    allowed = {"platform", "account", "url", "status", "planned_at", "posted_at", "views", "likes", "comments",
               "checked_at", "history", "milestone", "rate", "paid", "reminded"}
    fields = {k: (json.dumps(v) if k == "history" else v) for k, v in fields.items() if k in allowed}
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        with store.connect() as conn:
            conn.execute(f"UPDATE posts SET {cols} WHERE id=?", (*fields.values(), post_id))
    return get(post_id)


def mark_posted(post_id: str, url: str) -> Optional[Dict[str, Any]]:
    post = get(post_id)
    if not post:
        return None
    return update(post_id, url=url.strip(), status="posted", posted_at=time.time(),
                  platform=post.get("platform") or platform_of(url),
                  account=post.get("account") or account_of(url))


def delete(post_id: str) -> None:
    with store.connect() as conn:
        conn.execute("DELETE FROM posts WHERE id=?", (post_id,))


# --- views ------------------------------------------------------------------------

def fetch_stats(url: str) -> Optional[Dict[str, int]]:
    """The public view count of a post, read the way yt-dlp reads a page."""
    try:
        import yt_dlp
        from . import media
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                "extract_flat": False, **media.ytdlp_auth()}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        views = info.get("view_count")
        if views is None:
            return None
        return {"views": int(views), "likes": int(info.get("like_count") or 0),
                "comments": int(info.get("comment_count") or 0)}
    except Exception:
        return None


def check(post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Read one post's views now; note a milestone on Telegram."""
    if not post.get("url"):
        return None
    stats = fetch_stats(post["url"])
    now = time.time()
    if not stats:
        update(post["id"], checked_at=now)
        return None
    history = (post.get("history") or []) + [[round(now), stats["views"]]]
    passed = [m for m in MILESTONES if stats["views"] >= m > int(post.get("milestone") or 0)]
    updated = update(post["id"], views=stats["views"], likes=stats["likes"], comments=stats["comments"],
                     checked_at=now, history=history[-120:],
                     milestone=max(passed) if passed else int(post.get("milestone") or 0))
    if passed and max(passed) >= 10_000:
        earned = estimate(updated)
        notify.send(f"🚀 <b>Post #{post['n']}</b> passed <b>{max(passed):,}</b> views on "
                    f"{notify.esc(post.get('platform') or '')} {notify.esc(post.get('account') or '')}"
                    + (f" — about ${earned:,.2f} so far" if earned else "") +
                    f"\n<i>{notify.esc((post.get('hook') or post.get('title') or '')[:120])}</i>")
    return updated


def due_for_check(post: Dict[str, Any], now: float) -> bool:
    if post.get("status") != "posted" or not post.get("url"):
        return False
    posted = float(post.get("posted_at") or post.get("created_at") or now)
    if now - posted > TRACK_DAYS * 86400:
        return False
    last = float(post.get("checked_at") or 0)
    # Early on views move fast: check hourly for the first day, then every 6 h.
    every = 3600 if now - posted < 86400 else CHECK_EVERY
    return now - last >= every


# --- earnings -----------------------------------------------------------------------

def _pay(campaign_id: str) -> Dict[str, Any]:
    camp = store.get_campaign(campaign_id) if campaign_id else None
    return ((camp or {}).get("rulebook") or {}).get("pay") or {}


def estimate(post: Optional[Dict[str, Any]]) -> float:
    """Dollars this post has earned so far by the campaign's terms (0 when
    the rate isn't known). What the brief says doesn't pay, doesn't count."""
    if not post:
        return 0.0
    if post.get("paid") is not None:
        return float(post["paid"])
    pay = _pay(post.get("campaign_id") or "")
    rate = post.get("rate") or pay.get("per_1k")      # a rate added to the rules later still counts
    views = int(post.get("views") or 0)
    if not rate or not views:
        return 0.0
    if pay.get("min_views") and views < int(pay["min_views"]):
        return 0.0
    dollars = views / 1000.0 * float(rate)
    if pay.get("max_per_post"):
        dollars = min(dollars, float(pay["max_per_post"]))
    return round(dollars, 2)


def dashboard() -> Dict[str, Any]:
    posts = list_posts(1000)
    live = [p for p in posts if p["status"] == "posted"]
    total_views = sum(int(p.get("views") or 0) for p in live)
    earned = sum(estimate(p) for p in live)
    now = time.time()

    def group(key: str) -> List[Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for p in live:
            k = p.get(key) or "—"
            g = out.setdefault(k, {"name": k, "posts": 0, "views": 0, "earned": 0.0})
            g["posts"] += 1
            g["views"] += int(p.get("views") or 0)
            g["earned"] += estimate(p)
        rows = list(out.values())
        for g in rows:
            g["avg_views"] = int(g["views"] / g["posts"]) if g["posts"] else 0
            g["earned"] = round(g["earned"], 2)
        return sorted(rows, key=lambda g: -g["views"])

    camps = group("campaign_id")
    names = {c["id"]: c["name"] for c in store.list_campaigns()}
    for c in camps:
        c["name"] = names.get(c["name"], "No campaign" if c["name"] == "—" else c["name"])
    day_ago = now - 86400
    gained = 0
    for p in live:
        hist = p.get("history") or []
        before = next((v for t, v in reversed(hist) if t <= day_ago), 0)
        gained += max(0, int(p.get("views") or 0) - before)
    return {
        "daily": daily_views(live),
        "views": total_views, "earned": round(earned, 2), "posts": len(live),
        "planned": len([p for p in posts if p["status"] == "planned"]),
        "views_24h": gained,
        "by_campaign": camps, "by_platform": group("platform"), "by_account": group("account"),
        "by_style": group("style"),
        "top": sorted(live, key=lambda p: -int(p.get("views") or 0))[:5],
    }


def daily_views(posts: List[Dict[str, Any]], days: int = 14) -> List[Dict[str, Any]]:
    """Views gained each day over the last `days` days, across every post,
    from each post's view-count history (a post's first reading counts on
    the day it was taken)."""
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)
    gained = {start + timedelta(days=i): 0 for i in range(days)}
    for p in posts:
        prev = 0
        for t, v in sorted(p.get("history") or [], key=lambda x: x[0]):
            day = datetime.fromtimestamp(t).date()
            if day in gained:
                gained[day] += max(0, int(v) - prev)
            prev = max(prev, int(v))
    return [{"date": d.isoformat(), "views": v} for d, v in sorted(gained.items())]


def style_insights(min_posts: int = 3) -> str:
    """For the style brain: how each style does on YOUR accounts."""
    rows = [g for g in dashboard()["by_style"] if g["name"] != "—" and g["posts"] >= min_posts]
    if not rows:
        return ""
    lines = [f"- {g['name']}: {g['avg_views']:,} average views over {g['posts']} posts" for g in rows]
    return ("\n\nHow each style has done on this clipper's own accounts (real results — weigh these above "
            "the general reference when they disagree):\n" + "\n".join(lines))


# --- planner ----------------------------------------------------------------------------

def _slots(start: datetime, per_day: int) -> List[datetime]:
    """Posting times from `start` on, `per_day` a day."""
    slots = SLOTS[:max(1, min(len(SLOTS), per_day))]
    out = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while len(out) < 200:
        for h, m in slots:
            t = day.replace(hour=h, minute=m)
            if t > start + timedelta(minutes=10):
                out.append(t)
        day += timedelta(days=1)
    return out


def plan_job(job_id: str, accounts: Optional[Dict[str, List[str]]] = None,
             per_day: Optional[int] = None) -> List[Dict[str, Any]]:
    """Plan posts for a finished job's clips: best first, spread over the
    accounts' free slots, never the same clip twice on one account."""
    cfg = settings()
    accounts = accounts or cfg["accounts"]
    per_day = int(per_day or cfg["per_day"])
    clips = [c for c in store.list_clips(job_id) if not c.get("alt_of") and c.get("status") == "ready"
             and (json.loads(c.get("compliance") or "{}") or {}).get("status") != "blocked"]
    targets = [(plat, acc) for plat, accs in accounts.items() for acc in accs]
    if not clips or not targets:
        return []
    # Respect a campaign's platform when the clip was made for one.
    taken: Dict[tuple, set] = {}
    with store.connect() as conn:
        for r in conn.execute("SELECT platform, account, planned_at, posted_at, clip_id FROM posts "
                              "WHERE status IN ('planned','posted')"):
            key = (r["platform"], r["account"])
            when = r["planned_at"] or r["posted_at"]
            if when:
                taken.setdefault(key, set()).add(datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M"))
    already = {(p["clip_id"], p["platform"], p["account"]) for p in list_posts(2000)}
    made = []
    start = datetime.now()
    free = {t: [s for s in _slots(start, per_day)
                if s.strftime("%Y-%m-%d %H:%M") not in taken.get(t, set())] for t in targets}
    for clip in clips:                                    # already in rank order: best first
        post_meta = json.loads(clip.get("post") or "{}") or {}
        for plat, acc in targets:
            if post_meta.get("platform") and post_meta["platform"] != plat:
                continue
            if (clip["id"], plat, acc) in already:
                continue
            if not free[(plat, acc)]:
                continue
            when = free[(plat, acc)].pop(0)
            made.append(add(clip["id"], plat, acc, "", status="planned", planned_at=when.timestamp()))
            already.add((clip["id"], plat, acc))
    return made


# --- reminders, checks, scout, summary: one background loop ---------------------------

def _remind(post: Dict[str, Any]) -> None:
    from pathlib import Path
    clip = store.get_clip(post.get("clip_id") or "") or {}
    where = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube Shorts"}.get(post["platform"],
                                                                                          post["platform"])
    notify.send(f"⏰ <b>Time to post #{post['n']}</b> on {notify.esc(where)} {notify.esc(post.get('account') or '')}"
                f"\nWhen it's live, send me: <code>/posted {post['n']} link</code>")
    if clip.get("file") and Path(clip["file"]).exists():
        notify.send_video(Path(clip["file"]), notify.clip_caption(clip, post["n"]), notify._clip_seconds(clip))
    update(post["id"], reminded=1)


def watch_channel(url: str, campaign_id: str = "") -> Dict[str, Any]:
    cfg = settings()
    url = url.strip().rstrip("/")
    if not any(w["url"] == url for w in cfg["watch"]):
        cfg["watch"].append({"url": url, "campaign_id": campaign_id, "added": time.time()})
        # Mark what's there now as seen, so only new uploads are announced.
        for v in _latest(url, 10):
            cfg["seen"].append(v["id"])
    return save_settings(cfg)


def unwatch_channel(url: str) -> Dict[str, Any]:
    cfg = settings()
    cfg["watch"] = [w for w in cfg["watch"] if w["url"] != url.strip().rstrip("/") and str(url).strip() != w["url"]]
    return save_settings(cfg)


def _latest(channel_url: str, n: int = 6) -> List[Dict[str, Any]]:
    try:
        import yt_dlp
        from . import media
        target = channel_url if re.search(r"/(videos|shorts|streams)$", channel_url) else channel_url + "/videos"
        opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": n,
                **media.ytdlp_auth()}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
        return [{"id": e.get("id"), "title": e.get("title") or "", "url": e.get("url") or e.get("webpage_url") or "",
                 "duration": e.get("duration")} for e in (info or {}).get("entries") or [] if e and e.get("id")]
    except Exception:
        return []


def scout() -> List[Dict[str, Any]]:
    """New uploads on watched channels since last time; each gets a Telegram ping."""
    cfg = settings()
    seen = set(cfg["seen"])
    fresh = []
    for w in cfg["watch"]:
        for v in _latest(w["url"]):
            if v["id"] in seen:
                continue
            seen.add(v["id"])
            cfg["seen"].append(v["id"])
            url = v["url"] if v["url"].startswith("http") else f"https://www.youtube.com/watch?v={v['id']}"
            fresh.append({**v, "url": url, "channel": w["url"], "campaign_id": w.get("campaign_id", "")})
    save_settings(cfg)
    if fresh:
        try:                                   # a creator's channel: the new uploads join its catalog and get scanned
            from . import scan
            scan.on_new_uploads(fresh)
        except Exception:
            traceback.print_exc()
    for v in fresh[:5]:
        mins = f" · {int(v['duration'] // 60)} min" if v.get("duration") else ""
        notify.send(f"🆕 <b>New upload</b>{mins}\n{notify.esc(v['title'][:140])}\n{notify.esc(v['url'])}\n\n"
                    f"Clip it: send me the link (add <code>shorts</code>/<code>tiktok</code> for the length).")
    return fresh


def summary_text() -> str:
    d = dashboard()
    today = datetime.now().date()
    planned = [p for p in list_posts(300, "planned")
               if p.get("planned_at") and datetime.fromtimestamp(p["planned_at"]).date() == today]
    lines = ["☀️ <b>Morning summary</b>",
             f"Views in the last 24 h: <b>{d['views_24h']:,}</b> · all time {d['views']:,} over {d['posts']} posts",
             f"Earned so far (estimate): <b>${d['earned']:,.2f}</b>"]
    for c in d["by_campaign"][:4]:
        lines.append(f"• {notify.esc(c['name'])}: {c['views']:,} views · ${c['earned']:,.2f}")
    if d["top"]:
        t = d["top"][0]
        lines.append(f"Best post: #{t['n']} with {int(t.get('views') or 0):,} views — "
                     f"{notify.esc((t.get('hook') or '')[:80])}")
    if planned:
        lines.append(f"\n<b>Today's posts</b> ({len(planned)}):")
        for p in sorted(planned, key=lambda p: p["planned_at"]):
            lines.append(f"• {datetime.fromtimestamp(p['planned_at']).strftime('%H:%M')} "
                         f"{notify.esc(p['platform'])} {notify.esc(p.get('account') or '')} — #{p['n']}")
    else:
        lines.append("\nNothing planned today — send me a link, or /plan after a run.")
    return "\n".join(lines)


_started = False
_last_scout = 0.0


def tick(now: Optional[float] = None) -> None:
    """One pass of the background work: reminders, view checks, scout, summary."""
    global _last_scout
    now = now or time.time()
    for post in list_posts(500, "planned"):
        if post.get("planned_at") and post["planned_at"] <= now and not post.get("reminded"):
            if now - post["planned_at"] < 6 * 3600:          # don't remind about slots long gone
                _remind(post)
            else:
                update(post["id"], reminded=1)
    for post in list_posts(500, "posted"):
        if due_for_check(post, now):
            check(post)
    cfg = settings()
    if cfg["watch"] and now - _last_scout >= 2 * 3600:
        _last_scout = now
        scout()
    local = datetime.fromtimestamp(now)
    stamp = local.strftime("%Y-%m-%d")
    if (local.hour, local.minute) >= SUMMARY_AT and cfg.get("summary_sent") != stamp and notify.connected():
        save_settings({"summary_sent": stamp})
        if list_posts(1):
            notify.send(summary_text())


def start() -> bool:
    global _started
    if _started:
        return False
    _started = True

    def loop():
        time.sleep(20)
        while True:
            try:
                tick()
            except Exception:
                traceback.print_exc()
            time.sleep(60)

    threading.Thread(target=loop, name="money", daemon=True).start()
    return True
