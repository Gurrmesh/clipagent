"""Clips render in worker processes, truly at once, and come out the same.

usage: python tests/parallel_render.py <source.mp4> [clip_seconds] [clips]
Renders the same clips twice — in-process threads (the old way) and worker
processes (the new way) — and compares the time and the files.
"""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="par_")  # never touch the real data folder
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import media, render  # noqa: E402

FAILS = 0


def expect(ok, label):
    global FAILS
    print(("  ok   " if ok else "  FAIL ") + label, flush=True)
    FAILS += 0 if ok else 1


def run(source: Path, length: float, count: int, processes: bool, tag: str):
    render.RENDER_PROCESSES = processes
    info = media.probe(source)
    span = max(1.0, (info["duration"] - length) / max(1, count - 1))
    words = []
    jobs = []
    for i in range(count):
        a = min(i * span, info["duration"] - length)
        ws = [{"w": f"word{k}", "start": a + k * 0.4, "end": a + k * 0.4 + 0.35}
              for k in range(int(length / 0.4))]
        jobs.append((f"{tag}{i}", a, a + length, ws))
    t = time.time()
    with ThreadPoolExecutor(max_workers=render.RENDER_WORKERS) as pool:
        outs = list(pool.map(lambda j: render.render_clip(
            source=source, clip_id=j[0], start=j[1], end=j[2], words=j[3],
            edits={"layout": "fill", "hook": "Parallel test hook", "headline": "A test"},
            has_audio=info["has_audio"], source_size=(info["width"], info["height"])), jobs))
    return time.time() - t, outs


if __name__ == "__main__":
    src = Path(sys.argv[1])
    length = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    print(f"{count} clips of {length:.0f}s, {render.RENDER_WORKERS} at a time, on {os.cpu_count()} cores")
    old, old_outs = run(src, length, count, False, "thr")
    print(f"  threads (old):   {old:.1f}s")
    new, new_outs = run(src, length, count, True, "proc")
    print(f"  processes (new): {new:.1f}s  ({old / new:.2f}x)")
    for a, b in zip(old_outs, new_outs):
        da, db = media.probe(a["file"])["duration"], media.probe(b["file"])["duration"]
        expect(Path(b["file"]).exists() and abs(da - db) < 0.05,
               f"{Path(b['file']).name}: same length as the in-process render ({db:.2f}s)")
        expect(b.get("plan") is not None and b["plan"].note, "framing plan came back from the worker")
    render._drop_pool()
    print("\nall checks behaved" if not FAILS else f"\n{FAILS} check(s) failed")
    sys.exit(1 if FAILS else 0)
