"""ClipAgent Studio — FastAPI app."""
from __future__ import annotations

import csv
import io
import json
import os
import queue
import re
import shutil
import threading
import tempfile
import time
import traceback
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import (brandlogo, campaign, captions, doctor, instruct, media, money, notify, overlay, pipeline, render,
               store, styles, transcribe)
from .config import (ANTHROPIC_API_KEY, BASE_DIR, CLIP_DIR, MAX_CLIPS, THUMB_DIR,
                     WHISPER_API_KEY, WORK_DIR)

app = FastAPI(title="ClipAgent Studio")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

store.init()
money.init()
LOGO_PATH = render.BRAND_DIR / "logo.png"


def _loads(text: Any, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def clip_json(clip: Dict[str, Any]) -> Dict[str, Any]:
    framing = json.loads(clip.get("framing") or "{}")
    parts = _loads(clip.get("parts"), [])
    length = sum(p["end"] - p["start"] for p in parts) if len(parts) > 1 else clip["end"] - clip["start"]
    return {
        "id": clip["id"],
        "job_id": clip["job_id"],
        "rank": clip["rank"],
        "start": clip["start"],
        "end": clip["end"],
        "duration": round(length, 2),
        "clip_type": clip.get("clip_type") or "",
        "parts": parts,
        "variant": clip.get("variant") or ("stitched" if len(parts) > 1 else "continuous"),
        "judge": _loads(clip.get("judge"), {}),
        "headline": clip.get("headline") or "",
        "alt_of": clip.get("alt_of") or "",
        "saved": round(clip.get("saved") or 0.0, 2),
        "score": clip["score"],
        "verdict": clip.get("verdict") or "",
        "title": clip["title"],
        "hook": clip["hook"],
        "caption": clip.get("caption") or "",
        "hashtags": json.loads(clip.get("hashtags") or "[]"),
        "reason": clip["reason"],
        "tags": json.loads(clip.get("tags") or "[]"),
        "words": json.loads(clip.get("words") or "[]"),
        "edits": json.loads(clip.get("edits") or "{}"),
        "framing": {"kind": framing.get("kind", ""), "note": framing.get("note", "")},
        "status": clip["status"],
        "video_url": f"/media/clip/{clip['id']}.mp4" if clip.get("file") else None,
        "thumb_url": f"/media/thumb/{clip['id']}.jpg" if clip.get("thumb") else None,
        "clean_url": f"/media/thumb/{clip['id']}_clean.jpg" if clip.get("thumb") else None,
        # campaign mode
        "compliance": _loads(clip.get("compliance"), None),
        "post": _loads(clip.get("post"), None),
        "overlay": bool(clip.get("source_path")),
        "doctor": _loads(clip.get("doctor"), None),
        "render_error": clip.get("render_error") or "",
        "can_undo": bool(clip.get("undo")),
    }


def _blocked(clip: Dict[str, Any]) -> bool:
    return (_loads(clip.get("compliance"), {}) or {}).get("status") == "blocked"


def _save_upload(upload: UploadFile) -> Path:
    """An upload to a temp file, with every handle closed before it's moved.

    tempfile.mkstemp() returns an open descriptor as well as a name. Leaving it
    open is harmless on Linux, but Windows refuses to move or delete a file that
    anything still holds open — every upload failed there with WinError 32."""
    fd, name = tempfile.mkstemp(suffix=Path(upload.filename or "").suffix)
    with os.fdopen(fd, "wb") as out:
        shutil.copyfileobj(upload.file, out)
    return Path(name)


def _platform_list(text: str) -> List[str]:
    return [p for p in (x.strip().lower() for x in (text or "").split(",")) if p in campaign.PLATFORMS]


def _campaign_settings(campaign_id: str, settings: Dict[str, Any], platforms: str = "") -> Dict[str, Any]:
    """A clip-from-source run under a campaign: the brief's rules applied to the
    ingest settings, and a copy of the rulebook kept with the job."""
    camp = store.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "That campaign doesn't exist any more")
    rb = camp["rulebook"]
    if rb.get("mode") == "overlay":
        raise HTTPException(400, "This is a clip-bank campaign — drop its clips on the campaign's own form")
    settings, notes = campaign.source_settings(rb, settings)
    settings["platforms"] = _platform_list(platforms)
    logo = _logo_for_run(campaign_id, rb)
    settings["brand_logo"] = str(logo) if logo else ""
    settings["campaign"] = {"id": campaign_id, "name": camp["name"], "mode": "source",
                            "rules": rb, "notes": notes, "logo": settings["brand_logo"]}
    return settings


def _logo_for_run(campaign_id: str, rb: Dict[str, Any]) -> Optional[Path]:
    """The brand's logo for a run — and no run at all when the brief requires
    a logo that hasn't been added yet: every clip would only be blocked."""
    has = brandlogo.exists(campaign_id)
    if campaign.needs_brand_logo(rb) and not has:
        raise HTTPException(400, "This brief requires the brand's logo on every post — add the logo file "
                                 "on the campaign first (Brand logo → Add logo).")
    return brandlogo.path_for(campaign_id) if has and campaign.wants_brand_logo(rb) else None


def _settings(form: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "max_clips": max(1, min(24, int(form.get("max_clips", MAX_CLIPS)))),
        "layout": form.get("layout", "auto"),
        "caption_style": form.get("caption_style", "impact"),
        "caption_position": form.get("caption_position", "bottom"),
        "tighten": bool(form.get("tighten", True)),
        "drop_fillers": bool(form.get("drop_fillers", True)),
        "max_gap": float(form.get("max_gap", 0.6)),
        "auto_frame": bool(form.get("auto_frame", True)),
        "motion": bool(form.get("motion", True)),
        "structure": bool(form.get("structure", True)),
        "alternates": bool(form.get("alternates", True)),
        "headline": bool(form.get("headline", True)),
        "auto_style": bool(form.get("auto_style", True)),
        "style_recipe": str(form.get("style_recipe") or "auto"),
        "doctor": bool(form.get("doctor", True)),
        "platforms": _platform_list(",".join(form["platforms"]) if isinstance(form.get("platforms"), list)
                                    else str(form.get("platforms") or "")),
        "accent": form.get("accent", ""),
        "logo": bool(form.get("logo", False)),
        "logo_corner": form.get("logo_corner", "top-right"),
    }


# --- pages ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8"))


