"""Overlay-only renders for clip-bank campaigns: the footage itself is not touched.

A clip-bank campaign hands you finished edits and forbids trimming, cropping,
zooming or touching the audio. So this path never goes near the motion
engine. Every output frame is the source frame — scaled whole into a 9:16
canvas when it isn't vertical already, never cropped — with the hook drawn
on top, and the audio is copied across packet for packet.

It also measures what it made, for compliance.py: frame counts, an audio
fingerprint, and how far the footage outside the hook drifted from the source
(it shouldn't, beyond what re-encoding costs).
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .captions import hex_to_ass
from .config import BASE_DIR, CLIP_DIR, THUMB_DIR, WORK_DIR

FONTS_DIR = BASE_DIR / "fonts"
CANVAS = (1080, 1920)

NEON = {"pink": "#FF2D95", "cyan": "#00E5FF", "green": "#39FF14", "yellow": "#FFE600",
        "purple": "#B06BFF", "orange": "#FF8A00"}
HOOK_STYLES = {"bold": "Bold", "boxed": "Boxed", "neon": "Neon glow"}
HOOK_POSITIONS = {"auto": "Auto — clear of text and faces", "top": "Top", "center": "Center",
                  "bottom": "Bottom"}
DEFAULT_LOOK = {"hook_style": "neon", "hook_color": "pink", "hook_position": "auto",
                "hook_hold": "whole", "bars": "black"}

# Where each position puts the hook, as a share of the frame's height. The
# hook itself is drawn by hook_ass(); these bands are what it covers.
BANDS = {"top": (0.10, 0.26), "center": (0.42, 0.58), "bottom": (0.62, 0.78)}   # inside the apps' safe zone
# Other things equal, top is where a hook belongs; the bottom sits under
# TikTok's own caption overlay, the centre on the subject. A hook over a
# forehead is normal on TikTok; one over somebody else's words never is —
# so text costs far more than a face.
BAND_PREFERENCE = {"top": 0.0, "bottom": 0.15, "center": 0.3}
TEXT_HIT_COST, TEXT_AREA_COST, FACE_COST = 2.0, 4.0, 0.5

# Audio an MP4 can carry that every platform plays; anything else is re-encoded.
POSTABLE_AUDIO = {"aac", "mp3"}

HOOK_EM = 0.065                 # the hook's letter size (em) as a share of the frame width:
                                # capitals ~4.6% of the width, ~50px on a 1080-wide frame
POPPINS_LINE_HEIGHT = 1.762     # (usWinAscent + usWinDescent) / em for Poppins Bold


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        raise RuntimeError(f"{cmd[0]} failed:\n{tail}")
    return proc


def _escape(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


# --- what the source is ----------------------------------------------------------

def inspect(path: Path) -> Dict[str, Any]:
    """Size, shape and timing of a clip-bank file, from ffprobe."""
    out = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                "-show_streams", str(path)]).stdout
    info = json.loads(out)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        raise RuntimeError("That file has no video in it.")
    w, h = int(v["width"]), int(v["height"])
    sar = v.get("sample_aspect_ratio") or "1:1"
    try:
        num, den = (int(x) for x in sar.split(":"))
        sar_f = num / den if num and den else 1.0
    except ValueError:
        sar_f = 1.0
    rot = 0
    for side in v.get("side_data_list") or []:
        if "rotation" in side:
            rot = int(abs(float(side["rotation"]))) % 180
    if (v.get("tags") or {}).get("rotate"):
        rot = int(abs(float(v["tags"]["rotate"]))) % 180
    if rot == 90:                              # ffmpeg turns it upright on decode
        w, h = h, w
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1"
    try:
        n, d = (float(x) for x in rate.split("/"))
        fps = n / d if d else 30.0
    except ValueError:
        fps = 30.0
    return {
        "width": w, "height": h, "sar": sar_f, "fps": fps or 30.0,
        "duration": float(info.get("format", {}).get("duration") or v.get("duration") or 0),
        "has_audio": a is not None, "audio_codec": (a or {}).get("codec_name", ""),
        "video_codec": v.get("codec_name", ""),
    }


def layout_for(info: Dict[str, Any], bars: str = "black") -> Dict[str, Any]:
    """Where the footage sits in the output. A vertical clip keeps its own size;
    anything else is scaled whole into a 1080x1920 frame — fitted, never cropped."""
    w, h = info["width"], info["height"]
    display_w = w * info.get("sar", 1.0)
    ratio = display_w / h
    if abs(ratio - 9 / 16) <= 0.02 * (9 / 16) and abs(info.get("sar", 1.0) - 1.0) < 1e-3:
        cw, ch = w + w % 2, h + h % 2                     # pad an odd edge by one pixel, never trim it
        return {"vertical": True, "canvas": [cw, ch], "content": [0, 0, w, h], "bars": "none",
                "scaled": False}
    W, H = CANVAS
    scale = min(W / display_w, H / h)
    fw = max(2, int(round(display_w * scale / 2)) * 2)
    fh = max(2, int(round(h * scale / 2)) * 2)
    x, y = (W - fw) // 2, (H - fh) // 2
    return {"vertical": False, "canvas": [W, H], "content": [x, y, fw, fh],
            "bars": bars if bars in ("black", "blur") else "black", "scaled": True}


# --- where the hook goes ------------------------------------------------------------

HEAD_SECONDS = 3.0      # the opening, where a viewer decides — sampled densely


def _canvas_samples(source: Path, info: Dict[str, Any], layout: Dict[str, Any],
                    count: int = 10, width: int = 360) -> List[np.ndarray]:
    """Small BGR frames from the clip, laid out exactly as the output frame
    will be (bars included), so their rows line up with the hook bands.

    The first three seconds get two frames a second on top of the even spread:
    a brand's own title often shows only at the start, and two hooks fighting
    in the opening seconds is the worst place for them to meet."""
    W, H = layout["canvas"]
    x, y, fw, fh = layout["content"]
    s = width / W
    ch = max(2, int(round(H * s / 2)) * 2)
    if layout["vertical"]:
        fit = f"scale={width}:{ch},format=bgr24"
    else:
        sw, sh = max(2, int(round(fw * s / 2)) * 2), max(2, int(round(fh * s / 2)) * 2)
        fit = (f"scale={sw}:{sh},pad={width}:{ch}:{int(round(x * s))}:"
               f"{int(round(y * s))}:black,format=bgr24")
    duration = max(0.5, info["duration"])
    head = min(HEAD_SECONDS, duration / 3)
    size = width * ch * 3
    frames: List[np.ndarray] = []
    for args, chain, limit in (
            (["-ss", "0.4", "-t", f"{head:.2f}"], f"fps=2,{fit}", 6),
            ([], f"fps={count / duration:.5f},{fit}", count)):
        proc = subprocess.run(["ffmpeg", "-v", "error", *args, "-i", str(source), "-vf", chain,
                               "-frames:v", str(limit), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                              capture_output=True)
        data = proc.stdout or b""
        frames += [np.frombuffer(data[i * size:(i + 1) * size], np.uint8).reshape(ch, width, 3)
                   for i in range(len(data) // size)]
    return frames


def text_mask(frame: np.ndarray) -> np.ndarray:
    """Where a frame carries burned-in text — captions, titles, labels.

    Edited clips put their words on screen the same way: bright letters
    (white, yellow) with a dark outline or a dark box behind them, in
    horizontal lines. So: strong edges, merged sideways into line-shaped
    blobs, each holding both very bright and very dark pixels."""
    import cv2
    H, W = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    k = np.ones((3, 3), np.uint8)
    # A text edge: a light pixel right beside a dark one (an outline or a box).
    # Busy scenery — mesh, foliage, windows — has plenty of edges but rarely
    # that much contrast in three pixels, so it doesn't swamp the letters.
    # Both sides are judged on perceived brightness: yellow letters (~200)
    # count as light, while pure blue — bright in one channel, dark to the
    # eye — counts only as dark and can't pass for text on its own.
    edges = ((cv2.dilate(gray, k) >= 170) & (cv2.erode(gray, k) <= 55)).astype(np.uint8) * 255
    # Letters are short strokes. A long straight edge — a door frame, a
    # building, a horizon — isn't text, and left in it would join the words
    # beside it into one huge blob that the size checks then throw away.
    tall = cv2.morphologyEx(edges, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, H // 8))))
    wide = cv2.morphologyEx(edges, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, W // 2), 1)))
    edges = cv2.bitwise_and(edges, cv2.bitwise_not(cv2.bitwise_or(tall, wide)))
    lines = cv2.morphologyEx(edges, cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, W // 22), 5)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(lines)
    mask = np.zeros((H, W), bool)
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if not (0.012 * H <= h <= 0.2 * H) or w < 0.08 * W or w < 1.5 * h:
            continue
        if (edges[y:y + h, x:x + w] > 0).mean() < 0.12 or (gray[y:y + h, x:x + w] >= 170).mean() < 0.06:
            continue
        mask[y:y + h, x:x + w] = True
    return mask


def choose_position(source: Path, info: Dict[str, Any], layout: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """The clearest place for the hook: (position, why, the scores behind it).

    A landscape clip fitted with a tall bar above it takes the hook in the bar
    — nothing of the footage is covered at all. Otherwise each band is scored
    on how often the clip already has text there, and faces, and the cheapest
    wins, with top preferred when they're level."""
    W, H = layout["canvas"]
    if not layout["vertical"] and layout["content"][1] >= H * 0.12:
        return "top", "in the bar above the footage, so it covers none of it", {}
    frames = _canvas_samples(source, info, layout)
    if not frames:
        return "top", "couldn't look inside the clip, so the usual spot", {}
    try:
        from .framing import _detector
        detect, _ = _detector()
    except Exception:
        detect = None
    h, w = frames[0].shape[:2]
    scores: Dict[str, Dict[str, float]] = {b: {"text": 0.0, "text_hits": 0.0, "face": 0.0} for b in BANDS}
    for frame in frames:
        mask = text_mask(frame)
        faces = []
        if detect is not None:
            try:
                faces = [f for f in detect(frame) if f[2] >= 0.06 * w]
            except Exception:
                faces = []
        for band, (a, b) in BANDS.items():
            r0, r1 = int(a * h), int(b * h)
            cover = float(mask[r0:r1].mean()) if r1 > r0 else 0.0
            scores[band]["text"] += cover
            scores[band]["text_hits"] += 1.0 if cover > 0.01 else 0.0
            face_rows = 0.0
            for fx, fy, fw_, fh_ in faces:                        # a face with some headroom around it
                top, bottom = fy - 0.15 * fh_, fy + 1.15 * fh_
                face_rows = max(face_rows, max(0.0, min(bottom, r1) - max(top, r0)) / max(1, r1 - r0))
            scores[band]["face"] += face_rows
    for band in scores:
        for k in scores[band]:
            scores[band][k] = round(scores[band][k] / len(frames), 3)
        s = scores[band]
        s["cost"] = round(TEXT_HIT_COST * s["text_hits"] + TEXT_AREA_COST * s["text"]
                          + FACE_COST * s["face"] + BAND_PREFERENCE[band], 3)
    best = min(scores, key=lambda b: scores[b]["cost"])
    top = scores["top"]
    if best == "top":
        why = "the top is clear"
    elif top["text_hits"] >= 0.3:
        why = "the clip already has text at the top"
    elif top["face"] >= 0.3:
        why = "a face sits where the top hook would go"
    else:
        why = "it's the clearest part of the frame"
    return best, why, scores


