"""The end-to-end run: source in, ranked and rendered clips out."""
from __future__ import annotations

import hashlib
import json
import os
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import (brandlogo, notify, campaign, compliance, framing, highlights, judge, media, overlay, render, store,
               smartstitch, structure, styles, tighten, transcribe)
from . import doctor


def _frame_rate(source: Path):
    """Exact source frame rate, so cuts land on frames; None if it cannot be read."""
    try:
        from . import motion
        return motion.frame_rate(source)
    except Exception:
        return None


def _seamless() -> bool:
    return render.ENGINE != "classic"
from .config import ANTHROPIC_API_KEY, AUDIO_DIR, MAX_CLIPS

RENDER_WORKERS = int(os.getenv("RENDER_WORKERS", "3"))
PER_CLIP_SAMPLES = 24          # face samples per clip when tracking a speaker


def _stage(job_id: str, stage: str, progress: int) -> None:
    store.update_job(job_id, stage=stage, progress=max(0, min(100, progress)), status="running")


def _failed_stage(job_id: str) -> str:
    """'Failed at: <the step it was on>', so the page can show where it stopped."""
    prev = ((store.get_job(job_id) or {}).get("stage") or "").strip()
    return f"Failed at: {prev}" if prev and not prev.lower().startswith(("failed", "queued")) else "Failed"


def source_fingerprint(path: Path) -> str:
    """Identify a source by its bytes, so the same VOD is never paid for twice.

    Hashing a 4 GB VOD end to end would take longer than the transcription it
    saves, so this reads the head and tail plus the exact size — enough that
    two different videos will not collide in practice.
    """
    try:
        size = path.stat().st_size
        digest = hashlib.sha256(str(size).encode())
        chunk = 4 * 1024 * 1024
        with open(path, "rb") as fh:
            digest.update(fh.read(chunk))
            if size > chunk * 2:
                fh.seek(-chunk, os.SEEK_END)
                digest.update(fh.read(chunk))
        return digest.hexdigest()
    except OSError:
        return ""


# --- one clip -------------------------------------------------------------

def prepare_clip(
    source: Path,
    clip: Dict[str, Any],
    all_words: List[Dict[str, Any]],
    settings: Dict[str, Any],
    source_plan: framing.FramingPlan | None,
    fps=None,
) -> Tuple[List[Dict[str, Any]], List[Tuple[float, float]], float, framing.FramingPlan | None]:
    """Work out this clip's words, its cuts, and how it should be framed."""
    start, end = clip["start"], clip["end"]

    keep: List[Tuple[float, float]] = []
    saved = 0.0
    words = transcribe.words_between(all_words, start, end)
    if settings.get("tighten", True):
        keep, retimed, saved = tighten.plan_cuts(
            all_words, start, end,
            max_gap=float(settings.get("max_gap", 0.6)),
            drop_fillers=bool(settings.get("drop_fillers", True)),
            fps=fps,
        )
        if keep:
            words = retimed

    # A facecam sits in the same place all stream, so the source-level plan
    # holds. Anything else moves, so this clip gets looked at on its own —
    # the seamless renderer does that itself, densely, while it renders.
    plan = source_plan
    if (not _seamless() and settings.get("auto_frame", True)
            and (not plan or plan.kind != "facecam")):
        try:
            plan = framing.plan_framing(source, start, end, count=PER_CLIP_SAMPLES)
        except Exception:
            plan = source_plan
    return words, keep, saved, plan


def _frames(a: float, b: float, fps) -> float:
    """How long [a, b) plays once cut on the frame grid — exactly what the renderer does."""
    if not fps:
        return max(0.0, b - a)
    from fractions import Fraction
    n = round(Fraction(b) * fps) - round(Fraction(a) * fps)
    return float(Fraction(n) / fps) if n >= 2 else 0.0


def build_parts(
    parts: List[Dict[str, Any]],
    all_words: List[Dict[str, Any]],
    settings: Dict[str, Any],
    fps=None,
) -> Tuple[List[Tuple[float, float]], List[Dict[str, Any]], float, List[Tuple[float, str]]]:
    """Everything a stitched clip needs to render, part by part.

    Each part is tightened on its own, its words re-timed onto the finished
    clip's clock, and its label placed at the instant the part begins.
    Returns (absolute source segments in play order, words, seconds saved,
    [(output seconds, label)]).
    """
    segments: List[Tuple[float, float]] = []
    words_out: List[Dict[str, Any]] = []
    labels: List[Tuple[float, str]] = []
    saved = 0.0
    offset = 0.0
    prev_end = None
    for part in parts:
        ps, pe = float(part["start"]), float(part["end"])
        if prev_end is not None and prev_end - 1.0 < ps < prev_end:
            ps = prev_end                     # overlapping the part before: never repeat a word
        if pe - ps < 0.5:
            continue
        keep: List[Tuple[float, float]] = []
        retimed: List[Dict[str, Any]] = []
        if settings.get("tighten", True):
            keep, retimed, sv = tighten.plan_cuts(
                all_words, ps, pe,
                max_gap=float(settings.get("max_gap", 0.6)),
                drop_fillers=bool(settings.get("drop_fillers", True)),
                fps=fps,
            )
            if keep:
                saved += sv
        if keep:
            segs = [(ps + a, ps + b) for a, b in keep]
            pw = retimed
        else:
            segs = [(ps, pe)]
            pw = transcribe.words_between(all_words, ps, pe)
        length = sum(_frames(a, b, fps) for a, b in segs)
        if length <= 0:
            continue
        if (part.get("label") or "").strip():
            labels.append((round(offset, 3), part["label"].strip()))
        for w in pw:
            if w["start"] >= length - 0.02:
                continue
            words_out.append({"w": w["w"], "start": round(offset + w["start"], 3),
                              "end": round(offset + min(w["end"], length), 3)})
        segments.extend(segs)
        offset += length
        prev_end = pe
    return segments, words_out, round(saved, 2), labels