@app.get("/api/config")
def config() -> Dict[str, Any]:
    return {
        "caption_styles": captions.style_catalogue(),
        "caption_positions": list(captions.POSITIONS.keys()),
        "caption_grouping": captions.grouping_rules(),
        "frame": {"w": 1080, "h": 1920},
        "layouts": render.layout_catalogue(),
        "logo_corners": ["top-left", "top-right", "bottom-left", "bottom-right"],
        "max_clips": MAX_CLIPS,
        "presets": store.list_presets(),
        "has_logo": LOGO_PATH.exists(),
        "keys": {"claude": bool(ANTHROPIC_API_KEY), "whisper": bool(WHISPER_API_KEY)},
        "telegram": {"on": notify.enabled(), "connected": notify.connected()},
        "recipes": [{"id": k, "name": r["name"], "what": r["what"], "when": r["when"],
                     "available": k in styles.available_recipes(1.0)}
                    for k, r in styles.RECIPES.items()],
        "campaign": {
            "modes": campaign.MODES,
            "platforms": campaign.PLATFORM_NAMES,
            "permissions": {k: v[0] for k, v in campaign.PERMISSIONS.items()},
            "hook_styles": overlay.HOOK_STYLES,
            "hook_positions": overlay.HOOK_POSITIONS,
            "hook_colors": overlay.NEON,
            "look": overlay.DEFAULT_LOOK,
        },
    }


# --- jobs -----------------------------------------------------------------

@app.post("/api/jobs")
async def create_job(
    background: BackgroundTasks,
    url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    max_clips: int = Form(MAX_CLIPS),
    layout: str = Form("auto"),
    caption_style: str = Form("impact"),
    caption_position: str = Form("bottom"),
    tighten: bool = Form(True),
    drop_fillers: bool = Form(True),
    auto_frame: bool = Form(True),
    motion: bool = Form(True),
    structure: bool = Form(True),
    alternates: bool = Form(True),
    headline: bool = Form(True),
    auto_style: bool = Form(True),
    style_recipe: str = Form("auto"),
    doctor: bool = Form(True),
    accent: str = Form(""),
    logo: bool = Form(False),
    logo_corner: str = Form("top-right"),
    campaign_id: str = Form(""),
    platforms: str = Form(""),
) -> Dict[str, Any]:
    if not url and not file:
        raise HTTPException(400, "Paste a video link or upload a file")
    if not WHISPER_API_KEY:
        raise HTTPException(400, "No transcription key set — add OPENAI_API_KEY to your .env")

    settings = _settings({
        "max_clips": max_clips, "layout": layout, "caption_style": caption_style,
        "caption_position": caption_position, "tighten": tighten,
        "drop_fillers": drop_fillers, "auto_frame": auto_frame, "motion": motion,
        "structure": structure, "alternates": alternates, "headline": headline,
        "auto_style": auto_style, "style_recipe": style_recipe, "doctor": doctor, "accent": accent,
        "platforms": platforms, "logo": logo, "logo_corner": logo_corner,
    })
    if campaign_id:
        settings = _campaign_settings(campaign_id, settings, platforms)
    title = (file.filename if file else url) or "Untitled"
    job_id = store.create_job(title=title, source=url or "upload", settings=settings)
    if campaign_id:
        store.update_job(job_id, campaign_id=campaign_id)

    upload_path = None
    if file:
        try:
            tmp = _save_upload(file)
            upload_path = media.store_upload(tmp, file.filename, job_id)
        except (RuntimeError, OSError) as exc:
            store.update_job(job_id, status="failed", stage="Failed", error=str(exc)[:300])
            raise HTTPException(400, f"Couldn't take that file: {exc}"[:300])

    background.add_task(pipeline.run_job, job_id, url, upload_path)
    return {"job_id": job_id}


@app.post("/api/batch")
def create_batch(background: BackgroundTasks, body: Dict[str, Any]) -> Dict[str, Any]:
    """Queue a list of links. They run one after another, not all at once."""
    urls = [u.strip() for u in (body.get("urls") or []) if u and u.strip()]
    if not urls:
        raise HTTPException(400, "No links in the batch")
    if len(urls) > 25:
        raise HTTPException(400, "25 links at a time is the limit")
    if not WHISPER_API_KEY:
        raise HTTPException(400, "No transcription key set — add OPENAI_API_KEY to your .env")

    settings = _settings(body.get("settings") or {})
    camp_id = str(body.get("campaign_id") or "")
    if camp_id:                 # several videos for one campaign: each follows its brief
        settings = _campaign_settings(camp_id, settings, ",".join(settings.get("platforms") or []))
    batch_id = store.new_id()
    job_ids = []
    for url in urls:
        job_id = store.create_job(title=url, source=url, settings=settings)
        store.update_job(job_id, batch_id=batch_id, stage="Waiting in the queue",
                         **({"campaign_id": camp_id} if camp_id else {}))
        job_ids.append(job_id)

    def run_all():
        for job_id, url in zip(job_ids, urls):
            pipeline.run_job(job_id, url, None)

    background.add_task(run_all)
    return {"batch_id": batch_id, "job_ids": job_ids}


@app.get("/api/batch/{batch_id}")
def batch_detail(batch_id: str) -> Dict[str, Any]:
    jobs = store.jobs_in_batch(batch_id)
    if not jobs:
        raise HTTPException(404, "Batch not found")
    return {"batch_id": batch_id, "jobs": jobs}


