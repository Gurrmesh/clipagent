"""Make a test "talking head" video: a drawn person who moves, gestures and talks.

usage: python tools/make_test_footage.py SECONDS out.mp4 [--warm|--cold] [--portrait] [--seed N]
           [--title "TEXT" [--title-pos top|full|left|right|bottom|X,Y]] [--stream [--cam-corner br|bl|tr|tl]]
           [--still] [--no-clock]

Nothing is downloaded: the picture is drawn here with OpenCV (a face the
YuNet face finder recognises, a body, hands that gesture, a room behind), the
"voice" is a buzzy tone that comes and goes in sentence-length bursts, and a
small clock in the corner shows the source time of every frame. --warm and
--cold give the whole picture a camera colour cast, so colour matching between
two videos can be checked.

--title burns a big title into every frame, the way creators put their own
text on their videos (Anton, white with a black outline, from fonts/): near the
top-left (top), across the whole top (full), at the left or right edge, a
lower third (bottom), or with its top-left corner at X,Y (fractions of the frame). --stream draws a game-like scene that keeps moving, with
the person in a small facecam in one corner instead (--cam-corner, default
bottom right). --still keeps the person from swaying (a held shot). --no-clock
leaves out the clock (it is big text too).
"""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import math
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np


def person(img, cx, cy, r, mouth_open, lean, hand_up):
    h, w = img.shape[:2]
    body_w = int(r * 1.6)
    cv2.ellipse(img, (cx + lean, cy + int(r * 2.4)), (body_w, int(r * 1.7)), 0, 180, 360, (60, 50, 42), -1)
    cv2.rectangle(img, (cx + lean - body_w, cy + int(r * 2.4)), (cx + lean + body_w, h), (60, 50, 42), -1)
    cv2.rectangle(img, (cx - int(r * 0.25), cy + int(r * 0.8)), (cx + int(r * 0.25), cy + int(r * 1.25)),
                  (120, 150, 195), -1)                                                          # neck
    cv2.ellipse(img, (cx, cy - int(r * 0.55)), (int(r * 0.95), int(r * 0.7)), 0, 180, 360, (30, 40, 60), -1)
    cv2.ellipse(img, (cx, cy), (int(r * 0.78), r), 0, 0, 360, (140, 170, 215), -1)
    for dx in (-0.32, 0.32):
        ex, ey = cx + int(dx * r), cy - int(0.18 * r)
        cv2.ellipse(img, (ex, ey), (int(0.16 * r), int(0.08 * r)), 0, 0, 360, (255, 255, 255), -1)
        cv2.circle(img, (ex, ey), int(0.06 * r), (40, 30, 20), -1)
        cv2.line(img, (ex - int(0.17 * r), ey - int(0.16 * r)), (ex + int(0.15 * r), ey - int(0.19 * r)),
                 (40, 40, 60), max(2, int(r * 0.04)))
    cv2.ellipse(img, (cx, cy + int(0.12 * r)), (int(0.07 * r), int(0.12 * r)), 0, 0, 360, (110, 140, 190), -1)
    cv2.ellipse(img, (cx, cy + int(0.45 * r)), (int(0.25 * r), int(0.04 * r + 0.10 * r * mouth_open)), 0, 0, 360,
                (50, 50, 140), -1)
    # a hand that comes up when he makes a point
    hx = cx + lean + int(r * 1.9)
    hy = int(cy + r * 3.2 - hand_up * r * 2.2)
    cv2.line(img, (cx + lean + int(r * 1.2), cy + int(r * 3.0)), (hx, hy), (60, 50, 42), int(r * 0.45))
    cv2.circle(img, (hx, hy), int(r * 0.3), (140, 170, 215), -1)


FONTS = Path(__file__).resolve().parents[1] / "fonts"