def sample_frames(source: Path, info: Dict[str, Any], count: int = 6, width: int = 480) -> List[bytes]:
    """JPEG stills spread across a clip, for Claude to look at."""
    dur = max(0.1, float(info.get("duration") or 0))
    out: List[bytes] = []
    for i in range(count):
        t = dur * (i + 0.5) / count
        proc = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(source), "-frames:v", "1",
                               "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                              capture_output=True)
        if proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8":
            out.append(proc.stdout)
    return out


# --- the hook --------------------------------------------------------------------

def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def hook_ass(text: str, look: Dict[str, Any], layout: Dict[str, Any], duration: float,
             out_path: Path) -> Path:
    """The hook as a subtitle script, drawn by the same libass the captions use."""
    W, H = layout["canvas"]
    x0, y0, fw, fh = layout["content"]
    style = look.get("hook_style", "neon")
    colour = NEON.get(look.get("hook_color", "pink"), look.get("hook_color", "#FF2D95"))
    if not str(colour).startswith("#"):
        colour = NEON["pink"]
    text = _ass_text((text or "").strip().upper())
    # Big and wrapped beats small on one line: a phone screen is narrow. Only a
    # really long hook gives up some size, so it stays to about three lines.
    # libass reads Fontsize as the font's Windows line height (usWinAscent +
    # usWinDescent), which for Poppins is 1.76x its em — so the number asked
    # for is the em we want times 1.76. Asking for 8% of the width drew
    # capitals barely 3% of the width tall.
    size = W * HOOK_EM * POPPINS_LINE_HEIGHT
    if len(text) > 44:
        size *= max(0.72, 44 / len(text))
    size = int(round(size))
    margin = int(W * 0.08)

    position = look.get("hook_position", "top")
    if position == "center":
        tag = f"\\an5\\pos({W // 2},{H // 2})"
    elif position == "bottom":
        tag = f"\\an2\\pos({W // 2},{int(H * 0.78)})"
    elif not layout["vertical"] and y0 >= H * 0.12:
        tag = f"\\an2\\pos({W // 2},{y0 - int(H * 0.02)})"      # in the bar, just above the footage
    else:
        tag = f"\\an8\\pos({W // 2},{int(H * 0.11)})"

    hold = look.get("hook_hold", "whole")
    end = duration if hold == "whole" else min(duration, float(hold) if str(hold).replace(".", "").isdigit() else 3.0)
    t0, t1 = _ass_time(0), _ass_time(end + 0.05)
    fade = "" if hold == "whole" else "\\fad(0,250)"   # on screen from the first frame

    neon = hex_to_ass(colour)
    white = "&H00FFFFFF"
    black = "&H00000000"
    if style == "boxed":
        styles = f"Style: Hook,Poppins,{size},{white},{white},&H50000000,&H50000000,-1,0,0,0,100,100,1,0,3,18,0,8,{margin},{margin},0,1"
        events = [f"Dialogue: 1,{t0},{t1},Hook,,0,0,0,,{{{tag}{fade}}}{text}"]
    elif style == "bold":
        styles = f"Style: Hook,Poppins,{size},{white},{white},{black},&H80000000,-1,0,0,0,100,100,1,0,1,7,3,8,{margin},{margin},0,1"
        events = [f"Dialogue: 1,{t0},{t1},Hook,,0,0,0,,{{{tag}{fade}}}{text}"]
    else:                                                     # neon: a blurred glow under a crisp core
        styles = f"Style: Hook,Poppins,{size},{white},{white},{neon},&H00000000,-1,0,0,0,100,100,1,0,1,3,0,8,{margin},{margin},0,1"
        events = [
            f"Dialogue: 0,{t0},{t1},Hook,,0,0,0,,{{{tag}{fade}\\1c{neon}\\3c{neon}\\1a&H30&\\3a&H20&\\bord11\\blur14}}{text}",
            f"Dialogue: 1,{t0},{t1},Hook,,0,0,0,,{{{tag}{fade}\\3c{neon}\\bord3\\blur1.2}}{text}",
        ]
    script = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{styles}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + ("\n".join(events) if text else "") + "\n"
    out_path.write_text(script, encoding="utf-8")
    return out_path


