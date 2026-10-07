"""Turn a moment in the source video into a finished vertical clip."""
from __future__ import annotations

import multiprocessing
import os
import threading
import traceback
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Dict, List

from . import captions, media, tighten
from .framing import FramingPlan, crop_commands, static_crop_x
from .config import BASE_DIR, CLIP_DIR, FPS, RENDER_H, RENDER_W, THUMB_DIR, WORK_DIR

FONTS_DIR = BASE_DIR / "fonts"
BRAND_DIR = BASE_DIR / "data" / "brand"
BRAND_DIR.mkdir(parents=True, exist_ok=True)

# The seamless engine does its frame work in Python, and one Python process
# runs one thing at a time — so "3 clips at once" in threads really took
# turns. Each render runs in its own worker process instead, truly at once.
# RENDER_PROCESSES=0 in .env turns that off (renders go back in-process).
RENDER_PROCESSES = os.getenv("RENDER_PROCESSES", "1").strip() != "0"
RENDER_WORKERS = max(1, int(os.getenv("RENDER_WORKERS", "3")))
_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()


def _exit_with_parent(parent_pid: int) -> None:
    """Runs in each worker: when ClipAgent stops (window closed, restarted),
    the worker goes too. Windows doesn't end child processes with their
    parent, so without this every restart would leave renderers behind."""
    def watch():
        if os.name == "nt":
            import ctypes
            SYNCHRONIZE, INFINITE = 0x00100000, 0xFFFFFFFF
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(SYNCHRONIZE, False, parent_pid)
            if handle:
                k32.WaitForSingleObject(handle, INFINITE)
        else:
            import time
            while True:
                try:
                    os.kill(parent_pid, 0)
                except OSError:
                    break
                time.sleep(3)
        os._exit(0)
    threading.Thread(target=watch, name="exit-with-parent", daemon=True).start()


def _render_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(max_workers=RENDER_WORKERS,
                                        mp_context=multiprocessing.get_context("spawn"),
                                        initializer=_exit_with_parent, initargs=(os.getpid(),))
        return _pool


def _drop_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=False)
        _pool = None


def _seamless(**kwargs) -> Dict[str, Any]:
    """motion.render_clip in a worker process, or here if that isn't possible."""
    from . import motion
    if RENDER_PROCESSES:
        try:
            return _render_pool().submit(motion.render_clip, **kwargs).result()
        except BrokenProcessPool:
            # A worker died (out of memory, killed): start a fresh pool next
            # time, and render this one here so the clip isn't lost.
            traceback.print_exc()
            _drop_pool()
    return motion.render_clip(**kwargs)


LAYOUTS = {
    "auto": "Auto",
    "fill": "Fill frame",
    "blur": "Blurred bars",
    "split": "Facecam split",
    "stack": "Stacked split (two people)",
}

DEFAULT_EDITS: Dict[str, Any] = {
    "layout": "auto",
    "crop_x": 0.5,               # 0 = hug left, 1 = hug right
    "facecam": {"x": 0.0, "y": 0.0, "w": 0.28, "h": 0.30},
    "caption_style": "impact",
    "caption_position": "bottom",
    "caption_size": 1.0,
    "captions_on": True,
    "hook_on": True,
    "hook": "",
    "normalize_audio": True,
    "tighten": True,
    "auto_frame": True,
    "accent": "",             # brand kit: overrides the caption highlight colour
    "logo": False,
    "logo_corner": "top-right",
    "logo_scale": 0.16,       # share of frame width
    "motion": True,           # seamless camera: smooth follow, punch-in cuts, impact zooms
    "motion_style": "punchy", # punchy (funny, hype, reactions) or calm (stories, serious)
    "crop_auto": True,        # False once the user drags the crop slider
    "headline": "",           # the video's premise, small at the top for the whole clip
    "headline_on": True,
    "labels_on": True,        # BEFORE / AFTER marks on a stitched clip's jumps in time
    "style": "",              # the style brain's recipe for this clip (styles.RECIPES)
    "caption_look": {},       # word-pop options: max_words, karaoke, colors, censor
    "cards": [],              # headline label / title bar / comment bubble over the video
}

