"""Burned-in text and the facecam split (app/textdetect.py, app/framing.py, app/motion.py, app/editrender.py).

Run: python tests/framing_text.py        (~3-5 min: it renders real clips)
Everything is made here (tools/make_test_footage.py, Pillow with the fonts in fonts/), nothing is downloaded.

1. A talking head with a big title near the face: the crop never cuts a word — on every frame of the
   render the title is wholly inside the 9:16 window or wholly outside it — and the face stays framed.
2. A title across the whole top: it's cropped out with a clear note, or (camera moves off) the whole
   picture is shown and no zoom pushes its words off the edges.
3. No text: the framing is exactly what it was before this check existed.
4. A stream with a small facecam: the split layout, the face big in the top half, the content half
   never cutting the game's title.
5. The detector: big titles in Anton/Poppins on busy backgrounds are found; faces, striped shirts,
   bookshelves, noise and a chat column are not.
Also: one Edit Maker moment, the ranking nudge, the split surviving a re-render.
Then pull the printed frames and look at them.
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="framing_text_")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import make_test_footage as footage  # noqa: E402
from app import beats, editrender, edits, framing, highlights, motion, render, textdetect  # noqa: E402

FAILS = []
TMP = Path(os.environ["DATA_DIR"])
W, H = 1920, 1080
OW, OH = motion.OW, motion.OH


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def make(name, *args, seconds=8):
    out = TMP / f"{name}.mp4"
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_test_footage.py"), str(seconds), str(out), *args],
                   check=True)
    return out


def true_box(text, pos):
    """Where the footage tool burned the title: the box of its pixels (source px)."""
    _, alpha = footage.title_layer(W, H, text, pos)
    ys, xs = np.nonzero(alpha[..., 0] > 0.05)
    return (float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1))


def grab(path, at, name):
    jpg = TMP / f"{name}.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(path), "-frames:v", "1", "-q:v", "3",
                    str(jpg)], check=True)
    return jpg


WORDS = [{"w": w, "start": 0.3 + i * 0.4, "end": 0.6 + i * 0.4}
         for i, w in enumerate("this is the plan for gold today".split())]


def clip(src, cid, layout="fill", **edit):
    e = {"layout": "auto", "hook": "He made it back in a week", "caption_position": "pop", **edit}
    return motion.render_clip(source=src, clip_id=cid, start=1.0, end=7.0, words=WORDS, edits=e, has_audio=True,
                              layout=layout, plan=None, source_size=(W, H), debug=True)


def windows(out):
    """Every output frame's crop window (source px): x0, y0, x1, y1 — shake included."""
    cam = out["camera"]
    cw, ch = H * 9 / 16, float(H)
    z = cam.zoom
    s = OW * z / cw
    cx, cy = cam.cx - cam.sx / s, cam.cy - cam.sy / s
    hw, hh = cw / (2 * z), ch / (2 * z)
    return cx - hw, cy - hh, cx + hw, cy + hh


def never_cut(out, box):
    """Frames where the window cuts through the box (neither wholly inside nor wholly outside)."""
    x0, y0, x1, y1 = windows(out)
    bx0, by0, bx1, by1 = box
    inside = (x0 <= bx0) & (x1 >= bx1) & (y0 <= by0) & (y1 >= by1)
    outside = (x1 <= bx0) | (x0 >= bx1) | (y1 <= by0) | (y0 >= by1)
    return int((~(inside | outside)).sum()), int(inside.sum()), int(outside.sum())


def face_framed(out):
    """Share of frames with a face whose face box sits wholly inside the window."""
    x0, y0, x1, y1 = windows(out)
    an, tl = out["analysis"], out["timeline"]
    src = tl.src_index()
    ok = seen = 0
    for n, f in enumerate(src):
        faces = an.faces.get(int(f))
        if not faces:
            continue
        g = max(faces, key=lambda g: g[2])
        seen += 1
        fx0, fy0, fx1, fy1 = (g[0] - g[2] / 2) * W, (g[1] - g[3] / 2) * H, (g[0] + g[2] / 2) * W, (g[1] + g[3] / 2) * H
        ok += x0[n] <= fx0 and fx1 <= x1[n] and y0[n] <= fy0 and fy1 <= y1[n]
    return ok / max(1, seen), seen


