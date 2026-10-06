"""Creator Scan, part one: list a creator's videos and read their words.

Cheap first, always:
* Listing is metadata only — yt-dlp's flat playlist of each YouTube tab
  (/videos, /streams, /shorts separately), a Twitch channel's past broadcasts,
  Kick when it lets us. Instagram and TikTok only with gs's own cookies.
* Words come from the platform's own subtitles when there are any (free, no
  video download). Only without them is the audio downloaded and sent to
  Whisper — and for a long stream (over ~2 hours) only its loudest stretches.
* Every request goes through one `Net`: one injectable yt-dlp runner, at
  least SCAN_REQUEST_GAP seconds between requests, backing off when a site
  says "too many requests", and stopping the scan the moment YouTube asks to
  confirm we're not a bot (media.set_bot_block). No proxies, no tricks.

The parsers (`parse_flat`, `parse_vtt`, `parse_json3`, `parse_video_info`,
`apply_filters`, `dedupe`, `outlier_factors`) are pure, so they're tested on
saved samples without the network.
"""
from __future__ import annotations

import difflib
import html
import json
import re
import statistics
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import media, transcribe
from .config import AUDIO_CHUNK_SECONDS, SCAN_REQUEST_GAP

LONG_VOD = 2 * 3600            # longer than this without subtitles: transcribe only the loud stretches
LIST_CAP = 3000                # newest videos listed per tab (a channel with more says so)
DATE_SLACK_DAYS = 45           # a listing's dates are often "3 months ago": be lenient near a cut-off date
BACKOFF = (60.0, 300.0, 900.0)  # waits after "too many requests" before giving the site a long rest
REST_AFTER_429 = 1800.0         # then the scan waits this long and carries on by itself
PLATFORM_NAMES = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick", "instagram": "Instagram",
                  "tiktok": "TikTok"}


# --- errors ------------------------------------------------------------------------------

class ScanStop(Exception):
    """The scan must stop here; `message` says why in plain words."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BotBlocked(ScanStop):
    """YouTube (or another site) asked to confirm we're not a bot."""


class SlowDown(ScanStop):
    """The site keeps saying too many requests: rest `wait` seconds, then carry on."""

    def __init__(self, message: str, wait: float = REST_AFTER_429):
        super().__init__(message)
        self.wait = wait


class Gone(Exception):
    """This one video can't be read: private, removed, members-only, age-restricted, still live."""


class FetchFailed(Exception):
    """A passing failure (network, a refused request): try again on a later scan."""


def classify(text: str) -> str:
    t = (text or "").lower()
    if "sign in to confirm" in t or "not a bot" in t:
        return "bot"
    if "http error 429" in t or "too many requests" in t or " 429" in t:
        return "rate"
    if any(k in t for k in ("private video", "video unavailable", "has been removed", "no longer available",
                            "members-only", "members only", "join this channel", "confirm your age",
                            "age-restricted", "inappropriate for some users", "live event will begin",
                            "premieres in", "is live", "account associated with this video has been terminated",
                            "does not exist", "http error 404", "video is not available")):
        return "gone"
    return "other"


def bot_block() -> Optional[Dict[str, Any]]:
    """Part 1's download pause, read defensively (older ClipAgent builds don't have it)."""
    fn = getattr(media, "bot_block", None)
    try:
        return fn() if fn else None
    except Exception:
        return None


def set_bot_block(message: str, platform: str = "youtube") -> None:
    fn = getattr(media, "set_bot_block", None)
    if fn:
        try:
            fn(message, platform)
        except Exception:
            pass


def need(name: str) -> Callable[..., Any]:
    """A helper from media.py that the scan can't work without, or a plain error."""
    fn = getattr(media, name, None)
    if not callable(fn):
        raise RuntimeError(f"This copy of ClipAgent is missing its “{name}” download helper — update ClipAgent, "
                           "then press Resume.")
    return fn


# --- the one way to the network ---------------------------------------------------------------

