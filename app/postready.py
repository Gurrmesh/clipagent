"""Phone copies: a smaller file of a big clip or edit, for posting from a phone and for Telegram.

A finished clip or edit over 50 MB gets `<its name>_post.mp4` next to it, made in the
background: H.264 High at the same size (1080×1920; anything wider is brought down to 1080
across), a bitrate worked out from its length so the copy lands under ~45 MB, the same sound
(copied as it is when it's AAC, else AAC 160k), the same length, and the index at the front
(+faststart) so a phone can start playing it at once.

A copy belongs to one version of its clip: its modified time is set to the clip's. Anything
that writes the clip again — a re-render, a trim, an Undo, a re-made edit — gives the clip a
different time, so the old copy is stale: it is never served, it is removed when the clip is
replaced, and a new one is made (in the background, or right away when someone asks for it).
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

LIMIT = 50_000_000             # Telegram bots can send up to 50 MB; above this a phone copy is offered
TARGET = 45_000_000            # what a copy aims for
CEILING = 48_000_000           # what a copy may never be over (it's made again smaller)
VIDEO_KBPS = (4000, 8000)      # the picture's bitrate, normally
FLOOR_KBPS = 1500              # very long clips go lower still, so the copy stays under the limit
AUDIO_KBPS = 160
SUFFIX = "_post"

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_jobs: "queue.Queue[Tuple[Path, int]]" = queue.Queue()
_pending: set = set()
_worker: Optional[threading.Thread] = None


def copy_path(path: Path) -> Path:
    path = Path(path)
    return path.with_name(path.stem + SUFFIX + ".mp4")


def _stamp(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def needs_copy(path: Any) -> bool:
    """Is this clip too big to post from a phone or send on Telegram as it is?"""
    st = _stamp(Path(path)) if path else None
    return bool(st) and st[0] > LIMIT and not Path(path).stem.endswith(SUFFIX)


def fresh(path: Any) -> Optional[Path]:
    """The phone copy of this version of the clip, or None (none made yet, or made from an older version)."""
    if not path:
        return None
    src, copy = _stamp(Path(path)), _stamp(copy_path(Path(path)))
    if not src or not copy or copy[0] <= 0:
        return None
    return copy_path(Path(path)) if abs(copy[1] - src[1]) < 1_000_000 else None


def info(path: Any) -> Dict[str, Any]:
    """For the page (file sizes only, nothing slow): is a phone copy needed, is it ready, how big."""
    st = _stamp(Path(path)) if path else None
    if not st:
        return {"needed": False, "ready": False, "mb": None, "full_mb": None}
    need = needs_copy(path)
    ready = fresh(path) if need else None
    return {"needed": need, "ready": bool(ready), "full_mb": round(st[0] / 1e6, 1),
            "mb": round(ready.stat().st_size / 1e6) if ready else None}


def _lock(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(Path(path).resolve()).lower(), threading.Lock())


def _probe(path: Path) -> Dict[str, Any]:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                          str(path)], capture_output=True, text=True).stdout
    data = json.loads(out or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not video:
        raise RuntimeError("This video has no picture ClipAgent can read, so there's no phone copy to make")
    return {"duration": float((data.get("format") or {}).get("duration") or video.get("duration") or 0),
            "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
            "audio": (audio or {}).get("codec_name") or "",
            "audio_kbps": int((audio or {}).get("bit_rate") or 0) // 1000}


def video_kbps(duration: float, audio_kbps: int) -> int:
    """The picture's bitrate for a copy of this length: under ~45 MB, 4-8 Mbps; a long clip may go up to
    48 MB to keep 4 Mbps, and lower than 4 only when even that wouldn't fit."""
    def fits(total_bytes: float) -> int:
        return int(total_bytes * 8 / 1000 / max(1.0, duration) * 0.97) - audio_kbps
    lo, hi = VIDEO_KBPS
    kbps = min(hi, fits(TARGET))
    if kbps < lo:
        kbps = min(lo, fits(CEILING))
    return max(FLOOR_KBPS, kbps)


