"""Ingest + low level media helpers (yt-dlp, ffprobe, ffmpeg audio work)."""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .config import AUDIO_DIR, DATA_DIR, SOURCE_DIR, WORK_DIR

SUPPORTED_UPLOAD = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ts", ".flv",
                    ".mp3", ".wav", ".m4a"}


def run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, raising with the tail of stderr when it fails."""
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-12:]
        raise RuntimeError(f"{cmd[0]} failed:\n" + "\n".join(tail))
    return proc


def probe(path: Path) -> Dict[str, Any]:
    out = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]).stdout
    info = json.loads(out)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    duration = float(info["format"].get("duration") or 0)
    return {
        "duration": duration,
        "width": int(video["width"]) if video else 0,
        "height": int(video["height"]) if video else 0,
        "has_audio": audio is not None,
        "fps": _parse_fps(video.get("r_frame_rate")) if video else 0,
    }


def _parse_fps(rate: str | None) -> float:
    if not rate or "/" not in rate:
        return 0.0
    num, den = rate.split("/")
    return float(num) / float(den) if float(den) else 0.0


def safe_name(text: str, fallback: str = "video") -> str:
    text = re.sub(r"[^\w\s.-]", "", text or "").strip().replace(" ", "_")
    return (text or fallback)[:60]


# --- source acquisition ---------------------------------------------------

# YouTube sometimes decides a busy home connection is a bot. Logged-in cookies
# get past that: export them from a spare account (cookies.txt) or name a
# browser yt-dlp can read them from. Chrome and Edge lock theirs on Windows,
# so a cookies.txt file or Firefox are the ones that work there.
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES", "").strip().strip('"')
YTDLP_COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip().lower()


def _js_runtimes() -> Dict[str, Dict[str, str]]:
    """YouTube's player needs JavaScript solved for its full set of formats.
    yt-dlp only looks for Deno by default; Node is often already installed."""
    found = {}
    for name in ("deno", "node", "bun"):
        path = shutil.which(name)
        if path:
            found[name] = {"path": path}
    return found


def ytdlp_auth() -> Dict[str, Any]:
    """The cookie options from .env, for every yt-dlp call."""
    if YTDLP_COOKIES and Path(YTDLP_COOKIES).exists():
        return {"cookiefile": YTDLP_COOKIES}
    if YTDLP_COOKIES_FROM_BROWSER:
        return {"cookiesfrombrowser": (YTDLP_COOKIES_FROM_BROWSER,)}
    return {}


def cookie_status() -> Dict[str, Any]:
    """What the Settings page says about YouTube cookies (never the file's contents)."""
    if YTDLP_COOKIES:
        return {"kind": "file", "ok": Path(YTDLP_COOKIES).exists()}
    if YTDLP_COOKIES_FROM_BROWSER:
        return {"kind": "browser", "ok": True, "browser": YTDLP_COOKIES_FROM_BROWSER}
    return {"kind": "", "ok": False}


def _ytdlp_cli_auth() -> List[str]:
    """The same cookie and JavaScript options, for yt-dlp run as a program."""
    args: List[str] = []
    auth = ytdlp_auth()
    if auth.get("cookiefile"):
        args += ["--cookies", auth["cookiefile"]]
    elif auth.get("cookiesfrombrowser"):
        args += ["--cookies-from-browser", auth["cookiesfrombrowser"][0]]
    for name, where in _js_runtimes().items():
        args += ["--js-runtimes", f"{name}:{where['path']}"]
    return args


# --- which site a link is on ------------------------------------------------

_SITES = {"youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"), "twitch": ("twitch.tv",),
          "kick": ("kick.com",), "tiktok": ("tiktok.com",), "instagram": ("instagram.com",)}


def platform_of(url: str) -> str:
    """'youtube', 'twitch', 'kick', 'tiktok', 'instagram', 'other' — or '' for no link (an upload)."""
    if not str(url or "").startswith(("http://", "https://")):
        return ""
    host = (urlparse(url).hostname or "").lower()
    for name, hosts in _SITES.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    return "other"


def is_youtube(url: str) -> bool:
    return platform_of(url) == "youtube"


# --- YouTube's "confirm you're not a bot" pause --------------------------------
# When YouTube decides this PC is a robot, every further YouTube request only
# makes it worse. So the first time it says so, YouTube downloads stop until
# someone presses "Try again now" (or ClipAgent tries one link by itself after
# about 45 minutes). The state lives in a file, so a restart keeps the pause.
# Nothing here works around the block: no proxies, no tricks — it waits.

PAUSE_PATH = DATA_DIR / "download_pause.json"
AUTO_RETRY_SECONDS = 45 * 60
_pause_lock = threading.RLock()


def pause_message() -> str:
    """The plain words gs sees when YouTube downloads are paused."""
    if ytdlp_auth():
        return ("YouTube is asking this PC to prove it's not a robot — even with your cookies file, which may "
                "have run out — so ClipAgent paused YouTube downloads. Your links are saved. What helps: wait "
                "an hour or two; or export a fresh cookies file from the spare account (Settings shows how); "
                "or upload the video file instead.")
    return ("YouTube is asking this PC to prove it's not a robot, so ClipAgent paused YouTube downloads. "
            "Your links are saved. What helps: wait an hour or two; or set up a cookies file from a spare "
            "account (Settings shows how); or upload the video file instead.")