class YtDlpRunner:
    """yt-dlp's Python API — the only thing in Creator Scan that touches the network
    for listing and reading. Tests replace `catalog.RUNNER` with a stand-in."""

    def _opts(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        opts: Dict[str, Any] = {"quiet": True, "no_warnings": True, "noprogress": True, "skip_download": True,
                                "retries": 2, "extractor_retries": 1, **media.ytdlp_auth()}
        runtimes = getattr(media, "_js_runtimes", lambda: {})()
        if runtimes:
            opts["js_runtimes"] = runtimes
        opts.update(extra)
        return opts

    def info(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
        import yt_dlp
        with yt_dlp.YoutubeDL(self._opts(opts)) as ydl:
            info = ydl.extract_info(url, download=False)
            return ydl.sanitize_info(info) if info else {}

    def text(self, url: str) -> str:
        import yt_dlp
        with yt_dlp.YoutubeDL(self._opts({})) as ydl:
            with ydl.urlopen(url) as resp:
                return resp.read().decode("utf-8", "replace")


RUNNER: Any = YtDlpRunner()

_last_request: Dict[str, float] = {}
_turn_lock = threading.Lock()


class Net:
    """Polite access for one piece of work: a gap between requests (shared by everything
    running), backing off on 429, and a hard stop on a bot check."""

    def __init__(self, sleep: Optional[Callable[[float], None]] = None, gap: Optional[float] = None,
                 runner: Any = None, clock: Callable[[], float] = time.monotonic):
        self.sleep = sleep or time.sleep
        self.gap = max(SCAN_REQUEST_GAP if gap is None else gap, 0.0)
        self._runner = runner
        self.clock = clock
        self.requests = 0

    @property
    def runner(self) -> Any:
        return self._runner or RUNNER

    def _turn(self, platform: str) -> None:
        with _turn_lock:
            last = _last_request.get(platform)
            now = self.clock()
            wait = 0.0 if last is None else self.gap - (now - last)
            _last_request[platform] = max(now, (last or now) + self.gap) if wait > 0 else now
        if wait > 0:
            self.sleep(wait)

    def _call(self, platform: str, what: Callable[[], Any], url: str) -> Any:
        if platform == "youtube":
            block = bot_block()
            if block:
                raise BotBlocked(block.get("message") or media.explain_download_error("not a bot", url))
        waits = list(BACKOFF)
        while True:
            self._turn(platform)
            self.requests += 1
            try:
                return what()
            except Exception as exc:  # noqa: BLE001 — sorted into plain kinds below
                text = str(exc)
                kind = classify(text)
                if kind == "bot":
                    message = media.explain_download_error(text, url)
                    set_bot_block(message, platform)
                    raise BotBlocked(message) from exc
                if kind == "rate":
                    if waits:
                        self.sleep(waits.pop(0))
                        continue
                    raise SlowDown(f"{PLATFORM_NAMES.get(platform, platform)} keeps saying ClipAgent is asking "
                                   "too often, so the scan is resting for half an hour and then carries on by "
                                   "itself.") from exc
                if kind == "gone":
                    raise Gone(media.explain_download_error(text, url)) from exc
                raise FetchFailed(media.explain_download_error(text, url)) from exc

    def info(self, url: str, opts: Dict[str, Any], platform: str) -> Dict[str, Any]:
        return self._call(platform, lambda: self.runner.info(url, opts), url) or {}

    def text(self, url: str, platform: str) -> str:
        return self._call(platform, lambda: self.runner.text(url), url) or ""


# --- links -------------------------------------------------------------------------------

_YT_VIDEO = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})")


def classify_link(url: str) -> Dict[str, str]:
    """What a link points at: {"platform", "type": channel|video|playlist|profile|unknown, "url", "name"}."""
    u = (url or "").strip()
    if u and not re.match(r"https?://", u, re.I):
        u = "https://" + u
    low = u.lower()
    m = _YT_VIDEO.search(u)
    if m:
        return {"platform": "youtube", "type": "video", "url": f"https://www.youtube.com/watch?v={m.group(1)}",
                "name": m.group(1)}
    if "youtube.com/playlist" in low and "list=" in low:
        return {"platform": "youtube", "type": "playlist", "url": u, "name": ""}
    m = re.search(r"youtube\.com/((?:@[\w.\-]+)|(?:channel/[\w-]+)|(?:c/[\w.\-]+)|(?:user/[\w.\-]+))", u, re.I)
    if m:
        return {"platform": "youtube", "type": "channel", "url": f"https://www.youtube.com/{m.group(1)}",
                "name": m.group(1).split("/")[-1]}
    m = re.search(r"twitch\.tv/videos/(\d+)", u, re.I)
    if m:
        return {"platform": "twitch", "type": "video", "url": f"https://www.twitch.tv/videos/{m.group(1)}",
                "name": m.group(1)}
    m = re.search(r"twitch\.tv/([\w]+)", u, re.I)
    if m and m.group(1).lower() not in ("videos", "directory", "p", "search"):
        return {"platform": "twitch", "type": "channel", "url": f"https://www.twitch.tv/{m.group(1)}",
                "name": m.group(1)}
    m = re.search(r"kick\.com/([\w-]+)/videos/([\da-f-]{36})", u, re.I)
    if m:
        return {"platform": "kick", "type": "video", "url": f"https://kick.com/{m.group(1)}/videos/{m.group(2)}",
                "name": m.group(2)}
    m = re.search(r"kick\.com/([\w-]+)", u, re.I)
    if m and m.group(1).lower() not in ("video", "categories", "search", "auth"):
        return {"platform": "kick", "type": "channel", "url": f"https://kick.com/{m.group(1)}", "name": m.group(1)}
    m = re.search(r"instagram\.com/(?:reel|reels|p|tv)/([\w-]+)", u, re.I)
    if m:
        return {"platform": "instagram", "type": "video", "url": f"https://www.instagram.com/reel/{m.group(1)}/",
                "name": m.group(1)}
    m = re.search(r"instagram\.com/([\w.]+)", u, re.I)
    if m:
        return {"platform": "instagram", "type": "profile", "url": f"https://www.instagram.com/{m.group(1)}/",
                "name": m.group(1)}
    m = re.search(r"tiktok\.com/@([\w.]+)/video/(\d+)", u, re.I)
    if m:
        return {"platform": "tiktok", "type": "video", "url": f"https://www.tiktok.com/@{m.group(1)}/video/{m.group(2)}",
                "name": m.group(2)}
    m = re.search(r"tiktok\.com/@([\w.]+)", u, re.I)
    if m:
        return {"platform": "tiktok", "type": "profile", "url": f"https://www.tiktok.com/@{m.group(1)}",
                "name": m.group(1)}
    return {"platform": "", "type": "unknown", "url": u, "name": ""}