@app.get("/api/jobs")
def jobs() -> Dict[str, Any]:
    return {"jobs": store.list_jobs()}


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[[0-9;]+m")


def friendly_error(text: str) -> str:
    """A run's error in words a person can act on. The raw text stays in the
    job for the log; the UI shows this."""
    t = _ANSI.sub("", text or "").strip()
    if not t:
        return ""
    low = t.lower()
    if low.startswith("error:") or "[youtube]" in low or "[generic]" in low or "unsupported url" in low \
            or "unable to download" in low or "sign in to confirm" in low:
        return media.explain_download_error(t)
    if "error code: 413" in low or "request entity too large" in low:
        return "The audio was too big to send for transcription in one go. Press Try again."
    if "error code: 401" in low or "invalid x-api-key" in low or "incorrect api key" in low:
        return "The Claude or transcription key was refused — check the keys in the .env file."
    if "error code: 429" in low or "rate limit" in low:
        return "Claude or transcription is busy right now. Wait a minute and press Try again."
    if "error code: 529" in low or "overloaded" in low:
        return "Claude is overloaded right now. Wait a minute and press Try again."
    if re.search(r"error code: (4\d\d|5\d\d)", low):
        return "Claude or transcription turned down a request. Press Try again — if it keeps failing, the log has the details."
    if "connection error" in low or "timed out" in low:
        return "Lost the connection to Claude or transcription. Check the internet and press Try again."
    if re.search(r"(object has no attribute|traceback|keyerror|typeerror|indexerror|valueerror)", low):
        return "Something went wrong inside ClipAgent while making this. Press Try again."
    return t[:300]


@app.get("/api/videos")
def videos(limit: int = 60) -> Dict[str, Any]:
    """Every run, newest first, with what a library card needs: a poster,
    how many clips are ready, and the campaign it was for."""
    names = {c["id"]: c["name"] for c in store.list_campaigns()}
    out = []
    for j in store.list_jobs(max(1, min(200, limit))):
        full = store.get_job(j["id"]) or {}
        clips = store.list_clips(j["id"])
        main = [c for c in clips if not c.get("alt_of")]
        ready = [c for c in main if c.get("status") == "ready" and c.get("file")]
        poster = next((c for c in ready if c.get("thumb")), None)
        camp_id = full.get("campaign_id") or ""
        out.append({
            "id": j["id"], "title": j["title"], "status": j["status"], "stage": j["stage"],
            "progress": j["progress"], "duration": j["duration"], "created_at": j["created_at"],
            "error": friendly_error(full.get("error") or ""), "clips": len(ready), "total": len(main),
            "source": full.get("source") or "",
            "poster": f"/media/thumb/{poster['id']}.jpg" if poster else None,
            "campaign": {"id": camp_id, "name": names.get(camp_id, "")} if camp_id else None,
        })
    return {"videos": out}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str) -> Dict[str, Any]:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    framing = json.loads(job.get("framing") or "{}")
    camp = json.loads(job.get("settings") or "{}").get("campaign") or {}
    return {
        "id": job["id"],
        "title": job["title"],
        "status": job["status"],
        "stage": job["stage"],
        "progress": job["progress"],
        "duration": job["duration"],
        "error": friendly_error(job["error"]),
        "error_detail": _ANSI.sub("", job["error"] or "")[:2000],
        "framing": framing.get("note", ""),
        "created_at": job.get("created_at"),
        "source": job.get("source") or "",
        "platforms": json.loads(job.get("settings") or "{}").get("platforms") or [],
        "can_retry": bool(job.get("source_path")) and Path(job["source_path"]).exists(),
        "can_refetch": str(job.get("source") or "").startswith("http"),
        "campaign": {"id": camp.get("id"), "name": camp.get("name"), "mode": camp.get("mode"),
                     "notes": camp.get("notes", [])} if camp else None,
        "clips": [clip_json(c) for c in store.list_clips(job_id)],
    }


@app.post("/api/jobs/{job_id}/rerun")
def rerun_job(job_id: str, background: BackgroundTasks,
              body: Optional[Dict[str, Any]] = Body(None)) -> Dict[str, Any]:
    """Run a video again from the copy already on disk: fresh picks, structure,
    judging and renders, without downloading or transcribing it again."""
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    source = Path(job.get("source_path") or "")
    saved = json.loads(job.get("settings") or "{}")
    camp = saved.get("campaign") or {}
    url = job.get("source") if str(job.get("source") or "").startswith("http") else None
    have_copy = bool(job.get("source_path")) and source.exists()
    if not have_copy and not url and camp.get("mode") != "overlay":
        raise HTTPException(400, "The video is no longer on this PC — paste the link or drop the file in again")
    if camp.get("mode") == "overlay":
        # Same clip-bank files, the campaign's current rules.
        files = [f for f in saved.get("files", []) if Path(f).exists()]
        if not files:
            raise HTTPException(400, "The clip-bank files are no longer on disk — drop them in again")
        current = store.get_campaign(camp.get("id") or "")
        if current:
            _logo_for_run(current["id"], current["rulebook"])        # a required logo must be there
        settings = {**saved, "files": files, "links": [],
                    "campaign": {**camp, "rules": current["rulebook"] if current else camp.get("rules")}}
        new_id = store.create_job(title=job["title"], source="campaign", settings=settings)
        store.update_job(new_id, campaign_id=camp.get("id") or "")
        background.add_task(pipeline.run_overlay_job, new_id)
        return {"job_id": new_id}
    settings = _settings({**saved, **((body or {}).get("settings") or {})})
    if camp.get("id"):
        if store.get_campaign(camp["id"]):
            settings = _campaign_settings(camp["id"], settings, ",".join(saved.get("platforms") or []))
        else:                                   # campaign deleted since: the copy kept with the job
            rules = camp.get("rules") or {}
            settings, notes = campaign.source_settings(rules, settings)
            settings["platforms"] = saved.get("platforms") or []
            settings["campaign"] = {**camp, "notes": notes}
    title = job["title"] if have_copy or not str(job["title"] or "").startswith("http") else url
    new_id = store.create_job(title=title, source=job.get("source") or "upload", settings=settings)
    if camp.get("id"):
        store.update_job(new_id, campaign_id=camp["id"])
    if have_copy:
        background.add_task(pipeline.run_job, new_id, None, source)
    else:                                       # the download itself failed: fetch it again
        background.add_task(pipeline.run_job, new_id, url, None)
    return {"job_id": new_id}


# --- exports --------------------------------------------------------------

@app.get("/api/jobs/{job_id}/export.csv")
def export_csv(job_id: str) -> PlainTextResponse:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["rank", "score", "verdict", "file", "start", "end", "length_s",
                     "title", "hook", "caption", "hashtags", "why",
                     "campaign_check", "post_on", "caption_to_paste"])
    for clip in store.list_clips(job_id):
        gate = _loads(clip.get("compliance"), {}) or {}
        post = _loads(clip.get("post"), {}) or {}
        writer.writerow([
            clip["rank"], clip["score"], clip.get("verdict") or "",
            f"{clip['rank']:02d}_{media.safe_name(clip['title'])}.mp4",
            round(clip["start"], 2), round(clip["end"], 2),
            round(clip_json(clip)["duration"] - (clip.get("saved") or 0), 2),
            clip["title"], clip["hook"], clip.get("caption") or "",
            " ".join("#" + t for t in json.loads(clip.get("hashtags") or "[]")),
            clip["reason"],
            gate.get("status", ""), campaign.PLATFORM_NAMES.get(post.get("platform", ""), ""),
            post.get("text", ""),
        ])
    name = media.safe_name(job["title"] or "clips")
    return PlainTextResponse(
        buffer.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}_clips.csv"'},
    )


@app.get("/api/jobs/{job_id}/download.zip")
def download_all(job_id: str) -> FileResponse:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    # Clips the campaign gate blocked stay out of the bundle.
    clips = [c for c in store.list_clips(job_id)
             if c.get("file") and Path(c["file"]).exists() and not _blocked(c)]
    if not clips:
        raise HTTPException(404, "Nothing rendered yet — or every clip was blocked by the campaign check")

    name = media.safe_name(job["title"] or "clips")
    bundle = WORK_DIR / f"{job_id}_clips.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_STORED) as zf:
        for clip in clips:
            zf.write(clip["file"], f"{clip['rank']:02d}_{media.safe_name(clip['title'])}.mp4")
        zf.writestr(f"{name}_clips.csv", export_csv(job_id).body.decode("utf-8"))
    return FileResponse(bundle, media_type="application/zip", filename=f"{name}_clips.zip")


# --- clip editing ---------------------------------------------------------