# --- 5. the detector on stills -------------------------------------------------------------
print("== 5. the detector: big titles found, faces / stripes / shelves / noise / chat not")
FONTS = {"anton": str(ROOT / "fonts" / "Anton-Regular.ttf"), "poppins": str(ROOT / "fonts" / "Poppins-Bold.ttf")}
rng = np.random.default_rng(4)


def room(w=W, h=H):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    glow = np.exp(-(((xx - w * 0.3) / (w * 0.5)) ** 2 + ((yy - h * 0.25) / (h * 0.6)) ** 2))
    img = np.clip(np.stack([60 + 70 * glow, 70 + 80 * glow, 85 + 95 * glow], axis=-1), 0, 255).astype(np.uint8)
    for _ in range(8):
        x0, y0 = int(rng.integers(0, w - 200)), int(rng.integers(0, h - 60))
        cv2.rectangle(img, (x0, y0), (x0 + int(rng.integers(80, 400)), y0 + int(rng.integers(10, 60))),
                      tuple(int(c) for c in rng.integers(30, 220, 3)), -1)
    return img


def noise(w=W, h=H):
    acc = np.zeros((h, w, 3), np.float32)
    for s in (4, 16, 64):
        acc += cv2.resize(rng.uniform(0, 255, (h // s, w // s, 3)).astype(np.float32), (w, h)) / 3
    return np.clip(acc, 0, 255).astype(np.uint8)


def books(w=W, h=H):
    img = np.full((h, w, 3), (40, 50, 60), np.uint8)
    for row in range(4):
        base = (row + 1) * h // 4 - 10
        x = 0
        while x < w:
            bw, bh = int(rng.integers(14, 45)), int(rng.integers(int(h / 4 * 0.6), h // 4 - 14))
            cv2.rectangle(img, (x, base - bh), (x + bw - 2, base), tuple(int(c) for c in rng.integers(30, 230, 3)), -1)
            if rng.random() < 0.5:
                cv2.rectangle(img, (x + 2, base - bh + 10), (x + bw - 4, base - bh + 18), (230, 220, 200), -1)
            x += bw
    return img


def person(img, stripes=None):
    r = int(H * 0.12)
    cx, cy = int(W * rng.uniform(0.35, 0.65)), int(H * 0.36)
    footage.person(img, cx, cy, r, 0.5, 0, 0.5)
    if stripes:
        body = np.zeros(img.shape[:2], np.uint8)
        cv2.ellipse(body, (cx, cy + int(r * 2.4)), (int(r * 1.6), int(r * 1.7)), 0, 180, 360, 255, -1)
        cv2.rectangle(body, (cx - int(r * 1.6), cy + int(r * 2.4)), (cx + int(r * 1.6), H), 255, -1)
        yy, xx = np.mgrid[0:H, 0:W]
        v = {"v": xx, "h": yy, "d": xx + yy}[stripes]
        on = (v // 12) % 2 == 0
        img[(body > 0) & on] = (235, 235, 235)
        img[(body > 0) & ~on] = (40, 40, 130)
    return img


def chat(img, cap=0.015):
    x0 = int(W * 0.74)
    img[:, x0:] = (24, 24, 28)
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    f = ImageFont.truetype(FONTS["poppins"], int(cap * H / 0.72))
    for k, y in enumerate(range(10, H - 40, int(cap * H / 0.72 * 1.5))):
        d.text((x0 + 10, y), ["moonboi", "kingK", "trader99"][k % 3] + ": lets go gold to the moon", font=f,
               fill=(230, 230, 230))
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def burn(img, text, font, cap, x, y, style="outline"):
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    probe = ImageFont.truetype(FONTS[font], 200).getbbox("H")
    size = int(round(cap * H * 200 / (probe[3] - probe[1])))
    f = ImageFont.truetype(FONTS[font], size)
    d = ImageDraw.Draw(pil, "RGBA")
    bb = f.getbbox(text)
    if style == "band":
        d.rectangle([x * W - 10, y * H + bb[1] - 8, x * W + bb[2] + 10, y * H + bb[3] + 8], fill=(0, 0, 0, 170))
        d.text((x * W, y * H), text, font=f, fill=(255, 255, 255))
    else:
        d.text((x * W, y * H), text, font=f, fill=(255, 212, 0) if style == "yellow" else (255, 255, 255),
               stroke_width=max(2, size // 12), stroke_fill=(0, 0, 0))
    img[:] = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    return ((x * W + bb[0]) / W, (y * H + bb[1]) / H, (x * W + bb[2]) / W, (y * H + bb[3]) / H)


def jpeg(img):
    return cv2.imdecode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])[1], cv2.IMREAD_COLOR)


def found_whole(boxes, true):
    return any(b[0] <= true[0] + 0.004 and b[2] >= true[2] - 0.004 and b[1] <= true[1] + 0.006 and b[3] >= true[3] - 0.006
               for b in boxes)


positives = [(room, "HOW I MADE $10K TRADING", "anton", 0.035, 0.05, 0.06, "outline"),
             (noise, "Day 47 of trading live", "poppins", 0.04, 0.30, 0.80, "band"),
             (books, "NFP REACTION", "anton", 0.06, 0.55, 0.10, "yellow"),
             (room, "the market is rigged", "poppins", 0.028, 0.62, 0.45, "outline"),
             (books, "why 90% fail", "poppins", 0.05, 0.08, 0.70, "band"),
             (noise, "GOLD", "anton", 0.07, 0.42, 0.12, "outline")]
hit = 0
for bg, text, font, cap, x, y, style in positives:
    img = bg()
    if bg is room:
        img = person(img)
    true = burn(img, text, font, cap, x, y, style)
    boxes = textdetect.detect(jpeg(img))
    got = found_whole(boxes, true)
    hit += got
    if not got:
        print(f"   missed: “{text}” ({font}, {cap:.1%} letters, {style}, on {bg.__name__}): true "
              f"{[round(v, 3) for v in true]}, found {[[round(v, 3) for v in b[:4]] for b in boxes]}")
expect(hit == len(positives), f"big titles found whole: {hit} of {len(positives)} (Anton and Poppins, busy backgrounds)")
negatives = {"two faces": lambda: person(person(room())), "striped shirt": lambda: person(room(), "v"),
             "diagonal stripes": lambda: person(room(), "d"), "bookshelf": books,
             "bookshelf and a face": lambda: person(books()), "busy texture": noise,
             "chat column": lambda: chat(room()), "small captions": lambda: (lambda im: (burn(im, "this is small", "poppins",
                                                                                               0.015, 0.4, 0.85), im)[1])(room())}
for name, make_img in negatives.items():
    got = textdetect.detect(jpeg(make_img()))
    expect(not got, f"nothing found on {name}" + (f" (found {len(got)})" if got else ""))

# --- 3. no text: framing unchanged ---------------------------------------------------------
print("== 3. no text: the framing is what it always was")
plain = make("plain", "--no-clock")
out = clip(plain, "plain")
tl, an = out["timeline"], out["analysis"]
before = motion.plan_camera(tl, an, (W, H), {"layout": "auto", "motion": True}, "fill", motion._energy(plain, tl))
expect(out["plan"].layout == "fill" and out["plan"].kind in ("track", "static"), f"fill layout ({out['plan'].kind})")
expect(not out["plan"].text, "no burned-in text found")
expect(np.allclose(out["camera"].cx, before.cx) and np.allclose(out["camera"].zoom, before.zoom),
       "the camera path is exactly the one planned without the text check")
expect("cut" not in out["plan"].note and "whole picture" not in out["plan"].note, "no text note")
expect(framing.plan_framing(plain, 0, 8).kind != "facecam", "a talking head is not taken for a facecam")
grab(out["file"], 3.0, "f3_plain")

# --- 1. a title near the face ---------------------------------------------------------------
print("== 1. a big title beside the face: whole inside, or wholly out, on every frame")
for name, text, pos, want in (("gold", "GOLD", "0.42,0.10", "in"), ("corner", "MY TRADING PLAN", "0.70,0.08", "out")):
    src = make(name, "--title", text, "--title-pos", pos, "--no-clock", "--still")
    box = true_box(text, pos)
    out = clip(src, name)
    cut, inside, outside = never_cut(out, box)
    print(f"   {name}: {out['plan'].layout}, note: {out['plan'].note}")
    expect(out["plan"].layout == "fill", f"{name}: still a full-screen crop")
    expect(cut == 0, f"{name}: the title is never cut ({inside} frames whole inside, {outside} wholly outside, {cut} cut)")
    if want == "in":
        expect(inside == len(out["camera"].cx), f"{name}: kept whole in the frame (it fits beside the face)")
        expect("isn't cut off" in out["plan"].note, f"{name}: a plain note says the frame moved for the title")
    else:
        expect(outside == len(out["camera"].cx), f"{name}: left wholly out of the frame (too wide to fit with the face)")
        expect("out of the frame" in out["plan"].note, f"{name}: a plain note says the title was left out")
    share, seen = face_framed(out)
    expect(share >= 0.9 and seen > 10, f"{name}: the face stays wholly in the frame ({share:.0%} of {seen} checks)")
    grab(out["file"], 0.6, f"f1_{name}_hook")
    grab(out["file"], 4.0, f"f1_{name}")

# --- 2. a title across the whole top ---------------------------------------------------------
print("== 2. a title across the whole top")
text, pos = "HOW I TURNED $500 INTO $10,000 TRADING GOLD", "full"
full = make("full", "--title", text, "--title-pos", pos, "--no-clock", "--still")
box = true_box(text, pos)
out = clip(full, "full")
cut, inside, outside = never_cut(out, box) if out["plan"].layout == "fill" else (0, 0, 0)
print(f"   {out['plan'].layout}: {out['plan'].note}")
expect(cut == 0 and (out["plan"].layout == "blur" or outside == len(out["camera"].cx)),
       "with the camera free: cropped wholly out (or the whole picture), never cut")
expect("title across the top" in out["plan"].note, "the note names the title across the top")
grab(out["file"], 4.0, "f2_full_cropped")
out = clip(full, "full_still", motion=False)
print(f"   camera moves off: {out['plan'].layout}: {out['plan'].note}")
expect(out["plan"].layout == "blur", "camera moves off (no tighter crop allowed): the whole picture is shown")
expect("Showed the whole picture because of the title across the top" in out["plan"].note, "...with a plain note")
cam, k = out["camera"], OW / W
x0o = OW / 2 + cam.zoom * (box[0] * k - OW / 2) + cam.sx
x1o = OW / 2 + cam.zoom * (box[2] * k - OW / 2) + cam.sx
expect(bool((x0o >= 0).all() and (x1o <= OW).all()), "no punch-in pushes its words off the frame's edges")
grab(out["file"], 4.0, "f2_full_whole")

# --- 4. a stream with a small facecam ---------------------------------------------------------
print("== 4. a stream with a small facecam in the corner: the split")
stream = make("stream", "--stream", "--no-clock")
out = clip(stream, "stream", layout="fill")
print(f"   {out['plan'].layout}: {out['plan'].note}")
expect(out["plan"].layout == "split" and out["plan"].kind == "facecam", "split layout, remembered as a facecam")
top_h = out["base"]["top_h"]
expect(top_h >= 0.35 * OH, f"the facecam half is {top_h / OH:.0%} of the height")
frame = cv2.imread(str(grab(out["file"], 4.0, "f4_stream")))
detect, _ = framing._detector()
faces = [f for f in detect(frame) if f[3] > 40]
big = max(faces, key=lambda f: f[3]) if faces else None
expect(big is not None and big[1] + big[3] / 2 < top_h, "the face is in the top half")
expect(big is not None and big[3] >= 0.15 * OH,
       f"the face is big: {0 if big is None else big[3] / OH:.0%} of the frame height (a tiny 4% in a blurred frame before)")
plan = framing.FramingPlan(kind=out["plan"].kind, facecam=out["plan"].facecam)
expect(render.resolve_layout({"layout": "auto"}, plan) == "split", "a re-render on Auto keeps the split")
grab(out["file"], 0.5, "f4_stream_hook")
stream_t = make("stream_title", "--stream", "--no-clock", "--title", "NFP DAY LIVE TRADING GOLD", "--title-pos", "full")
box = true_box("NFP DAY LIVE TRADING GOLD", "full")
out = clip(stream_t, "stream_title", layout="fill")
base = out["base"]
print(f"   with a title on the game: {out['plan'].layout}, content {base.get('content')}: {out['plan'].note}")
if base.get("content") == "whole":
    ok = True
else:
    x0, y0, w, h = motion._window(base["bottom"], base["bot_h"])
    inside = x0 <= box[0] and x0 + w >= box[2] and y0 <= box[1] and y0 + h >= box[3]
    outside = x0 + w <= box[0] or x0 >= box[2] or y0 + h <= box[1] or y0 >= box[3]
    ok = inside or outside
expect(out["plan"].layout == "split" and ok, "the content half never cuts the game's title")
grab(out["file"], 4.0, "f4_stream_title")

# --- one Edit Maker moment -------------------------------------------------------------------
print("== an Edit Maker moment with a title beside the face")
song = TMP / "song.mp3"
subprocess.run([sys.executable, str(ROOT / "tools" / "make_test_song.py"), "128", "20", "4.365", "0.365", str(song)],
               check=True)
snd = {"id": "s", "file": str(song), "analysis": beats.analyze(song)}
corner = TMP / "corner.mp4"
ms = [{"id": f"m{i}", "source": "T", "start": 0.5 + i * 2.4, "end": 2.5 + i * 2.4, "hit": 1.5 + i * 2.4, "text": "",
       "drop": i == 1} for i in range(3)]
tl = edits.build_timeline(ms, "cinematic", 6, snd, {}, {}, durations={"T": 8.0}, hook="")
made = editrender.render(tl, {"T": {"source_path": str(corner)}}, snd, TMP / "edit.mp4", TMP / "edit.jpg")
box = true_box("MY TRADING PLAN", "0.70,0.08")
fm = list(made["framing"].values())
print(f"   notes: {made['notes']}")
ok = True
for f in fm:
    if f["whole"]:
        continue
    for z in (f["zmin"], min(f["zmax"], 1.3)):
        s = max(OW / W, OH / H) * z
        hw, hh = OW / (2 * s), OH / (2 * s)
        cx = min(max(f["cx"], hw), W - hw)
        inside = cx - hw <= box[0] and cx + hw >= box[2] and f["cy"] - hh <= box[1] and f["cy"] + hh >= box[3]
        outside = cx + hw <= box[0] or cx - hw >= box[2] or f["cy"] + hh <= box[1] or f["cy"] - hh >= box[3]
        ok = ok and (inside or outside)
expect(ok and made["notes"], "every moment's window keeps the title whole or out, at every zoom it may use, with a note")
grab(TMP / "edit.mp4", 2.0, "f_edit")

# --- the ranking nudge ------------------------------------------------------------------------
print("== the moment finder: a light nudge, never more")
risk, why = textdetect.text_risk(full, 1.0, 7.0)
expect(risk > 0.3 and "across the top" in why, f"a full-width title is a risk ({risk}: {why})")
expect(textdetect.text_risk(plain, 1.0, 7.0)[0] == 0, "no text, no risk")
cands = [{"start": 0, "end": 30, "score": 80, "title": "a"}, {"start": 40, "end": 70, "score": 78, "title": "b"}]
ranked = highlights.rank(cands, [], 100, 2, 10, 60, text_risk=lambda a, b: (1.0, "a title") if a < 1 else (0.0, ""))
expect([c["title"] for c in ranked] == ["b", "a"] and ranked[1]["score"] == 72, "the risky moment loses 8 points")

print(f"\nframes to look at: {TMP}/f*.jpg")
print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed")
sys.exit(1 if FAILS else 0)