def _pause_read() -> Dict[str, Any]:
    try:
        data = json.loads(PAUSE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _pause_write(state: Dict[str, Any]) -> None:
    tmp = PAUSE_PATH.with_name(PAUSE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")          # written and closed before the swap
    for attempt in range(5):
        try:
            os.replace(tmp, PAUSE_PATH)
            return
        except PermissionError:          # Windows: something (a virus scan) has the file open for a moment
            time.sleep(0.2 * (attempt + 1))
    os.replace(tmp, PAUSE_PATH)


def pause_state() -> Dict[str, Any]:
    """Everything kept about the pause: the block (or None), whether Telegram was told, the last probe."""
    with _pause_lock:
        return _pause_read()


def update_pause_state(**fields) -> Dict[str, Any]:
    with _pause_lock:
        state = {**_pause_read(), **fields}
        _pause_write(state)
        return state


def bot_block() -> Optional[Dict[str, Any]]:
    """{"since": iso, "message": plain words, "platform": "youtube", ...} while YouTube's robot check
    has downloads paused, else None. Extra keys: "since_ts", "auto_retry_at" (epoch seconds, or None
    once the one automatic try has been used), "auto_tried"."""
    block = pause_state().get("block")
    if not isinstance(block, dict) or not block.get("since"):
        return None
    since_ts = float(block.get("since_ts") or time.time())
    tried = bool(block.get("auto_tried"))
    return {"since": block["since"], "message": block.get("message") or pause_message(),
            "platform": block.get("platform") or "youtube", "since_ts": since_ts, "auto_tried": tried,
            "auto_retry_at": None if tried else since_ts + AUTO_RETRY_SECONDS}


def set_bot_block(message: str, platform: str = "youtube") -> None:
    """Pause YouTube downloads. A block that is already on keeps its start time."""
    with _pause_lock:
        state = _pause_read()
        if isinstance(state.get("block"), dict) and state["block"].get("since"):
            return
        now = time.time()
        state["block"] = {
            "since": datetime.now().astimezone().isoformat(timespec="seconds"), "since_ts": now,
            "message": message or pause_message(), "platform": platform or "youtube",
            # hit again while ClipAgent was trying by itself: it doesn't keep trying
            "auto_tried": state.get("probe") == "auto",
        }
        state["probe"] = ""
        _pause_write(state)


def clear_bot_block(*, probe: str = "") -> None:
    """Lift the pause. `probe="auto"` marks ClipAgent's own one-time try, so a block straight after
    it stays until someone presses Try again."""
    with _pause_lock:
        state = _pause_read()
        state["block"] = None
        state["probe"] = probe
        _pause_write(state)


def youtube_paused(url: str) -> bool:
    """True when this link is a YouTube link and YouTube downloads are paused."""
    return is_youtube(url) and bot_block() is not None


def is_bot_check(text: str) -> bool:
    t = (text or "").lower()
    return "not a bot" in t or "sign in to confirm" in t


def noticed_bot_check(text: str, url: str = "") -> bool:
    """Any YouTube call that fails with the robot check pauses YouTube downloads (view checks too)."""
    if is_bot_check(text) and (is_youtube(url) or "[youtube" in (text or "").lower()):
        set_bot_block(pause_message())
        return True
    return False


def _youtube_worked(url: str) -> None:
    """A YouTube download went through: a later block gets its one automatic try again."""
    if is_youtube(url) and pause_state().get("probe"):
        update_pause_state(probe="")


def explain_download_error(text: str, url: str = "") -> str:
    """What went wrong with a download, in plain words, and what to do."""
    t = (text or "").lower()
    has_cookies = bool(ytdlp_auth())
    if "not a bot" in t or "sign in to confirm" in t:
        if has_cookies:
            return ("YouTube is still blocking downloads from this PC, even with the cookies — they may "
                    "have expired. Export fresh cookies.txt from the spare account, or wait an hour "
                    "and try again. Uploading the video file works meanwhile.")
        return ("YouTube is blocking downloads from this PC right now — it thinks the connection is a "
                "bot (usually after lots of downloads). It normally clears in a few hours. To skip the "
                "wait: add cookies from a spare YouTube account (YTDLP_COOKIES in .env), switch to a "
                "phone hotspot, or download the video yourself and upload the file.")
    if "private video" in t:
        return "That video is private, so it can't be downloaded."
    if "members-only" in t or "join this channel" in t or "members only" in t:
        return "That video is for channel members only."
    if "confirm your age" in t or "age-restricted" in t or "inappropriate for some users" in t:
        return ("That video is age-restricted. It needs cookies from a logged-in, age-verified account "
                "(YTDLP_COOKIES in .env).")
    if "live event will begin" in t or "premieres in" in t:
        return "That stream or premiere hasn't started yet."
    if "is live" in t or "live stream" in t and "not available" in t:
        return "That stream is still live — clip it once it has finished and the VOD is up."
    if "unsupported url" in t:
        return ("That link isn't a video page ClipAgent can download (share pages like Frame.io or Google "
                "Drive previews aren't). Download the file yourself and upload it instead.")
    if "video unavailable" in t or "has been removed" in t or "no longer available" in t:
        return "That video is unavailable — removed, blocked in your country, or the link is wrong."
    if "http error 429" in t or "too many requests" in t:
        return "The site says too many requests from this PC. Wait a while and try again."
    if "http error 403" in t:
        return "The site refused the download (403). Try again later, or upload the file."
    if ("getaddrinfo" in t or "timed out" in t or "connection" in t and "refused" in t
            or "unable to download webpage" in t or "network is unreachable" in t):
        return "Couldn't reach the site — check the PC's internet connection and try again."
    if "requested format is not available" in t:
        return "No downloadable video format was offered for that link. Try again later, or upload the file."
    last = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    tail = (last[-1] if last else "unknown error").replace("ERROR: ", "")
    return f"Couldn't download that link: {tail[:240]}"


class DownloadError(RuntimeError):
    """A download failure, already explained in plain words. `raw` keeps what
    yt-dlp actually said, for "Technical details"."""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw or ""


class BotBlocked(DownloadError):
    """YouTube's robot check: YouTube downloads are paused. `hit` is True when
    this very call ran into it, False when it was refused because of an earlier block."""

    def __init__(self, message: str, raw: str = "", hit: bool = False):
        super().__init__(message, raw)
        self.hit = hit


VIDEO_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"


def _refuse_while_paused(url: str) -> None:
    """While YouTube has downloads paused, ClipAgent doesn't ask it again."""
    if youtube_paused(url):
        raise BotBlocked((bot_block() or {}).get("message") or pause_message())


def _explained(exc: BaseException, url: str) -> DownloadError:
    """yt-dlp's error as a DownloadError in plain words; the robot check pauses YouTube downloads."""
    raw = str(exc)
    if noticed_bot_check(raw, url):
        return BotBlocked(pause_message(), raw=raw, hit=True)
    return DownloadError(explain_download_error(raw, url), raw=raw)


def _ydl_opts(**extra) -> Dict[str, Any]:
    opts = {"format": VIDEO_FORMAT, "merge_output_format": "mp4", "noplaylist": True, "quiet": True,
            "no_warnings": True, "retries": 3, "concurrent_fragment_downloads": 4, **ytdlp_auth(), **extra}
    runtimes = _js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes
    return opts


def _ydl_extract(url: str, opts: Dict[str, Any], download: bool,
                 info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The one place the yt-dlp library reads a link (tests replace this).
    Given `info` from read_info, it downloads from that instead of asking the site again."""
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        if info is not None:
            return ydl.process_ie_result(info, download=download)
        return ydl.extract_info(url, download=download)


def read_info(url: str) -> Dict[str, Any]:
    """What the site says about a video — title, length, upload date — without downloading it."""
    _refuse_while_paused(url)
    try:
        info = _ydl_extract(url, _ydl_opts(), download=False)
    except Exception as exc:          # yt-dlp's own errors are long and technical
        raise _explained(exc, url) from exc
    if not info:
        raise DownloadError("That link didn't lead to a video. Check the link, or upload the file instead.")
    if info.get("_type") in ("playlist", "multi_video") and not info.get("formats"):
        raise DownloadError("That link is a whole channel or playlist, not one video. Open the video you "
                            "want and paste its own link.")
    if info.get("is_live") or info.get("live_status") == "is_live":
        raise DownloadError("That stream is still live — clip it once it has finished and the VOD is up.")
    return info


def source_meta(info: Optional[Dict[str, Any]], *, title: str = "", duration: float = 0.0) -> Dict[str, Any]:
    """The facts kept with every job (jobs.source_meta). An upload has no info: empty upload date."""
    info = info or {}
    try:
        length = float(info.get("duration") or duration or 0)
    except (TypeError, ValueError):
        length = float(duration or 0)
    return {
        "upload_date": str(info.get("upload_date") or ""),
        "uploader": str(info.get("uploader") or ""),
        "channel": str(info.get("channel") or info.get("uploader") or ""),
        "title": str(info.get("title") or title or ""),
        "duration": round(length, 2),
        "webpage_url": str(info.get("webpage_url") or info.get("original_url") or ""),
        "extractor": str(info.get("extractor_key") or info.get("extractor") or ("" if info else "upload")),
    }


def download(url: str, job_id: str, progress=None, info: Optional[Dict[str, Any]] = None) -> Tuple[Path, str]:
    """Download a URL with yt-dlp. Returns (path, title).

    Works for YouTube, Twitch VODs and clips, Kick, TikTok, Instagram, X,
    Vimeo, Dailymotion and anything else yt-dlp supports. Pass the `info`
    read_info returned and the site isn't asked twice.
    """
    _refuse_while_paused(url)
    target = SOURCE_DIR / job_id
    target.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                progress(min(24, int(done / total * 24)))

    opts = _ydl_opts(outtmpl=str(target / "source.%(ext)s"), progress_hooks=[hook])
    try:
        got = _ydl_extract(url, opts, download=True, info=info)
        title = (got or {}).get("title") or (info or {}).get("title") or "Untitled"
    except Exception as exc:          # yt-dlp's own errors are long and technical
        raise _explained(exc, url) from exc
    _youtube_worked(url)

    files = [p for p in target.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not files:
        raise DownloadError("The download finished but no video file came out of it. Try again, "
                            "or upload the file.")
    return max(files, key=lambda p: p.stat().st_size), title


# --- yt-dlp run as a program (sound only, sections) ---------------------------------

def _run_streaming(cmd: List[str], on_line: Optional[Callable[[str], None]] = None) -> Tuple[int, str]:
    """Run a command, handing each output line to `on_line`; returns (exit code, the last lines).
    An argument list, never a shell; ffmpeg's carriage-return progress arrives as lines too."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    tail: "collections.deque[str]" = collections.deque(maxlen=60)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        if on_line:
            try:
                on_line(line)
            except Exception:              # a progress note must never stop a download
                pass
    return proc.wait(), "\n".join(tail)


RUNNER: Callable[..., Tuple[int, str]] = _run_streaming      # tests swap in a fake
_sleep = time.sleep


def _ytdlp(url: str, *args: str) -> List[str]:
    return [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--newline", "--socket-timeout", "30",
            "--retries", "5", "--fragment-retries", "10", *_ytdlp_cli_auth(), *args, url]


def _made_files(folder: Path, stem: str) -> List[Path]:
    return [p for p in folder.glob(f"{stem}.*") if p.is_file()
            and not p.name.endswith((".part", ".ytdl", ".json", ".tmp")) and ".part-Frag" not in p.name]


def _percent(line: str) -> Optional[float]:
    m = re.search(r"(\d{1,3}(?:\.\d+)?)%", line)
    return min(1.0, float(m.group(1)) / 100) if m else None


def download_audio_only(url: str, out_dir: Path, *, progress=None) -> Path:
    """The cheapest sound-only copy of a video (Twitch's "Audio_Only", YouTube's smallest audio),
    for finding the loud moments before any video is downloaded. A download cut off part-way
    picks up where it stopped. `progress(fraction)` gets 0-1."""
    _refuse_while_paused(url)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = _ytdlp(url, "-f", "wa/worst", "--concurrent-fragments", "4", "-o", str(out_dir / "audio.%(ext)s"),
                 "--progress-template", "download:[sound] %(progress._percent_str)s")

    def line(text: str) -> None:
        if progress and text.startswith("[sound]"):
            pct = _percent(text)
            if pct is not None:
                progress(pct)

    last = ""
    for attempt in range(3):
        code, last = RUNNER(cmd, line)
        if code == 0:
            made = _made_files(out_dir, "audio")
            if made:
                _youtube_worked(url)
                return max(made, key=lambda p: p.stat().st_size)
            last = last or "no audio file came out"
        if noticed_bot_check(last, url):
            raise BotBlocked(pause_message(), raw=last, hit=True)
        if attempt < 2:
            _sleep(10 * (attempt + 1))
    raise DownloadError("Couldn't download this video's sound to find its best parts. "
                        + explain_download_error(last, url), raw=last)


SECTION_TRIES = 3
# --force-keyframes-at-cuts re-encodes each part, so it starts exactly where asked. A quick x264
# preset keeps that from taking as long as the stream itself; the clips are encoded again later.
SECTION_ENCODE = "ffmpeg_o:-c:v libx264 -preset veryfast -crf 19 -c:a aac -b:a 160k"


def _clock(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _length_words(seconds: float) -> str:
    m = int(round(seconds / 60))
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{max(1, m)} min"


def _piece_ok(path: Path) -> Optional[Dict[str, Any]]:
    try:
        info = probe(path)
    except Exception:
        return None
    return info if info["duration"] > 0.5 and info["width"] > 0 else None


def download_sections(url: str, sections: List[Tuple[float, float]], out_dir: Path, *,
                      pad: float = 8.0, progress=None) -> List[Dict[str, Any]]:
    """Download only these stretches of a video, each padded by `pad` seconds on both sides.

    Returns one {"path", "start", "end", "offset", "duration", "index"} per stretch that came
    through, in order: start/end are the stretch asked for, offset is where the file begins in
    the original video (after padding), so original time = offset + time in the file.
    A stretch that fails is tried again (three goes); one already on disk from an earlier try is
    kept. `progress(fraction, text)` gets 0-1 and plain words."""
    _refuse_while_paused(url)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = []
    for i, (s, e) in enumerate(sections):
        s, e = float(s), float(e)
        if e - s >= 1.0:
            plan.append((i, s, e, max(0.0, s - pad), e + pad))
    done: List[Dict[str, Any]] = []
    last = ""
    for k, (i, s, e, a, b) in enumerate(plan):
        final = out_dir / f"section_{i:02d}_{int(a * 100)}-{int(b * 100)}.mp4"
        got = _piece_ok(final) if final.exists() else None
        span = b - a

        def line(text: str, k=k, span=span) -> None:
            m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", text)
            if progress and m:
                t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                progress((k + min(1.0, t / span)) / len(plan), f"part {k + 1} of {len(plan)}")

        for attempt in range(SECTION_TRIES):
            if got:
                break
            work = out_dir / f"_part{i:02d}"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            cmd = _ytdlp(url, "-f", VIDEO_FORMAT, "--download-sections", f"*{a:.2f}-{b:.2f}",
                         "--force-keyframes-at-cuts", "--downloader-args", SECTION_ENCODE,
                         "--merge-output-format", "mp4", "-o", str(work / "piece.%(ext)s"))
            code, last = RUNNER(cmd, line)
            made = _made_files(work, "piece") if code == 0 else []
            if made:
                _replace(max(made, key=lambda p: p.stat().st_size), final)
                got = _piece_ok(final)
            shutil.rmtree(work, ignore_errors=True)
            if got:
                break
            if noticed_bot_check(last, url):
                raise BotBlocked(pause_message(), raw=last, hit=True)
            if attempt < SECTION_TRIES - 1:
                _sleep(5 * (attempt + 1))
        if got:
            done.append({"path": final, "start": s, "end": e, "offset": a,
                         "duration": round(got["duration"], 3), "index": i})
        if progress:
            progress((k + 1) / len(plan), f"part {k + 1} of {len(plan)}")
    if not done:
        raise DownloadError(f"Couldn't download any of the {len(plan)} parts. "
                            + explain_download_error(last, url), raw=last)
    _youtube_worked(url)
    return done


def _codec_key(path: Path) -> Tuple[Any, ...]:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)]).stdout
    streams = json.loads(out).get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return (v.get("codec_name"), v.get("width"), v.get("height"), v.get("pix_fmt"), v.get("r_frame_rate"),
            a.get("codec_name"), a.get("sample_rate"), a.get("channels"))


def _replace(src: Path, dst: Path) -> None:
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:          # Windows: a player or a virus scan has it open for a moment
            time.sleep(0.5 * (attempt + 1))
    os.replace(src, dst)


def join_sections(pieces: List[Dict[str, Any]], out: Path) -> List[Dict[str, Any]]:
    """Put downloaded parts end to end in one file, and say where each one came from.

    Returns the manifest: one {"source_start", "source_end", "vod_start", "vod_end", "why"} per
    part — source_* in the joined file, vod_* in the original video — so a time in the joined
    file maps back exactly (see vod_time). The joins are hard cuts between unrelated moments."""
    out = Path(out)
    if not pieces:
        raise DownloadError("There were no parts to put together.")
    lengths = [float(probe(Path(p["path"]))["duration"]) for p in pieces]
    tmp = out.with_name(out.stem + ".joining.mp4")
    listing = out.with_name(out.stem + ".parts.txt")
    same = len({_codec_key(Path(p["path"])) for p in pieces}) == 1
    joined = False
    if same:
        lines = []
        for p in pieces:
            path = Path(p["path"]).resolve().as_posix().replace("'", "'\\''")
            lines.append(f"file '{path}'")
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", str(listing), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                 "-movflags", "+faststart", str(tmp)])
            joined = abs(probe(tmp)["duration"] - sum(lengths)) <= 0.3 + 0.1 * len(pieces)
        except RuntimeError:
            joined = False
        finally:
            listing.unlink(missing_ok=True)
    if not joined:
        # Different sizes or a copy that didn't line up: draw them all to the first one's size.
        first = probe(Path(pieces[0]["path"]))
        w, h = first["width"] or 1280, first["height"] or 720
        fps = round(first["fps"] or 30, 3)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        graph, labels = [], []
        for n, p in enumerate(pieces):
            cmd += ["-i", str(p["path"])]
        for n, p in enumerate(pieces):
            graph.append(f"[{n}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:"
                         f"(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{n}]")
            if probe(Path(p["path"]))["has_audio"]:
                graph.append(f"[{n}:a:0]aresample=48000,aformat=channel_layouts=stereo[a{n}]")
            else:
                graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{lengths[n]:.3f}[a{n}]")
            labels.append(f"[v{n}][a{n}]")
        graph.append("".join(labels) + f"concat=n={len(pieces)}:v=1:a=1[v][a]")
        run(cmd + ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                   "-preset", "veryfast", "-crf", "19", "-c:a", "aac", "-b:a", "160k",
                   "-movflags", "+faststart", str(tmp)])
    _replace(tmp, out)
    manifest, cursor = [], 0.0
    for p, length in zip(pieces, lengths):
        manifest.append({"source_start": round(cursor, 3), "source_end": round(cursor + length, 3),
                         "vod_start": round(float(p["offset"]), 3),
                         "vod_end": round(float(p["offset"]) + length, 3), "why": p.get("why", "")})
        cursor += length
    return manifest