@app.post("/api/clips/{clip_id}/render")
def rerender(clip_id: str, edits: Dict[str, Any], background: BackgroundTasks) -> Dict[str, Any]:
    """Kick off a re-render and return at once.

    Rendering a minute of video takes long enough that holding the request
    open invites a proxy timeout, so the editor polls the clip instead.
    """
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")

    had_file = bool(clip.get("file")) and Path(clip["file"]).exists()

    def work():
        instruct.snapshot(clip_id)       # the version before, so this edit can be undone
        try:
            pipeline.rerender_clip(clip_id, edits or {})
        except Exception as exc:
            traceback.print_exc()
            # The clip that was there still works: keep it, and say why the edit didn't take.
            if had_file and Path(clip["file"]).exists():
                store.update_clip(clip_id, status="ready", undo="",
                                  render_error=str(exc)[:300] or "The re-render failed")
            else:
                store.update_clip(clip_id, status="failed", render_error=str(exc)[:300] or "The re-render failed")
            return
        instruct.sync_hook(clip_id)
        doctor.recheck(clip_id)          # the old report described the clip before your edit

    store.update_clip(clip_id, status="rendering", render_error="")
    background.add_task(work)
    return {"id": clip_id, "status": "rendering"}


@app.post("/api/jobs/{job_id}/ask")
def ask_for_changes(job_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """'Clip 2: end it after the punchline' — read it, start re-making, say what was understood."""
    try:
        return instruct.request(job_id, str(body.get("text") or ""), clip_ids=body.get("clip_ids") or None,
                                focus_id=str(body.get("focus") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _ask_json(r: Dict[str, Any]) -> Dict[str, Any]:
    reply = dict(r.get("reply") or {})
    if reply.get("error"):
        reply["error"] = friendly_error(reply["error"])
    return {"id": r["id"], "text": r["text"], "status": r["status"], "created_at": r["created_at"], **reply}


@app.get("/api/jobs/{job_id}/asks")
def asks(job_id: str) -> Dict[str, Any]:
    return {"asks": [_ask_json(r) for r in store.list_requests(job_id)]}


@app.get("/api/asks/{req_id}")
def ask_status(req_id: str) -> Dict[str, Any]:
    r = store.get_request(req_id)
    if not r:
        raise HTTPException(404, "Request not found")
    return _ask_json(r)


@app.post("/api/clips/{clip_id}/undo")
def undo_clip(clip_id: str) -> Dict[str, Any]:
    try:
        return clip_json(instruct.undo(clip_id))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/clips/{clip_id}/text")
def clip_text(clip_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Save the caption and hashtags you edited in the editor's Post tab."""
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    fields: Dict[str, Any] = {}
    if "caption" in body:
        fields["caption"] = str(body.get("caption") or "")[:2200]
    if "hashtags" in body:
        raw = body.get("hashtags") or []
        tags = raw.split() if isinstance(raw, str) else [str(t) for t in raw]
        fields["hashtags"] = json.dumps([t.strip().lstrip("#") for t in tags if t.strip().lstrip("#")][:30])
    store.update_clip(clip_id, **fields)
    return clip_json(store.get_clip(clip_id))


@app.get("/api/clips/{clip_id}")
def clip_detail(clip_id: str) -> Dict[str, Any]:
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    return clip_json(clip)


@app.get("/api/clips/{clip_id}/waveform")
def clip_waveform(clip_id: str, pad: float = 8.0) -> Dict[str, Any]:
    """Loudness and words around this clip, so the editor can show a timeline.

    The span is padded either side of the current cut, which is what makes it
    possible to drag the start earlier than Claude put it.
    """
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    job = store.get_job(clip["job_id"])
    if not job or not job.get("source_path") or not Path(job["source_path"]).exists():
        return {"start": clip["start"], "end": clip["end"], "points": [], "words": [], "missing": True}

    source = Path(job["source_path"])
    duration = job.get("duration") or media.probe(source)["duration"]
    start = max(0.0, clip["start"] - pad)
    end = min(duration, clip["end"] + pad)

    transcript = json.loads(job.get("transcript") or "{}")
    words = [w for w in transcribe.in_order(transcript.get("words") or []) if w["end"] > start and w["start"] < end]
    return {
        "start": round(start, 2),
        "end": round(end, 2),
        "clip_start": clip["start"],
        "clip_end": clip["end"],
        "points": media.waveform(source, start, end),
        "words": words[:900],
    }


@app.get("/api/clips/{clip_id}/download")
def download(clip_id: str, anyway: bool = False) -> FileResponse:
    clip = store.get_clip(clip_id)
    if not clip or not clip.get("file"):
        raise HTTPException(404, "Clip not rendered")
    if _blocked(clip) and not anyway:
        summary = (_loads(clip.get("compliance"), {}) or {}).get("summary", "")
        raise HTTPException(409, f"The campaign check blocked this clip. {summary}")
    safe = media.safe_name(clip["title"] or "clip")
    return FileResponse(clip["file"], media_type="video/mp4",
                        filename=f"{clip['rank']:02d}_{safe}.mp4")


# --- campaigns ------------------------------------------------------------

def _campaign_json(camp: Dict[str, Any]) -> Dict[str, Any]:
    rb = camp["rulebook"]
    logo = None
    if brandlogo.exists(camp["id"]):
        path = brandlogo.path_for(camp["id"])
        logo = {"url": f"/media/campaign-logo/{camp['id']}.png?v={int(path.stat().st_mtime)}"}
    return {"id": camp["id"], "name": camp["name"], "mode": camp["mode"], "brief": camp.get("brief", ""),
            "rulebook": rb, "card": campaign.card(rb), "logo": logo}


@app.post("/api/campaigns/read")
def read_campaign(body: Dict[str, Any]) -> Dict[str, Any]:
    """Claude reads a pasted brief. Nothing is saved until you check it and save."""
    brief = (body.get("brief") or "").strip()
    if not ANTHROPIC_API_KEY:
        raise HTTPException(400, "Reading a brief needs Claude — add ANTHROPIC_API_KEY to your .env")
    try:
        rb = campaign.read_brief(brief)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Claude couldn't read the brief: {exc}"[:300])
    return {"brief": brief, "rulebook": rb, "card": campaign.card(rb)}


@app.post("/api/campaigns/preview")
def preview_campaign(body: Dict[str, Any]) -> Dict[str, Any]:
    """The rulebook card with unsaved changes applied, so toggles update live."""
    rb = campaign.merge_user_edits(body.get("rulebook") or {}, body.get("edits") or {}, body.get("brief") or "")
    return {"rulebook": rb, "card": campaign.card(rb)}


@app.post("/api/campaigns")
def save_campaign(body: Dict[str, Any]) -> Dict[str, Any]:
    brief = (body.get("brief") or "").strip()
    draft = body.get("rulebook") or {}
    if not draft.get("perms"):
        raise HTTPException(400, "Read the brief first")
    rb = campaign.merge_user_edits(draft, body.get("edits") or {}, brief)
    campaign_id = store.save_campaign(rb["name"], rb["mode"], brief, rb, body.get("id") or None)
    return _campaign_json(store.get_campaign(campaign_id))


@app.get("/api/campaigns")
def campaigns() -> Dict[str, Any]:
    """The campaigns, each with how much it has produced and earned."""
    camps = store.list_campaigns()
    by_camp: Dict[str, Dict[str, Any]] = {}
    for post in money.list_posts(2000, "posted"):
        s = by_camp.setdefault(post.get("campaign_id") or "", {"posts": 0, "views": 0, "earned": 0.0})
        s["posts"] += 1
        s["views"] += int(post.get("views") or 0)
        s["earned"] += money.estimate(post)
    videos: Dict[str, int] = {}
    for j in store.list_jobs(500):
        full = store.get_job(j["id"]) or {}
        if full.get("campaign_id") and j["status"] == "done":
            videos[full["campaign_id"]] = videos.get(full["campaign_id"], 0) + 1
    for c in camps:
        s = by_camp.get(c["id"], {"posts": 0, "views": 0, "earned": 0.0})
        c["stats"] = {"videos": videos.get(c["id"], 0), "posts": s["posts"], "views": s["views"],
                      "earned": round(s["earned"], 2)}
    return {"campaigns": camps}


@app.get("/api/campaigns/{campaign_id}")
def campaign_detail(campaign_id: str) -> Dict[str, Any]:
    camp = store.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "Campaign not found")
    return _campaign_json(camp)


@app.put("/api/campaigns/{campaign_id}")
def update_campaign(campaign_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    camp = store.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "Campaign not found")
    rb = campaign.merge_user_edits(camp["rulebook"], body.get("edits") or {}, camp.get("brief") or "")
    store.save_campaign(rb["name"], rb["mode"], camp.get("brief") or "", rb, campaign_id)
    return _campaign_json(store.get_campaign(campaign_id))


@app.delete("/api/campaigns/{campaign_id}")
def remove_campaign(campaign_id: str) -> Dict[str, Any]:
    store.delete_campaign(campaign_id)
    brandlogo.remove(campaign_id)
    return {"campaigns": store.list_campaigns()}


@app.post("/api/campaigns/{campaign_id}/logo")
async def campaign_logo(campaign_id: str, file: UploadFile = File(...)) -> Dict[str, Any]:
    """The brand's logo for this campaign — the file its brief hands out."""
    camp = store.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "Campaign not found")
    if Path(file.filename or "").suffix.lower() not in brandlogo.EXTENSIONS:
        raise HTTPException(400, "Use the logo as a PNG (transparent background works best), WEBP or JPG")
    tmp = _save_upload(file)
    try:
        brandlogo.save(campaign_id, tmp)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        tmp.unlink(missing_ok=True)
    return _campaign_json(camp)


@app.delete("/api/campaigns/{campaign_id}/logo")
def remove_campaign_logo(campaign_id: str) -> Dict[str, Any]:
    camp = store.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "Campaign not found")
    brandlogo.remove(campaign_id)
    return _campaign_json(camp)


@app.get("/media/campaign-logo/{name}")
def campaign_logo_file(name: str) -> FileResponse:
    path = brandlogo.path_for(Path(name).stem)
    if not path.exists():
        raise HTTPException(404, "No logo")
    return FileResponse(path, media_type="image/png")


@app.post("/api/campaigns/{campaign_id}/jobs")
async def campaign_job(
    campaign_id: str,
    background: BackgroundTasks,
    files: Optional[List[UploadFile]] = File(None),
    links: str = Form(""),
    versions: int = Form(1),
    hook_style: str = Form("neon"),
    hook_color: str = Form("pink"),
    hook_position: str = Form("auto"),
    hook_hold: str = Form("whole"),
    bars: str = Form("black"),
    platforms: str = Form(""),
) -> Dict[str, Any]:
    """A clip-bank campaign run: every file posted whole, a hook on top, each checked."""
    camp = store.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "Campaign not found")
    rb = camp["rulebook"]
    if rb.get("mode") != "overlay":
        raise HTTPException(400, "This campaign cuts clips from longer footage — use the video link form")
    urls = [u.strip() for u in (links or "").splitlines() if u.strip().startswith("http")]
    files = [f for f in (files or []) if f and f.filename]
    if not files and not urls:
        raise HTTPException(400, "Drop in the campaign's clips, or paste links to them")
    if len(files) + len(urls) > 40:
        raise HTTPException(400, "40 clips at a time is the limit")
    logo = _logo_for_run(campaign_id, rb)
    settings = {
        "campaign": {"id": campaign_id, "name": camp["name"], "mode": "overlay", "rules": rb,
                     "logo": str(logo) if logo else ""},
        "versions": max(1, min(4, int(versions or 1))), "links": urls,
        "hook_style": hook_style if hook_style in overlay.HOOK_STYLES else "neon",
        "hook_color": hook_color if hook_color in overlay.NEON else "pink",
        "hook_position": hook_position if hook_position in overlay.HOOK_POSITIONS else "auto",
        "hook_hold": hook_hold if hook_hold in ("whole", "3") else "whole",
        "bars": bars if bars in ("black", "blur") else "black",
        "platforms": _platform_list(platforms),
    }
    count = len(files) + len(urls)
    job_id = store.create_job(title=f"{camp['name']} — {count} clip{'s' if count != 1 else ''}",
                              source="campaign", settings=settings)
    store.update_job(job_id, campaign_id=campaign_id)
    stored, names = [], []
    for i, f in enumerate(files):
        tmp = None
        try:
            tmp = _save_upload(f)
            stored.append(str(media.store_upload(tmp, f.filename, job_id, stem=f"clip_{i:02d}")))
            names.append(f.filename)
        except (RuntimeError, OSError) as exc:
            if tmp:
                tmp.unlink(missing_ok=True)
            store.update_job(job_id, status="failed", stage="Failed", error=f"{f.filename}: {exc}"[:300])
            raise HTTPException(400, f"{f.filename}: {exc}"[:300])
    settings.update(files=stored, file_names=names)
    store.update_job(job_id, settings=json.dumps(settings))
    background.add_task(pipeline.run_overlay_job, job_id)
    return {"job_id": job_id}