# --- the render ------------------------------------------------------------------

def _graph(layout: Dict[str, Any], ass: Optional[Path], logo_at: Optional[Tuple[int, int]] = None) -> str:
    W, H = layout["canvas"]
    x, y, fw, fh = layout["content"]
    subs = f",subtitles='{_escape(ass)}':fontsdir='{_escape(FONTS_DIR)}'" if ass else ""
    out = "[vb]" if logo_at else "[v]"
    if layout["vertical"]:
        g = f"[0:v]pad={W}:{H}:0:0:black,setsar=1{subs},format=yuv420p{out}"
    else:
        fit = f"scale={fw}:{fh}:flags=lanczos,setsar=1"
        if layout["bars"] == "blur":
            g = (f"[0:v]split=2[bga][fga];"
                 f"[bga]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                 f"gblur=sigma=36,eq=brightness=-0.18,setsar=1[bg];"
                 f"[fga]{fit}[fg];[bg][fg]overlay={x}:{y}{subs},format=yuv420p{out}")
        else:
            g = f"[0:v]{fit},pad={W}:{H}:{x}:{y}:black{subs},format=yuv420p{out}"
    if logo_at:
        # The brand's logo goes on last, over everything, for the whole clip.
        g += f";[vb][1:v]overlay={logo_at[0]}:{logo_at[1]}:format=yuv420,format=yuv420p[v]"
    return g


