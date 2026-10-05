"""Work out how to frame a clip, instead of asking the user to type percentages.

Two jobs, both driven by face detection on sampled frames:

1. Find the facecam. On a stream layout the webcam sits in a fixed corner, so
   faces cluster in one small, stable rectangle. That rectangle is the facecam
   box for the split layout.
2. Follow the speaker. On talking-head footage faces move and swap, so we
   build a crop track over time and let ffmpeg move the crop window.
"""
from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import WORK_DIR
from .media import probe, run

SAMPLE_COUNT = 48          # frames sampled across the span
MIN_FACE_FRACTION = 0.035  # ignore faces smaller than this share of frame width


@dataclass
class Face:
    t: float
    x: float   # all box values are fractions of frame width/height
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class FramingPlan:
    kind: str                                  # "facecam" | "track" | "static" | "none"
    facecam: Optional[Dict[str, float]] = None  # fractions, for the split layout
    track: List[Tuple[float, float]] = field(default_factory=list)  # (t, centre_x)
    confidence: float = 0.0
    note: str = ""
    two_shot: float = 0.0     # share of sampled frames with two people side by side

    def to_json(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "facecam": self.facecam,
            "track": [[round(t, 2), round(x, 4)] for t, x in self.track],
            "confidence": round(self.confidence, 2),
            "note": self.note,
            "two_shot": round(self.two_shot, 2),
        }


# --- detection ------------------------------------------------------------

def _detector():
    """YuNet if its model has been dropped in models/, else the bundled Haar."""
    import cv2

    model = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
    if model.exists() and hasattr(cv2, "FaceDetectorYN"):
        net = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.6, 0.3, 5000)

        def detect_yunet(frame):
            h, w = frame.shape[:2]
            net.setInputSize((w, h))
            _, faces = net.detect(frame)
            return [(f[0], f[1], f[2], f[3]) for f in (faces if faces is not None else [])]

        return detect_yunet, "yunet"

    cascade_file = getattr(cv2, "data", None) and \
        Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if not hasattr(cv2, "CascadeClassifier") or not (cascade_file and cascade_file.exists()):
        # OpenCV 5 removed the cascade detector and its data files.
        raise RuntimeError(
            f"this build of OpenCV ({cv2.__version__}) ships no face cascade — "
            "either pin opencv-python-headless<5.0.0, or put the YuNet model in "
            "models/face_detection_yunet_2023mar.onnx (see models/README.md)"
        )
    cascade = cv2.CascadeClassifier(str(cascade_file))

    def detect_haar(frame):
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        grey = cv2.equalizeHist(grey)
        found = cascade.detectMultiScale(grey, scaleFactor=1.12, minNeighbors=6, minSize=(40, 40))
        return [(float(x), float(y), float(w), float(h)) for (x, y, w, h) in found]

    return detect_haar, "haar"


def sample_faces(source: Path, start: float, end: float, count: int = SAMPLE_COUNT) -> List[Face]:
    """Detect faces on frames sampled evenly across the span."""
    import cv2

    detect, _ = _detector()
    info = probe(source)
    width, height = info["width"] or 1, info["height"] or 1

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        return []

    span = max(0.1, end - start)
    times = [start + span * (i + 0.5) / count for i in range(count)]
    faces: List[Face] = []
    try:
        for t in times:
            capture.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            for (x, y, w, h) in detect(frame):
                if w / width < MIN_FACE_FRACTION:
                    continue
                faces.append(Face(t=t, x=x / width, y=y / height,
                                  w=w / width, h=h / height))
    finally:
        capture.release()
    return faces


# --- turning detections into a plan ---------------------------------------

def _cluster(faces: List[Face], radius: float = 0.12) -> List[List[Face]]:
    """Group faces that keep appearing in the same part of the frame."""
    clusters: List[List[Face]] = []
    for face in faces:
        for cluster in clusters:
            ref = cluster[0]
            if math.hypot(face.cx - ref.cx, face.cy - ref.cy) <= radius:
                cluster.append(face)
                break
        else:
            clusters.append([face])
    return sorted(clusters, key=len, reverse=True)


def two_shot_share(faces: List[Face], count: int) -> float:
    """How often two people sit side by side in the frame (a podcast wide
    shot): two faces of similar size, at the same height, well apart."""
    by_time: Dict[float, List[Face]] = {}
    for f in faces:
        by_time.setdefault(f.t, []).append(f)
    hits = 0
    for group in by_time.values():
        group = sorted(group, key=lambda f: f.cx)
        if any(b.cx - a.cx > 0.22 and 0.5 < a.h / max(b.h, 1e-6) < 2.0 and abs(a.cy - b.cy) < 0.2
               for i, a in enumerate(group) for b in group[i + 1:]):
            hits += 1
    return hits / max(1, count)


def plan_framing(
    source: Path,
    start: float,
    end: float,
    target_aspect: float = 9 / 16,
    count: int = SAMPLE_COUNT,
) -> FramingPlan:
    """Decide how this span should be framed."""
    try:
        faces = sample_faces(source, start, end, count=count)
    except Exception as exc:
        return FramingPlan(kind="none", note=f"Face detection unavailable: {exc}")

    if not faces:
        return FramingPlan(kind="none", note="No faces found — falling back to a centre crop.")

    two = two_shot_share(faces, count)
    plan = _plan_from_faces(source, faces, count)
    plan.two_shot = two
    return plan