# --- presets and brand kit ------------------------------------------------

@app.get("/api/presets")
def presets() -> Dict[str, Any]:
    return {"presets": store.list_presets()}


@app.post("/api/presets")
def save_preset(body: Dict[str, Any]) -> Dict[str, Any]:
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Give the preset a name")
    store.save_preset(name, body.get("settings") or {})
    return {"presets": store.list_presets()}


@app.delete("/api/presets/{name}")
def remove_preset(name: str) -> Dict[str, Any]:
    store.delete_preset(name)
    return {"presets": store.list_presets()}


@app.post("/api/brand/logo")
async def upload_logo(file: UploadFile = File(...)) -> Dict[str, Any]:
    if Path(file.filename).suffix.lower() not in {".png", ".webp"}:
        raise HTTPException(400, "Use a PNG or WEBP with a transparent background")
    with open(LOGO_PATH, "wb") as out:
        shutil.copyfileobj(file.file, out)
    return {"has_logo": True}


@app.delete("/api/brand/logo")
def delete_logo() -> Dict[str, Any]:
    LOGO_PATH.unlink(missing_ok=True)
    return {"has_logo": False}


@app.get("/media/logo.png")
def logo_file() -> FileResponse:
    if not LOGO_PATH.exists():
        raise HTTPException(404, "No logo")
    return FileResponse(LOGO_PATH, media_type="image/png")