# "seamless" renders frame by frame (motion.py): frame-accurate cuts with the
# sound locked to the picture, smooth speaker tracking, punch-ins and impact
# zooms. "classic" is the original single-ffmpeg-command renderer, kept as a
# fallback. CLIPAGENT_ENGINE=classic in .env forces the old one everywhere.
ENGINE = os.getenv("CLIPAGENT_ENGINE", "seamless").strip().lower()


def merge_edits(*layers: Dict[str, Any] | None) -> Dict[str, Any]:
    out = dict(DEFAULT_EDITS)
    for layer in layers:
        for key, value in (layer or {}).items():
            if value is not None:
                out[key] = value
    return out


def resolve_layout(edits: Dict[str, Any], plan: FramingPlan | None) -> str:
    """'auto' means: let what we found in the video decide."""
    layout = edits.get("layout", "auto")
    if layout != "auto":
        return layout
    if plan and plan.kind == "facecam":
        return "split"
    return "fill"


def _video_chain(
    edits: Dict[str, Any],
    plan: FramingPlan | None = None,
    cmd_file: Path | None = None,
    source_size: tuple[int, int] | None = None,
) -> str:
    layout = resolve_layout(edits, plan)

    if layout == "blur":
        return (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={RENDER_W}:{RENDER_H}:force_original_aspect_ratio=increase,"
            f"crop={RENDER_W}:{RENDER_H},gblur=sigma=42,eq=brightness=-0.12[bgb];"
            f"[fg]scale={RENDER_W}:-2[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v]"
        )

    if layout == "split":
        cam = dict(DEFAULT_EDITS["facecam"])
        if plan and plan.facecam and not edits.get("facecam_manual"):
            cam.update(plan.facecam)
        cam.update({k: v for k, v in (edits.get("facecam") or {}).items() if v is not None})
        top, bottom = 864, RENDER_H - 864
        return (
            f"[0:v]split=2[cam][game];"
            f"[cam]crop=iw*{cam['w']:.4f}:ih*{cam['h']:.4f}:iw*{cam['x']:.4f}:ih*{cam['y']:.4f},"
            f"scale={RENDER_W}:{top}:force_original_aspect_ratio=increase,"
            f"crop={RENDER_W}:{top}[camv];"
            f"[game]scale={RENDER_W}:{bottom}:force_original_aspect_ratio=increase,"
            f"crop={RENDER_W}:{bottom}[gamev];"
            f"[camv][gamev]vstack=inputs=2[v]"
        )

    # fill: either a crop window that follows the speaker, or a fixed one.
    if cmd_file and source_size:
        source_w, source_h = source_size
        crop_w = source_h * 9 / 16
        start_x = max(0.0, min(source_w - crop_w, (plan.track[0][1] * source_w) - crop_w / 2))
        escaped = _ass_escape_path(cmd_file)
        return (
            f"[0:v]sendcmd=f='{escaped}',"
            f"crop@auto=w={crop_w:.0f}:h={source_h}:x={start_x:.0f}:y=0,"
            f"scale={RENDER_W}:{RENDER_H}[v]"
        )

    crop_x = edits.get("crop_x")
    if crop_x is None or edits.get("crop_auto", True):
        auto_x = static_crop_x(plan) if plan else None
        crop_x = auto_x if auto_x is not None else (crop_x if crop_x is not None else 0.5)
    crop_x = max(0.0, min(1.0, float(crop_x)))
    return (
        f"[0:v]scale={RENDER_W}:{RENDER_H}:force_original_aspect_ratio=increase,"
        f"crop={RENDER_W}:{RENDER_H}:(iw-ow)*{crop_x:.4f}:(ih-oh)/2[v]"
    )


