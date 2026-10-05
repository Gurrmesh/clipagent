"""The Edit Maker's renderer (app/editrender.py) — offline, made-up footage and a made-up song.

Run: python tests/edit_render.py
Makes its own media with ffmpeg and tools/make_test_song.py (nothing is
downloaded), renders short edits and checks the file: 1080×1920, 30 fps, the
right length, sound at -14 LUFS, a bright flash on the drop, the colour
channels pulled apart by the glitch, every frame taken from the right moment
of the footage at the right speed, and the words kept off the face.
Then pull frames and look at them (the paths are printed).
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
import re
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="editrender_")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app import beats, editrender, edits  # noqa: E402

FAILS = []
TMP = Path(os.environ["DATA_DIR"])


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def frame(path, n):
    """Output frame n of a video, as BGR."""
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{n})", "-fps_mode", "passthrough",
                           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True)
    return np.frombuffer(proc.stdout, np.uint8).reshape(1920, 1080, 3)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    return json.loads(out)["streams"]


def lufs(path):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.findall(r"I:\s*(-?[\d.]+) LUFS", err)
    return float(m[-1]) if m else float("nan")


# --- media made here ---------------------------------------------------------------------
print("== making test media")
song_path = TMP / "song128.mp3"
subprocess.run([sys.executable, str(ROOT / "tools" / "make_test_song.py"), "128", "40", "10.365", "0.365",
                str(song_path)], check=True)
bars = TMP / "bars.mp4"                       # moving colour bars, a voice-like tone
ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=40", "-f", "lavfi", "-i",
   "sine=f=220:d=40:sample_rate=48000", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac",
   "-shortest", str(bars))
steps = TMP / "steps.mp4"                     # a white bar that moves down 10 px every second of the source
ff("-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=30",
   "-vf", "geq=lum='if(between(Y\\,10*floor(T)+10\\,10*floor(T)+16)\\,235\\,16)':cb=128:cr=128",
   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "8", "-g", "15", str(steps))


def bar_second(img):
    """Which whole second of the source a frame shows: where its white bar sits (colour can't move it)."""
    rows = img[:, 400:680, 1].astype(np.float32).mean(axis=1)
    y = float(np.argmax(rows)) / (1920 / 360)            # back to source pixels
    return int(round((y - 13) / 10))
song = {"id": "s1", "file": str(song_path), "analysis": beats.analyze(song_path)}
print(f"  song: {song['analysis']['bpm']} BPM, drop at {song['analysis']['drop']} s")

# --- a 6 s Velocity edit ------------------------------------------------------------------
print("== a 6 s Velocity edit, every effect")
ms = [{"id": f"m{i}", "source": "B", "start": 3.0 + i * 6, "end": 6.0 + i * 6, "hit": 4.5 + i * 6,
       "text": "", "drop": i == 2} for i in range(5)]
tl = edits.build_timeline(ms, "velocity", 6, song, {}, {}, durations={"B": 40.0}, hook="Seven years to get here")
out = TMP / "velocity.mp4"
info = editrender.render(tl, {"B": {"source_path": str(bars)}}, song, out, TMP / "velocity.jpg")
streams = probe(out)
v = next(s for s in streams if s["codec_type"] == "video")
expect(int(v["width"]) == 1080 and int(v["height"]) == 1920, "1080×1920")
expect(v["r_frame_rate"] == "30/1", "30 frames a second")
expect(abs(float(v["duration"]) - tl["length"]) <= 0.1, f"as long as the timeline ({float(v['duration']):.2f} s)")
expect(any(s["codec_type"] == "audio" for s in streams), "has sound")
loud = lufs(out)
expect(abs(loud + 14) <= 1.0, f"loudness about -14 LUFS ({loud:.1f})")
expect((TMP / "velocity.jpg").stat().st_size > 5000, "a thumbnail of the drop")
n_drop = int(round(tl["drop_at"] * 30))
plain = edits.build_timeline(ms, "velocity", 6, song, {"flash": False, "glitch": False}, {}, durations={"B": 40.0},
                             hook="Seven years to get here")
out2 = TMP / "velocity_plain.mp4"
editrender.render(plain, {"B": {"source_path": str(bars)}}, song, out2, TMP / "plain.jpg")
fa, fb = frame(out, n_drop), frame(out2, n_drop)
lum_a, lum_b = float(fa[600:1300, 250:830].mean()), float(fb[600:1300, 250:830].mean())
expect(lum_a > lum_b + 60 and lum_a > 215, f"the drop frame flashes white ({lum_a:.0f} vs {lum_b:.0f} without)")


def shift(a, b, ch):
    """How far channel `ch` of frame a is moved sideways against frame b (column profiles)."""
    pa = a[400:1500, :, ch].astype(np.float32).mean(axis=0)
    pb = b[400:1500, :, ch].astype(np.float32).mean(axis=0)
    pa, pb = pa - pa.mean(), pb - pb.mean()
    best = max(range(-30, 31), key=lambda d: float(np.dot(np.roll(pb, d)[40:-40], pa[40:-40])))
    return best


ga, gb = frame(out, n_drop + 3), frame(out2, n_drop + 3)
sb, sr, sg = shift(ga, gb, 0), shift(ga, gb, 2), shift(ga, gb, 1)
expect(7 <= abs(sb) <= 15 and abs(sr + sb) <= 2 and abs(sg) <= 1,          # colour is stored at half width: ±1 px
       f"the glitch pulls blue and red apart (blue {sb:+d} px, red {sr:+d} px, green {sg:+d} px)")
expect(info["frames"] == int(round(tl["length"] * 30)), "every frame written")

# --- the right footage at the right moment ---------------------------------------------------
print("== frames come from the right place in the footage")
off = {k: False for k in edits.EFFECTS}
grey_ms = [{"id": f"g{i}", "source": "S", "start": 2.2 + i * 5, "end": 5.6 + i * 5, "hit": 3.5 + i * 5, "text": "",
            "drop": i == 2} for i in range(5)]
tg = edits.build_timeline(grey_ms, "velocity", 6, song, off, {}, durations={"S": 30.0}, grade="none")
outg = TMP / "steps_out.mp4"
editrender.render(tg, {"S": {"source_path": str(steps)}}, song, outg, TMP / "steps.jpg")
bad = []
for seg in tg["segments"]:
    for t in (0.1, seg["dur"] / 2):
        n = int(round((seg["at"] + t) * 30))
        src_t = edits.src_time(seg, round(n / 30 - seg["at"], 6))
        if abs(src_t - round(src_t)) < 0.1:            # too near a step to tell
            continue
        got = bar_second(frame(outg, n))
        if got != int(src_t):
            bad.append((round(seg["at"] + t, 2), round(src_t, 2), got))
expect(not bad, f"each frame shows its own second of footage {bad[:3]}")
drop_seg = next(s for s in tg["segments"] if s["drop"])
dn = int(round(drop_seg["at"] * 30))
got = bar_second(frame(outg, dn))
expect(got == int(grey_ms[2]["hit"]), f"the frame on the drop is the drop moment's hit (second {got})")

# --- a speech edit with his voice and no song ---------------------------------------------
print("== Cinematic, his voice only")
words = [{"w": w, "start": 4.0 + i * 0.4, "end": 4.3 + i * 0.4} for i, w in enumerate("this is the line he says".split())]
sp = [{"id": "c1", "source": "B", "start": 4.0, "end": 6.4, "hit": 5.0, "text": "", "drop": True},
      {"id": "c2", "source": "B", "start": 20.0, "end": 23.0, "hit": 21.0, "text": "", "drop": False}]
tc = edits.build_timeline(sp, "cinematic", 10, None, {}, {"B": words}, durations={"B": 40.0})
outc = TMP / "cinematic.mp4"
editrender.render(tc, {"B": {"source_path": str(bars)}}, None, outc, TMP / "cinematic.jpg")
loud = lufs(outc)
expect(abs(loud + 14) <= 1.0, f"his voice at about -14 LUFS ({loud:.1f})")
fc = frame(outc, 45)
expect(fc[:editrender.BAR_H - 10].max() < 30 and fc[-editrender.BAR_H + 10:].max() < 30, "black cinema bars")

# --- picture and sound stay together ----------------------------------------------------------
print("== his voice stays on his lips")
sync = TMP / "sync.mp4"                     # a white flash and a beep at the same instant, every second (k + 0.5)
ff("-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=30,geq=lum='if(lt(mod(T-0.5+10\\,1)\\,0.03)\\,235\\,16)':cb=128:cr=128",
   "-f", "lavfi", "-i", "aevalsrc='if(lt(mod(t-0.5+10\\,1)\\,0.06)\\,0.6*sin(2*PI*1000*t)\\,0)':s=48000:d=30",
   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-g", "15", "-c:a", "pcm_s16le", "-shortest",
   str(sync.with_suffix(".mov")))
sync = sync.with_suffix(".mov")
swords = [{"w": f"w{i}", "start": 2.0 + i * 0.4, "end": 2.3 + i * 0.4} for i in range(60)]
sm = [{"id": "a", "source": "Y", "start": 3.37, "end": 7.91, "hit": 5.0, "text": "", "drop": True},
      {"id": "b", "source": "Y", "start": 14.12, "end": 18.66, "hit": 16.0, "text": "", "drop": False}]
ts = edits.build_timeline(sm, "cinematic", 12, None, {"letterbox": False, "grain": False, "text": False},
                          {"Y": swords}, durations={"Y": 30.0})
outs = TMP / "sync_out.mp4"
editrender.render(ts, {"Y": {"source_path": str(sync)}}, None, outs, TMP / "sync.jpg")
raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(outs), "-vf", "scale=54:96,format=gray", "-f", "rawvideo", "-"],
                     capture_output=True).stdout