def vod_time(t: float, sections: List[Dict[str, Any]]) -> Optional[float]:
    """A time in a joined source back to the original video's clock (None outside every part)."""
    for n, s in enumerate(sections or []):
        last = n == len(sections) - 1
        if s["source_start"] - 1e-6 <= t < s["source_end"] or (last and t <= s["source_end"] + 1e-6):
            return round(float(s["vod_start"]) + (t - float(s["source_start"])), 3)
    return None


def chat_activity(url: str, info: Dict[str, Any], out_dir: Path) -> Optional[List[float]]:
    """Chat messages per second from a stream's chat replay, when the site still hands it out.

    Twitch's replay used to come through yt-dlp as "rechat" subtitles; current yt-dlp no longer
    offers it, so this usually returns None and the sound is used instead."""
    subs = info.get("subtitles") or {}
    if "rechat" not in subs:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    code, last = RUNNER(_ytdlp(url, "--skip-download", "--write-subs", "--sub-langs", "rechat",
                               "-o", str(out_dir / "chat.%(ext)s")))
    if noticed_bot_check(last, url):
        raise BotBlocked(pause_message(), raw=last, hit=True)
    files = sorted(out_dir.glob("chat*.json"))
    if code != 0 or not files:
        return None
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        for f in files:
            f.unlink(missing_ok=True)
    comments = data.get("comments") if isinstance(data, dict) else data
    times = []
    for c in comments or []:
        try:
            times.append(float(c.get("content_offset_seconds")))
        except (AttributeError, TypeError, ValueError):
            continue
    if len(times) < 50:
        return None
    length = int(max(float(info.get("duration") or 0), max(times)) + 1)
    counts = [0.0] * length
    for t in times:
        if 0 <= t < length:
            counts[int(t)] += 1
    return counts


