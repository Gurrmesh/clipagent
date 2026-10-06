"""Phone copies (app/postready.py): a smaller file of a clip or edit over 50 MB — offline.

Run: python tests/post_ready.py
Makes a busy 60 s clip over 50 MB with ffmpeg (nothing is downloaded), then checks the
phone copy: made in the background, under 50 MB, the same length, sound and picture, the
index at the front; gone when the clip is made again or undone, and made again; served by
the download links (?phone=1) for clips and edits, the campaign block included; and sent
to Telegram instead of the full file.
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="post_ready_")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("WHISPER_API_KEY", "x")
os.environ["TELEGRAM_BOT_TOKEN"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import editrender, edits, instruct, main, notify, postready, store  # noqa: E402
from app.config import CLIP_DIR, WORK_DIR  # noqa: E402

FAILS = []
TMP = Path(os.environ["DATA_DIR"])


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    return json.loads(out)


def atoms(path):
    """The top-level boxes of an MP4, in file order."""
    names = []
    with open(path, "rb") as fh:
        while True:
            head = fh.read(8)
            if len(head) < 8:
                break
            size, kind = struct.unpack(">I4s", head)
            if size == 1:
                size = struct.unpack(">Q", fh.read(8))[0]
                fh.seek(size - 16, 1)
            else:
                fh.seek(size - 8, 1)
            names.append(kind.decode("latin-1"))
            if size == 0:
                break
    return names


def busy_clip(path, seconds=60, kbps=8000):
    """A 1080×1920 clip that's hard to compress (moving pattern + noise), with sound, at a high bitrate."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=s=1080x1920:r=30:d={seconds},noise=alls=30:allf=t", "-f", "lavfi", "-i",
                    f"sine=f=330:d={seconds}:sample_rate=48000", "-c:v", "libx264", "-preset", "ultrafast",
                    "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps}k", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(path)], check=True)


store.init()
client = TestClient(main.app)

print("== the 50 MB line")
edge = TMP / "edge.mp4"
with open(edge, "wb") as fh:
    fh.truncate(postready.LIMIT)
expect(not postready.needs_copy(edge), "a file of exactly 50 MB needs no phone copy (Telegram takes it)")
with open(edge, "r+b") as fh:
    fh.truncate(postready.LIMIT + 1)
expect(postready.needs_copy(edge), "one byte more does")
expect(not postready.needs_copy(postready.copy_path(edge)), "a phone copy is never copied again")
expect(postready.video_kbps(60, 160) <= 8000 and postready.video_kbps(30, 160) == 8000,
       f"bitrate from the length: 30 s → {postready.video_kbps(30, 160)}k, 60 s → {postready.video_kbps(60, 160)}k")
long_kbps = postready.video_kbps(90, 160)
expect(long_kbps >= 3900 and (long_kbps + 160) * 90 / 8 * 1000 < postready.CEILING,
       f"a 90 s clip keeps about 4 Mbps ({long_kbps}k) and still lands under 48 MB "
       f"({(long_kbps + 160) * 90 / 8 / 1000:.0f} MB)")
expect(postready.video_kbps(200, 160) < 4000, "a very long one goes lower rather than over 50 MB")
edge.unlink()

print("== a big clip gets its phone copy in the background")
job = store.create_job("Big talk", "upload", {})
cid = store.create_clip(job, {"start": 0, "end": 60, "title": "Big one", "hook": "h", "score": 80, "reason": "",
                              "tags": [], "rank": 1})
clip_file = CLIP_DIR / f"{cid}.mp4"
busy_clip(clip_file)
store.update_clip(cid, file=str(clip_file), status="ready")
full = clip_file.stat().st_size
expect(full > 50_000_000, f"the test clip is over 50 MB ({full / 1e6:.0f} MB)")
info = main.clip_json(store.get_clip(cid))["phone"]
expect(info["needed"] and not info["ready"], f"the page is told a phone copy is needed ({info})")
t0 = time.time()
postready.refresh(clip_file)
expect(time.time() - t0 < 1.0, "making it doesn't hold anything up (it runs in the background)")
expect(postready.wait_idle(600), "the phone copy got made")
copy = postready.copy_path(clip_file)
expect(copy.name == f"{cid}_post.mp4" and copy.parent == clip_file.parent, f"next to the clip as {copy.name}")
size = copy.stat().st_size if copy.exists() else 10 ** 12
expect(size < 50_000_000, f"under 50 MB ({size / 1e6:.1f} MB)")
a, b = probe(clip_file), probe(copy)
dur_a, dur_b = float(a["format"]["duration"]), float(b["format"]["duration"])
expect(abs(dur_a - dur_b) <= 0.1, f"the same length ({dur_a:.2f} s and {dur_b:.2f} s)")
v = next(s for s in b["streams"] if s["codec_type"] == "video")
au = [s for s in b["streams"] if s["codec_type"] == "audio"]
expect(v["codec_name"] == "h264" and v["profile"] == "High" and (v["width"], v["height"]) == (1080, 1920),
       f"H.264 High, 1080×1920 ({v['codec_name']} {v['profile']} {v['width']}×{v['height']})")
