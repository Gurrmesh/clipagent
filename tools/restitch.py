"""Give a finished run's rejected stitched versions a second chance.

The structure pass logs every proposal. A stitched version thrown out only for
being too long can often be saved by dropping a middle context part (see
structure.fit). This takes those, judges them against the continuous cut the
run already made, renders them, and adds them to the run — the winner as the
main clip, the other as the "B" version. No new picking, no new structure call.

usage: python tools/restitch.py <job_id>
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import judge, pipeline, render, store, structure  # noqa: E402
from app.config import DATA_DIR  # noqa: E402

job_id = sys.argv[1]
job = store.get_job(job_id)
transcript = json.loads(job["transcript"])
words = transcript["words"]
settings = json.loads(job.get("settings") or "{}")
source = Path(job["source_path"])
log_path = Path(DATA_DIR) / "logs" / f"{job_id}_structure.json"
log = json.loads(log_path.read_text(encoding="utf-8"))
rows = store.list_clips(job_id)
fps = pipeline._frame_rate(source)
from app import media  # noqa: E402
info = media.probe(source)

for entry in log:
    problem = entry.get("stitch_problem") or ""
    raw = (entry.get("proposed") or {}).get("stitched") or {}
    if not problem or problem == "none proposed" or not raw.get("parts"):
        continue
    row = next((r for r in rows if r["title"] == entry["title"] and not r.get("alt_of")), None)
    if row is None:
        print("no clip row for", entry["title"])
        continue
    cont = structure._as_variant(row["start"], row["end"], row["hook"])
    parts = structure.clean_stitch(raw["parts"], words, job["duration"])
    problem = structure.stitch_problem(parts, cont)
    print(f"{entry['title']!r}: {len(parts)} parts, "
          f"{sum(p['end'] - p['start'] for p in parts):.1f}s -> {problem or 'usable now'}")
    if problem:
        continue

    clip = {"title": row["title"], "type": row.get("clip_type"), "hook": row["hook"],
            "headline": row.get("headline") or "", "start": row["start"], "end": row["end"],
            "variants": {"continuous": cont, "stitched": {"parts": parts, "hook": (raw.get("hook") or "")[:70]}}}
    judge.compare([clip], words, clip["headline"])
    result = clip.get("judge") or {}
    if not result.get("stitched"):
        print("   judge failed:", pipeline.highlights.LAST_ERROR)
        continue
    print(f"   judge: one stretch {result['continuous']['overall']} vs stitched "
          f"{result['stitched']['overall']} -> {result['winner']}")

    edits = json.loads(row.get("edits") or "{}")
    edits["hook"] = clip["variants"]["stitched"]["hook"] or edits.get("hook", "")
    new_id = store.create_clip(job_id, {
        "rank": row["rank"], "start": min(p["start"] for p in parts), "end": max(p["end"] for p in parts),
        "score": row["score"], "title": row["title"], "hook": edits["hook"], "reason": row["reason"],
        "tags": json.loads(row.get("tags") or "[]"), "words": [], "edits": edits,
        "caption": row.get("caption") or "", "hashtags": json.loads(row.get("hashtags") or "[]"),
        "verdict": row.get("verdict") or "", "type": row.get("clip_type") or "",
        "parts": parts, "variant": "stitched", "judge": result, "headline": row.get("headline") or "",
        "alt_of": "" if result["winner"] == "stitched" else row["id"],
    })
    store.update_clip(row["id"], judge=json.dumps(result),
                      alt_of=new_id if result["winner"] == "stitched" else "")

    t = time.time()
    segments, clip_words, saved, labels = pipeline.build_parts(parts, words, settings, fps)
    out = render.render_clip(source=source, clip_id=new_id, start=parts[0]["start"], end=parts[-1]["end"],
                             words=clip_words, edits=render.merge_edits(edits), has_audio=info["has_audio"],
                             plan=None, source_size=(info["width"], info["height"]),
                             segments=segments, labels=labels)
    used = out.get("plan")
    store.update_clip(new_id, file=str(out["file"]), thumb=str(out["thumb"]), status="ready",
                      words=json.dumps(clip_words), saved=saved,
                      framing=json.dumps(used.to_json() if used else {}))
    print(f"   rendered {new_id} in {time.time() - t:.0f}s")
    entry["stitch_problem"] = ""
    entry["restitched"] = {"clip_id": new_id, "judge": result}

log_path.write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
print("done")