# --- media ----------------------------------------------------------------

@app.get("/media/clip/{name}")
def clip_file(name: str) -> FileResponse:
    path = CLIP_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404, "Not rendered yet")
    return FileResponse(path, media_type="video/mp4")


@app.get("/media/thumb/{name}")
def thumb_file(name: str) -> FileResponse:
    path = THUMB_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404, "No thumbnail")
    return FileResponse(path, media_type="image/jpeg")


# --- money: posts, earnings, planner, watched channels ---------------------------------

def _post_json(p: Dict[str, Any]) -> Dict[str, Any]:
    return {**p, "earned": money.estimate(p)}


@app.get("/api/money")
def money_overview() -> Dict[str, Any]:
    cfg = money.settings()
    return {"dashboard": money.dashboard(), "posts": [_post_json(p) for p in money.list_posts(300)],
            "accounts": cfg["accounts"], "per_day": cfg["per_day"], "watch": cfg["watch"],
            "campaigns": [{"id": c["id"], "name": c["name"]} for c in store.list_campaigns()]}


@app.post("/api/posts")
def add_post(body: Dict[str, Any]) -> Dict[str, Any]:
    url = (body.get("url") or "").strip()
    if not url and not body.get("clip_id"):
        raise HTTPException(400, "Paste the post's link")
    post = money.add(body.get("clip_id") or "", body.get("platform") or "", body.get("account") or "", url,
                     rate=body.get("rate"))
    if url:
        threading.Thread(target=money.check, args=(post,), daemon=True).start()
    return _post_json(post)


@app.post("/api/posts/{post_id}")
def edit_post(post_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    post = money.get(post_id)
    if not post:
        raise HTTPException(404, "No such post")
    if body.get("url") and post["status"] != "posted":
        post = money.mark_posted(post_id, body["url"])
    fields = {k: body[k] for k in ("rate", "paid", "account", "platform", "status", "url") if k in body}
    if "views" in body:
        fields["views"] = int(body["views"] or 0)
        fields["history"] = (post.get("history") or []) + [[round(time.time()), fields["views"]]]
        fields["checked_at"] = time.time()
    post = money.update(post_id, **fields)
    return _post_json(post)


@app.post("/api/posts/{post_id}/check")
def check_post(post_id: str) -> Dict[str, Any]:
    post = money.get(post_id)
    if not post:
        raise HTTPException(404, "No such post")
    return _post_json(money.check(post) or money.get(post_id))


@app.delete("/api/posts/{post_id}")
def remove_post(post_id: str) -> Dict[str, Any]:
    money.delete(post_id)
    return {"ok": True}


@app.post("/api/money/settings")
def money_settings(body: Dict[str, Any]) -> Dict[str, Any]:
    accounts = body.get("accounts")
    if accounts is not None:
        accounts = {k: ["@" + a.strip().lstrip("@") for a in v if a.strip()]
                    for k, v in accounts.items() if k in ("tiktok", "instagram", "youtube")}
    return money.save_settings({"accounts": accounts, "per_day": body.get("per_day")})


@app.post("/api/jobs/{job_id}/plan")
def plan_job_posts(job_id: str) -> Dict[str, Any]:
    if not any(money.settings()["accounts"].values()):
        raise HTTPException(400, "Add the accounts you post to first (Money tab)")
    made = money.plan_job(job_id)
    return {"planned": [_post_json(p) for p in made]}


@app.post("/api/watch")
def watch(body: Dict[str, Any]) -> Dict[str, Any]:
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "Paste a channel link")
    return money.watch_channel(url, body.get("campaign_id") or "")


@app.post("/api/unwatch")
def unwatch(body: Dict[str, Any]) -> Dict[str, Any]:
    return money.unwatch_channel(body.get("url") or "")


# --- Telegram ---------------------------------------------------------------------
# Links sent to the bot run one after another, like a batch.

URL_RE = re.compile(r"https?://\S+")
_tg_jobs: "queue.Queue[tuple]" = queue.Queue()
_tg_worker_started = False
_tg_pending = 0                 # links sent from Telegram that haven't finished
_tg_lock = threading.Lock()

TG_HELP = ("<b>What I can do</b>\n"
           "• Send a video link → I clip it and send you the clips.\n"
           "• Add a number for how many clips: <code>link 6</code>\n"
           "• Add a campaign's name to clip it for that campaign: <code>link lovable</code>\n"
           "• Add where it's going for the right length: <code>link shorts</code>, <code>tiktok</code>, "
           "<code>reels</code>\n"
           "/status — what's running\n"
           "/plan — schedule the last video's clips across your accounts\n"
           "/posted 12 link — post #12 is live (or just <code>/posted link</code>)\n"
           "/money — views and earnings · /summary — the morning summary now\n"
           "/views 12 34k — type the views for post #12 (Instagram needs this)\n"
           "/accounts tiktok @you @alt — set the accounts you post to\n"
           "/watch channel-link — tell me when it uploads · /unwatch channel-link\n"
           "/campaigns — your campaigns\n"
           "/clips — send the last video's clips again\n"
           "/change 2 end it right after the punchline — change the last video's clips in your own words "
           "(<code>/change all bigger captions</code>)\n"
           "/retry — try the last failed video again")


def _tg_worker() -> None:
    global _tg_pending
    while True:
        job_id, url, source = _tg_jobs.get()
        try:
            if source:
                pipeline.run_job(job_id, None, Path(source))
            else:
                pipeline.run_job(job_id, url, None)
        except Exception:
            traceback.print_exc()
        finally:
            with _tg_lock:
                _tg_pending -= 1


def _tg_enqueue(job_id: str, url: Optional[str], source: Optional[str] = None) -> int:
    """Queue a run; returns its place in line (1 = starting now)."""
    global _tg_worker_started, _tg_pending
    with _tg_lock:
        if not _tg_worker_started:
            _tg_worker_started = True
            threading.Thread(target=_tg_worker, name="telegram-jobs", daemon=True).start()
        _tg_pending += 1
        place = _tg_pending
    store.update_job(job_id, stage="Waiting in the queue" if place > 1 else "Queued")
    _tg_jobs.put((job_id, url, source))
    return place


