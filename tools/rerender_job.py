"""Re-render every clip of a job with the current renderer, 3 at a time.

usage: python tools/rerender_job.py <job_id>
Keeps each clip's moment, hook and settings; only the rendering changes.
"""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline, store  # noqa: E402

job_id = sys.argv[1]
LOG = Path(__file__).resolve().parents[1] / "data" / "rerender.log"
LOG.write_text("", encoding="utf-8")
_print = print


def print(*args, **kw):  # noqa: A001 - also keep a log file the app folder can show
    _print(*args, **kw)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(" ".join(str(a) for a in args) + "\n")


clips = sorted(store.list_clips(job_id), key=lambda c: c["rank"])
print(f"re-rendering {len(clips)} clips of job {job_id}", flush=True)
for c in clips:
    store.update_clip(c["id"], status="rendering")


def one(c):
    t = time.time()
    try:
        done = pipeline.rerender_clip(c["id"], {})
        note = json.loads(done.get("framing") or "{}").get("note", "")
        return f"#{c['rank']} ready in {time.time() - t:.0f}s  saved={done.get('saved')}s  {note}"
    except Exception as exc:
        traceback.print_exc()
        store.update_clip(c["id"], status="failed", reason=str(exc)[:300])
        return f"#{c['rank']} FAILED after {time.time() - t:.0f}s: {exc}"


start = time.time()
with ThreadPoolExecutor(max_workers=3) as pool:
    for fut in as_completed([pool.submit(one, c) for c in clips]):
        print(fut.result(), flush=True)
print(f"ALL DONE in {time.time() - start:.0f}s", flush=True)
