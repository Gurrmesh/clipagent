"""Make a test "talking head" video: a drawn person who moves, gestures and talks.

usage: python tools/make_test_footage.py SECONDS out.mp4 [--warm|--cold] [--portrait] [--seed N]

Nothing is downloaded: the picture is drawn here with OpenCV (a face the
YuNet face finder recognises, a body, hands that gesture, a room behind), the
"voice" is a buzzy tone that comes and goes in sentence-length bursts, and a
small clock in the corner shows the source time of every frame. --warm and
--cold give the whole picture a camera colour cast, so colour matching between
two videos can be checked.
"""
import math
import subprocess
import sys

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


def main():
    seconds = float(sys.argv[1])
    out = sys.argv[2]
    tint = "warm" if "--warm" in sys.argv else ("cold" if "--cold" in sys.argv else "")
    portrait = "--portrait" in sys.argv
    seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 1
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
    for i in range(n):
        t = i / fps
        img = room.copy()
        talking = 0.3 < t % 2.5 < 2.1
        mouth = (0.5 + 0.5 * math.sin(t * 2 * math.pi * 6)) if talking else 0.0
        sway = math.sin(t * 0.7) * W * 0.06 + math.sin(t * 2.3) * W * 0.01
        cx = int(W * (0.62 if not portrait else 0.5) + sway)
        cy = int(H * (0.36 if not portrait else 0.32) + math.sin(t * 1.1) * H * 0.015)
        hand = max(0.0, math.sin(t * 2 * math.pi / 5.0)) ** 2
        person(img, cx, cy, r, mouth, int(math.sin(t * 0.9) * r * 0.2), hand)
        if cast is not None:                       # the camera's white balance tints everything
            img = cv2.multiply(img, cast, dtype=cv2.CV_8U)
        cv2.putText(img, f"{t:6.2f}", (cx - 170, H - 40), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255), 5)
        enc.stdin.write(img.tobytes())
    enc.stdin.close()
    enc.wait()


if __name__ == "__main__":
    main()