def same_channel(a: str, b: str) -> bool:
    """Two links to the same channel (the watch list and a creator's links are written differently)."""
    ca, cb = classify_link(a), classify_link(b)
    if not ca["platform"] or ca["platform"] != cb["platform"]:
        return False
    return ca["type"] in ("channel", "profile") and ca["type"] == cb["type"] and \
        ca["name"].lower().lstrip("@") == cb["name"].lower().lstrip("@")


# --- dates ------------------------------------------------------------------------------------

def ymd(value: Any) -> str:
    """'YYYYMMDD' from yt-dlp's upload_date, a timestamp, or an ISO date; '' when unknown."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y%m%d")
        except (OverflowError, OSError, ValueError):
            return ""
    s = str(value).strip()
    if re.fullmatch(r"\d{8}", s):
        return s
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else ""


def _day(text: str) -> Optional[datetime]:
    t = ymd(text)
    try:
        return datetime.strptime(t, "%Y%m%d") if t else None
    except ValueError:
        return None


# --- listing: pure parsers ---------------------------------------------------------------------

_GONE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}


def _num(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    v = _num(value)
    return int(v) if v is not None else None


def parse_flat(info: Dict[str, Any], platform: str, kind: str, group: str = "",
               channel: str = "") -> List[Dict[str, Any]]:
    """Rows from a yt-dlp flat playlist (`-J --flat-playlist`). Private, deleted,
    upcoming and still-live entries are left out; members-only ones are marked.
    `group` (a channel's tab) is what the outlier factor compares within; `channel`
    keeps de-duplication to copies on different channels."""
    rows: List[Dict[str, Any]] = []
    for e in (info or {}).get("entries") or []:
        if not isinstance(e, dict):
            continue
        if e.get("_type") == "playlist" or e.get("entries"):        # a tab inside a tab: flatten one level
            rows += parse_flat(e, platform, kind, group, channel)
            continue
        vid = str(e.get("id") or "").strip()
        title = str(e.get("title") or "").strip()
        if not vid or title.lower() in _GONE_TITLES:
            continue
        live = e.get("live_status") or ""
        if live in ("is_upcoming", "is_live", "post_live"):
            continue
        url = str(e.get("url") or e.get("webpage_url") or "")
        k = kind
        if platform == "youtube":
            if "/shorts/" in url:
                k = "short"
            elif live == "was_live" and kind == "video":
                k = "stream"
            url = (f"https://www.youtube.com/shorts/{vid}" if k == "short"
                   else f"https://www.youtube.com/watch?v={vid}")
        elif platform == "twitch":
            vid = vid if vid.startswith("v") else "v" + vid
            url = url or f"https://www.twitch.tv/videos/{vid[1:]}"
        exact = ymd(e.get("upload_date"))
        approx = "" if exact else ymd(e.get("timestamp") or e.get("release_timestamp"))
        row = {"platform": platform, "video_id": vid, "url": url, "title": title[:300],
               "duration": _num(e.get("duration")), "upload_date": exact or approx,
               "views": _int(e.get("view_count")), "likes": _int(e.get("like_count")), "kind": k,
               "_approx_date": bool(approx and not exact), "_group": group or f"{platform}:{kind}",
               "_channel": channel or group or platform}
        if e.get("availability") in ("subscriber_only", "premium_only", "needs_auth"):
            row.update(status="skipped", error="Members-only or paid video — ClipAgent can't read it.")
        rows.append(row)
    return rows


def parse_kick_videos(data: Any, slug: str) -> List[Dict[str, Any]]:
    """Rows from Kick's public channel-videos list (shape read defensively: it isn't documented)."""
    items = data if isinstance(data, list) else ((data or {}).get("data") or (data or {}).get("videos") or [])
    rows = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        video = it.get("video") if isinstance(it.get("video"), dict) else {}
        uuid = str(video.get("uuid") or it.get("uuid") or "").strip()
        if not re.fullmatch(r"[\da-f-]{36}", uuid):
            continue
        dur = _num(it.get("duration"))
        if dur and dur > 24 * 3600:            # Kick gives milliseconds
            dur = dur / 1000.0
        rows.append({"platform": "kick", "video_id": uuid, "url": f"https://kick.com/{slug}/videos/{uuid}",
                     "title": str(it.get("session_title") or video.get("title") or it.get("slug") or "Kick stream")[:300],
                     "duration": dur, "upload_date": ymd(it.get("start_time") or it.get("created_at")
                                                         or video.get("created_at")),
                     "views": _int(video.get("views") if video.get("views") is not None else it.get("views")),
                     "likes": None, "kind": "vod", "_approx_date": False, "_group": f"kick:{slug}",
                     "_channel": f"kick:{slug}"})
    return rows


def apply_filters(rows: List[Dict[str, Any]], settings: Dict[str, Any],
                  rules: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Mark each row listed or skipped (with the reason in plain words) by the creator's
    settings and the campaign's earliest allowed upload date. Unknown facts never skip
    a video: they're checked again once the video itself is read."""
    since = _day(settings.get("since") or "")
    camp_day = _day(((rules or {}).get("min_upload_date") or {}).get("date") or "")
    min_views = int(settings.get("min_views") or 0)
    min_secs = float(settings.get("min_minutes") or 0) * 60
    out = []
    for r in rows:
        r = dict(r)
        if r.get("status") == "skipped":
            out.append(r)
            continue
        reason = skip_reason(r, settings, since, camp_day, min_views, min_secs, lenient=bool(r.get("_approx_date")))
        r["status"], r["error"] = ("skipped", reason) if reason else ("listed", "")
        out.append(r)
    return out