def render(source: Path, clip_id: str, hook: str, look: Dict[str, Any],
           info: Optional[Dict[str, Any]] = None, logo: Optional[Path] = None) -> Dict[str, Any]:
    """One clip-bank file, with its hook (and the brand's logo, when the
    campaign has one), ready to post. Returns the file, its thumbnail, the
    layout used, where the logo went, and whether the audio went across untouched."""
    info = info or inspect(source)
    look = {**DEFAULT_LOOK, **{k: v for k, v in (look or {}).items() if v not in (None, "")}}
    layout = layout_for(info, look.get("bars", "black"))
    position, why = look.get("hook_position", "auto"), ""
    if position == "auto" and (hook or "").strip():
        try:
            position, why, _ = choose_position(source, info, layout)
        except Exception as exc:                     # placement is a nicety; never lose the clip to it
            position, why = "top", f"auto placement failed ({exc}), so the usual spot"[:160]
    ass = None
    if (hook or "").strip():
        ass = hook_ass(hook, {**look, "hook_position": position}, layout, info["duration"],
                       WORK_DIR / f"{clip_id}_hook.ass")
    out = CLIP_DIR / f"{clip_id}.mp4"

    placed = None
    if logo and Path(logo).exists():
        from . import brandlogo
        canvas = tuple(layout["canvas"])
        prep = brandlogo.prepare(Path(logo), canvas, brandlogo.OVERLAY_MAX_W, brandlogo.OVERLAY_MAX_H)
        hb = brandlogo.hook_box(str(ass), canvas, FONTS_DIR) if ass else None
        lx, ly, lwhy = brandlogo.place_overlay(layout, hb, position, prep["w"], prep["h"])
        placed = {"src": str(logo), "png": str(prep["png"]), "box": [lx, ly, prep["w"], prep["h"]],
                  "why": lwhy, "tone": prep["tone"]}

    inputs = ["-i", str(source)] + (["-i", placed["png"]] if placed else [])
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
            "-filter_complex", _graph(layout, ass, tuple(placed["box"][:2]) if placed else None),
            "-map", "[v]"]
    video = ["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
             "-fps_mode", "passthrough", "-movflags", "+faststart"]
    audio_copied = True
    if info["has_audio"]:
        copied = False
        if (info.get("audio_codec") or "").lower() in POSTABLE_AUDIO:
            try:
                _run(base + ["-map", "0:a:0", "-c:a", "copy"] + video + [str(out)])
                copied = True
            except RuntimeError:
                pass
        if not copied:
            # Anything TikTok and Instagram don't take in an MP4 (the PCM a camera
            # .mov carries, vorbis, opus) is re-encoded to AAC at a high rate. The
            # gate then measures that the sound itself came through unchanged.
            audio_copied = False
            _run(base + ["-map", "0:a:0", "-c:a", "aac", "-b:a", "256k", "-ar", "48000"] + video + [str(out)])
    else:
        _run(base + video + [str(out)])

    thumb = THUMB_DIR / f"{clip_id}.jpg"
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
          "-ss", f"{min(1.0, info['duration'] / 3):.2f}", "-i", str(out),
          "-frames:v", "1", "-q:v", "4", str(thumb)])
    return {"file": out, "thumb": thumb, "clean": thumb, "layout": layout,
            "audio_copied": audio_copied, "hook_ass": str(ass) if ass else "", "look": look,
            "position": position, "position_why": why, "logo": placed}


