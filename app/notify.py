"""Telegram updates.

ClipAgent messages you when a video's clips are ready (the clips themselves,
with the caption to paste), when something goes wrong, and when it has to
wait on something. It also takes orders: send the bot a link and it clips it.

Setup: make a bot with @BotFather, put its token in .env as
TELEGRAM_BOT_TOKEN, restart, and press Start on the bot. The first private
chat that presses Start becomes the owner; the bot ignores everyone else.

Nothing here may break a job. Every send goes through one background queue,
so a slow upload never holds up a render, and every failure is only logged.
"""
from __future__ import annotations

import html
import json
import os
import queue
import re
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from .config import DATA_DIR, WORK_DIR

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FIXED_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# all | none | a number (the top N clips of each video)
SEND_CLIPS = os.getenv("TELEGRAM_SEND_CLIPS", "all").strip().lower()
STATE_PATH = DATA_DIR / "telegram.json"
MAX_UPLOAD = 49 * 1024 * 1024          # the Bot API takes 50 MB
CAPTION_MAX = 1000                     # Telegram allows 1024 characters

_state_lock = threading.Lock()
_out: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_started = False
_sent_once: set = set()                # (job, kind) problems already reported


def enabled() -> bool:
    return bool(TOKEN)


def esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


# --- state ------------------------------------------------------------------

def _load() -> Dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _update_state(**fields) -> Dict[str, Any]:
    with _state_lock:
        state = {**_load(), **fields}
        _save(state)
        return state


def chat_id() -> str:
    return FIXED_CHAT or str(_load().get("chat_id") or "")


def connected() -> bool:
    return enabled() and bool(chat_id())


# --- the Bot API ----------------------------------------------------------------

def _call(method: str, data: Optional[Dict[str, Any]] = None, files=None,
          timeout: float = 60.0) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    resp = httpx.post(url, data=data or {}, files=files, timeout=timeout)
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method}: {body.get('description', resp.status_code)}")
    return body.get("result") or {}


# --- sending ----------------------------------------------------------------------

def send(text: str) -> None:
    """Queue an HTML message to the owner. Does nothing when not set up."""
    if connected() and text:
        _out.put({"kind": "text", "text": text[:4000]})


def send_video(path: Path, caption: str, duration: float = 0.0) -> None:
    if connected() and Path(path).exists():
        _out.put({"kind": "video", "path": str(path), "caption": caption, "duration": duration})


def problem(job_id: str, kind: str, text: str) -> None:
    """A heads-up about a wait or a hiccup, once per job and kind."""
    key = (job_id, kind)
    if key in _sent_once:
        return
    _sent_once.add(key)
    send(text)


def download_pause(paused: bool, waiting: int = 0) -> None:
    """The one message when YouTube downloads pause (not one per link), and the one when they work
    again. downloads.py decides when; the "already said it" flag lives on disk, so a restart
    doesn't repeat it."""
    if paused:
        send("⏸ <b>YouTube downloads are paused.</b> YouTube is asking this PC to prove it's not a robot, "
             "so I've stopped asking it for videos. Your links are saved and run when it lets up. Twitch, "
             "Kick and TikTok links and uploaded files keep going.\n\n"
             "What helps: wait an hour or two; or set up a cookies file from a spare account (ClipAgent → "
             "Settings shows how); or upload the video file instead.\n"
             "I'll try one link again by myself in about 45 min. Send /resume to try now.")
    else:
        rest = (f" Carrying on with the {waiting} saved link{'s' if waiting != 1 else ''}, one after another."
                if waiting else "")
        send(f"▶️ <b>YouTube downloads are working again.</b>{rest}")


def _small_copy(path: Path, duration: float) -> Optional[Path]:
    """A copy under Telegram's 50 MB limit, for clips that are bigger."""
    if duration <= 0:
        return None
    out = WORK_DIR / f"tg_{path.stem}.mp4"
    kbps = int(MAX_UPLOAD * 8 / 1000 / duration * 0.92) - 160
    if kbps < 300:
        return None
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast",
         "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
        capture_output=True)
    return out if proc.returncode == 0 and out.exists() else None


