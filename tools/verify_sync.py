"""Render the synthetic flash/beep source through the seamless engine and report A/V offsets."""
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import synctest
from app import motion
work = Path(sys.argv[1]); work.mkdir(parents=True, exist_ok=True)
src = work / "sync_source.mp4"
if not src.exists():
    synctest.make_source(src)
keep = synctest.keep_plan(30.0)
out = motion.render_clip(source=src, clip_id="_synccheck", start=2.0, end=32.0, words=[{"w": "sync", "start": 0.5, "end": 1.0}],
                         edits={"hook": "Sync check", "normalize_audio": True, "motion": True},
                         has_audio=True, layout="fill", plan=None, source_size=(1920, 1080), keep=keep)
synctest.report(f"seamless engine, {len(keep)} cuts", synctest.measure(out["file"]))
Path(out["file"]).unlink(missing_ok=True)
