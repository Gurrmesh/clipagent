"""Links that wait: the download queue's pause.

When YouTube asks this PC to prove it's not a robot, asking again only makes it
worse. So pipeline.run_job parks a YouTube link here instead of failing it:
the job stays queued (jobs.paused = "youtube") and YouTube is left alone.
Twitch, Kick, TikTok links and uploaded files keep going. "Try again now" (or
ClipAgent's own one try after about 45 minutes) lifts the pause and runs the
saved links again, oldest first; if the first one meets the robot check again,
everything stays paused.

Links that were still waiting in a queue when ClipAgent was closed are kept
too (jobs.paused = "restart") and run once it's open again.

Everything lives in the database and data/download_pause.json (media.py), so a
restart keeps the pause and the waiting links. Telegram hears about it once
when the pause starts and once when YouTube downloads work again — never once
per link. Nothing here gets round the block: no proxies, no tricks.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Dict, List, Optional

from . import media, notify, store

WAITING_STAGE = ("Paused — YouTube asked this PC to prove it's not a robot, so YouTube downloads are on hold. "
                 "This link is saved and runs when they start again.")
RESTART_STAGE = "Waiting in the queue — kept when ClipAgent restarted, and runs again by itself"
TICK_SECONDS = 60

_lock = threading.Lock()          # the runner, and the Telegram notes
_busy = False
_ticker_started = False


def park(job_id: str, url: str = "", *, hit: bool = False, raw: str = "") -> None:
    """Keep a YouTube link waiting instead of failing it. `hit`: this job met the robot check itself,
    so it shows the full explanation; the others say they're waiting."""
    message = (media.bot_block() or {}).get("message") or media.pause_message()
    fields: Dict[str, Any] = {"status": "queued", "paused": "youtube", "progress": 0, "error": "",
                              "stage": message if hit else WAITING_STAGE}
    if raw:
        fields["error_raw"] = raw[:4000]
    store.update_job(job_id, **fields)
    _announce_pause()


def is_parked(job_id: str) -> bool:
    job = store.get_job(job_id) or {}
    return job.get("status") == "queued" and bool(job.get("paused"))


def status() -> Dict[str, Any]:
    """What the pages show: is YouTube paused, since when, why, how many links wait, when ClipAgent tries."""
    block = media.bot_block()
    waiting = store.waiting_jobs()
    youtube = [j for j in waiting if j.get("paused") == "youtube"]
    return {
        "paused": bool(block),
        "since": (block or {}).get("since", ""),
        "since_ts": (block or {}).get("since_ts"),
        "message": (block or {}).get("message", ""),
        "platform": (block or {}).get("platform", ""),
        "waiting": len(youtube),
        "waiting_all": len(waiting),
        "auto_retry_at": (block or {}).get("auto_retry_at"),
        "auto_tried": bool((block or {}).get("auto_tried")),
        "resuming": _busy,
        "cookies": media.cookie_status()["kind"],
    }


# --- Telegram: once when paused, once when it works again ----------------------------

def _announce_pause() -> None:
    with _lock:
        if media.pause_state().get("announced"):
            return
        media.update_pause_state(announced=True)
    notify.download_pause(True)


def youtube_ok(url: str) -> None:
    """A YouTube download went through. If the pause was announced, say it's over (once)."""
    if not media.is_youtube(url):
        return
    with _lock:
        state = media.pause_state()
        if not state.get("announced") or state.get("block"):
            return
        media.update_pause_state(announced=False)
    notify.download_pause(False, waiting=len([j for j in store.waiting_jobs() if j.get("paused") == "youtube"]))


# --- running the waiting links ----------------------------------------------------------

def _next(tried: set) -> Optional[Dict[str, Any]]:
    """The oldest waiting link that can run now. While YouTube is paused its links stay put."""
    blocked = media.bot_block() is not None
    for job in store.waiting_jobs():
        if job["id"] in tried:
            continue
        if blocked and media.is_youtube(job.get("source") or ""):
            if job.get("paused") != "youtube":            # kept at a restart, now waiting for YouTube
                park(job["id"], job.get("source") or "")
            continue
        return job
    return None


def _run_waiting() -> None:
    global _busy
    from . import pipeline                                # late: pipeline imports this module
    tried: set = set()
    try:
        while True:
            job = _next(tried)
            if not job:
                break
            tried.add(job["id"])
            url = job.get("source") or ""
            if not url.startswith("http"):
                store.update_job(job["id"], status="failed", paused="", progress=100, stage="Failed",
                                 error="This one has no link to download again — drop the file in again.")
                continue
            store.update_job(job["id"], paused="", stage="Queued", error_raw="")
            try:
                pipeline.run_job(job["id"], url, None)
            except Exception:
                traceback.print_exc()
    finally:
        with _lock:
            _busy = False
        if media.bot_block() is None and media.pause_state().get("probe"):
            media.update_pause_state(probe="")            # that try wasn't stopped by the robot check


def run_waiting(background: bool = True) -> bool:
    """Run the waiting links one after another (one runner at a time). False if one is already going."""
    global _busy
    with _lock:
        if _busy:
            return False
        _busy = True
    if background:
        threading.Thread(target=_run_waiting, name="waiting-links", daemon=True).start()
    else:
        _run_waiting()
    return True


def try_again_now(background: bool = True) -> Dict[str, Any]:
    """The "Try again now" button (and Telegram /resume): lift the pause and run the saved links,
    oldest first. If the first one meets the robot check again, everything stays paused."""
    media.clear_bot_block()
    started = run_waiting(background)
    return {**status(), "started": started}


def tick(now: Optional[float] = None, background: bool = True) -> bool:
    """Once a minute: about 45 minutes into a pause, try one waiting link by itself — once per
    pause. If it still meets the robot check, the pause stays until someone presses Try again."""
    block = media.bot_block()
    if not block or block.get("auto_retry_at") is None or _busy:
        return False
    if (now or time.time()) < block["auto_retry_at"]:
        return False
    if not [j for j in store.waiting_jobs() if j.get("paused") == "youtube"]:
        media.clear_bot_block()          # nothing waiting: lift it, the next YouTube link finds out
        return True
    media.clear_bot_block(probe="auto")
    run_waiting(background)
    return True


def _ticker() -> None:
    while True:
        time.sleep(TICK_SECONDS)
        try:
            tick()
        except Exception:                # never let the loop die
            traceback.print_exc()


def start(background: bool = True) -> None:
    """At startup: the auto-retry clock, and any links kept from before a restart that can run now."""
    global _ticker_started
    if not _ticker_started:
        _ticker_started = True
        threading.Thread(target=_ticker, name="youtube-pause", daemon=True).start()
    if _next(set()):
        run_waiting(background)


def waiting_ids() -> List[str]:
    return [j["id"] for j in store.waiting_jobs()]