def _log_structure(job_id: str, clips: List[Dict[str, Any]]) -> None:
    """What the structure pass proposed and what the judge made of it, for later."""
    try:
        from .config import DATA_DIR
        folder = Path(DATA_DIR) / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{job_id}_structure.json").write_text(json.dumps([{
            "title": c.get("title"), "type": c.get("type"), "variant": c.get("variant"),
            "proposed": c.get("structure_raw"), "stitch_problem": c.get("stitch_problem"),
            "judge": c.get("judge"),
        } for c in clips], indent=1, default=str), encoding="utf-8")
    except Exception:
        traceback.print_exc()


def _clip_edits(base: Dict[str, Any], clip: Dict[str, Any], headline: str) -> Dict[str, Any]:
    """Per-clip edits: its hook and headline, and a camera that suits the moment."""
    style = "punchy" if clip.get("type") in highlights.PUNCHY_TYPES else "calm"
    return render.merge_edits(base, {
        "hook": clip.get("hook", ""),
        "headline": clip.get("headline") or headline,
        "motion_style": style,
    })


def job_rules(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The campaign rulebook a job was run under — the copy taken when it
    started, so editing the campaign later doesn't quietly re-judge old clips."""
    if not job:
        return None
    camp = json.loads(job.get("settings") or "{}").get("campaign") or {}
    return camp.get("rules") or None


def _fit_length(clip: Dict[str, Any], lo: Optional[float], hi: Optional[float], duration: float) -> None:
    """Keep a clip inside a campaign's length limits after its edges were snapped."""
    if lo and clip["end"] - clip["start"] < lo:
        clip["end"] = round(min(duration, clip["start"] + lo + 0.3), 2)
        if clip["end"] - clip["start"] < lo:
            clip["start"] = round(max(0.0, clip["end"] - lo - 0.3), 2)
    if hi and clip["end"] - clip["start"] > hi:
        clip["end"] = round(clip["start"] + hi - 0.2, 2)


def _gate_source(rb: Dict[str, Any], clip_id: str) -> None:
    """Check a rendered clip-from-source campaign clip and store the verdict."""
    row = store.get_clip(clip_id)
    if not row or not row.get("file"):
        return
    edits = json.loads(row.get("edits") or "{}")
    post = json.loads(row.get("post") or "{}")
    clip = {"start": row["start"], "end": row["end"], "saved": row.get("saved") or 0,
            "parts": json.loads(row.get("parts") or "[]")}
    result = compliance.check_source(rb, clip, edits, Path(row["file"]), post,
                                     edits.get("hook", "") if edits.get("hook_on", True) else "",
                                     edits.get("tone"))
    store.update_clip(clip_id, compliance=json.dumps(result))


def _campaign_stage(job_id: str) -> str:
    rows = store.list_clips(job_id)
    verdicts = [json.loads(r.get("compliance") or "{}").get("status") for r in rows]
    ready, check, blocked = (verdicts.count(s) for s in ("ready", "check", "blocked"))
    parts = [f"{ready} ready to post"]
    if check:
        parts.append(f"{check} to check")
    if blocked:
        parts.append(f"{blocked} blocked")
    failed = sum(1 for r in rows if r["status"] == "failed")
    if failed:
        parts.append(f"{failed} failed")
    return "Done — " + ", ".join(parts)


def _overlay_note(made: Dict[str, Any]) -> str:
    fit = ("Posted whole at its own size" if made["layout"]["vertical"] else
           "Fitted whole inside 9:16 — nothing cropped")
    note = fit + "."
    if made.get("position_why"):
        note = f"{fit}. Hook at the {made['position']}: {made['position_why']}."
    if made.get("logo"):
        note += f" Brand logo {made['logo']['why']}."
    return note


def _made_fields(made: Dict[str, Any]) -> Dict[str, Any]:
    """What an overlay render decided, kept with the clip's edits."""
    return {"layout_info": made["layout"], "audio_copied": made["audio_copied"],
            "hook_position_used": made.get("position", ""), "hook_position_why": made.get("position_why", ""),
            "brand_logo_drawn": made.get("logo")}


def _campaign_logo(settings: Dict[str, Any], rb: Dict[str, Any]) -> Optional[Path]:
    """The brand's logo for this run: the campaign's current file, if it has one
    and the brief doesn't ban logos."""
    camp = settings.get("campaign") or {}
    if not campaign.wants_brand_logo(rb):
        return None
    if camp.get("id") and brandlogo.exists(camp["id"]):
        return brandlogo.path_for(camp["id"])
    path = camp.get("logo") or ""
    return Path(path) if path and Path(path).exists() else None


def _watch_sources(job_id: str, rb: Dict[str, Any], sources: List[Tuple[Path, Dict[str, Any], str]]
                   ) -> List[Optional[Dict[str, Any]]]:
    """Claude looks at each clip and writes text for what's actually in it.
    A clip it couldn't look at gets None, and the brief's own hooks instead."""
    if not ANTHROPIC_API_KEY:
        return [None] * len(sources)
    _stage(job_id, f"Watching {len(sources)} clip{'s' if len(sources) != 1 else ''} to write hooks and captions", 28)

    def one(item: Tuple[Path, Dict[str, Any], str]) -> Optional[Dict[str, Any]]:
        f, info, stem = item
        try:
            return campaign.watch_clip(rb, overlay.sample_frames(f, info), stem, info["duration"])
        except Exception:
            traceback.print_exc()
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(one, sources))