# --- measurements for the gate ---------------------------------------------------

def frame_count(path: Path) -> Optional[int]:
    """Frames a viewer actually sees. Counted by decoding, not by packets: a
    file cut without re-encoding (common in clip banks) carries a few lead-in
    packets its edit list hides, so packets overcount (484 vs 480 seen)."""
    try:
        out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                    "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)]).stdout
        # Newer ffprobe prints the stream twice for a camera .mov (once more under
        # its program): "469\n\n469". The first number is the count.
        first = next(line for line in out.splitlines() if line.strip())
        return int(first.split(",")[0])
    except (RuntimeError, ValueError):
        return None


def audio_fingerprint(path: Path) -> Optional[str]:
    """MD5 of the decoded sound. The same packets decode to the same samples,
    so an untouched track matches its source exactly; any change doesn't."""
    try:
        out = _run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "md5", "-"]).stdout
        return out.strip().split("=", 1)[-1] or None
    except RuntimeError:
        return None


def audio_packets_hash(path: Path) -> Optional[str]:
    """Hash of the compressed audio packets themselves — a second way to show
    the track is untouched, for files whose decoders pad differently."""
    try:
        proc = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
                               "-c", "copy", "-f", "data", "-"], capture_output=True)
        if proc.returncode != 0 or not proc.stdout:
            return None
        return hashlib.md5(proc.stdout).hexdigest()
    except OSError:
        return None


