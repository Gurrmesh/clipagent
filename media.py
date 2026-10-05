"""Ingest + low level media helpers (yt-dlp, ffprobe, ffmpeg audio work)."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .config import AUDIO_DIR, SOURCE_DIR, WORK_DIR

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
    """A download failure, already explained in plain words."""


def download(url: str, job_id: str, progress=None) -> Tuple[Path, str]:
    """Download a URL with yt-dlp. Returns (path, title).

    Works for YouTube, Twitch VODs and clips, Kick, TikTok, Instagram, X,
    Vimeo, Dailymotion and anything else yt-dlp supports.
    """
    import yt_dlp

    target = SOURCE_DIR / job_id
    target.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                progress(min(24, int(done / total * 24)))

    opts = {
        "outtmpl": str(target / "source.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "retries": 3,
        "concurrent_fragment_downloads": 4,
        **ytdlp_auth(),
    }
    runtimes = _js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") or "Untitled"
    except Exception as exc:          # yt-dlp's own errors are long and technical
        raise DownloadError(explain_download_error(str(exc), url)) from exc

    files = [p for p in target.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not files:
        raise DownloadError("The download finished but no video file came out of it. Try again, "
                            "or upload the file.")
    return max(files, key=lambda p: p.stat().st_size), title


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