def _encode(src: Path, out: Path, meta: Dict[str, Any], kbps: int) -> None:
    copy_audio = meta["audio"] == "aac" and 0 < meta["audio_kbps"] <= 256
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src),
           "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0",
           "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p",
           "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k",
           "-fps_mode", "passthrough"]
    if meta["width"] > 1080:
        cmd += ["-vf", "scale=1080:-2:flags=lanczos"]
    cmd += ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ar", "48000"]
    cmd += ["-movflags", "+faststart", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-4:])
        raise RuntimeError(f"Couldn't make the phone copy:\n{tail}")


def _put(tmp: Path, target: Path, mtime_ns: int) -> None:
    """The new copy in place of the old one. Windows won't replace a file that's open (a phone
    copy being downloaded): wait a little, then write over it instead."""
    os.utime(tmp, ns=(time.time_ns(), mtime_ns))
    for _ in range(10):
        try:
            tmp.replace(target)
            return
        except PermissionError:
            time.sleep(0.4)
    try:
        shutil.copyfile(tmp, target)
        os.utime(target, ns=(time.time_ns(), mtime_ns))
    except OSError as exc:
        raise RuntimeError("The phone copy is made but the old one is open somewhere (a download?) — "
                           "try again in a minute") from exc
    finally:
        tmp.unlink(missing_ok=True)


def make(path: Any) -> Path:
    """The phone copy of this clip as it is now: the one already made, or a new one. Waits if
    one is being made. Raises RuntimeError in plain words."""
    src = Path(path)
    with _lock(src):
        ready = fresh(src)
        if ready:
            return ready
        stamp = _stamp(src)
        if not stamp:
            raise RuntimeError("The video file isn't there any more — make it again first")
        meta = _probe(src)
        if meta["duration"] <= 0:
            raise RuntimeError("Couldn't tell how long this video is, so the phone copy wasn't made")
        audio = meta["audio_kbps"] if meta["audio"] == "aac" and 0 < meta["audio_kbps"] <= 256 else AUDIO_KBPS
        kbps = video_kbps(meta["duration"], audio)
        tmp = src.with_name(src.stem + SUFFIX + ".part.mp4")
        try:
            for _ in range(3):
                _encode(src, tmp, meta, kbps)
                size = tmp.stat().st_size
                if size <= CEILING or kbps <= FLOOR_KBPS:
                    break
                kbps = max(FLOOR_KBPS, int(kbps * TARGET / size * 0.95))       # came out big: again, smaller
            if _stamp(src) != stamp:
                # The clip was made again while this copy was being made: it's a copy of a version that's gone.
                raise RuntimeError("The video changed while its phone copy was being made — ask again in a moment")
            if tmp.stat().st_size > LIMIT:
                raise RuntimeError("This video is too long for a phone copy under 50 MB")
            _put(tmp, copy_path(src), stamp[1])
        finally:
            tmp.unlink(missing_ok=True)
        return copy_path(src)


def remove_stale(path: Any) -> None:
    """Delete a phone copy that's not of this version of the clip (or of no clip at all). A copy
    that's open (being downloaded) stays until next time — it's never served while stale."""
    if not path:
        return
    copy = copy_path(Path(path))
    if copy.exists() and not fresh(path):
        try:
            copy.unlink()
        except OSError:
            pass


def _work() -> None:
    while True:
        path, mtime = _jobs.get()
        try:
            st = _stamp(path)
            if st and st[1] == mtime and needs_copy(path) and not fresh(path):
                make(path)
        except Exception:                       # a phone copy must never break anything
            traceback.print_exc()
        finally:
            with _locks_guard:
                _pending.discard((str(path), mtime))


def refresh(path: Any) -> None:
    """After a clip or edit is (re)made: the old phone copy goes, and a new one is made in the
    background when the file is over 50 MB. Never raises, never waits."""
    global _worker
    try:
        if not path:
            return
        path = Path(path)
        remove_stale(path)
        st = _stamp(path)
        if not st or not needs_copy(path) or fresh(path):
            return
        key = (str(path), st[1])
        with _locks_guard:
            if key in _pending:
                return
            _pending.add(key)
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_work, name="phone-copies", daemon=True)
                _worker.start()
        _jobs.put((path, st[1]))
    except Exception:
        traceback.print_exc()


def wait_idle(timeout: float = 300.0) -> bool:
    """For tests: wait until no phone copy is waiting or being made."""
    end = time.time() + timeout
    while time.time() < end:
        with _locks_guard:
            if not _pending:
                return True
        time.sleep(0.2)
    return False