def _pcm(path: Path, seconds: float, rate: int) -> Optional[np.ndarray]:
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-t", f"{seconds:.2f}",
                           "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"], capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) < rate * 4:
        return None
    return np.frombuffer(proc.stdout, np.float32).astype(np.float64)


def audio_match(source: Path, output: Path, seconds: float = 60.0, rate: int = 24000) -> Optional[Dict[str, float]]:
    """How closely the output's sound matches the source's when the track had
    to be re-encoded (an MP4 can't carry PCM, say): lined up to the sample,
    then the leftover difference as a signal-to-noise ratio, plus the level
    change. A clean re-encode leaves the level within a fraction of a dB and
    the difference far below the sound; a volume change, a fade or new music
    doesn't."""
    a, b = _pcm(source, seconds, rate), _pcm(output, seconds, rate)
    if a is None or b is None:
        return None
    n = min(len(a), len(b))
    if n < rate:
        return None
    a, b = a[:n], b[:n]
    win = min(n, rate * 10)
    max_lag = int(rate * 0.1)
    size = 1 << int(np.ceil(np.log2(win + max_lag * 2)))
    fa = np.fft.rfft(a[:win], size)
    fb = np.fft.rfft(b[:win], size)
    xc = np.fft.irfft(fa * np.conj(fb), size)
    lags = np.concatenate([xc[:max_lag + 1], xc[-max_lag:]])
    idx = int(np.argmax(lags))
    lag = idx if idx <= max_lag else idx - len(lags)        # a[i + lag] lines up with b[i]
    if lag > 0:
        a, b = a[lag:], b[:len(b) - lag]
    elif lag < 0:
        a, b = a[:len(a) + lag], b[-lag:]
    pa, pb = float(np.mean(a * a)), float(np.mean(b * b))
    if pa < 1e-10:
        return {"snr_db": 99.0, "gain_db": 0.0, "lag": lag, "silent": True}
    noise = float(np.mean((b - a) ** 2))
    return {"snr_db": round(float(10 * np.log10(pa / max(noise, 1e-12))), 1),
            "gain_db": round(float(10 * np.log10(max(pb, 1e-12) / pa)), 2), "lag": lag}


def _frames_at(path: Path, indices: List[int], chain: str, size: Tuple[int, int]) -> List[np.ndarray]:
    """Decoded RGB frames number `indices` of a video, after `chain` (crop/scale)."""
    sel = "+".join(f"eq(n\\,{i})" for i in indices)
    w, h = size
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select='{sel}',{chain}format=rgb24",
         "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or b"").decode("utf-8", "replace")[-400:])
    frame = w * h * 3
    data = proc.stdout
    return [np.frombuffer(data[i * frame:(i + 1) * frame], np.uint8).reshape(h, w, 3)
            for i in range(len(data) // frame)]


def hook_mask(ass_path: str, layout: Dict[str, Any], at: float) -> np.ndarray:
    """Where the hook covers the frame: drawn alone on black and on white, any
    pixel either picture changed is the hook's — dark outlines included."""
    W, H = layout["canvas"]
    mask = np.zeros((H, W), bool)
    if not ass_path or not Path(ass_path).exists():
        return mask
    for bg in ("black", "white"):
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={bg}:s={W}x{H}:r=25:d={at + 0.5:.2f}",
             "-vf", f"subtitles='{_escape(Path(ass_path))}':fontsdir='{_escape(FONTS_DIR)}',format=rgb24",
             "-ss", f"{at:.2f}", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True)
        if proc.returncode != 0 or len(proc.stdout) < W * H * 3:
            continue
        img = np.frombuffer(proc.stdout[:W * H * 3], np.uint8).reshape(H, W, 3).astype(np.int16)
        base = 0 if bg == "black" else 255
        mask |= (np.abs(img - base) > 6).any(axis=2)
    if mask.any():
        import cv2
        mask = cv2.dilate(mask.astype(np.uint8), np.ones((19, 19), np.uint8)).astype(bool)
    return mask


def frame_size(path: Path) -> Optional[Tuple[int, int]]:
    try:
        out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                    "stream=width,height", "-of", "csv=p=0", str(path)]).stdout
        w, h = (int(v) for v in out.strip().split(",")[:2])
        return w, h
    except (RuntimeError, ValueError):
        return None