def skip_reason(r: Dict[str, Any], settings: Dict[str, Any], since: Optional[datetime] = None,
                camp_day: Optional[datetime] = None, min_views: int = 0, min_secs: float = 0.0,
                lenient: bool = False) -> str:
    kind = r.get("kind") or "video"
    if kind in ("short", "clip") and not settings.get("include_shorts"):
        return "A short — shorts are clips already. Switch on “Include shorts” to read them."
    if kind in ("stream", "vod") and not settings.get("include_streams", True):
        return "A stream — streams are switched off for this creator."
    day = _day(r.get("upload_date") or "")
    slack = timedelta(days=DATE_SLACK_DAYS if lenient else 0)
    if day and since and day + slack < since:
        return f"Uploaded {day:%d %b %Y} — before {since:%d %b %Y}, the date you set."
    if day and camp_day and day + slack < camp_day:
        return f"Uploaded {day:%d %b %Y} — the campaign only takes videos uploaded from {camp_day:%d %b %Y}."
    if min_views and r.get("views") is not None and int(r["views"]) < min_views:
        return f"{int(r['views']):,} views — under the {min_views:,} you set."
    if min_secs and r.get("duration") and float(r["duration"]) < min_secs:
        return f"{float(r['duration']) / 60:.0f} min long — shorter than the {min_secs / 60:.0f} min you set."
    return ""