def _tg_campaign(words: List[str]) -> Optional[Dict[str, Any]]:
    names = [w.lower() for w in words if len(w) > 2]
    for camp in store.list_campaigns():
        name = (camp.get("name") or "").lower()
        if names and any(w in name for w in names):
            return camp
    return None


def _tg_start_links(text: str) -> str:
    urls = URL_RE.findall(text)[:5]
    rest = URL_RE.sub(" ", text).split()
    count = next((int(w) for w in rest if w.isdigit()), MAX_CLIPS)
    words = [w for w in rest if not w.isdigit()]
    platform_words = {"shorts": "youtube", "youtube": "youtube", "yt": "youtube", "tiktok": "tiktok",
                      "tt": "tiktok", "reels": "instagram", "instagram": "instagram", "ig": "instagram"}
    platforms = list(dict.fromkeys(platform_words[w.lower()] for w in words if w.lower() in platform_words))
    words = [w for w in words if w.lower() not in platform_words]
    camp = _tg_campaign(words) if words else None
    if not WHISPER_API_KEY:
        return "⚠️ No transcription key in .env, so I can't clip anything yet."
    settings = _settings({"max_clips": count})
    settings["platforms"] = platforms
    note = ""
    if camp:
        try:
            settings = _campaign_settings(camp["id"], settings, ",".join(platforms))
            note = f" for <b>{notify.esc(camp['name'])}</b>"
        except HTTPException as exc:
            return f"⚠️ {notify.esc(exc.detail)}"
    lines = []
    for url in urls:
        job_id = store.create_job(title=url, source=url, settings=settings)
        if camp:
            store.update_job(job_id, campaign_id=camp["id"])
        ahead = _tg_enqueue(job_id, url)
        where = "starting now" if ahead <= 1 else f"#{ahead} in line"
        lines.append(f"👍 Got it{note} — {settings['max_clips']} clips, {where}.\n{notify.esc(url)}")
    if words and not camp:
        lines.append("<i>(No campaign matched those words, so it's a normal run.)</i>")
    lines.append("I'll message you when the clips are ready.")
    return "\n".join(lines)


def _tg_status() -> str:
    jobs = store.list_jobs(10)
    active = [j for j in jobs if j["status"] in ("running", "queued")]
    lines = []
    if active:
        lines.append("<b>Working on</b>")
        for j in active:
            lines.append(f"• {notify.esc((j['title'] or '')[:60])} — {notify.esc(j['stage'])} "
                         f"({int(j['progress'] or 0)}%)")
    else:
        lines.append("Nothing running right now.")
    done = [j for j in jobs if j["status"] in ("done", "failed")][:3]
    if done:
        lines.append("\n<b>Last finished</b>")
        for j in done:
            icon = "✅" if j["status"] == "done" else "❌"
            lines.append(f"{icon} {notify.esc((j['title'] or '')[:60])}")
    return "\n".join(lines)


def _tg_retry() -> str:
    failed = next((j for j in store.list_jobs(20) if j["status"] == "failed"), None)
    if not failed:
        return "No failed videos to retry."
    job = store.get_job(failed["id"]) or {}
    settings = json.loads(job.get("settings") or "{}")
    source = job.get("source_path") or ""
    url = job.get("source") if job.get("source") not in (None, "", "upload", "campaign") else None
    if (settings.get("campaign") or {}).get("mode") == "overlay":
        return "That one is a clip-bank run — retry it from ClipAgent."
    if source and Path(source).exists():
        new_id = store.create_job(title=job["title"], source=job.get("source") or "upload", settings=settings)
        _tg_enqueue(new_id, None, source)
    elif url:
        new_id = store.create_job(title=url, source=url, settings=settings)
        _tg_enqueue(new_id, url)
    else:
        return "I don't have that video any more — send the link again."
    if job.get("campaign_id"):
        store.update_job(new_id, campaign_id=job["campaign_id"])
    return f"🔁 Trying <b>{notify.esc((job.get('title') or '')[:80])}</b> again."


def telegram_command(text: str) -> Optional[str]:
    low = text.strip().lower()
    if URL_RE.search(text) and not low.startswith("/"):
        return _tg_start_links(text)
    if low.startswith("/start"):
        return "✅ Already connected. " + TG_HELP
    if low.startswith("/status"):
        return _tg_status()
    if low.startswith("/campaigns"):
        camps = store.list_campaigns()
        if not camps:
            return "No campaigns yet — add one in ClipAgent's Campaigns tab."
        return "<b>Your campaigns</b>\n" + "\n".join(
            f"• {notify.esc(c['name'])}" for c in camps) + "\n\nSend <code>link name</code> to clip for one."
    if low.startswith("/clips"):
        last = next((j for j in store.list_jobs(20) if j["status"] == "done"), None)
        if not last:
            return "No finished videos yet."
        notify.job_finished(last["id"])
        return None
    if low.startswith("/retry"):
        return _tg_retry()
    if low.startswith("/change") or low.startswith("/edit"):
        return _tg_change(text)
    if low.startswith("/plan"):
        return _tg_plan()
    if low.startswith("/posted"):
        return _tg_posted(text)
    if low.startswith("/views") and len(re.findall(r"\d[\d,.]*[km]?", low)) >= 2:
        return _tg_set_views(low)
    if low.startswith("/money") or low.startswith("/views"):
        return _tg_money()
    if low.startswith("/summary"):
        return money.summary_text()
    if low.startswith("/accounts"):
        return _tg_accounts(text)
    if low.startswith("/watch") or low.startswith("/unwatch"):
        return _tg_watch(text)
    if URL_RE.search(text):                      # "/clip link" or any other command with a link
        return _tg_start_links(text.split(None, 1)[1] if " " in text else text)
    return TG_HELP


def _tg_change(text: str) -> str:
    """/change 2 end it after the punchline — the last finished video's clips, changed in your words."""
    body = text.strip().split(None, 1)[1].strip() if len(text.strip().split(None, 1)) > 1 else ""
    if not body:
        return ("Tell me what to change on the last video's clips, e.g.\n"
                "<code>/change 2 end it right after the punchline</code>\n"
                "<code>/change all karaoke captions, a bit bigger</code>")
    last = next((j for j in store.list_jobs(20) if j["status"] == "done"), None)
    if not last:
        return "No finished videos to change yet."
    ask = re.sub(r"^(\d+[a-zA-Z]?)\b[:,.-]?", r"Clip \1:", body)     # "/change 2 …" means clip 2

    def work() -> None:
        try:
            out = instruct.request(last["id"], ask, source="telegram")
        except ValueError as exc:
            notify.send(f"⚠️ {notify.esc(str(exc))}")
            return
        lines = []
        if out.get("question"):
            lines.append(f"❓ {notify.esc(out['question'])}")
        if out.get("understood"):
            lines.append(f"✏️ {notify.esc(out['understood'])}")
        for c in out.get("cant") or []:
            lines.append(f"• Can't: {notify.esc(c)}")
        for c in out.get("clips") or []:
            for p in c.get("problems") or []:
                lines.append(f"• #{c['label']}: {notify.esc(p)}")
        if any(c.get("remake") for c in out.get("clips") or []):
            lines.append("I'll send the new versions here as each one is ready. Undo is on the clip in ClipAgent.")
        notify.send("\n".join(lines) or "I couldn't match that to anything I can change.")

    threading.Thread(target=work, daemon=True).start()
    return f"👀 Reading that for <b>{notify.esc((last['title'] or '')[:70])}</b>…"


