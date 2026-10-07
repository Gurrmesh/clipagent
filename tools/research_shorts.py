"""Map published Shorts back onto the long video they were cut from.

usage: python tools/research_shorts.py <job_id> <out_dir> <youtube_id> [<youtube_id> ...]

For each Short: download it, transcribe it, find where each stretch of its
speech comes from in the source transcript, detect its cuts, and save a
contact sheet. Writes <out_dir>/report.json.
"""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from app import media, store, transcribe  # noqa: E402


def norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def locate(short_words, source_words, n=3):
    """For each short word, the source time it most plausibly came from (or None)."""
    src_tokens = [norm(w["w"]) for w in source_words]
    index = {}
    for i in range(len(src_tokens) - n + 1):
        index.setdefault(tuple(src_tokens[i:i + n]), []).append(i)
    toks = [norm(w["w"]) for w in short_words]
    hits = [None] * len(toks)
    for i in range(len(toks) - n + 1):
        key = tuple(toks[i:i + n])
        if key in index and len(index[key]) <= 3:
            for k in range(n):
                if hits[i + k] is None:
                    hits[i + k] = index[key][0] + k
    return hits


def segments(short_words, hits, source_words):
    """Group consecutive matched words into (short_start, short_end, src_start, src_end) runs."""
    runs = []
    cur = None
    for sw, h in zip(short_words, hits):
        if h is None:
            continue
        s_t = source_words[h]["start"]
        if cur and abs((s_t - cur["src_end"]) - (sw["start"] - cur["short_end"])) < 2.5 and s_t >= cur["src_end"] - 0.5:
            cur["short_end"] = sw["end"]
            cur["src_end"] = source_words[h]["end"]
            cur["text"].append(sw["w"])
        else:
            if cur:
                runs.append(cur)
            cur = {"short_start": sw["start"], "short_end": sw["end"], "src_start": s_t,
                   "src_end": source_words[h]["end"], "text": [sw["w"]]}
    if cur:
        runs.append(cur)
    for r in runs:
        r["text"] = " ".join(r["text"])
        for k in ("short_start", "short_end", "src_start", "src_end"):
            r[k] = round(r[k], 2)
    return runs


def cuts(path: Path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=64:36,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    fr = np.frombuffer(raw, np.uint8).reshape(-1, 36, 64).astype(np.int16)
    fps = 30.0
    try:
        num, den = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                   "stream=r_frame_rate", "-of", "csv=p=0", str(path)],
                                  capture_output=True, text=True).stdout.strip().split("/")
        fps = float(num) / float(den)
    except ValueError:
        pass
    d = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2)) / 255.0
    out = []
    for i in range(1, len(d)):
        base = float(np.median(d[max(0, i - 12):i + 12]))
        if d[i] > 0.075 and d[i] > 3 * base + 0.02 and (not out or (i + 1) / fps - out[-1] > 0.3):
            out.append(round((i + 1) / fps, 2))
    return out, len(fr) / fps


def sheet(path: Path, out: Path, every: float = 1.0):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vf",
                    f"fps=1/{every},scale=108:192,tile=10x5", "-frames:v", "1", str(out)])


def main():
    job_id, out_dir, ids = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]
    out_dir.mkdir(parents=True, exist_ok=True)
    job = store.get_job(job_id)
    source_words = json.loads(job["transcript"])["words"]
    report = []
    for vid in ids:
        mp4 = out_dir / f"{vid}.mp4"
        if not mp4.exists():
            path, title = media.download(f"https://www.youtube.com/shorts/{vid}", f"_research_{vid}")
            mp4.write_bytes(Path(path).read_bytes())
        info = media.probe(mp4)
        wav = media.extract_audio(mp4, f"_research_{vid}")
        tr = transcribe.transcribe(wav)
        wav.unlink(missing_ok=True)
        hits = locate(tr["words"], source_words)
        matched = sum(h is not None for h in hits)
        runs = segments(tr["words"], hits, source_words)
        cut_times, dur = cuts(mp4)
        sheet(mp4, out_dir / f"{vid}_sheet.jpg")
        report.append({"id": vid, "duration": round(info["duration"], 1), "words": len(tr["words"]),
                       "matched_words": matched, "runs": runs, "cuts": cut_times,
                       "first_words": " ".join(w["w"] for w in tr["words"] if w["start"] < 3.0),
                       "transcript": tr["text"]})
        print(f"{vid}: {info['duration']:.1f}s, {len(cut_times)} cuts, {len(runs)} source runs", flush=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