def _plan_from_faces(source: Path, faces: List[Face], count: int) -> FramingPlan:
    clusters = _cluster(faces)
    main = clusters[0]
    hit_rate = len(main) / max(1, count)
    avg_w = sum(f.w for f in main) / len(main)
    spread_x = max(f.cx for f in main) - min(f.cx for f in main)
    spread_y = max(f.cy for f in main) - min(f.cy for f in main)

    # A facecam is small, pinned in place, and present most of the time.
    is_facecam = avg_w < 0.22 and spread_x < 0.10 and spread_y < 0.10 and hit_rate > 0.30
    if is_facecam:
        info = probe(source)
        aspect = (info["width"] / info["height"]) if info["height"] else 16 / 9
        box = _facecam_box(main, aspect)
        return FramingPlan(
            kind="facecam", facecam=box, confidence=min(1.0, hit_rate * 1.6),
            note=f"Facecam found in the {_corner(box)} of the frame, on {int(hit_rate * 100)}% of sampled frames.",
        )

    # Otherwise follow whoever is on screen.
    track = _speaker_track(faces)
    if not track:
        return FramingPlan(kind="none", note="Faces were too scattered to track.")
    moved = max(x for _, x in track) - min(x for _, x in track)
    kind = "track" if moved > 0.04 else "static"
    return FramingPlan(
        kind=kind, track=track, confidence=min(1.0, len(faces) / SAMPLE_COUNT),
        note=("Following the speaker across the frame." if kind == "track"
              else "One subject, holding still — using a fixed crop on them."),
    )


def _facecam_box(cluster: List[Face], source_aspect: float) -> Dict[str, float]:
    """Grow the detected head into a webcam-shaped box around it.

    Box sizes are fractions of frame width and height, and the frame is not
    square, so the aspect correction has to go through the source's own aspect
    ratio — otherwise a 16:9 source yields a box twice as wide as intended.
    A box that runs past an edge is slid back inside rather than trimmed, so
    the face stays where it belongs inside the crop.
    """
    cx = sum(f.cx for f in cluster) / len(cluster)
    cy = sum(f.cy for f in cluster) / len(cluster)
    face_w = sum(f.w for f in cluster) / len(cluster)

    # In a webcam frame a head spans roughly a third of the width.
    box_w = max(0.10, min(1.0, face_w * 3.2))
    want = 1080 / 864                       # the split layout's top panel
    box_h = min(1.0, box_w * source_aspect / want)

    # Sit the face a little above centre, the way people frame a webcam.
    x = cx - box_w / 2
    y = cy - box_h * 0.42

    x = max(0.0, min(x, 1.0 - box_w))
    y = max(0.0, min(y, 1.0 - box_h))
    return {"x": round(x, 4), "y": round(y, 4),
            "w": round(box_w, 4), "h": round(box_h, 4)}


def _corner(box: Dict[str, float]) -> str:
    vertical = "top" if box["y"] + box["h"] / 2 < 0.5 else "bottom"
    horizontal = "left" if box["x"] + box["w"] / 2 < 0.5 else "right"
    return f"{vertical} {horizontal}"


def _speaker_track(
    faces: List[Face],
    deadzone: float = 0.035,
    cut_threshold: float = 0.22,
    smoothing: float = 0.35,
) -> List[Tuple[float, float]]:
    """A centre-x per sampled second.

    Small drift is ignored (deadzone) and gentle movement is eased in, but a
    big jump — the other person starting to talk — is a hard cut, because a
    slow pan between two speakers looks like a mistake.
    """
    by_time: Dict[float, List[Face]] = {}
    for face in faces:
        by_time.setdefault(round(face.t, 2), []).append(face)

    track: List[Tuple[float, float]] = []
    current: Optional[float] = None
    for t in sorted(by_time):
        # Largest face at this moment is the one nearest the camera.
        target = max(by_time[t], key=lambda f: f.w).cx
        if current is None:
            current = target
        elif abs(target - current) >= cut_threshold:
            current = target                                   # hard cut
        elif abs(target - current) > deadzone:
            current += (target - current) * smoothing          # ease toward
        track.append((t, max(0.0, min(1.0, current))))
    return track


# --- handing the plan to ffmpeg -------------------------------------------

def crop_commands(
    plan: FramingPlan,
    clip_start: float,
    duration: float,
    source_w: int,
    source_h: int,
    label: str = "crop@auto",
    remap=None,
) -> Optional[Path]:
    """Write a sendcmd file that walks the crop window across the frame.

    `remap` is passed when dead air has been cut out of the clip: the crop
    times have to move onto the shortened timeline with the picture.
    """
    if plan.kind != "track" or not plan.track:
        return None

    crop_w = source_h * 9 / 16
    if crop_w > source_w:
        return None
    max_x = source_w - crop_w

    lines = []
    for t, centre in plan.track:
        local = t - clip_start
        if local < 0 or local > duration:
            continue
        x = max(0.0, min(max_x, centre * source_w - crop_w / 2))
        at = remap(local) if remap else local
        lines.append(f"{max(0.0, at):.2f} {label} x {x:.0f};")
    if not lines:
        return None

    path = WORK_DIR / f"crop_{abs(hash((clip_start, duration, len(lines)))) % 10**10}.cmd"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def static_crop_x(plan: FramingPlan) -> Optional[float]:
    """For a subject who holds still: one crop position, as a 0-1 fraction."""
    if plan.kind == "static" and plan.track:
        return sum(x for _, x in plan.track) / len(plan.track)
    return None