def run_overlay_job(job_id: str) -> None:
    try:
        _run_overlay_job(job_id)
    finally:
        notify.job_finished(job_id)


def run_job(job_id: str, url: Optional[str] = None, upload_path: Optional[Path] = None) -> None:
    """Run a video end to end, then report it on Telegram (when set up)."""
    try:
        _run_job(job_id, url, upload_path)
    finally:
        notify.job_finished(job_id)


def _run_overlay_job(job_id: str) -> None:
    """A clip-bank campaign: each file posted whole, with a hook on top.

    No transcription, no picking, no camera: the brief forbids touching the
    footage, so the brand's clips go out as they are, the hook drawn over
    them, every one checked by the gate."""
    job = store.get_job(job_id)
    if not job:
        return
    settings = json.loads(job.get("settings") or "{}")
    rb = (settings.get("campaign") or {}).get("rules") or {}
    files = [Path(p) for p in settings.get("files", [])]
    names = list(settings.get("file_names", []))
    versions = max(1, min(4, int(settings.get("versions", 1))))
    look = {k: settings.get(k, v) for k, v in overlay.DEFAULT_LOOK.items()}
    logo = _campaign_logo(settings, rb)
    try:
        def failed_row(rank: int, title: str, reason: str) -> None:
            bad = store.create_clip(job_id, {"rank": rank, "start": 0, "end": 0, "title": title[:80],
                                             "reason": reason[:300], "type": "campaign"})
            store.update_clip(bad, status="failed")

        for i, url in enumerate(settings.get("links", [])):
            _stage(job_id, f"Downloading clip {i + 1} of {len(settings['links'])}", 3 + i)
            try:
                path, title = media.download(url, f"{job_id}_{i}")
                files.append(path)
                names.append(title)
            except Exception as exc:
                failed_row(900 + i, url, f"Couldn't download: {exc}")
        _stage(job_id, "Reading the clips", 20)
        sources = []
        for i, f in enumerate(files):
            name = names[i] if i < len(names) and names[i] else f.stem
            try:
                sources.append((f, overlay.inspect(f), Path(name).stem))
            except Exception as exc:
                failed_row(950 + i, name, f"Couldn't read this file: {exc}")
        if not sources:
            raise RuntimeError("None of the files could be read as video.")
        if files:
            store.update_job(job_id, source_path=str(files[0]),
                             duration=sum(info["duration"] for _, info, _ in sources))

        count = len(sources) * versions
        hooks, hook_note = campaign.plan_hooks(rb, count, generate=False)
        watched = _watch_sources(job_id, rb, sources)
        if not any(watched) and not any(hooks):
            hooks, hook_note = campaign.plan_hooks(rb, count)          # nothing seen: the brief alone
        platforms = campaign.assign_platforms(rb, versions, settings.get("platforms") or [])
        hook_on = campaign.allowed(rb, "hook")
        used_hooks: set = set()
        used_caps: set = set()

        def fresh(options: List[str], used: set, v: int) -> str:
            """The clip's best option nobody else in this run has: seven clips
            all saying "The ref had to stop it" reads like spam, even when it's true."""
            if not options:
                return ""
            order = options[v % len(options):] + options[:v % len(options)]
            pick = next((o for o in order if o.lower() not in used), order[0])
            used.add(pick.lower())
            return pick

        plan = []
        n = 0
        for (f, info, stem), seen in zip(sources, watched):
            for v in range(versions):
                if seen:
                    hook = fresh(seen["hooks"], used_hooks, v) if hook_on else ""
                    extra = fresh(seen["captions"], used_caps, v)
                    post = campaign.build_post(rb, n, platforms[v], extra=extra, line=seen["line"])
                    note = f"Claude watched it: {seen['what']}" if seen["what"] else "Claude watched it."
                else:
                    hook, extra, note = hooks[n], "", hook_note
                    post = campaign.build_post(rb, n, platforms[v])
                plan.append({"n": n, "file": f, "info": info, "hook": hook, "post": post, "extra": extra,
                             "note": note, "what": (seen or {}).get("what", ""),
                             "title": stem + (f" · version {v + 1}" if versions > 1 else "")})
                n += 1
        _stage(job_id, "Checking the hooks and captions against the brief's tone rules", 40)
        tone = campaign.check_text(rb, [{"id": p["n"], "hook": p["hook"], "extra": p["extra"], "what": p["what"]}
                                        for p in plan])

        rows = []
        for p in plan:
            edits = {**look, "campaign_mode": "overlay", "hook": p["hook"], "tone": tone.get(p["n"]),
                     "clip_shows": p["what"]}
            clip_id = store.create_clip(job_id, {
                "rank": p["n"] + 1, "start": 0.0, "end": round(p["info"]["duration"], 3), "score": 0,
                "title": p["title"][:80], "hook": p["hook"], "reason": p["note"][:300],
                "caption": p["post"]["caption"], "hashtags": p["post"]["hashtags"],
                "edits": edits, "type": "campaign", "post": p["post"], "source_path": str(p["file"]),
            })
            rows.append((clip_id, p, edits))

        def build(clip_id: str, p: Dict[str, Any], edits: Dict[str, Any]) -> None:
            made = overlay.render(p["file"], clip_id, p["hook"], look, p["info"], logo=logo)
            store.update_clip(clip_id, file=str(made["file"]), thumb=str(made["thumb"]), status="checking",
                              framing=json.dumps({"kind": "overlay", "note": _overlay_note(made)}))
            result = compliance.check_overlay(rb, p["file"], made["file"], made, p["post"], p["hook"],
                                              edits.get("tone"), p["info"])
            store.update_clip(clip_id, status="ready", compliance=json.dumps(result),
                              edits=json.dumps({**edits, **_made_fields(made)}))

        _stage(job_id, f"Rendering and checking {len(rows)} clips", 45)
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, RENDER_WORKERS)) as pool:
            futures = {pool.submit(build, cid, p, e): cid for cid, p, e in rows}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    traceback.print_exc()
                    store.update_clip(futures[future], status="failed", reason=str(exc)[:300])
                done += 1
                _stage(job_id, f"Rendered and checked {done} of {len(rows)}", 45 + int(done / len(rows) * 54))
        store.update_job(job_id, status="done", stage=_campaign_stage(job_id), progress=100)
    except Exception as exc:
        traceback.print_exc()
        store.update_job(job_id, status="failed", stage=_failed_stage(job_id), error=str(exc)[:500], progress=100)