def _when(ts: Optional[float]) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%a %H:%M") if ts else ""


def _tg_plan() -> str:
    last = next((j for j in store.list_jobs(20) if j["status"] == "done"), None)
    if not last:
        return "No finished videos to plan yet."
    cfg = money.settings()
    if not any(cfg["accounts"].values()):
        return ("Tell me your accounts first, e.g.\n<code>/accounts tiktok @you</code>\n"
                "<code>/accounts instagram @you</code>\n<code>/accounts youtube @you</code>")
    made = money.plan_job(last["id"])
    if not made:
        return "Nothing new to plan — every clip is already scheduled on every account."
    lines = [f"🗓 Planned {len(made)} posts for <b>{notify.esc((last['title'] or '')[:60])}</b>:"]
    for p in made[:15]:
        lines.append(f"• {_when(p['planned_at'])} {notify.esc(p['platform'])} {notify.esc(p['account'])} — #{p['n']}")
    lines.append("I'll remind you at each time with the clip and caption.")
    return "\n".join(lines)


def _tg_posted(text: str) -> str:
    parts = text.split()
    urls = URL_RE.findall(text)
    num = next((int(p) for p in parts[1:] if p.isdigit()), None)
    if not urls:
        return "Send it like this: <code>/posted 12 https://www.tiktok.com/@you/video/...</code>"
    if num is not None:
        post = money.by_number(num)
        if not post:
            return f"There's no post #{num}."
        post = money.mark_posted(post["id"], urls[0])
    else:
        post = money.add("", url=urls[0])
    threading.Thread(target=money.check, args=(post,), daemon=True).start()
    return (f"✅ Post #{post['n']} is live on {notify.esc(post['platform'] or 'that platform')}. "
            "I'll track its views and tell you when it takes off.")


def _tg_set_views(low: str) -> str:
    """/views 12 34k — for posts ClipAgent can't read (Instagram without a login)."""
    nums = re.findall(r"(\d[\d,.]*)([km]?)", low)
    n = int(nums[0][0].replace(",", "").split(".")[0])
    value, unit = nums[1]
    views = int(float(value.replace(",", "")) * {"k": 1_000, "m": 1_000_000}.get(unit, 1))
    post = money.by_number(n)
    if not post:
        return f"There's no post #{n}."
    history = (post.get("history") or []) + [[round(time.time()), views]]
    money.update(post["id"], views=views, history=history, checked_at=time.time())
    return f"📈 Post #{n}: {views:,} views — about ${money.estimate(money.get(post['id'])):,.2f}."


def _tg_money() -> str:
    d = money.dashboard()
    if not d["posts"]:
        return "No posts tracked yet. When one is live: <code>/posted link</code>"
    lines = [f"💰 <b>{d['views']:,}</b> views on {d['posts']} posts · about <b>${d['earned']:,.2f}</b> earned",
             f"Last 24 h: {d['views_24h']:,} views"]
    for c in d["by_campaign"][:5]:
        lines.append(f"• {notify.esc(c['name'])}: {c['views']:,} views · ${c['earned']:,.2f}")
    if len(d["by_style"]) > 1:
        lines.append("\n<b>By style</b> (average views)")
        for g in d["by_style"][:5]:
            lines.append(f"• {notify.esc(g['name'])}: {g['avg_views']:,} over {g['posts']} posts")
    if d["top"]:
        lines.append("\n<b>Top posts</b>")
        for p in d["top"][:3]:
            lines.append(f"• #{p['n']} {int(p.get('views') or 0):,} views — {notify.esc((p.get('hook') or '')[:60])}")
    return "\n".join(lines)


def _tg_accounts(text: str) -> str:
    parts = text.split()[1:]
    cfg = money.settings()
    names = {"tiktok": "tiktok", "tt": "tiktok", "instagram": "instagram", "ig": "instagram", "reels": "instagram",
             "youtube": "youtube", "yt": "youtube", "shorts": "youtube"}
    if parts and parts[0].lower() in names:
        plat = names[parts[0].lower()]
        cfg["accounts"][plat] = ["@" + a.lstrip("@") for a in parts[1:]]
        money.save_settings({"accounts": cfg["accounts"]})
    lines = ["<b>Your accounts</b>"] + [f"• {k}: {' '.join(v) or '—'}" for k, v in cfg["accounts"].items()]
    lines.append("Change: <code>/accounts tiktok @one @two</code> (nothing after it clears that platform)")
    return "\n".join(lines)


def _tg_watch(text: str) -> str:
    urls = URL_RE.findall(text)
    if text.lower().startswith("/unwatch"):
        if urls:
            money.unwatch_channel(urls[0])
    elif urls:
        words = [w for w in URL_RE.sub(" ", text).split()[1:]]
        camp = _tg_campaign(words) if words else None
        money.watch_channel(urls[0], camp["id"] if camp else "")
    watch = money.settings()["watch"]
    if not watch:
        return "Not watching any channels. <code>/watch https://www.youtube.com/@channel</code>"
    return "👀 <b>Watching</b> (checked every 2 h)\n" + "\n".join(f"• {notify.esc(w['url'])}" for w in watch)


def _settle_interrupted_jobs() -> None:
    """Jobs still marked running when the app starts were cut off by a restart
    (nothing survives one), so say so instead of showing them as busy forever."""
    for job in store.list_jobs(200):
        if job["status"] not in ("running", "queued"):
            continue
        if (job.get("stage") or "").startswith("Done"):
            store.update_job(job["id"], status="done", progress=100)
        else:
            store.update_job(job["id"], status="failed", stage="Failed", progress=100,
                             error="ClipAgent was closed or restarted while this was running. "
                                   "Press Try again — it won't download the video again.")


@app.on_event("startup")
def _start_telegram() -> None:
    _settle_interrupted_jobs()
    money.start()
    if notify.start(telegram_command):
        print("Telegram updates are on" + ("" if notify.connected() else
              " — open your bot in Telegram and press Start to connect it"), flush=True)


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