def footage_drift(source: Path, output: Path, layout: Dict[str, Any], ass_path: str,
                  duration: float, frames: int, samples: int = 6,
                  covered: Optional[List[List[int]]] = None) -> Dict[str, Any]:
    """How closely the footage in the output matches the source, outside the hook
    (and outside anything else drawn on purpose, like the brand's logo).

    The source frame is scaled exactly as the render scaled it (in YUV, then
    converted, the same order the render used); the output frame is cut back
    to where the footage sits. Two measures, because each catches what the
    other misses:
      psnr    re-encoding alone leaves ~40 dB; a crop, zoom, shift or a frame
              out of step drops it far below 30
      colour  the average level and spread of each colour channel; encoding
              leaves them where they were, a grade (saturation, contrast,
              brightness) moves them even when the picture lines up
    """
    x, y, fw, fh = layout["content"]
    size = frame_size(output)
    if size and list(size) != list(layout["canvas"]):
        return {"psnr_min": None, "size": list(size),
                "error": f"The picture is {size[0]}x{size[1]}, not the {layout['canvas'][0]}x{layout['canvas'][1]} it was rendered at."}
    n = max(1, frames or int(duration * 30))
    idx = sorted({min(n - 1, int((i + 0.5) / samples * n)) for i in range(samples)})
    ref_chain = f"scale={fw}:{fh}:flags=lanczos,setsar=1,format=yuv420p," if layout["scaled"] else ""
    out_chain = f"crop={fw}:{fh}:{x}:{y},"
    ref = _frames_at(source, idx, ref_chain, (fw, fh))
    out = _frames_at(output, idx, out_chain, (fw, fh))
    if len(ref) != len(idx) or len(out) != len(idx):
        return {"psnr_min": None, "error": f"Read {len(out)} of {len(idx)} sample frames."}
    full = hook_mask(ass_path, layout, min(duration / 2, max(0.3, duration - 0.3)))
    for bx, by, bw, bh in covered or []:                     # the brand's logo, drawn on purpose
        pad = 6
        full[max(0, by - pad):by + bh + pad, max(0, bx - pad):bx + bw + pad] = True
    mask = full[y:y + fh, x:x + fw]
    keep = ~mask
    if not keep.any():
        return {"psnr_min": None, "error": "The hook covers the whole picture."}
    values, bias, spread = [], [], []
    for a, b in zip(ref, out):
        pa, pb = a.astype(np.float32)[keep], b.astype(np.float32)[keep]
        diff = pa - pb
        mse = float(np.mean(diff * diff))
        values.append(99.0 if mse < 1e-6 else 10 * math.log10(255.0 ** 2 / mse))
        bias.append(float(np.max(np.abs(pa.mean(axis=0) - pb.mean(axis=0)))))
        sa, sb = pa.std(axis=0), pb.std(axis=0)
        ok = sa > 4
        if ok.any():
            spread.append(float(np.max(np.abs(sb[ok] / sa[ok] - 1.0))))
    return {"psnr_min": round(min(values), 1), "psnr": [round(v, 1) for v in values],
            "colour_shift": round(max(bias), 2), "contrast_change": round(max(spread) if spread else 0.0, 4),
            "frames_compared": len(values), "covered": round(float(mask.mean()), 3)}