def norm_title(title: str) -> str:
    t = re.sub(r"[^\w\s]", " ", (title or "").lower())
    t = re.sub(r"\b(full|stream|vod|live|podcast|ep|episode|part)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _similar_length(a: Optional[float], b: Optional[float], tolerance: Optional[float] = None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= (tolerance if tolerance is not None else max(5.0, 0.01 * max(a, b)))


def dedupe(rows: List[Dict[str, Any]], existing: Iterable[Dict[str, Any]] = ()) -> Tuple[List[Dict[str, Any]],
                                                                                         List[Dict[str, Any]]]:
    """The same video posted on two of the creator's channels (same title, about the same
    length) is read once: the copy already in the catalog wins, then the one with more
    views. Videos on the SAME channel are never merged — a daily stream called "LIVE
    trading" is a new video every day. A video already in the catalog only counts as a
    twin when its length matches to the second.
    Returns (kept, duplicates); a video listed twice (two tabs) is kept once, silently."""
    held: List[Tuple[str, Optional[float], Tuple[str, str], str, bool]] = [
        (norm_title(e.get("title") or ""), _num(e.get("duration")), (e.get("platform"), e.get("video_id")), "", True)
        for e in existing]
    order = sorted(rows, key=lambda r: (-(r.get("views") if r.get("views") is not None else -1),
                                        0 if r.get("platform") == "youtube" else 1))
    keep_ids: set = set()
    dropped: List[Dict[str, Any]] = []
    for r in order:
        rid = (r.get("platform"), r.get("video_id"))
        if rid in keep_ids:
            continue
        key = norm_title(r.get("title") or "")
        chan = r.get("_channel") or r.get("_group") or ""
        twin = None
        if key:
            for k_title, k_dur, k_id, k_chan, k_old in held:
                if k_id == rid or k_title != key:
                    continue
                if k_old:
                    if _similar_length(k_dur, _num(r.get("duration")), 2.0):
                        twin = k_id
                        break
                elif k_chan != chan and _similar_length(k_dur, _num(r.get("duration"))):
                    twin = k_id
                    break
        if twin is not None:
            dropped.append(r)
            continue
        keep_ids.add(rid)
        held.append((key, _num(r.get("duration")), rid, chan, False))
    kept, seen = [], set()
    for r in rows:
        rid = (r.get("platform"), r.get("video_id"))
        if rid in keep_ids and rid not in seen:
            seen.add(rid)
            kept.append(r)
    return kept, dropped


def outlier_factors(rows: List[Dict[str, Any]], min_group: int = 3) -> None:
    """views ÷ the median views of the same channel tab, on each row (None when unknown).
    A video that did 5x its channel's usual is read first."""
    groups: Dict[str, List[int]] = {}
    for r in rows:
        if r.get("views"):
            groups.setdefault(r.get("_group") or "", []).append(int(r["views"]))
    medians = {g: statistics.median(v) for g, v in groups.items() if len(v) >= min_group}
    for r in rows:
        med = medians.get(r.get("_group") or "")
        r["outlier"] = round(int(r["views"]) / med, 2) if med and r.get("views") is not None else None


# --- listing: the network side -------------------------------------------------------------

_FLAT = {"extract_flat": "in_playlist", "playlistend": LIST_CAP,
         "extractor_args": {"youtubetab": {"approximate_date": [""]}}}


def list_link(net: Net, link: str, settings: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Every video one link leads to, and plain notes on what couldn't be listed."""
    c = classify_link(link)
    plat, kind = c["platform"], c["type"]
    notes: List[str] = []
    flat = {**_FLAT, "sleep_interval_requests": net.gap}
    if not plat:
        return [], [f"ClipAgent doesn't know how to list {link} — paste a YouTube, Twitch or Kick channel, "
                    "or links to single videos."]
    if kind == "video":
        return [{"platform": plat, "video_id": _video_id(c), "url": c["url"], "title": "", "duration": None,
                 "upload_date": "", "views": None, "likes": None,
                 "kind": {"twitch": "vod", "kick": "vod", "instagram": "clip", "tiktok": "clip"}.get(plat, "video"),
                 "_approx_date": False, "_group": f"{plat}:single", "_channel": c["url"].lower()}], notes
    if plat == "youtube" and kind == "playlist":
        return parse_flat(net.info(c["url"], flat, plat), plat, "video", c["url"], c["url"]), notes
    if plat == "youtube":
        tabs = [("videos", "video")]
        if settings.get("include_streams", True):
            tabs.append(("streams", "stream"))
        if settings.get("include_shorts"):
            tabs.append(("shorts", "short"))
        rows: List[Dict[str, Any]] = []
        for tab, k in tabs:
            try:
                got = parse_flat(net.info(f"{c['url']}/{tab}", flat, plat), plat, k, f"{c['url']}/{tab}",
                                 c["url"].lower())
            except (Gone, FetchFailed) as exc:
                if "does not have a" in str(exc).lower():
                    continue                        # a channel without a Live or Shorts tab
                notes.append(f"Couldn't list the {tab} of {c['url']}: {exc}")
                continue
            if len(got) >= LIST_CAP:
                notes.append(f"{c['url']}/{tab} has more than {LIST_CAP:,} videos — the newest {LIST_CAP:,} "
                             "are in the catalog.")
            rows += got
        return rows, notes
    if plat == "twitch":
        if not settings.get("include_streams", True):
            return [], [f"Streams are switched off for this creator, so {c['url']} wasn't listed."]
        url = f"{c['url']}/videos?filter=archives&sort=time"
        rows = parse_flat(net.info(url, flat, plat), plat, "vod", url, c["url"].lower())
        if not rows:
            notes.append(f"{c['url']} has no past broadcasts right now (Twitch deletes them after a few weeks).")
        return rows, notes
    if plat == "kick":
        if not settings.get("include_streams", True):
            return [], [f"Streams are switched off for this creator, so {c['url']} wasn't listed."]
        slug = c["name"]
        try:
            data = json.loads(net.text(f"https://kick.com/api/v2/channels/{slug}/videos", plat) or "null")
            rows = parse_kick_videos(data, slug)
        except (Gone, FetchFailed, ValueError):
            rows = []
        if not rows:
            notes.append(f"Kick didn't let ClipAgent list {c['url']}'s videos without logging in (and ClipAgent "
                         "never logs in for you). Open the streams you want on kick.com and paste their links "
                         f"(kick.com/{slug}/videos/…) as links of this creator — they'll be read like the rest.")
        return rows, notes
    if plat in ("instagram", "tiktok"):
        name = PLATFORM_NAMES[plat]
        if not settings.get("include_shorts"):
            return [], [f"{name} posts are short clips already, so {c['url']} wasn't read. Switch on "
                        "“Include shorts” to read them."]
        if plat == "instagram" and not media.ytdlp_auth():
            return [], [f"Instagram only lists a profile to a logged-in browser. If you've set up your own "
                        f"cookies (YTDLP_COOKIES in .env), it can try; until then {c['url']} is skipped. "
                        "Paste single reel links instead."]
        try:
            rows = parse_flat(net.info(c["url"], flat, plat), plat, "clip", c["url"], c["url"].lower())
        except (Gone, FetchFailed) as exc:
            return [], [f"{name} wouldn't list {c['url']} ({exc}). Paste single post links instead."]
        if not rows:
            notes.append(f"{name} listed nothing for {c['url']}. Paste single post links instead.")
        return rows, notes
    return [], [f"ClipAgent doesn't know how to list {link}."]


def _video_id(c: Dict[str, str]) -> str:
    if c["platform"] == "twitch":
        return "v" + c["name"]
    return c["name"]


# --- one video: its facts and its subtitles --------------------------------------------------

def pick_subtitles(info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The track to read: English subtitles someone wrote, else the auto-captions in the
    language actually spoken (the "-orig" track), else English auto-captions.
    json3 first (it has a time for every word), then vtt."""
    def best(tracks: Any) -> Optional[Dict[str, Any]]:
        by_ext = {t.get("ext"): t for t in (tracks or []) if isinstance(t, dict) and t.get("url") or
                  isinstance(t, dict) and t.get("data")}
        for ext in ("json3", "vtt"):
            if ext in by_ext:
                return by_ext[ext]
        return None

    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    for lang in sorted(subs, key=lambda k: (k != "en", k)):
        if (lang == "en" or lang.startswith("en-")) and not lang.endswith("-orig") and "-" not in lang[3:]:
            t = best(subs[lang])
            if t:
                return {**t, "lang": lang, "auto": False}
    orig = [k for k in autos if k.endswith("-orig")]
    for lang in orig + [k for k in ("en", "en-US", "en-GB") if k in autos]:
        if lang in ("en", "en-US", "en-GB") and orig and not orig[0].startswith("en"):
            continue                       # "en" would be a machine translation of another language
        t = best(autos.get(lang))
        if t:
            return {**t, "lang": lang, "auto": True}
    return None


def parse_video_info(info: Dict[str, Any]) -> Dict[str, Any]:
    """What one video's full yt-dlp info tells the scan."""
    heat = []
    for h in info.get("heatmap") or []:
        try:
            heat.append({"start_time": round(float(h["start_time"]), 2), "end_time": round(float(h["end_time"]), 2),
                         "value": round(float(h["value"]), 4)})
        except (KeyError, TypeError, ValueError):
            continue
    return {"title": str(info.get("title") or "")[:300], "duration": _num(info.get("duration")),
            "upload_date": ymd(info.get("upload_date") or info.get("timestamp") or info.get("release_timestamp")),
            "views": _int(info.get("view_count")), "likes": _int(info.get("like_count")), "heatmap": heat,
            "live_status": info.get("live_status") or "", "availability": info.get("availability") or "",
            "subtitles": pick_subtitles(info), "url": info.get("webpage_url") or ""}


def read_info(net: Net, row: Dict[str, Any]) -> Dict[str, Any]:
    opts = {"extractor_args": {"youtube": {"skip": ["dash", "hls"]}}} if row.get("platform") == "youtube" else {}
    facts = parse_video_info(net.info(row["url"], opts, row.get("platform") or ""))
    if facts["live_status"] in ("is_live", "is_upcoming", "post_live"):
        raise Gone("This stream is still live (or hasn't started) — it's read once the VOD is up.")
    return facts


def read_subtitles(net: Net, facts: Dict[str, Any], platform: str) -> Optional[Dict[str, Any]]:
    """The video's words from its subtitle track, or None when it has none worth reading."""
    track = facts.get("subtitles")
    if not track:
        return None
    text = track.get("data") or net.text(track["url"], platform)
    transcript = parse_json3(text) if track.get("ext") == "json3" else parse_vtt(text)
    if len(transcript["segments"]) < 3:
        return None
    transcript["lang"] = track.get("lang") or ""
    transcript["auto"] = bool(track.get("auto"))
    return transcript


# --- subtitle parsers -----------------------------------------------------------------------

_CUE_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")
_INLINE = re.compile(r"<(\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{1,2}:\d{2}[.,]\d{3})>")
_TAG = re.compile(r"</?[^>]+>")


def _secs(text: str) -> float:
    m = _CUE_TIME.search(text)
    if not m:
        return 0.0
    h = int(m.group(1) or 0)
    return h * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", text))).strip()


def _line_words(line: str, start: float) -> List[Tuple[str, float]]:
    """Words of one caption line with their times, from YouTube's inline <00:00:01.500> stamps."""
    out: List[Tuple[str, float]] = []
    t = start
    for i, part in enumerate(_INLINE.split(line)):
        if i % 2 == 1:
            t = _secs(part)
            continue
        for w in _clean(part).split():
            out.append((w, t))
    return out


def _finish(segments: List[Dict[str, Any]], words: List[Dict[str, Any]]) -> Dict[str, Any]:
    segments.sort(key=lambda s: s["start"])
    for a, b in zip(segments, segments[1:]):
        if a["end"] > b["start"]:
            a["end"] = round(max(a["start"] + 0.05, b["start"]), 3)
    for a, b in zip(words, words[1:]):
        if a["end"] > b["start"] or a["end"] <= a["start"]:
            a["end"] = round(max(a["start"] + 0.04, b["start"]), 3)
    return {"segments": segments, "words": transcribe.in_order(words) if words else [],
            "text": " ".join(s["text"] for s in segments)}


def parse_vtt(text: str) -> Dict[str, Any]:
    """WebVTT → {"segments": [{start, end, text}], "words": [...]}.

    YouTube's auto-captions roll: each cue shows the line before again above the new
    one, and a 10 ms cue repeats what's on screen between them. Here every cue keeps
    only what's new — the settle cues are skipped and a first line equal to the line
    just read is dropped — so no sentence is read twice. Inline word stamps
    (<00:01:02.345>) become word times; plain subtitles give segment times only."""
    segments: List[Dict[str, Any]] = []
    words: List[Dict[str, Any]] = []
    last = ""
    blocks = re.split(r"\r?\n\s*\r?\n", (text or "").replace("﻿", ""))
    for block in blocks:
        lines = block.strip("\r\n").splitlines()
        tline = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if tline is None:
            continue
        left, right = lines[tline].split("-->", 1)
        start, end = _secs(left), _secs(right)
        if end - start < 0.05:
            continue                                   # a settle cue: repeats what's already on screen
        raw = [ln for ln in lines[tline + 1:] if ln.strip()]
        shown = [(ln, _clean(ln)) for ln in raw]
        shown = [(ln, c) for ln, c in shown if c]
        while shown and shown[0][1] == last:
            shown.pop(0)
        if not shown:
            continue
        seg_text = " ".join(c for _, c in shown)
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": seg_text})
        if any(_INLINE.search(ln) for ln, _ in shown):
            for ln, _ in shown:
                for w, t in _line_words(ln, start):
                    words.append({"w": w, "start": round(t, 3), "end": round(end, 3)})
        last = shown[-1][1]
    return _finish(segments, words)


def parse_json3(text: str) -> Dict[str, Any]:
    """YouTube's json3 captions → segments, and word times when the track has them (auto-captions)."""
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {"segments": [], "words": [], "text": ""}
    segments: List[Dict[str, Any]] = []
    words: List[Dict[str, Any]] = []
    timed = False
    for ev in data.get("events") or []:
        segs = ev.get("segs") or []
        t0 = float(ev.get("tStartMs") or 0) / 1000.0
        dur = float(ev.get("dDurationMs") or 0) / 1000.0
        body = "".join(str(s.get("utf8") or "") for s in segs)
        clean = _clean(body.replace("\n", " "))
        if not clean:
            continue
        segments.append({"start": round(t0, 3), "end": round(t0 + max(dur, 0.05), 3), "text": clean})
        if any("tOffsetMs" in s for s in segs):
            timed = True
        for s in segs:
            for w in _clean(str(s.get("utf8") or "")).split():
                words.append({"w": w, "start": round(t0 + float(s.get("tOffsetMs") or 0) / 1000.0, 3),
                              "end": round(t0 + max(dur, 0.05), 3)})
    return _finish(segments, words if timed else [])


def spread_words(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Words spread evenly over their segment — for subtitles that only time whole lines."""
    out: List[Dict[str, Any]] = []
    for seg in segments:
        toks = str(seg.get("text") or "").split()
        if not toks:
            continue
        step = max(0.04, (float(seg["end"]) - float(seg["start"])) / len(toks))
        for i, tok in enumerate(toks):
            out.append({"w": tok, "start": round(float(seg["start"]) + i * step, 3),
                        "end": round(float(seg["start"]) + (i + 1) * step, 3)})
    return transcribe.in_order(out)


# --- words from audio (Whisper) -------------------------------------------------------------

MAX_LONG_WAITS = 12


def _status_of(exc: BaseException) -> Optional[int]:
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)


def _whisper_chunk(client: Any, part: Path, wait: Callable[[float], None]) -> Dict[str, Any]:
    """One chunk through Whisper. Short waits for the quota are slept through by
    transcribe._create; a long one (the hourly or daily allowance used up) is waited
    out here through `wait` — which shows "waiting" — and the same chunk tried again."""
    for _ in range(MAX_LONG_WAITS):
        try:
            result = transcribe._create(client, part, sleep=wait)
            return result.model_dump() if hasattr(result, "model_dump") else dict(result)
        except Exception as exc:  # noqa: BLE001 — only the quota is waited out
            cause = exc if _status_of(exc) == 429 else exc.__cause__
            if cause is None or _status_of(cause) != 429:
                raise
            wait(transcribe._wait_seconds(cause) + 2)
    raise RuntimeError("The transcription service kept saying its limit is used up — press Resume later.")


def whisper(audio: Path, wait: Callable[[float], None], offset: float = 0.0,
            progress: Optional[Callable[[float], None]] = None) -> Dict[str, Any]:
    """A file's words through Whisper, in chunks, times shifted by `offset`."""
    client = transcribe._client()
    chunks = media.split_audio(audio, AUDIO_CHUNK_SECONDS)
    words: List[Dict[str, Any]] = []
    segments: List[Dict[str, Any]] = []
    try:
        for i, (part, off) in enumerate(chunks):
            data = _whisper_chunk(client, part, wait)
            base = offset + off
            for w in data.get("words") or []:
                words.append({"w": str(w.get("word") or "").strip(), "start": round(float(w.get("start", 0)) + base, 3),
                              "end": round(float(w.get("end", 0)) + base, 3)})
            for s in data.get("segments") or []:
                segments.append({"text": str(s.get("text") or "").strip(),
                                 "start": round(float(s.get("start", 0)) + base, 2),
                                 "end": round(float(s.get("end", 0)) + base, 2)})
            if progress:
                progress((i + 1) / len(chunks))
    finally:
        for part, _ in chunks:
            if part != audio:
                part.unlink(missing_ok=True)
    if not words and segments:
        words = spread_words(segments)
    return {"segments": segments, "words": transcribe.in_order([w for w in words if w["w"]]),
            "text": " ".join(s["text"] for s in segments).strip()}


def cut_audio(audio: Path, start: float, end: float, out: Path) -> Path:
    media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{max(0.5, end - start):.2f}",
               "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", str(out)])
    return out


def whisper_windows(audio: Path, windows: List[Dict[str, Any]], wait: Callable[[float], None],
                    work: Path) -> Dict[str, Any]:
    """Only these stretches of a long recording through Whisper (words keep stream time)."""
    words: List[Dict[str, Any]] = []
    segments: List[Dict[str, Any]] = []
    for i, w in enumerate(sorted(windows, key=lambda x: float(x["start"]))):
        piece = cut_audio(audio, float(w["start"]), float(w["end"]), work / f"window_{i:02d}.mp3")
        try:
            got = whisper(piece, wait, offset=float(w["start"]))
        finally:
            piece.unlink(missing_ok=True)
        words += got["words"]
        segments += got["segments"]
    return {"segments": segments, "words": transcribe.in_order(words),
            "text": " ".join(s["text"] for s in segments).strip(),
            "windows": [{"start": round(float(w["start"]), 1), "end": round(float(w["end"]), 1),
                         "why": str(w.get("why") or "")[:80]} for w in windows]}


def loud_summary(envelope: List[float], hop: float = 1.0, step: float = 2.0) -> Dict[str, Any]:
    """A small copy of the loudness curve (dB, the loudest reading every `step` seconds)."""
    per = max(1, int(round(step / max(hop, 1e-6))))
    vals = [round(max(envelope[i:i + per]), 1) for i in range(0, len(envelope), per) if envelope[i:i + per]]
    return {"hop": round(per * hop, 3), "db": vals}


# --- checking a quote -----------------------------------------------------------------------

def norm_tokens(text: str) -> List[str]:
    t = html.unescape(text or "").lower().replace("’", "'").replace("'", "")
    t = re.sub(r"\[[^\]]*\]", " ", t)                       # [Music], [Laughter]
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t).replace("$", " ").replace("%", " percent ")
    return re.findall(r"[a-z0-9]+", t)


def find_quote(quote: str, transcript: Dict[str, Any], start: float, end: float,
               margin: float = 20.0, need: float = 0.8) -> Optional[Tuple[float, float, float]]:
    """Where the quote is really said near [start, end]: (from, to, match 0-1), or None.
    Fuzzy, after normalising, so captions' small slips ("gonna"/"going to") don't sink a real
    quote — but a sentence that isn't there scores far under `need`."""
    q = norm_tokens(quote)
    if len(q) < 2:
        return None
    lo, hi = start - margin, end + margin
    words = [w for w in transcript.get("words") or [] if lo <= float(w["start"]) <= hi]
    toks: List[Tuple[str, float, float]] = []
    if words:
        for w in words:
            for t in norm_tokens(str(w.get("w") or "")):
                toks.append((t, float(w["start"]), float(w["end"])))
    else:
        for seg in transcript.get("segments") or []:
            if float(seg["end"]) < lo or float(seg["start"]) > hi:
                continue
            pieces = norm_tokens(str(seg.get("text") or ""))
            span = (float(seg["end"]) - float(seg["start"])) / max(1, len(pieces))
            for i, t in enumerate(pieces):
                toks.append((t, float(seg["start"]) + i * span, float(seg["start"]) + (i + 1) * span))
    if not toks:
        return None
    n = len(q)
    names = [t[0] for t in toks]
    best = (0.0, 0, 0)
    for size in sorted({max(1, n - 2), n, n + 2}):
        for i in range(0, max(1, len(names) - size + 1)):
            window = names[i:i + size]
            sm = difflib.SequenceMatcher(None, q, window, autojunk=False)
            if sm.real_quick_ratio() < best[0] or sm.quick_ratio() < best[0]:
                continue
            r = sm.ratio()
            if r > best[0]:
                best = (r, i, min(len(names), i + size) - 1)
    ratio, a, b = best
    if ratio < need:
        return None
    return toks[a][1], toks[b][2], round(ratio, 3)