def _ass_escape_path(path: Path) -> str:
    # the subtitles filter needs its own escaping for ':' and '\'
    return str(path).replace("\\", "/").replace(":", r"\:")


def render_clip(
    source: Path,
    clip_id: str,
    start: float,
    end: float,
    words: List[Dict[str, Any]],
    edits: Dict[str, Any] | None = None,
    has_audio: bool = True,
    plan: FramingPlan | None = None,
    source_size: tuple[int, int] | None = None,
    keep: List[tuple[float, float]] | None = None,
    segments: List[tuple[float, float]] | None = None,
    labels: List[tuple[float, str]] | None = None,
) -> Dict[str, Path]:
    """Render one clip and its poster frame. Returns {'file':..., 'thumb':...}.

    `segments` (absolute source seconds, in play order) is a stitched clip:
    parts from anywhere in the video, jumps in time marked with `labels`."""
    edits = merge_edits(edits)
    if ENGINE != "classic" and edits.get("engine", "seamless") != "classic":
        try:
            return _seamless(
                source=Path(source), clip_id=clip_id, start=start, end=end, words=words,
                edits=edits, has_audio=has_audio, layout=resolve_layout(edits, plan), plan=plan,
                source_size=source_size or tuple(media.probe(Path(source))[k] for k in ("width", "height")),
                keep=keep, segments=segments, labels=labels,
            )
        except Exception:
            # Never lose a clip to the new renderer: log it and cut it the old way.
            traceback.print_exc()
            if resolve_layout(edits, plan) == "stack":      # the old renderer has no stacked split
                edits = {**edits, "layout": "blur"}
    if segments:
        # The old renderer reads one stretch of the source, so it can only
        # make a stitched clip whose parts run forwards.
        if any(b[0] < a[1] - 0.01 for a, b in zip(segments, segments[1:])):
            raise RuntimeError("This stitched clip jumps back in time, which needs the seamless renderer")
        start, end = segments[0][0], segments[-1][1]
        keep = [(round(a - start, 3), round(b - start, 3)) for a, b in segments]
    return _render_classic(source, clip_id, start, end, words, edits, has_audio, plan,
                           source_size, keep)


