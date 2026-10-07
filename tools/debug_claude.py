"""Replay a job's Claude steps with full tracebacks and every raw tool reply saved.

usage: python tools/debug_claude.py <job_id> <out.json>
Runs pick -> rerank -> structure -> judge on the job's cached transcript
(no download, no transcription) and records exactly what Claude returned, so a
malformed reply can be seen and replayed offline.
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
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import highlights, judge, store, structure  # noqa: E402

job_id, out_path = sys.argv[1], Path(sys.argv[2])
job = store.get_job(job_id)
transcript = json.loads(job["transcript"])
calls = []


def save():
    out_path.write_text(json.dumps(calls, indent=1, default=str), encoding="utf-8")


real = highlights._client


class Spy:
    def __init__(self, inner):
        self.inner = inner

    def create(self, **kw):
        entry = {"tool": kw.get("tool_choice", {}).get("name")}
        try:
            msg = self.inner.create(**kw)
        except Exception as exc:
            entry["error"] = repr(exc)
            calls.append(entry)
            save()
            raise
        entry["stop"] = msg.stop_reason
        entry["usage"] = [msg.usage.input_tokens, msg.usage.output_tokens]
        entry["blocks"] = [{"type": getattr(b, "type", ""), "input": getattr(b, "input", None),
                            "text": getattr(b, "text", None)} for b in msg.content]
        calls.append(entry)
        save()
        return msg


highlights._client = lambda: type("C", (), {"messages": Spy(real().messages)})()
stage = "pick"
try:
    clips = highlights.find_highlights(title=job["title"], segments=transcript["segments"],
                                       duration=job["duration"], peaks=[], want=10)
    print("picked", len(clips), "LAST_ERROR:", highlights.LAST_ERROR or "-")
    for c in clips:
        highlights.snap_to_words(c, transcript["words"])
    headline = next((c["headline"] for c in clips if c.get("headline")), "")
    stage = "structure"
    structure.plan(job["title"], clips, transcript["segments"], transcript["words"], job["duration"], headline)
    stage = "judge"
    judge.compare(clips, transcript["words"], headline)
    for c in clips:
        j = c.get("judge") or {}
        print(f"  {c.get('type'):9} {c['variant']:10} {c['title'][:40]:40} "
              f"{j.get('continuous', {}).get('overall')} {j.get('stitched', {}).get('overall') if j.get('stitched') else ''} "
              f"| {c.get('stitch_problem')}")
except Exception:
    print("FAILED in", stage)
    traceback.print_exc()
finally:
    save()
    print("calls:", [(c["tool"], c.get("stop"), c.get("usage"), c.get("error")) for c in calls])