def _spread_windows(duration: float, total: float, count: int = 8) -> List[Dict[str, Any]]:
    """Parts spread evenly across a video, when there was nothing to measure."""
    count = max(1, count)
    each = min(360.0, max(60.0, total / count))
    out = []
    for n in range(count):
        mid = duration * (n + 0.5) / count
        start = max(0.0, min(duration - each, mid - each / 2))
        out.append({"start": round(start, 2), "end": round(start + each, 2), "score": 0,
                    "why": f"spread evenly — part {n + 1} of {count}, around {_clock(mid)}"})
    return out


def download_long(url: str, job_id: str, info: Dict[str, Any], *, limit_seconds: float,
                  keep_seconds: float = 3600.0, progress=None) -> Tuple[Path, List[dict], str]:
    """A video longer than the limit (a 6-hour Twitch VOD): find its best parts first, then
    download only those (at most `keep_seconds` of them) and join them into the job's normal
    source.mp4.

    Best parts: the chat replay when the site still has it, otherwise the loudness of the
    sound alone (shouting, laughter, big reactions), otherwise parts spread evenly.
    Returns (source.mp4, sections manifest for jobs.source_meta["sections"], a plain note).
    `progress(fraction, text)` gets 0-1 and plain words."""
    duration = float(info.get("duration") or 0)
    folder = SOURCE_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    # The work folder belongs to the link, not the job: a run that stopped part-way (YouTube's
    # robot check, the internet dropping, Try again) keeps the parts it already has.
    key = hashlib.sha1(str(info.get("webpage_url") or url).encode("utf-8")).hexdigest()[:16]
    parts = SOURCE_DIR / "_long" / key
    parts.mkdir(parents=True, exist_ok=True)
    plan_file = parts / "plan.json"
    platform = platform_of(url)
    kind = "stream" if platform in ("twitch", "kick") or info.get("was_live") \
        or info.get("live_status") == "was_live" else "video"
    long_words = f"This {kind} is {_length_words(duration)} long"
    budget = max(60.0, min(float(keep_seconds), float(limit_seconds)))

    def say(fraction: float, text: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, fraction)), text)

    windows: List[Dict[str, Any]] = []
    how = ""
    try:                                       # picked last time: no need to listen again
        saved = json.loads(plan_file.read_text(encoding="utf-8"))
        if abs(float(saved["duration"]) - duration) < 2 and abs(float(saved["budget"]) - budget) < 1:
            windows, how = list(saved["windows"]), str(saved["how"])
    except (OSError, ValueError, TypeError, KeyError):
        pass
    if not windows and platform == "twitch":
        say(0.0, f"{long_words} — checking its chat replay for the busiest moments…")
        try:
            chat = chat_activity(url, info, parts)
        except BotBlocked:
            raise
        except Exception:
            traceback.print_exc()
            chat = None
        if chat:
            env = [20 * math.log10(1 + c) - 60 for c in chat]
            windows = _busy_windows(env, 1.0, max_total=budget, noun=("burst of chat", "bursts of chat"),
                                    unit="far busier than the chat around it")
            how = "chat"
    if not windows:
        say(0.01, f"{long_words} — finding its loudest moments first (listening to the sound only)…")
        audio = None
        try:
            audio = download_audio_only(url, parts, progress=lambda f: say(
                0.01 + f * 0.39, f"{long_words} — finding its loudest moments first (listening to the sound "
                                 f"only, {int(f * 100)}%)…"))
            say(0.4, f"{long_words} — measuring how loud every second is…")
            windows = loud_windows(loudness_envelope(audio), 1.0, max_total=budget)
            how = "sound"
        except BotBlocked:
            raise
        except Exception:
            traceback.print_exc()
            windows = []
        finally:
            if audio:
                Path(audio).unlink(missing_ok=True)
    if windows and how in ("chat", "sound"):
        plan_file.write_text(json.dumps({"duration": duration, "budget": budget, "how": how,
                                         "windows": windows}), encoding="utf-8")
    if not windows:
        windows = _spread_windows(duration, budget)
        how = "spread"

    total = sum(w["end"] - w["start"] for w in windows)
    say(0.45, f"Picked the {len(windows)} best parts ({_length_words(total)}) — downloading just those…")
    pieces = download_sections(url, [(w["start"], w["end"]) for w in windows], parts, pad=8.0,
                               progress=lambda f, text: say(0.45 + f * 0.5, f"Downloading the {len(windows)} best "
                                                                              f"parts — {text}…"))
    for p in pieces:
        p["why"] = windows[p["index"]].get("why", "")
    say(0.96, f"Joining the {len(pieces)} parts into one video…")
    out = folder / "source.mp4"
    manifest = join_sections(pieces, out)
    shutil.rmtree(parts, ignore_errors=True)               # scratch: the joined copy is the source now

    got = sum(s["source_end"] - s["source_start"] for s in manifest)
    chosen = {"chat": "the moments its chat replay went busiest",
              "sound": "its loudest moments (shouting, laughter, big reactions), found by listening to the "
                       "sound alone first. Loud isn't always best, so a great quiet moment can be missed",
              "spread": "parts spread evenly across it, because ClipAgent couldn't listen to its sound first"}[how]
    note = (f"{long_words} — longer than ClipAgent's {_length_words(limit_seconds)} limit — so it downloaded only "
            f"{len(manifest)} parts ({_length_words(got)}): {chosen}.")
    if platform == "twitch" and how != "chat":
        note += " Twitch's chat replay isn't available to ClipAgent, so chat couldn't help pick."
    if len(manifest) < len(windows):
        note += f" {len(windows) - len(manifest)} of the {len(windows)} parts wouldn't download and were left out."
    note += " Clips never run across the joins between parts."
    return out, manifest, note