def _run_job(job_id: str, url: Optional[str] = None, upload_path: Optional[Path] = None) -> None:
    job = store.get_job(job_id)
    if not job:
        return
    settings = json.loads(job.get("settings") or "{}")
    if (settings.get("campaign") or {}).get("mode") == "overlay":
        return _run_overlay_job(job_id)
    rules = job_rules(job)                       # a clip-from-source campaign, or None
    want = int(settings.get("max_clips", MAX_CLIPS))

    try:
        # 1. Get the video ---------------------------------------------------
        if upload_path:
            _stage(job_id, "Reading your file", 10)
            source = Path(upload_path)
            title = job.get("title") or source.stem
        else:
            _stage(job_id, "Downloading video", 4)
            source, title = media.download(
                url, job_id, progress=lambda p: _stage(job_id, "Downloading video", 4 + p)
            )
        info = media.probe(source)
        fps = _frame_rate(source)
        fingerprint = source_fingerprint(source)
        store.update_job(job_id, title=title, source_path=str(source),
                         duration=info["duration"], source_hash=fingerprint)

        if not info["has_audio"]:
            raise RuntimeError("That video has no audio track, so there is nothing to transcribe.")

        # 2. Audio, and the energy curve -------------------------------------
        _stage(job_id, "Extracting audio", 30)
        wav = media.extract_audio(source, job_id)
        curve = media.energy_curve(wav)
        peaks = media.peak_windows(curve)

        # 3. Transcribe, unless we already have this exact source -------------
        cached = store.cached_transcript(fingerprint)
        if cached:
            _stage(job_id, "Reusing the transcript from last time", 60)
            result = {**cached, "words": transcribe.in_order(cached.get("words") or [])}
        else:
            _stage(job_id, "Transcribing speech", 36)
            result = transcribe.transcribe(
                wav, progress=lambda p: _stage(job_id, "Transcribing speech", 36 + int(p * 0.24)),
                on_wait=lambda w: notify.problem(
                    job_id, "rate", f"⏳ <b>{notify.esc(title[:80])}</b>: the transcription service's "
                                    f"free limit is used up, so I'm waiting about {max(1, round(w / 60))} "
                                    "min and then carrying on. Nothing to do."),
            )
            store.cache_transcript(fingerprint, result, info["duration"])
        store.update_job(job_id, transcript=json.dumps(result))
        wav.unlink(missing_ok=True)

        # 4. Claude picks the moments, then ranks them all together -----------
        _stage(job_id, "Claude is finding the moments", 62)
        clips = highlights.find_highlights(
            title=title, segments=result["segments"], duration=info["duration"],
            peaks=peaks, want=want,
            progress=lambda p: _stage(
                job_id,
                "Claude is finding the moments" if p < 80 else "Ranking them against each other",
                62 + int(p * 0.13)),
            min_len=settings.get("min_len"), max_len=settings.get("max_len"),
            guidance=campaign.picker_guidance(rules) if rules else "",
            platforms=settings.get("platforms") or None,
        )
        if not clips:
            store.update_job(job_id, status="done", stage="No clip-worthy moments found",
                             progress=100)
            return
        for clip in clips:
            highlights.snap_to_words(clip, result["words"])
            if rules:
                _fit_length(clip, settings.get("min_len"), settings.get("max_len"), info["duration"])
        headline = next((c["headline"] for c in clips if c.get("headline")), "")

        # 4b. Each moment built two ways — one unbroken stretch, and stitched
        # from the parts a cold viewer needs — then judged against each other.
        # The same pass proposes Smart Stitch's teasers, callbacks and reactions.
        smart_on = smartstitch.allowed(rules)[0] and (smartstitch.mode_of(settings) != "never"
                                                      or settings.get("inserts", True) is not False)
        if settings.get("structure", True) or smart_on:
            try:
                _stage(job_id, "Judging — building each clip two ways", 72)
                ceiling = highlights.length_window(settings.get("min_len"), settings.get("max_len"),
                                                   settings.get("platforms") or None)[1]
                structure.plan(title, clips, result["segments"], result["words"],
                               info["duration"], headline, max_len=ceiling,
                               loud=smartstitch.loud_gaps(curve, result["words"]))
                _stage(job_id, "Judging continuous vs stitched", 74)
                if settings.get("structure", True):
                    judge.compare(clips, result["words"], headline)
                else:                       # one version only, as asked: no stitched one
                    for clip in clips:
                        clip["variants"]["stitched"] = None
                        structure.choose(clip, "continuous")
            except Exception as exc:
                # The picks are good on their own: make them the plain way
                # rather than losing the whole run to this step.
                traceback.print_exc()
                highlights.LAST_ERROR = f"structure/judge step: {exc}"[:300]
                for clip in clips:
                    clip.setdefault("variants", {"continuous": structure._as_variant(
                        clip["start"], clip["end"], clip.get("hook", "")), "stitched": None})
                    clip["variants"]["stitched"] = None
                    structure.choose(clip, "continuous")
            _log_structure(job_id, clips)
        else:
            for clip in clips:
                clip["variants"] = {"continuous": structure._as_variant(
                    clip["start"], clip["end"], clip.get("hook", "")), "stitched": None}
                structure.choose(clip, "continuous")

        # 4c. Smart Stitch: a teaser of the best part up front, proof shots,
        # reactions and callbacks — each checked against the honesty rules.
        if smart_on:
            _stage(job_id, "Looking for teasers, proof shots and reactions", 75)
            smartstitch.prepare(job_id, source, clips, result["words"], settings, rules, headline,
                                info["duration"])

        # 5. How is this video framed? ---------------------------------------
        source_plan = None
        if settings.get("auto_frame", True):
            _stage(job_id, "Looking at how the video is framed", 76)
            try:
                source_plan = framing.plan_framing(source, 0, info["duration"])
                store.update_job(job_id, framing=json.dumps(source_plan.to_json()))
            except Exception:
                source_plan = None

        # 6. Build and render --------------------------------------------------
        # Everything chosen on the ingest screen, including the brand kit,
        # has to reach the first render — not just the re-render.
        base_edits = {
            "layout": settings.get("layout", "auto"),
            "caption_style": settings.get("caption_style", "impact"),
            "caption_position": settings.get("caption_position", "bottom"),
            "tighten": settings.get("tighten", True),
            "drop_fillers": settings.get("drop_fillers", True),
            "max_gap": settings.get("max_gap", 0.6),
            "auto_frame": settings.get("auto_frame", True),
            "motion": settings.get("motion", True),
            "headline_on": settings.get("headline", True),
            "accent": settings.get("accent", ""),
            "logo": settings.get("logo", False),
            "logo_corner": settings.get("logo_corner", "top-right"),
            "normalize_audio": settings.get("normalize_audio", True),
            "captions_on": settings.get("captions_on", True),
            "hook_on": settings.get("hook_on", True),
            "brand_logo": settings.get("brand_logo", ""),
        }

        # 5b. The style brain: how each clip should be made -------------------
        # Each clip gets a recipe from the Clip Style Database (word-pop
        # captions, headline label, title bar, comment bubble) and its on-screen
        # text. What ends up on screen becomes the clip's hook, so the campaign
        # tone check below reads exactly what viewers will.
        style_edits: Dict[int, Dict[str, Any]] = {}
        if settings.get("auto_style", True):
            _stage(job_id, "Choosing a style for each clip", 77)
            two = source_plan.two_shot if source_plan else 0.0
            allowed = styles.campaign_recipes(rules, two) if rules else styles.available_recipes(two)
            forced = settings.get("style_recipe") or "auto"
            if forced in styles.RECIPES:
                # One look for every clip, picked by you; Claude still writes its text.
                # A look this video (or the brief) can't take falls back to word-pop.
                fallback = ["wordpop"] if "wordpop" in allowed else allowed
                if forced not in allowed:
                    allowed = fallback
                elif forced == "stack":
                    # the split only suits clips where two people share the frame
                    allowed = ["stack"] + [r for r in fallback if r != "stack"][:1]
                else:
                    allowed = [forced]
            plans = styles.direct(title, clips, result["words"], allowed,
                                  guidance=campaign.picker_guidance(rules) if rules else "")
            for i, (clip, plan) in enumerate(zip(clips, plans)):
                style_edits[i] = styles.apply(plan, clip)
                shown = styles.on_screen_text(style_edits[i])
                if shown:
                    clip["hook"] = shown
                clip["style_plan"] = plan

        # A campaign's caption is built from the brief's own lines, and what
        # Claude wrote (hooks, caption words) is checked against its tone rules.
        posts: Dict[int, Dict[str, Any]] = {}
        tone: Dict[int, Dict[str, str]] = {}
        if rules:
            platform = campaign.assign_platforms(rules, 1, settings.get("platforms") or [])[0]
            for i, clip in enumerate(clips):
                posts[i] = campaign.build_post(rules, i, platform, extra=clip.get("caption", ""),
                                               own_tags=clip.get("hashtags", []))
            _stage(job_id, "Checking hooks and captions against the brief", 75)
            tone = campaign.check_text(rules, [{
                "id": i, "hook": c.get("hook", "") if base_edits["hook_on"] else "",
                "extra": posts[i]["caption"].replace(posts[i]["line"], "").strip()}
                for i, c in enumerate(clips)])

        clip_ids = []
        runners_up = []
        for i, clip in enumerate(clips):
            edits = render.merge_edits(_clip_edits(base_edits, clip, headline), style_edits.get(i) or {})
            extra_fields: Dict[str, Any] = {}
            if rules:
                edits = campaign.clamp_edits(edits, rules)
                edits["tone"] = tone.get(i)
                extra_fields = {"post": posts[i], "caption": posts[i]["caption"],
                                "hashtags": posts[i]["hashtags"]}
            edits.update(smartstitch.clip_edits(clip, clip.get("parts") or [], settings, result["words"]))
            stitched = len(clip.get("parts") or []) > 1
            main_id = store.create_clip(job_id, {
                **clip,
                **extra_fields,
                "words": [] if stitched else transcribe.words_between(
                    result["words"], clip["start"], clip["end"]),
                "edits": edits,
            })
            clip_ids.append((main_id, clip, edits))
            # The other version is made too, so both can be posted and the
            # platform can settle what the judge could only estimate.
            if settings.get("alternates", True) and clip.get("variants", {}).get("stitched"):
                other = "continuous" if clip["variant"] == "stitched" else "stitched"
                alt = structure.choose({**clip}, other)
                alt["alt_of"] = main_id
                alt_edits = render.merge_edits(_clip_edits(base_edits, alt, headline), style_edits.get(i) or {})
                if rules:
                    alt_edits = {**campaign.clamp_edits(alt_edits, rules), "tone": tone.get(i)}
                alt_edits.update(smartstitch.clip_edits(alt, alt["parts"], settings, result["words"]))
                alt_stitched = len(alt["parts"]) > 1
                alt_id = store.create_clip(job_id, {
                    **alt,
                    **extra_fields,
                    "words": [] if alt_stitched else transcribe.words_between(
                        result["words"], alt["start"], alt["end"]),
                    "edits": alt_edits,
                })
                runners_up.append((alt_id, alt, alt_edits))
        clip_ids += runners_up                   # the chosen versions render first

        # Face sampling is as slow as encoding, so both happen in the pool.
        def build(clip_id: str, clip: Dict[str, Any], edits: Dict[str, Any]) -> None:
            parts = clip.get("parts") or []
            # A teaser or inserts: rendered with them; otherwise (or if they
            # couldn't be drawn) the usual way below. `smart` says which.
            out, words, saved, smart = smartstitch.render(source, clip_id, parts, edits, result["words"],
                                                          settings, fps, info, source_plan, rules)
            plan = source_plan
            if out is not None:
                pass
            elif len(parts) > 1:
                segments, words, saved, labels = build_parts(parts, result["words"], settings, fps)
                plan = source_plan
                out = render.render_clip(
                    source=source, clip_id=clip_id, start=clip["start"], end=clip["end"],
                    words=words, edits=edits, has_audio=info["has_audio"], plan=plan,
                    source_size=(info["width"], info["height"]),
                    segments=segments, labels=labels,
                )
            else:
                words, keep, saved, plan = prepare_clip(source, clip, result["words"],
                                                        settings, source_plan, fps=fps)
                lo = settings.get("min_len") if rules else None
                if keep and lo and sum(b - a for a, b in keep) < lo:
                    # Cutting the dead air would take it under the brief's minimum:
                    # keep the pauses instead.
                    keep, saved = [], 0.0
                    words = transcribe.words_between(result["words"], clip["start"], clip["end"])
                out = render.render_clip(
                    source=source, clip_id=clip_id, start=clip["start"], end=clip["end"],
                    words=words, edits=edits, has_audio=info["has_audio"], plan=plan,
                    source_size=(info["width"], info["height"]), keep=keep,
                )
            used = out.get("plan") or plan
            store.update_clip(
                clip_id, file=str(out["file"]), thumb=str(out["thumb"]), status="ready",
                words=json.dumps(words), saved=saved,
                framing=json.dumps(used.to_json() if used else {}),
                **({"edits": json.dumps({**edits, "smart": smart})} if smart or edits.get("smart") else {}),
            )
            if rules:
                _gate_source(rules, clip_id)
            if settings.get("doctor", True):
                # The clip doctor: checks the finished file, fixes what it can in
                # one re-render. Claude looks at the main versions; the runner-up
                # versions get the measured checks only.
                doctor.treat(clip_id, rules, use_claude=not clip.get("alt_of"))

        _stage(job_id, f"Framing, rendering and checking {len(clip_ids)} clips", 80)
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, RENDER_WORKERS)) as pool:
            futures = {pool.submit(build, cid, clip, edits): cid
                       for cid, clip, edits in clip_ids}
            for future in as_completed(futures):
                clip_id = futures[future]
                try:
                    future.result()
                except Exception as exc:       # one bad clip must not sink the run
                    traceback.print_exc()
                    store.update_clip(clip_id, status="failed", reason=str(exc)[:300])
                done += 1
                _stage(job_id, f"Rendered and checked {done} of {len(clip_ids)}",
                       80 + int(done / len(clip_ids) * 19))

        stage = _campaign_stage(job_id) if rules else "Done"
        if highlights.LAST_ERROR:
            stage = f"{stage} — Claude had a problem, so some steps fell back: {highlights.LAST_ERROR[:160]}"
        store.update_job(job_id, status="done", stage=stage, progress=100)

    except Exception as exc:
        traceback.print_exc()
        store.update_job(job_id, status="failed", stage=_failed_stage(job_id),
                         error=str(exc)[:500], progress=100)