def title_layer(W, H, text, pos):
    """The burned-in title as (colour image, alpha 0..1), drawn once with Pillow."""
    from PIL import Image, ImageDraw, ImageFont
    cap = {"full": 0.075, "top": 0.06, "bottom": 0.055}.get(pos, 0.06)
    probe = ImageFont.truetype(str(FONTS / "Anton-Regular.ttf"), 200)
    b = probe.getbbox("H")
    size = int(round(cap * H * 200 / (b[3] - b[1])))
    font = ImageFont.truetype(str(FONTS / "Anton-Regular.ttf"), size)
    if pos == "full":                                  # as wide as the frame allows
        while font.getlength(text) < 0.9 * W and size < 400:
            size += 4
            font = ImageFont.truetype(str(FONTS / "Anton-Regular.ttf"), size)
    tw = font.getlength(text)
    if "," in pos:                                     # "x,y": where its left/top edge goes, as fractions
        fx, fy = (float(v) for v in pos.split(","))
        x, y = fx * W, fy * H
    else:
        x, y = {"top": (0.035 * W, 0.05 * H), "full": ((W - tw) / 2, 0.04 * H),
                "left": (0.03 * W, 0.42 * H), "right": (0.97 * W - tw, 0.42 * H),
                "bottom": (0.05 * W, 0.80 * H)}.get(pos, (0.035 * W, 0.05 * H))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((x, y), text, font=font, fill=(255, 255, 255, 255),
                             stroke_width=max(3, size // 14), stroke_fill=(0, 0, 0, 255))
    rgba = np.array(img)
    colour = cv2.cvtColor(rgba[..., :3], cv2.COLOR_RGB2BGR).astype(np.float32)
    alpha = (rgba[..., 3:4].astype(np.float32) / 255.0)
    return colour, alpha


def game_frame(W, H, t, rng_shapes):
    """A game-like scene that never holds still: a scrolling sky and ground, moving shapes, a crosshair."""
    yy = np.linspace(0, 1, H, dtype=np.float32)[:, None]
    xx = np.linspace(0, 1, W, dtype=np.float32)[None, :]
    wave = 0.5 + 0.5 * np.sin(2 * np.pi * (xx * 3 + t * 0.4))
    img = np.empty((H, W, 3), np.uint8)
    img[..., 0] = np.clip(170 - 90 * yy + 25 * wave, 0, 255)
    img[..., 1] = np.clip(120 + 40 * yy * wave, 0, 255)
    img[..., 2] = np.clip(60 + 120 * yy, 0, 255)
    for k, (px, py, r, vx, col) in enumerate(rng_shapes):
        cx = int((px + vx * t) % 1.2 * W - 0.1 * W)
        cy = int(py * H + 30 * np.sin(t * 2 + k))
        pts = np.array([[cx, cy - r], [cx + r, cy + r], [cx - r, cy + r]], np.int32)
        cv2.fillPoly(img, [pts], col)
    cv2.line(img, (W // 2 - 30, H // 2), (W // 2 + 30, H // 2), (255, 255, 255), 3)
    cv2.line(img, (W // 2, H // 2 - 30), (W // 2, H // 2 + 30), (255, 255, 255), 3)
    return img


def main():
    seconds = float(sys.argv[1])
    out = sys.argv[2]
    tint = "warm" if "--warm" in sys.argv else ("cold" if "--cold" in sys.argv else "")
    portrait = "--portrait" in sys.argv
    seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 1
    title = sys.argv[sys.argv.index("--title") + 1] if "--title" in sys.argv else ""
    title_pos = sys.argv[sys.argv.index("--title-pos") + 1] if "--title-pos" in sys.argv else "top"
    stream = "--stream" in sys.argv
    corner = sys.argv[sys.argv.index("--cam-corner") + 1] if "--cam-corner" in sys.argv else "br"
    still = "--still" in sys.argv
    clock = "--no-clock" not in sys.argv
    rng = np.random.default_rng(seed)
    W, H = (1080, 1920) if portrait else (1920, 1080)
    fps = 30
    n = int(seconds * fps)
    # the room: a wall with a soft light, shelves, a lamp
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    glow = np.exp(-(((xx - W * 0.3) / (W * 0.5)) ** 2 + ((yy - H * 0.25) / (H * 0.6)) ** 2))
    base = np.stack([60 + 70 * glow, 70 + 80 * glow, 85 + 95 * glow], axis=-1)
    room = np.clip(base, 0, 255).astype(np.uint8)
    for k in range(6):
        x0 = int(rng.integers(0, W - 300))
        y0 = int(rng.integers(0, H // 2))
        col = tuple(int(c) for c in rng.integers(40, 200, 3))
        cv2.rectangle(room, (x0, y0), (x0 + int(rng.integers(120, 300)), y0 + 24), col, -1)
    cv2.circle(room, (int(W * 0.12), int(H * 0.3)), 60, (180, 230, 255), -1)
    room = cv2.GaussianBlur(room, (0, 0), 6)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(fps),
           "-i", "-", "-f", "lavfi", "-t", str(seconds), "-i",
           "aevalsrc='0.25*sin(2*PI*(150+30*sin(2*PI*3*t))*t)*(0.6+0.4*sin(2*PI*7*t))"
           "*gt(mod(t\\,2.5)\\,0.3)*lt(mod(t\\,2.5)\\,2.1)':s=48000",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-g", "60", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "96k", "-shortest", out]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    r = int(H * 0.11) if not portrait else int(W * 0.17)
    cast = {"warm": (0.78, 0.95, 1.22, 0), "cold": (1.22, 1.0, 0.8, 0)}.get(tint)
    layer = title_layer(W, H, title, title_pos) if title else None
    shapes = [(float(rng.uniform(0, 1)), float(rng.uniform(0.45, 0.95)), int(rng.integers(20, 90)),
               float(rng.uniform(-0.15, 0.15)), tuple(int(c) for c in rng.integers(20, 230, 3)))
              for _ in range(14)]
    # the facecam: a webcam picture of the person, a quarter of the frame high, in one corner
    cam_h = int(H * 0.25)
    cam_w = int(cam_h * 4 / 3)
    cam_x = W - cam_w - int(0.02 * W) if corner in ("br", "tr") else int(0.02 * W)
    cam_y = H - cam_h - int(0.03 * H) if corner in ("br", "bl") else int(0.03 * H)
    cam_room = cv2.resize(room, (cam_w, cam_h), interpolation=cv2.INTER_AREA) if stream else None
    for i in range(n):
        t = i / fps
        talking = 0.3 < t % 2.5 < 2.1
        mouth = (0.5 + 0.5 * math.sin(t * 2 * math.pi * 6)) if talking else 0.0
        hand = max(0.0, math.sin(t * 2 * math.pi / 5.0)) ** 2
        if stream:
            img = game_frame(W, H, t, shapes)
            cam = cam_room.copy()
            cr = int(cam_h * 0.16)
            person(cam, cam_w // 2 + int(math.sin(t * 0.8) * cam_w * 0.02), int(cam_h * 0.42), cr, mouth, 0, hand)
            cv2.rectangle(cam, (0, 0), (cam_w - 1, cam_h - 1), (255, 255, 255), 4)
            img[cam_y:cam_y + cam_h, cam_x:cam_x + cam_w] = cam
            cx = W // 2
        else:
            img = room.copy()
            sway = 0.0 if still else math.sin(t * 0.7) * W * 0.06 + math.sin(t * 2.3) * W * 0.01
            cx = int(W * (0.62 if not portrait else 0.5) + sway)
            cy = int(H * (0.36 if not portrait else 0.32) + (0.0 if still else math.sin(t * 1.1) * H * 0.015))
            person(img, cx, cy, r, mouth, int(math.sin(t * 0.9) * r * 0.2), hand)
        if layer is not None:                      # the creator's own title, burned in
            colour, alpha = layer
            img = (img.astype(np.float32) * (1 - alpha) + colour * alpha).astype(np.uint8)
        if cast is not None:                       # the camera's white balance tints everything
            img = cv2.multiply(img, cast, dtype=cv2.CV_8U)
        if clock:
            cv2.putText(img, f"{t:6.2f}", (cx - 170, H - 40), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255), 5)
        enc.stdin.write(img.tobytes())
    enc.stdin.close()
    enc.wait()


if __name__ == "__main__":
    main()