def _render_classic(
    source: Path,
    clip_id: str,
    start: float,
    end: float,
    words: List[Dict[str, Any]],
    edits: Dict[str, Any],
    has_audio: bool,
    plan: FramingPlan | None,
    source_size: tuple[int, int] | None,
    keep: List[tuple[float, float]] | None,
) -> Dict[str, Path]:
    duration = max(0.5, end - start)

    remap = tighten.make_remap(keep) if keep else None
    # `duration` is how much of the SOURCE to read; `out_duration` is how long
    # the finished clip runs. They differ as soon as dead air is cut out, and
    # conflating them truncates the input before the later segments arrive.
    out_duration = sum(b - a for a, b in keep) if keep else duration

    cmd_file = None
    if resolve_layout(edits, plan) == "fill" and plan and source_size:
        cmd_file = crop_commands(plan, start, duration, source_size[0],
                                 source_size[1], remap=remap)  # duration = source span

    chain = _video_chain(edits, plan, cmd_file, source_size)
    if keep:
        expr = tighten.select_expression(keep)
        chain = (f"[0:v]select='{expr}',setpts=N/FRAME_RATE/TB[vsrc];"
                 + chain.replace("[0:v]", "[vsrc]", 1))
    # Captions and the hook are independent switches: turning captions off must
    # still leave the hook on screen.
    show_captions = bool(edits.get("captions_on", True)) and bool(words)
    hook_text = edits.get("hook", "") if edits.get("hook_on", True) else ""

    brand = None
    if edits.get("brand_logo") and Path(edits["brand_logo"]).exists():
        from . import brandlogo
        brand = brandlogo.prepare(Path(edits["brand_logo"]), (RENDER_W, RENDER_H),
                                  brandlogo.SOURCE_MAX_W, brandlogo.SOURCE_MAX_H)
        brand["at"] = brandlogo.source_box((RENDER_W, RENDER_H), brand["w"], brand["h"])

    if show_captions or hook_text.strip():
        ass_path = WORK_DIR / f"{clip_id}.ass"
        captions.build_ass(
            words=words if show_captions else [],
            duration=out_duration,
            style_name=edits.get("caption_style", "impact"),
            position=edits.get("caption_position", "bottom"),
            size_scale=float(edits.get("caption_size", 1.0)),
            hook=hook_text,
            accent=edits.get("accent", ""),
            out_path=ass_path,
            top_offset=brandlogo.source_text_offset((RENDER_W, RENDER_H), brand["h"]) if brand else 0,
            look=edits.get("caption_look") or None,
        )
        chain += (
            f";[v]subtitles='{_ass_escape_path(ass_path)}'"
            f":fontsdir='{_ass_escape_path(FONTS_DIR)}'[vout]"
        )
        vlabel = "[vout]"
    else:
        vlabel = "[v]"

    logo_path = BRAND_DIR / "logo.png"
    extra_inputs: List[str] = []
    if edits.get("logo") and logo_path.exists():
        extra_inputs = ["-i", str(logo_path)]
        scale = max(0.05, min(0.4, float(edits.get("logo_scale", 0.16))))
        from .motion import _logo_corners
        corners = _logo_corners()
        x, y = corners.get(edits.get("logo_corner", "top-right"), corners["top-right"])
        chain += (f";[1:v]scale={int(RENDER_W * scale)}:-1[logo];"
                  f"{vlabel}[logo]overlay={x}:{y}[vbrand]")
        vlabel = "[vbrand]"
    if brand:
        n = 1 + len(extra_inputs) // 2
        extra_inputs += ["-i", str(brand["png"])]
        chain += f";{vlabel}[{n}:v]overlay={brand['at'][0]}:{brand['at'][1]}:format=yuv420,format=yuv420p[vcamp]"
        vlabel = "[vcamp]"

    out_file = CLIP_DIR / f"{clip_id}.mp4"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
        *extra_inputs,
        "-filter_complex", chain,
        "-map", vlabel,
    ]
    if has_audio:
        steps = []
        if keep:
            steps += [f"aselect='{tighten.select_expression(keep)}'", "asetpts=N/SR/TB"]
        if edits.get("normalize_audio", True):
            steps.append("loudnorm=I=-14:TP=-1.0:LRA=11")
        if steps:
            # Audio joins the filter graph so the cuts land on both streams.
            chain_audio = f";[0:a:0]{','.join(steps)}[aout]"
            cmd[cmd.index("-filter_complex") + 1] += chain_audio
            cmd += ["-map", "[aout]"]
        else:
            cmd += ["-map", "0:a:0?"]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    from .motion import VIDEO_CAP          # the same bitrate ceiling as the seamless engine
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *VIDEO_CAP,
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
        "-movflags", "+faststart", "-shortest", str(out_file),
    ]
    media.run(cmd)

    thumb = THUMB_DIR / f"{clip_id}.jpg"
    media.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{min(1.0, out_duration / 3):.2f}", "-i", str(out_file),
        "-frames:v", "1", "-q:v", "4", str(thumb),
    ])

    # A frame with the framing applied but nothing burned on it. The editor
    # draws its live caption preview over this, so restyling captions does not
    # show two sets of words at once.
    clean = THUMB_DIR / f"{clip_id}_clean.jpg"
    try:
        at = start + (keep[0][0] if keep else 0.0) + min(1.0, out_duration / 3)
        media.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{at:.3f}", "-i", str(source),
            "-filter_complex", _video_chain(edits, plan, None, source_size),
            "-map", "[v]", "-frames:v", "1", "-q:v", "4", str(clean),
        ])
    except RuntimeError:
        clean = thumb

    return {"file": out_file, "thumb": thumb, "clean": clean}


def layout_catalogue() -> List[Dict[str, str]]:
    return [{"id": key, "label": label} for key, label in LAYOUTS.items()]
