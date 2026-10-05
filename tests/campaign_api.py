"""Campaign Mode through the real API, end to end, without Claude.

usage: python tests/campaign_api.py <folder with vert.mp4, land.mp4, short_noaudio.mp4> <ketone brief.txt>

Saves the Ketone-IQ rulebook (the reader's answer stood in, as in
campaign_gate.py), runs a clip-bank job through POST /api/campaigns/{id}/jobs
and the background pipeline, then checks what a user would hit: verdicts on
every clip, blocked downloads refused, the zip leaving blocked clips out, the
CSV carrying the exact caption, and a hook re-render being checked again.
Also: a clip-from-source campaign's settings and editor clamps.
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import io
import sys
import zipfile
from pathlib import Path

import os as _os
import tempfile as _tempfile
_os.environ["DATA_DIR"] = _tempfile.mkdtemp(prefix="clipagent_test_")  # never touch the real data folder
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import campaign, main, store  # noqa: E402
from tests.campaign_gate import KETONE_ANSWER  # noqa: E402

FAILS = []


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def run(folder: Path, brief_path: Path) -> None:
    brief = brief_path.read_text(encoding="utf-8")
    client = TestClient(main.app)

    cfg = client.get("/api/config").json()
    expect("campaign" in cfg and "neon" in cfg["campaign"]["hook_styles"], "config carries the campaign options")

    draft = campaign.normalize(KETONE_ANSWER, brief)
    # the card's live preview: allow zooms by hand
    prev = client.post("/api/campaigns/preview", json={"rulebook": draft, "edits": {"overrides": {"zoom": "yes"}}, "brief": brief}).json()
    zoom = next(p for p in prev["card"]["permissions"] if p["key"] == "zoom")
    expect(zoom["allowed"] and zoom["source"] == "you", "an override on the card takes effect")

    saved = client.post("/api/campaigns", json={"brief": brief, "rulebook": draft, "edits": {
        "name": "Ketone-IQ test", "caption": {"hashtags": ["#KetoneIQPartner"]}}}).json()
    cid = saved["id"]
    expect(saved["name"] == "Ketone-IQ test" and saved["mode"] == "overlay", "campaign saved")
    expect(any(c["id"] == cid for c in client.get("/api/campaigns").json()["campaigns"]), "listed")

    bad = client.post("/api/jobs", data={"url": "https://example.com/x", "campaign_id": cid})
    expect(bad.status_code == 400, "a clip-bank campaign refuses the clip-from-footage form")

    files = [("files", (n, open(folder / n, "rb"), "video/mp4")) for n in ("vert.mp4", "land.mp4", "short_noaudio.mp4")]
    r = client.post(f"/api/campaigns/{cid}/jobs", files=files,
                    data={"versions": "2", "hook_style": "neon", "hook_color": "cyan", "platforms": ""})
    expect(r.status_code == 200, f"clip-bank job accepted ({r.status_code} {r.text[:120]})")
    job_id = r.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}").json()          # background task ran inside the request
    print("  stage:", job["stage"])
    clips = job["clips"]
    expect(job["status"] == "done" and len(clips) == 6, "3 files x 2 versions = 6 clips")
    by = {}
    for c in clips:
        g = c["compliance"] or {}
        print(f"   #{c['rank']} {c['title'][:30]:30} {g.get('status'):8} {c['post']['platform'] or '-':9} hook={c['hook'][:30]!r}")
        by.setdefault(c["title"].split(" · ")[0], []).append(c)
    v1 = [c for c in clips if c["title"].endswith("version 1")]
    v2 = [c for c in clips if c["title"].endswith("version 2")]
    expect(all(c["post"]["platform"] == "tiktok" for c in v1), "version 1 of each clip goes to TikTok")
    expect(all(c["compliance"]["status"] == "blocked" for c in v2),
           "version 2 is blocked: this brief is TikTok-only and once per account")
    vert1 = next(c for c in v1 if c["title"].startswith("vert"))
    short1 = next(c for c in v1 if c["title"].startswith("short"))
    expect(vert1["compliance"]["status"] == "ready", "a good clip is ready to post")
    expect(short1["compliance"]["status"] == "blocked", "the 8-second clip is blocked (15s minimum)")
    expect(len({c["post"]["line"] for c in clips}) > 1, "required caption lines are taken in turn")

    d = client.get(f"/api/clips/{short1['id']}/download")
    expect(d.status_code == 409, "a blocked clip won't download")
    d = client.get(f"/api/clips/{short1['id']}/download?anyway=1")
    expect(d.status_code == 200 and len(d.content) > 1000, "…unless you say anyway")
    d = client.get(f"/api/clips/{vert1['id']}/download")
    expect(d.status_code == 200, "a ready clip downloads")

    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/jobs/{job_id}/download.zip").content))
    mp4s = [n for n in z.namelist() if n.endswith(".mp4")]
    ready = sum(1 for c in clips if c["compliance"]["status"] != "blocked")
    expect(len(mp4s) == ready, f"the zip holds only the {ready} clips not blocked")
    csv_text = client.get(f"/api/jobs/{job_id}/export.csv").text
    expect("#KetoneIQPartner" in csv_text and "campaign_check" in csv_text, "CSV carries the caption to paste")

    # a new hook of your own: re-rendered and checked again (no Claude here, so the tone check can't run)
    client.post(f"/api/clips/{vert1['id']}/render", json={"hook": "These nerds were way ahead", "hook_style": "bold"})
    again = client.get(f"/api/clips/{vert1['id']}").json()
    tone = next(c for c in again["compliance"]["checks"] if c["id"] == "tone")
    print("  after re-render:", again["compliance"]["status"], "| tone:", tone["status"], tone["detail"][:70])
    expect(again["hook"] == "These nerds were way ahead" and again["edits"]["hook_style"] == "bold", "hook and style changed")
    expect(again["compliance"]["status"] == "check" and tone["status"] == "warn",
           "a hook nobody could check is flagged, not passed")
    footage = next(c for c in again["compliance"]["checks"] if c["id"] == "footage")
    expect(footage["status"] == "pass", "the re-render still leaves the footage untouched")

    # run again: same files, a fresh job
    rr = client.post(f"/api/jobs/{job_id}/rerun", json={})
    expect(rr.status_code == 200, "Run again works for a clip-bank job")

    # ---- a clip-from-source campaign: what the rules switch off
    src_rb = campaign.normalize({**KETONE_ANSWER, "mode": "source",
                                 "full_clip": {"value": False, "quote": ""},
                                 "grey_areas": []}, brief)
    settings, notes = campaign.source_settings(src_rb, main._settings({}))
    print("  source-mode notes:", *notes, sep="\n    ")
    expect(not settings["tighten"] and not settings["motion"] and settings["layout"] == "blur"
           and not settings["structure"] and not settings["normalize_audio"], "the brief's bans reach the run settings")
    edits = campaign.clamp_edits({"tighten": True, "motion": True, "layout": "fill", "normalize_audio": True,
                                  "logo": True, "hook_on": True}, src_rb)
    expect(not edits["tighten"] and not edits["motion"] and edits["layout"] == "blur"
           and not edits["normalize_audio"] and not edits["logo"] and edits["hook_on"],
           "the editor can't switch a banned feature back on")

    store.delete_campaign(cid)

    # ---- a campaign whose brief requires the brand's own logo (Gamebred FC)
    from app import brandlogo
    from tests.brand_logo import GAMEBRED_ANSWER, GAMEBRED_BRIEF, make_logo
    g = campaign.normalize(GAMEBRED_ANSWER, GAMEBRED_BRIEF)
    gsaved = client.post("/api/campaigns", json={"brief": GAMEBRED_BRIEF, "rulebook": g, "edits": {"name": "Gamebred test"}}).json()
    gid = gsaved["id"]
    expect(gsaved["card"]["resolved"]["brand_logo"] == "required" and gsaved["logo"] is None,
           "saved: logo required, none added yet")
    vert = lambda: [("files", ("vert.mp4", open(folder / "vert.mp4", "rb"), "video/mp4"))]
    r = client.post(f"/api/campaigns/{gid}/jobs", files=vert(), data={"versions": "1", "platforms": "instagram"})
    expect(r.status_code == 400 and "logo" in r.text, "no run while a required logo is missing")
    r = client.post(f"/api/campaigns/{gid}/logo", files={"file": ("notes.txt", b"hello", "text/plain")})
    expect(r.status_code == 400, "a file that isn't an image is refused")
    logo_png = make_logo(folder / "api_logo.png")
    r = client.post(f"/api/campaigns/{gid}/logo", files={"file": ("gamebred logo.png", open(logo_png, "rb"), "image/png")})
    expect(r.status_code == 200 and r.json()["logo"] and r.json()["logo"]["url"].startswith("/media/campaign-logo/"),
           "logo uploaded")
    expect(client.get(r.json()["logo"]["url"]).status_code == 200, "the logo is served back for the card")
    r = client.post(f"/api/campaigns/{gid}/jobs", files=vert(), data={"versions": "1", "platforms": "instagram"})
    expect(r.status_code == 200, f"with the logo, the run goes ({r.status_code})")
    gjob = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    gclip = gjob["clips"][0]
    gate = gclip["compliance"]
    logo_check = next((c for c in gate["checks"] if c["id"] == "brand_logo"), {})
    print(f"  gamebred clip: {gate['status']} · {logo_check.get('detail', '')} · {gclip['framing'].get('note', '')}")
    expect(logo_check.get("status") == "pass" and gate["status"] != "blocked", "the clip shows the logo and isn't blocked")
    expect("@gamebredfightingchampionships" in gclip["post"]["text"], "the Instagram tag is in the caption")
    client.post(f"/api/clips/{gclip['id']}/render", json={"hook": "He did not see that coming"})
    again = client.get(f"/api/clips/{gclip['id']}").json()
    logo_check = next((c for c in again["compliance"]["checks"] if c["id"] == "brand_logo"), {})
    expect(logo_check.get("status") == "pass", "a re-rendered clip keeps the logo")
    r = client.delete(f"/api/campaigns/{gid}/logo").json()
    expect(r["logo"] is None and not brandlogo.exists(gid), "logo removed")
    client.post(f"/api/campaigns/{gid}/logo", files={"file": ("l.png", open(logo_png, "rb"), "image/png")})
    client.delete(f"/api/campaigns/{gid}")
    expect(not brandlogo.exists(gid), "deleting the campaign deletes its logo too")

    print("\nFAILED:" if FAILS else "\nall API checks behaved", FAILS or "")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    run(Path(sys.argv[1]), Path(sys.argv[2]))