lum = np.frombuffer(raw, np.uint8).reshape(-1, 96, 54)[:, 30:66, 10:44].mean(axis=(1, 2))
flashes = [i / 30 for i in range(1, len(lum)) if lum[i] > lum.max() * 0.6 and lum[i - 1] <= lum.max() * 0.6]
pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(outs), "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                     capture_output=True).stdout
y = np.abs(np.frombuffer(pcm, np.int16).astype(np.float32))
env = np.convolve(y, np.ones(96) / 96, mode="same")
on = env > env.max() * 0.3
beeps = [i / 48000 for i in range(1, len(on)) if on[i] and not on[i - 1]]
gaps = [b - min(flashes, key=lambda f: abs(f - b)) for b in beeps if flashes]
expect(len(flashes) >= 6 and len(beeps) >= 6, f"flashes and beeps found ({len(flashes)}, {len(beeps)})")
expect(gaps and max(abs(g) for g in gaps) < 0.025,
       f"every beep with its flash (worst {max(abs(g) for g in gaps) * 1000 if gaps else 0:.0f} ms)")

# --- words: never cut, never over the face ---------------------------------------------------
print("== the words")
lines, size = editrender.fit("BRO TURNED 500 DOLLARS INTO 2 MILLION AND STILL WAKES UP AT 4 EVERY DAY", 140, 3, 92)
expect(len(lines) <= 3 and " ".join(lines).split() == "BRO TURNED 500 DOLLARS INTO 2 MILLION AND STILL WAKES UP "
       "AT 4 EVERY DAY".split(), f"a long hook shrinks to {size} instead of losing words")
