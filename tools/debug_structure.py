"""Why did (or didn't) each clip get a stitched version? One structure call, raw.

usage: python tools/debug_structure.py <job_id> [out.json]
Prints, per clip, what Claude proposed and what validation made of it.
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import highlights, store, structure  # noqa: E402

job_id = sys.argv[1]
out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(f"structure_debug_{job_id}.json")
job = store.get_job(job_id)
transcript = json.loads(job["transcript"])
rows = [c for c in store.list_clips(job_id) if not c.get("alt_of")]
clips = [{"start": c["start"], "end": c["end"], "title": c["title"], "hook": c["hook"],
          "type": c.get("clip_type") or "story", "headline": c.get("headline") or ""} for c in rows]
headline = next((c["headline"] for c in clips if c["headline"]), "")

raw = {}
real_client = highlights._client


class Spy:
    def __init__(self, inner):
        self.inner = inner

    def create(self, **kw):
        msg = self.inner.create(**kw)
        for b in msg.content:
            if getattr(b, "type", "") == "tool_use":
                raw["input"] = b.input
        raw["usage"] = {"in": msg.usage.input_tokens, "out": msg.usage.output_tokens}
        raw["stop"] = msg.stop_reason
        return msg


highlights._client = lambda: type("C", (), {"messages": Spy(real_client().messages)})()
structure.plan(job["title"], clips, transcript["segments"], transcript["words"], job["duration"], headline)
out_path.write_text(json.dumps({"raw": raw, "clips": clips}, indent=1, default=str))

print("usage:", raw.get("usage"), "stop:", raw.get("stop"))
for item in (raw.get("input") or {}).get("clips", []):
    i = item.get("id")
    c = clips[i] if isinstance(i, int) and 0 <= i < len(clips) else {}
    st = item.get("stitched")
    print(f"\n[{i}] {c.get('type')} {c.get('title')!r}  span {c.get('start')}-{c.get('end')}")
    print("   why:", item.get("why"))
    if not st:
        print("   stitched: none proposed")
        continue
    parts = st.get("parts") or []
    total = sum(float(p["end"]) - float(p["start"]) for p in parts)
    print(f"   stitched proposed: {len(parts)} parts, {total:.1f}s, hook {st.get('hook')!r}")
    for p in parts:
        print(f"     {p.get('role'):8} {float(p['start']):7.1f}-{float(p['end']):7.1f} ({float(p['end']) - float(p['start']):4.1f}s) label={p.get('label')!r}")
    got = c.get("variants", {}).get("stitched")
    print("   kept:", bool(got), "| problem:", structure.stitch_problem(
        [q for q in (structure._clean_part(p, transcript["words"], job["duration"]) for p in parts) if q],
        c["variants"]["continuous"]) if not got else "")