def _deliver(item: Dict[str, Any]) -> None:
    cid = chat_id()
    if item["kind"] == "text":
        _call("sendMessage", {"chat_id": cid, "text": item["text"], "parse_mode": "HTML",
                              "disable_web_page_preview": "true"})
        return
    path = Path(item["path"])
    temp = None
    if path.stat().st_size > MAX_UPLOAD:
        temp = _small_copy(path, item["duration"])
        if not temp:
            _call("sendMessage", {"chat_id": cid, "parse_mode": "HTML",
                                  "text": item["caption"] + "\n\n<i>Too big to send here — "
                                          "download it from ClipAgent.</i>"})
            return
        path = temp
    try:
        data = {"chat_id": cid, "caption": item["caption"], "parse_mode": "HTML",
                "supports_streaming": "true", "width": "1080", "height": "1920"}
        if item["duration"]:
            data["duration"] = str(int(round(item["duration"])))
        with open(path, "rb") as fh:
            # No custom thumbnail: Telegram takes the first frame, which is the hook.
            _call("sendVideo", data, files={"video": (path.name, fh, "video/mp4")}, timeout=600)
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def _sender() -> None:
    while True:
        item = _out.get()
        for attempt in range(3):
            try:
                _deliver(item)
                break
            except Exception as exc:                       # never let the sender die
                wait = 5 * (attempt + 1)
                match = re.search(r"retry after (\d+)", str(exc))
                if match:
                    wait = int(match.group(1)) + 1
                print(f"[telegram] send failed ({exc}); retrying in {wait}s")
                time.sleep(wait)
        time.sleep(0.4)                                    # stay well under the rate limit


# --- receiving ------------------------------------------------------------------

def _poller(handler: Callable[[str], Optional[str]]) -> None:
    offset = int(_load().get("offset") or 0)
    while True:
        try:
            updates = _call("getUpdates", {"offset": str(offset), "timeout": "50",
                                           "allowed_updates": '["message"]'}, timeout=70)
        except Exception as exc:
            text = str(exc)
            # 409: another copy of ClipAgent is polling this bot.
            time.sleep(30 if "409" in text or "Conflict" in text else 10)
            continue
        for upd in updates or []:
            offset = int(upd["update_id"]) + 1
            _update_state(offset=offset)
            handle_update(upd, handler)


def handle_update(upd: Dict[str, Any], handler: Callable[[str], Optional[str]]) -> None:
    """One incoming message: the owner's gets an answer, anyone else's doesn't."""
    msg = upd.get("message") or {}
    chat = msg.get("chat") or {}
    text = (msg.get("text") or "").strip()
    if not text or chat.get("type") != "private":
        return
    owner = chat_id()
    if not owner:
        if text.startswith("/start"):
            _update_state(chat_id=str(chat["id"]))
            send("✅ <b>Connected.</b> ClipAgent will message you here when clips are "
                 "ready or something needs you.\n\nSend me a video link any time and I'll "
                 "clip it. /help shows what else I can do.")
        return
    if str(chat.get("id")) != owner:
        try:
            _call("sendMessage", {"chat_id": str(chat["id"]), "text": "This bot is private."})
        except Exception:
            pass
        return
    try:
        reply = handler(text)
    except Exception as exc:
        traceback.print_exc()
        reply = f"⚠️ That didn't work: {esc(str(exc)[:300])}"
    if reply:
        send(reply)


def start(handler: Callable[[str], Optional[str]]) -> bool:
    """Start the sender and the poller, once per process."""
    global _started
    if _started or not enabled():
        return False
    _started = True
    threading.Thread(target=_sender, name="telegram-send", daemon=True).start()
    threading.Thread(target=_poller, args=(handler,), name="telegram-poll", daemon=True).start()
    return True


# --- job reports ----------------------------------------------------------------