expect(editrender.wrap("HE MADE $2 MILLION IN 7 YEARS", 12) == ["HE MADE", "$2 MILLION", "IN 7 YEARS"],
       "a number stays with what it counts")
face = {"top": 420.0, "bottom": 900.0}
top = editrender.hook_top(2, 140, face, False)
expect(top + editrender.block_height(2, 140) <= face["top"] - 20 or top >= face["bottom"],
       f"the hook stays off the face (top at {top})")
high = {"top": 260.0, "bottom": 700.0}
top = editrender.hook_top(2, 140, high, False)
expect(top >= high["bottom"], f"no room above the head: the hook goes under the chin (top at {top})")
ass = TMP / "w.ass"
editrender.build_ass({**tc, "hook": {"text": "x", "start": 0, "end": 2.8}}, ass, None)
body = ass.read_text(encoding="utf-8")
expect("Dialogue" in body and "this is the line he says".split()[0] in body, "subtitles written from his words")
rng = np.random.default_rng(1)
img = np.zeros((300, 400, 3), np.uint8)
img[:, 195:205] = 255
g = editrender.glitch(img, rng)
peak = [int(np.argmax(g[..., c].astype(np.float32).mean(axis=0))) for c in range(3)]
expect(peak[1] in range(195, 205) and 8 <= abs(peak[0] - peak[1]) <= 18 and (peak[0] - peak[1]) * (peak[2] - peak[1]) < 0,
       f"the glitch moves blue and red opposite ways, green stays (columns {peak})")

print(f"\n  look at the frames: {out}  {outc}")
print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed")
sys.exit(1 if FAILS else 0)