def rerender_overlay(clip_id: str, edits: Dict[str, Any]) -> Dict[str, Any]:
    """A clip-bank clip with a new hook or look: re-drawn, re-checked."""
    clip = store.get_clip(clip_id)
    job = store.get_job(clip["job_id"]) if clip else None
    rb = job_rules(job) or {}
    source = Path(clip.get("source_path") or "")
    if not source.exists():
        raise RuntimeError("The original clip-bank file is gone — run the campaign again to edit this clip")
    current = json.loads(clip.get("edits") or "{}")
    look = {k: edits.get(k, current.get(k, v)) for k, v in overlay.DEFAULT_LOOK.items()}
    hook = (edits.get("hook", current.get("hook", "")) or "").strip()[:120]
    if not campaign.allowed(rb, "hook"):
        hook = ""
    tone = current.get("tone")
    if hook != current.get("hook", ""):
        tone = campaign.check_text(rb, [{"id": 0, "hook": hook, "extra": "",
                                         "what": current.get("clip_shows", "")}]).get(0)
    post = json.loads(clip.get("post") or "{}")
    info = overlay.inspect(source)
    made = overlay.render(source, clip_id, hook, look, info,
                          logo=_campaign_logo(json.loads(job.get("settings") or "{}") if job else {}, rb))
    result = compliance.check_overlay(rb, source, made["file"], made, post, hook, tone, info)
    store.update_clip(
        clip_id, status="ready", hook=hook, file=str(made["file"]), thumb=str(made["thumb"]),
        compliance=json.dumps(result),
        framing=json.dumps({"kind": "overlay", "note": _overlay_note(made)}),
        edits=json.dumps({**current, **look, "hook": hook, "tone": tone, **_made_fields(made)}),
    )
    store.update_job(clip["job_id"], stage=_campaign_stage(clip["job_id"]))
    return store.get_clip(clip_id)


