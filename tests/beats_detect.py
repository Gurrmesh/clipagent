"""Beat and drop detection (app/beats.py) on songs with known answers.

Run: python tests/beats_detect.py
Makes three songs with tools/make_test_song.py (128, 92 and 150 BPM, each with
a quiet intro and a loud drop) and checks the tempo, that every tracked beat
sits on a real beat, and that the drop is found on the right beat.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import beats  # noqa: E402

FAILS = []


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


tmp = Path(tempfile.mkdtemp(prefix="beats_"))
cases = [(128, 60, 20.625, 0.37), (92, 50, 26.457, 0.12), (150, 45, 12.4, 0.0)]
for bpm, secs, drop_at, off in cases:
    out = tmp / f"s{bpm}.mp3"
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_test_song.py"), str(bpm), str(secs), str(drop_at),
                    str(off), str(out)], check=True)
    period = 60.0 / bpm
    first_loud = off + period * -(-(drop_at - off) // period)          # first beat at/after drop_at
    a = beats.analyze(out)
    errs = [abs(((b - off + period / 2) % period) - period / 2) for b in a["beats"]]
    print(f"== {bpm} BPM: found {a['bpm']} BPM, {len(a['beats'])} beats, drop {a['drop']} (true {first_loud:.2f})")
    expect(abs(a["bpm"] - bpm) <= 2, "tempo within 2 BPM (no half/double tempo)")
    expect(max(errs) < 0.05, f"every beat within 50 ms of a real beat (worst {max(errs) * 1000:.0f} ms)")
    expect(abs(a["drop"] - first_loud) < 0.1, "the drop is found on the right beat")
    expect(len(a["bars"]) >= len(a["beats"]) // 4 - 1, "bar lines found")

print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed")
sys.exit(1 if FAILS else 0)