def store_upload(tmp_path: Path, filename: str, job_id: str, stem: str = "source") -> Path:
    target = SOURCE_DIR / job_id
    target.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower() or ".mp4"
    if suffix not in SUPPORTED_UPLOAD:
        raise RuntimeError(f"Unsupported file type: {suffix}")
    dest = target / f"{stem}{suffix}"
    shutil.move(str(tmp_path), dest)
    return dest


# --- audio ----------------------------------------------------------------

def extract_audio(src: Path, job_id: str) -> Path:
    """16 kHz mono wav — what Whisper wants, and small."""
    out = AUDIO_DIR / f"{job_id}.wav"
    run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(out)])
    return out


def split_audio(wav: Path, chunk_seconds: int) -> List[Tuple[Path, float]]:
    """Split into chunks for the transcription API. Returns [(path, offset)].

    Chunks are always re-encoded to 64 kbps mono mp3, never copied as raw
    PCM. The source wav is 16 kHz/16-bit PCM (~32 KB/s), so a 900s chunk
    copied verbatim is already ~27.5 MB -- over the 25 MB upload cap both
    OpenAI and Groq enforce, and this can trip even on a single short file
    that's never "chunked" at all. MP3 at 64 kbps runs ~8 KB/s, so the same
    900s chunk lands under 7 MB with plenty of margin.
    """
    duration = probe(wav)["duration"]
    count = max(1, math.ceil(duration / chunk_seconds))
    chunks: List[Tuple[Path, float]] = []
    for i in range(count):
        offset = i * chunk_seconds
        length = min(chunk_seconds, duration - offset)
        part = wav.with_name(f"{wav.stem}_part{i:03d}.mp3")
        run(["ffmpeg", "-y", "-ss", str(offset), "-t", str(length),
             "-i", str(wav), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "libmp3lame", "-b:a", "64k", str(part)])
        chunks.append((part, float(offset)))
    return chunks