def _loads(text: Any, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _clip_seconds(clip: Dict[str, Any]) -> float:
    parts = _loads(clip.get("parts"), [])
    if len(parts) > 1:
        return sum(p["end"] - p["start"] for p in parts) - float(clip.get("saved") or 0)
    return float(clip["end"] - clip["start"]) - float(clip.get("saved") or 0)


def _post_text(clip: Dict[str, Any]) -> str:
    post = _loads(clip.get("post"), {}) or {}
    if post.get("text"):
        return post["text"]
    caption = (clip.get("caption") or "").strip()
    tags = " ".join("#" + t.lstrip("#") for t in _loads(clip.get("hashtags"), []) if t)
    return (caption + ("\n\n" + tags if tags else "")).strip()


STATUS_ICON = {"ready": "✅ Ready", "check": "⚠️ Check", "blocked": "⛔ Blocked"}


def clip_caption(clip: Dict[str, Any], n: int) -> str:
    comp = _loads(clip.get("compliance"), None)
    head = [f"<b>#{n}</b>", f"{int(clip.get('score') or 0)}/100", f"{_clip_seconds(clip):.0f}s"]
    if comp:
        head.append(STATUS_ICON.get(comp.get("status"), ""))
    post = _loads(clip.get("post"), {}) or {}
    if post.get("platform"):
        head.append({"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube"}.get(
            post["platform"], post["platform"]))
    lines = [" · ".join(h for h in head if h)]
    if clip.get("hook"):
        lines.append(f"<b>{esc(clip['hook'])}</b>")
    if comp and comp.get("status") == "check":
        lines.append(f"<i>{esc(comp.get('summary', ''))[:220]}</i>")
    doc = _loads(clip.get("doctor"), None)
    if doc and doc.get("summary") and doc.get("status") != "good":
        lines.append(f"🩺 <i>{esc(doc['summary'])[:260]}</i>")
    text = _post_text(clip)
    if text:
        room = CAPTION_MAX - len("\n".join(lines)) - 40
        body = text if len(text) <= room else text[:max(0, room - 1)] + "…"
        lines.append(f"\nTap to copy:\n<code>{esc(body)}</code>")
    return "\n".join(lines)[:1024]


def _wanted(clips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if SEND_CLIPS == "none":
        return []
    if SEND_CLIPS.isdigit():
        return clips[:int(SEND_CLIPS)]
    return clips


def job_finished(job_id: str) -> None:
    """The report for a finished (or failed) job, clips included."""
    if not connected():
        return
    try:
        from . import store                                 # late: store imports config only
        job = store.get_job(job_id)
        if not job or job.get("status") in ("queued", "running"):    # not finished: waiting, or still going
            return
        title = esc((job.get("title") or "Your video")[:120])
        if job.get("status") == "failed":
            send(f"❌ <b>{title}</b> didn't work.\n\n{esc(job.get('error') or 'Unknown error')[:900]}"
                 "\n\nSend /retry to try it again.")
            return
        clips = store.list_clips(job_id)
        main = [c for c in clips if not c.get("alt_of")]
        ready = [c for c in main if c.get("status") == "ready" and c.get("file")
                 and Path(c["file"]).exists()]
        failed = [c for c in main if c.get("status") == "failed"]
        comp = [(_loads(c.get("compliance"), {}) or {}).get("status") for c in ready]
        blocked = [c for c, s in zip(ready, comp) if s == "blocked"]
        postable = [c for c, s in zip(ready, comp) if s != "blocked"]
        if not ready:
            send(f"🤷 <b>{title}</b>: {esc(job.get('stage') or 'no clips came out of it.')}")
            return
        lines = [f"🎬 <b>{title}</b>", f"{len(ready)} clip{'s' if len(ready) != 1 else ''} ready"]
        if any(comp):
            counts = {k: comp.count(k) for k in ("ready", "check", "blocked")}
            lines.append(f"Campaign check: {counts['ready']} ready · {counts['check']} check · "
                         f"{counts['blocked']} blocked")
        alts = len([c for c in clips if c.get("alt_of") and c.get("status") == "ready"])
        if alts:
            lines.append(f"+ {alts} other version{'s' if alts != 1 else ''} in ClipAgent")
        docs = [(_loads(c.get("doctor"), {}) or {}).get("status") for c in ready]
        if any(docs):
            lines.append(f"Clip doctor: {docs.count('good')} good · {docs.count('fixed')} fixed · "
                         f"{docs.count('check')} to look at")
        if failed:
            lines.append(f"{len(failed)} failed to render — open ClipAgent to retry them")
        if blocked:
            lines.append(f"Not sending {len(blocked)} blocked clip{'s' if len(blocked) != 1 else ''} "
                         "(they break the brief).")
        send_list = _wanted(postable)
        if send_list:
            lines.append(f"\nSending {len(send_list)} now, best first 👇")
        send("\n".join(lines))
        for n, clip in enumerate(send_list, start=1):
            send_video(Path(clip["file"]), clip_caption(clip, n), _clip_seconds(clip))
    except Exception:
        traceback.print_exc()