expect(len(au) == 1 and au[0]["codec_name"] == "aac", "with its sound (AAC)")
order = atoms(copy)
expect("moov" in order and "mdat" in order and order.index("moov") < order.index("mdat"),
       f"the index is at the front, so a phone plays it at once ({' '.join(order)})")
dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(copy), "-f", "null", "-"], capture_output=True, text=True)
expect(dec.returncode == 0 and not dec.stderr.strip(), "it plays through without errors")
info = main.clip_json(store.get_clip(cid))["phone"]
expect(info["ready"] and info["mb"] == round(size / 1e6), f"the page shows it ready, with its size ({info['mb']} MB)")

print("== the download links")
r = client.get(f"/api/clips/{cid}/download?phone=1")
expect(r.status_code == 200 and len(r.content) == size and "_phone.mp4" in r.headers.get("content-disposition", ""),
       f"?phone=1 gives the phone copy ({len(r.content) / 1e6:.1f} MB, {r.headers.get('content-disposition')})")
r = client.get(f"/api/clips/{cid}/download")
expect(r.status_code == 200 and len(r.content) == full, "without it, the full file")
store.update_clip(cid, compliance=json.dumps({"status": "blocked", "summary": "Blocked: no logos."}))
r = client.get(f"/api/clips/{cid}/download?phone=1")
expect(r.status_code == 409, f"a clip the campaign check blocked: no phone copy either ({r.status_code})")
r = client.get(f"/api/clips/{cid}/download?phone=1&anyway=1")
expect(r.status_code == 200 and len(r.content) == size, "…unless you download it anyway")
store.update_clip(cid, compliance="")

print("== made again: the old copy goes, a new one comes")
instruct.snapshot(cid)                                           # what the editor does before a re-render
remade = WORK_DIR / "remade.mp4"
busy_clip(remade, 50, 9000)                                      # a "re-render": a different version
editrender.swap_in(remade, clip_file)                            # the Windows-safe swap (edits use it too)
expect(not copy.exists(), "the phone copy of the old version is gone the moment the clip is replaced")
info = main.clip_json(store.get_clip(cid))["phone"]
expect(info["needed"] and not info["ready"], "the page doesn't offer a stale copy as ready")
expect(postready.wait_idle(600) and postready.fresh(clip_file), "a new one is made for the new version")
new_dur = float(probe(copy)["format"]["duration"])
expect(abs(new_dur - 50.0) <= 0.1, f"…of the new version ({new_dur:.2f} s long)")
r = client.post(f"/api/clips/{cid}/undo")
expect(r.status_code == 200, "Undo works")
expect(not postready.fresh(clip_file), "after Undo the copy of the undone version isn't served")
r = client.get(f"/api/clips/{cid}/download?phone=1")
got = Path(TMP / "got.mp4")
got.write_bytes(r.content)
expect(r.status_code == 200 and abs(float(probe(got)["format"]["duration"]) - dur_a) <= 0.1
       and len(r.content) < 50_000_000, "asked for after Undo: made right then, of the version that's back")
postready.wait_idle(600)

print("== edits too")
eid = store.create_edit("Big edit", {"sources": [], "style": "motivation"})
edit_file = edits.EDIT_DIR / f"{eid}.mp4"
edit_file.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(clip_file, edit_file)
store.update_edit(eid, file=str(edit_file), status="done")
out = client.get(f"/api/edits/{eid}").json()
expect(out.get("phone", {}).get("needed"), "the edit page is told a phone copy is needed")
r = client.get(f"/api/edits/{eid}/download?phone=1")
expect(r.status_code == 200 and len(r.content) < 50_000_000 and postready.copy_path(edit_file).exists(),
       f"?phone=1 makes and gives the edit's phone copy ({len(r.content) / 1e6:.1f} MB)")
store.update_edit(eid, compliance={"status": "blocked", "summary": "Blocked"})
r = client.get(f"/api/edits/{eid}/download?phone=1")
expect(r.status_code == 409, "a blocked edit: no phone copy either")
store.update_edit(eid, compliance=None)
r = client.delete(f"/api/edits/{eid}")
expect(r.status_code == 200 and not postready.copy_path(edit_file).exists(), "deleting the edit deletes its copy")

print("== Telegram gets the phone copy, not a failure")
sent = []
notify.chat_id = lambda: "1"
notify._call = lambda method, data=None, files=None, timeout=60.0: sent.append(
    (method, files["video"][0] if files else "", (data or {}).get("caption", "") or (data or {}).get("text", "")))
notify._deliver({"kind": "video", "path": str(clip_file), "caption": "<b>#1</b>", "duration": 60.0})
expect(sent and sent[0][0] == "sendVideo" and sent[0][1] == f"{cid}_post.mp4" and "Phone copy" in sent[0][2],
       f"a clip over 50 MB goes as its phone copy ({sent[:1]})")
small = WORK_DIR / "small.mp4"
busy_clip(small, 5, 2000)
sent.clear()
notify._deliver({"kind": "video", "path": str(small), "caption": "x", "duration": 5.0})
expect(sent and sent[0][1] == "small.mp4" and not postready.copy_path(small).exists(),
       "a small clip goes as it is, with no copy made")

print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed")
sys.exit(1 if FAILS else 0)