# --- loudness -------------------------------------------------------------

def _rms_values(wav: Path) -> List[float]:
    """Per-frame RMS readings from ffmpeg, normalised to 0-1."""
    proc = subprocess.run(
        ["ffmpeg", "-i", str(wav), "-af", "astats=metadata=1:reset=1,"
         "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    values: List[float] = []
    for line in proc.stdout.splitlines():
        if "RMS_level=" in line:
            try:
                values.append(float(line.split("=")[1]))
            except ValueError:
                continue
    if not values:
        return []
    finite = [v for v in values if v > -120]
    if not finite:
        return []
    lo, hi = min(finite), max(finite)
    span = (hi - lo) or 1.0
    return [max(0.0, (v - lo) / span) for v in values]


def energy_curve(wav: Path, target_bucket: float = 1.0) -> List[Dict[str, float]]:
    """Loudness over time as [{"t": seconds, "energy": 0-1}].

    This is the signal that catches a crowd reaction, a shout or a burst of
    laughter — the things a transcript alone misses.

    astats resets every N audio FRAMES, not every N seconds, and the frame size
    depends on the decoder. So the real spacing is derived from the duration
    and the number of readings, then resampled to `target_bucket` seconds.
    Assuming one second per reading put the peaks at roughly 8x their true
    timestamps.
    """
    normalised = _rms_values(wav)
    if not normalised:
        return []

    duration = probe(wav)["duration"] or float(len(normalised))
    step = duration / len(normalised)          # true seconds per reading

    # One value per `target_bucket` seconds, keeping the loudest reading in
    # each bucket — a half-second shout must survive the averaging.
    buckets: Dict[int, float] = {}
    for i, value in enumerate(normalised):
        slot = int((i * step) / target_bucket)
        buckets[slot] = max(buckets.get(slot, 0.0), value)
    return [{"t": round(slot * target_bucket, 2), "energy": round(v, 3)}
            for slot, v in sorted(buckets.items())]


def peak_windows(
    curve: List[Dict[str, float]],
    top: int = 25,
    smooth_seconds: float = 2.5,
    min_gap_seconds: float = 15.0,
) -> List[Dict[str, float]]:
    """Loudest moments, smoothed and spread out so one spike cannot win twice."""
    if not curve:
        return []
    bucket = (curve[1]["t"] - curve[0]["t"]) if len(curve) > 1 else 1.0
    bucket = bucket or 1.0
    span = max(1, int(smooth_seconds / bucket))
    gap = max(1, int(min_gap_seconds / bucket))

    smoothed = []
    for i in range(len(curve)):
        window = curve[max(0, i - span):i + span + 1]
        smoothed.append(sum(p["energy"] for p in window) / len(window))

    picked: List[int] = []
    for idx in sorted(range(len(smoothed)), key=lambda i: smoothed[i], reverse=True):
        if all(abs(idx - p) >= gap for p in picked):
            picked.append(idx)
        if len(picked) >= top:
            break
    return [{"t": curve[i]["t"], "energy": round(smoothed[i], 3)} for i in sorted(picked)]


def loudness_envelope(audio: Path, hop: float = 1.0) -> List[float]:
    """How loud each `hop` seconds of a file is, in dB (RMS; -90 for silence), measured by ffmpeg.

    The sound is brought down to 8 kHz mono first and cut into blocks of exactly one hop, so
    reading n sits at n * hop seconds — even for a 10-hour stream that takes a minute or two."""
    rate = 8000
    samples = max(1, int(round(hop * rate)))
    chain = (f"aresample={rate},aformat=channel_layouts=mono,asetnsamples=n={samples}:p=0,"
             "astats=metadata=1:reset=1{extra},"
             "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-")
    proc = None
    for extra in (":measure_perchannel=none:measure_overall=RMS_level", ""):   # older ffmpeg lacks these
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio), "-vn",
                               "-af", chain.format(extra=extra), "-f", "null", "-"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            break
    if proc is None or proc.returncode != 0:
        tail = (proc.stderr if proc else "").strip().splitlines()[-6:]
        raise RuntimeError("ffmpeg couldn't measure the loudness:\n" + "\n".join(tail))
    values: List[float] = []
    for line in proc.stdout.splitlines():
        if "RMS_level=" not in line:
            continue
        try:
            v = float(line.split("=", 1)[1])
        except ValueError:
            v = -90.0
        values.append(round(max(-90.0, v), 2) if math.isfinite(v) else -90.0)
    return values


def _rolling_median(x, half: int):
    """The median of the `half` readings either side of each reading (numpy only)."""
    import numpy as np
    step = max(1, half // 30)                      # medians on a coarser grid, then drawn back out
    coarse = x[::step]
    h = max(1, half // step)
    padded = np.pad(coarse, h, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * h + 1)
    med = np.median(windows, axis=1)
    return np.interp(np.arange(len(x)), np.arange(len(coarse)) * step, med)


def loud_windows(envelope: List[float], hop: float = 1.0, *, count: int = 8, min_len: float = 180,
                 max_len: float = 360, max_total: float = 3600,
                 avoid: Optional[List[Tuple[float, float]]] = None) -> List[Dict[str, Any]]:
    """The liveliest stretches of a long video, from its loudness envelope (dB per hop).

    Excitement — shouting, laughter, a crowd going off — shows up as bursts well above what's
    normal for the minutes around them, plus stretches that stay louder than the video usually
    is. Picks up to `count` windows of `min_len`..`max_len` seconds (grown while the minutes
    next to them stay lively), at most `max_total` seconds in all, never inside `avoid`; windows
    close together are merged. Returns [{"start", "end", "score" 0-100, "why"}] sorted by start."""
    return _busy_windows(envelope, hop, count=count, min_len=min_len, max_len=max_len, max_total=max_total,
                         avoid=avoid)


def _busy_windows(envelope: List[float], hop: float = 1.0, *, count: int = 8, min_len: float = 180,
                  max_len: float = 360, max_total: float = 3600,
                  avoid: Optional[List[Tuple[float, float]]] = None,
                  noun: Tuple[str, str] = ("loud burst", "loud bursts"),
                  unit: str = "{db:.0f} dB louder than the minutes around it") -> List[Dict[str, Any]]:
    import numpy as np
    e = np.asarray(envelope, dtype=float)
    if e.size == 0 or hop <= 0:
        return []
    e = np.clip(np.where(np.isfinite(e), e, -90.0), -90.0, 0.0)
    n = e.size
    duration = n * hop
    max_total = max(hop, float(max_total))
    min_len = min(float(min_len), max_total, duration)
    max_len = max(min_len, min(float(max_len), max_total))
    L = max(1, int(round(min_len / hop)))
    Lmax = max(L, int(round(max_len / hop)))

    base = _rolling_median(e, max(1, int(round(150 / hop))))     # what's normal in the 5 minutes around
    excess = np.clip(e - base, 0.0, None)
    burst = np.clip(excess - 3.0, 0.0, None) ** 1.5                # a little wobble isn't excitement;
    level = np.clip(e - np.median(e) - 2.0, 0.0, None)             # big bursts count for much more
    score = burst + 0.15 * level                                    # (+ staying louder than usual)

    blocked = np.zeros(n, dtype=bool)
    for a, b in avoid or []:
        blocked[max(0, int(a / hop)):max(0, int(math.ceil(b / hop)))] = True
    score[blocked] = 0.0
    used = blocked.copy()
    csum = np.concatenate([[0.0], np.cumsum(score)])
    ucum = lambda: np.concatenate([[0], np.cumsum(used)])          # noqa: E731
    gap = max(1, int(round(30 / hop)))

    picked: List[List[int]] = []
    total = 0.0
    while len(picked) < count and total + L * hop <= max_total + 1e-6:
        uc = ucum()
        free = (uc[L:] - uc[:-L]) == 0                             # a window may start here
        dens = np.where(free, csum[L:] - csum[:-L], -1.0)
        i = int(np.argmax(dens))
        if dens[i] <= 0:
            break
        a, b = i, i + L
        mean = (csum[b] - csum[a]) / L
        step = max(1, int(round(30 / hop)))
        while (b - a) + step <= Lmax and total + (b - a + step) * hop <= max_total + 1e-6:
            sides = []
            if a - step >= 0 and not used[a - step:a].any():
                sides.append((csum[a] - csum[a - step], "left"))
            if b + step <= n and not used[b:b + step].any():
                sides.append((csum[b + step] - csum[b], "right"))
            if not sides:
                break
            gain, side = max(sides)
            if gain / step < 0.6 * mean:                           # the minutes next to it are calmer
                break
            if side == "left":
                a -= step
            else:
                b += step
        # The excitement in the middle, with the lead-up before it and the reaction after it.
        weight = score[a:b]
        if weight.sum() > 0:
            centre = float((weight * np.arange(a, b)).sum() / weight.sum())
            shifted = int(round(min(max(0.0, centre - (b - a) / 2), n - (b - a))))
            if not used[shifted:shifted + (b - a)].any():
                a, b = shifted, shifted + (b - a)
        picked.append([a, b])
        total += (b - a) * hop
        used[max(0, a - gap):min(n, b + gap)] = True

    if not picked:
        return []
    picked.sort()
    merged: List[List[int]] = [picked[0]]
    for a, b in picked[1:]:
        last = merged[-1]
        between = (a - last[1]) * hop
        if between <= 90 and total + between <= max_total + 1e-6 and not blocked[last[1]:a].any():
            total += between
            last[1] = b
        else:
            merged.append([a, b])

    sums = [float(csum[b] - csum[a]) / max(1, b - a) for a, b in merged]
    best = max(sums) or 1.0
    out = []
    for (a, b), dens in zip(merged, sums):
        seg = excess[a:b]
        k = int(np.argmax(seg))
        peak_t, peak_db = (a + k) * hop, float(seg[k])
        loud = seg > 6.0
        bursts = int(np.count_nonzero(loud[1:] & ~loud[:-1]) + (1 if loud.size and loud[0] else 0))
        if bursts:
            why = (f"{bursts} {noun[0] if bursts == 1 else noun[1]} — the biggest at {_clock(peak_t)}, "
                   + unit.format(db=peak_db))
        else:
            why = f"a stretch that stays lively around {_clock((a + b) / 2 * hop)}"
        out.append({"start": round(a * hop, 2), "end": round(min(duration, b * hop), 2),
                    "score": int(round(100 * dens / best)), "why": why})
    return out


def waveform(source: Path, start: float, end: float, points: int = 420) -> List[float]:
    """A fixed-width loudness shape for one span, for the editor's timeline."""
    span = max(0.2, end - start)
    key = abs(hash((str(source), round(start, 2), round(end, 2)))) % 10 ** 10
    tmp = WORK_DIR / f"wave_{key}.wav"
    try:
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{start:.3f}", "-t", f"{span:.3f}", "-i", str(source),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)])
        values = _rms_values(tmp)
    except RuntimeError:
        return []
    finally:
        tmp.unlink(missing_ok=True)

    if not values:
        return []
    out = []
    for i in range(points):
        a = int(i * len(values) / points)
        b = max(a + 1, int((i + 1) * len(values) / points))
        chunk = values[a:b]
        out.append(round(max(chunk) if chunk else 0.0, 3))
    return out