def rerender_clip(clip_id: str, edits: Dict[str, Any]) -> Dict[str, Any]:
    """Apply editor changes to one clip and re-cut it."""
    clip = store.get_clip(clip_id)
    if not clip:
        raise RuntimeError("Clip not found")
    if clip.get("source_path"):
        return rerender_overlay(clip_id, edits)
    job = store.get_job(clip["job_id"])
    if not job or not job.get("source_path"):
        raise RuntimeError("Source video is no longer available")
    source = Path(job["source_path"])
    if not source.exists():
        raise RuntimeError("The source video has been cleaned up — re-run the video to edit this clip")
    rules = job_rules(job)

    current = json.loads(clip.get("edits") or "{}")
    merged = render.merge_edits(current, edits)
    if edits.get("facecam"):
        merged["facecam_manual"] = True
    if rules:
        # The editor can't break the brief either: what it forbids stays off,
        # and a new hook gets the tone check again. The brand's logo is the
        # campaign's current file — added or replaced since the run, it shows up.
        logo = _campaign_logo(json.loads(job.get("settings") or "{}"), rules)
        merged["brand_logo"] = str(logo) if logo else ""
        merged = campaign.clamp_edits(merged, rules)
        if merged.get("hook", "") != current.get("hook", ""):
            merged["tone"] = campaign.check_text(rules, [{"id": 0, "hook": merged.get("hook", ""),
                                                          "extra": ""}]).get(0)
        job_settings = json.loads(job.get("settings") or "{}")
        lo, hi = job_settings.get("min_len"), job_settings.get("max_len")
        start_e = float(edits.get("start", clip["start"]))
        end_e = float(edits.get("end", clip["end"]))
        if lo and end_e - start_e < lo:
            raise RuntimeError(f"The brief needs clips of at least {lo:.0f}s — this trim is {end_e - start_e:.1f}s")
        if hi and end_e - start_e > hi:
            raise RuntimeError(f"The brief allows at most {hi:.0f}s — this trim is {end_e - start_e:.1f}s")

    start = float(edits.get("start", clip["start"]))
    end = float(edits.get("end", clip["end"]))
    if end - start < 1.0:
        raise RuntimeError("A clip needs to be at least 1 second long")

    transcript = json.loads(job.get("transcript") or "{}")
    all_words = transcribe.in_order(transcript.get("words") or [])
    info = media.probe(source)

    stored_plan = json.loads(clip.get("framing") or "{}")
    plan = None
    if stored_plan.get("kind"):
        plan = framing.FramingPlan(
            kind=stored_plan["kind"], facecam=stored_plan.get("facecam"),
            track=[(t, x) for t, x in stored_plan.get("track", [])],
            confidence=stored_plan.get("confidence", 0), note=stored_plan.get("note", ""),
        )

    span_changed = abs(start - clip["start"]) > 0.05 or abs(end - clip["end"]) > 0.05
    keep: List[Tuple[float, float]] = []
    saved = 0.0
    words = edits.get("words")
    fps = _frame_rate(source)

    # A teaser or inserts (or asked for one): Smart Stitch re-makes it. Without
    # them it says so in merged["smart"] and the usual re-render runs below.
    done = smartstitch.rerender(clip, job, merged, edits, start, end, span_changed, all_words, info, plan,
                                fps, rules, source)
    if done is not None:
        return done
    words = edits.get("words")

    # A stitched clip keeps its parts. Dragging the trim handles means one
    # continuous stretch, so that turns it back into a continuous clip.
    parts = json.loads(clip.get("parts") or "[]")
    if len(parts) > 1 and not span_changed:
        segments, auto_words, saved, labels = build_parts(parts, all_words, merged, fps)
        if not words:
            words = auto_words
        words = transcribe.respell(words, merged.get("spell"))
        store.update_clip(clip_id, status="rendering")
        out = render.render_clip(
            source=source, clip_id=clip_id, start=clip["start"], end=clip["end"], words=words,
            edits=merged, has_audio=info["has_audio"], plan=plan,
            source_size=(info["width"], info["height"]), segments=segments, labels=labels,
        )
        used = out.get("plan") or plan
        store.update_clip(
            clip_id, status="ready", hook=merged.get("hook", ""), headline=merged.get("headline", ""),
            words=json.dumps(words), edits=json.dumps(merged), saved=saved,
            framing=json.dumps(used.to_json() if used else {}),
            file=str(out["file"]), thumb=str(out["thumb"]),
        )
        if rules:
            _gate_source(rules, clip_id)
            store.update_job(clip["job_id"], stage=_campaign_stage(clip["job_id"]))
        return store.get_clip(clip_id)
    if len(parts) > 1:
        store.update_clip(clip_id, variant="continuous")

    if merged.get("tighten", True) and all_words:
        keep, retimed, saved = tighten.plan_cuts(
            all_words, start, end,
            max_gap=float(merged.get("max_gap", 0.6)),
            drop_fillers=bool(merged.get("drop_fillers", True)),
            fps=fps,
        )
        lo = json.loads(job.get("settings") or "{}").get("min_len") if rules else None
        if keep and lo and sum(b - a for a, b in keep) < lo:
            keep, saved = [], 0.0                 # under the brief's minimum: keep the pauses
        elif keep and not words:
            words = retimed
    if words is None:
        words = transcribe.words_between(all_words, start, end)
    words = transcribe.respell(words, merged.get("spell"))

    # Re-look at the framing only when the span moved; otherwise the stored
    # plan still describes this moment.
    if (not _seamless() and span_changed and merged.get("auto_frame", True)
            and (not plan or plan.kind != "facecam")):
        try:
            plan = framing.plan_framing(source, start, end, count=PER_CLIP_SAMPLES)
        except Exception:
            pass

    store.update_clip(clip_id, status="rendering")
    out = render.render_clip(
        source=source, clip_id=clip_id, start=start, end=end, words=words,
        edits=merged, has_audio=info["has_audio"], plan=plan,
        source_size=(info["width"], info["height"]), keep=keep,
    )
    used = out.get("plan") or plan
    store.update_clip(
        clip_id, start=start, end=end, status="ready", hook=merged.get("hook", ""),
        headline=merged.get("headline", ""), words=json.dumps(words),
        parts=json.dumps([{"start": round(start, 2), "end": round(end, 2), "role": "payoff", "label": ""}]), edits=json.dumps(merged), saved=saved,
        framing=json.dumps(used.to_json() if used else {}),
        file=str(out["file"]), thumb=str(out["thumb"]),
    )
    if rules:
        _gate_source(rules, clip_id)
        store.update_job(clip["job_id"], stage=_campaign_stage(clip["job_id"]))
    return store.get_clip(clip_id)
