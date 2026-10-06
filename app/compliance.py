"""The gate: every campaign clip is checked against its brief before it can be downloaded.

Hard checks are measurements made in code on the finished file — frame
counts, an audio fingerprint, how far the footage drifted from the source,
the caption's exact words — so they can't be talked round. The one soft
check is Claude's read of whether the text you wrote stays inside the brief's
tone rules; a clear break blocks the clip, a doubt only flags it.

A clip comes out as one of:
  ready    every check passed
  check    nothing failed, but something needs a look (flagged)
  blocked  a rule is broken; the clip isn't offered for download
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import brandlogo, campaign, overlay

PSNR_FLOOR = 30.0          # dB; re-encoding alone sits near 40, a crop or zoom in the low 20s
# Measured: re-encoding moves a channel's average by ~0.1 level on real footage
# and its spread by under 0.6% even on heavy grain; a grade you can barely see
# (saturation +6%, contrast +3%, brightness +1%) moves them 1.2-2.3 levels / 1-3%.
COLOUR_SHIFT_MAX = 0.6     # 0-255 levels a channel's average may move
CONTRAST_MAX = 0.009       # share a channel's spread may change
# Measured on real speech and music: AAC at 256k from PCM matches its source to
# 36-48 dB at the same level; turning it up just 0.5 dB drops that to ~24 dB,
# an EQ or a fade far lower.
AUDIO_SNR_FLOOR = 30.0
AUDIO_GAIN_MAX = 0.2


def _item(cid: str, label: str, status: str, detail: str = "") -> Dict[str, str]:
    return {"id": cid, "label": label, "status": status, "detail": detail}


def summarize(checks: List[Dict[str, str]]) -> Dict[str, Any]:
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    if fails:
        status = "blocked"
        summary = "Blocked: " + "; ".join(c["label"].lower() for c in fails)
    elif warns:
        status = "check"
        summary = "Check before posting: " + "; ".join(c["label"].lower() for c in warns)
    else:
        status = "ready"
        summary = "Ready to post — every rule checked."
    return {"status": status, "summary": summary, "checks": checks}


def _length(r: Dict[str, Any], seconds: float) -> Dict[str, str]:
    lo, hi = r.get("min_len"), r.get("max_len")
    if not lo and not hi:
        return _item("length", "Length", "pass", f"{seconds:.1f}s — the brief sets no length.")
    # No slack either way: a brand measuring 14.97s as "under 15" is its call, not ours.
    if lo and seconds < lo:
        return _item("length", "Length", "fail", f"{seconds:.2f}s — the brief needs at least {lo:.0f}s.")
    if hi and seconds > hi:
        return _item("length", "Length", "fail", f"{seconds:.2f}s — the brief allows at most {hi:.0f}s.")
    if lo and hi:
        within = f"between the brief's {lo:.0f}s and {hi:.0f}s"
    elif lo:
        within = f"over the brief's {lo:.0f}s minimum"
    else:
        within = f"under the brief's {hi:.0f}s maximum"
    return _item("length", "Length", "pass", f"{seconds:.2f}s — {within}.")


def _caption(r: Dict[str, Any], post: Dict[str, Any]) -> List[Dict[str, str]]:
    out = []
    text = post.get("text") or ""
    if r["one_of"]:
        hit = next((line for line in r["one_of"] if line and line in text), "")
        out.append(_item("caption_line", "Required caption line",
                         "pass" if hit else "fail",
                         f"Has “{hit}”." if hit else "None of the brief's required lines is in the caption."))
    missing = [t for t in r["all_of"] if t and t not in text]
    if r["all_of"]:
        out.append(_item("caption_text", "Required caption text", "fail" if missing else "pass",
                         ("Missing: " + "; ".join(missing)) if missing else "All present."))
    required = ["#" + t.lstrip("#") for t in r["hashtags"]]
    lower = text.lower()
    missing_tags = [t for t in required if t.lower() not in lower]
    used = {w.lower() for w in text.split() if w.startswith("#")}
    extra = [t for t in used if t not in {x.lower() for x in required}]
    if missing_tags:
        out.append(_item("hashtags", "Required hashtags", "fail", "Missing " + " ".join(missing_tags)))
    elif extra and not r["other_hashtags"]:
        out.append(_item("hashtags", "Required hashtags", "fail",
                         "Has hashtags the brief didn't ask for: " + " ".join(sorted(extra))))
    else:
        out.append(_item("hashtags", "Required hashtags", "pass",
                         " ".join(required) if required else "The brief asks for none."))
    missing_m = [m for m in r["mentions"] if ("@" + m.lstrip("@")).lower() not in lower]
    if r["mentions"]:
        out.append(_item("mentions", "Required tags", "fail" if missing_m else "pass",
                         ("Missing " + " ".join(missing_m)) if missing_m else "All tagged."))
    return out


def _tone(tone: Optional[Dict[str, str]], hook: str) -> Dict[str, str]:
    if not tone:
        return _item("tone", "Hook and caption tone", "pass" if not hook else "warn",
                     "No text of yours to check." if not hook else "Tone check didn't run.")
    status = {"ok": "pass", "risky": "warn", "breaks_rule": "fail"}.get(tone.get("status"), "warn")
    detail = tone.get("reason") or ("Fits the brief's tone rules." if status == "pass" else "")
    return _item("tone", "Hook and caption tone", status, detail)


def _brand_logo(r: Dict[str, Any], drawn: Optional[Dict[str, Any]], seen: Optional[Dict[str, Any]],
                canvas_w: int = 1080) -> Optional[Dict[str, str]]:
    """The brand's own logo: there when the brief requires it, and really showing."""
    need = r.get("brand_logo", "unstated")
    label = "Brand logo on screen"
    if need == "forbidden":
        return _item("brand_logo", "No logos", "fail" if drawn else "pass",
                     "A logo is drawn on — the brief doesn't allow logos." if drawn else "None, as the brief asks.")
    if not drawn:
        if need == "required":
            return _item("brand_logo", label, "fail",
                         "The brief requires the brand's logo on screen — add the logo file on the campaign, then run it again.")
        return None
    share = f"{drawn['box'][2] / max(1, canvas_w) * 100:.0f}% of the width"
    where = f", {drawn['why']}" if drawn.get("why") else ""
    if not seen or seen.get("ok") is None:
        return _item("brand_logo", label, "warn",
                     "Couldn't confirm the logo shows in the finished clip. " + ((seen or {}).get("error") or ""))
    if seen["ok"]:
        return _item("brand_logo", label, "pass", f"Showing start to end, {share}{where}.")
    return _item("brand_logo", label, "fail" if need == "required" else "warn",
                 "The logo doesn't show where it was drawn — hidden or covered.")


def _platform(r: Dict[str, Any], platform: str) -> Dict[str, str]:
    no_dupes = r["posting"].get("no_duplicates")
    if not platform:
        return _item("platform", "Where to post", "fail" if no_dupes else "warn",
                     "More versions of this clip than platforms — one would be the same clip twice on one account."
                     if no_dupes else "No platform assigned.")
    if r["platforms"] and platform not in r["platforms"]:
        names = ", ".join(campaign.PLATFORM_NAMES.get(p, p) for p in r["platforms"])
        return _item("platform", "Where to post", "fail",
                     f"{campaign.PLATFORM_NAMES.get(platform, platform)} isn't allowed — only {names}.")
    return _item("platform", "Where to post", "pass", campaign.PLATFORM_NAMES.get(platform, platform))


def check_overlay(rb: Dict[str, Any], source: Path, output: Path, made: Dict[str, Any],
                  post: Dict[str, Any], hook: str, tone: Optional[Dict[str, str]],
                  src_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A clip-bank clip: the footage, sound and length must be the source's own."""
    r = campaign.resolve(rb)
    a = r["allowed"]
    checks: List[Dict[str, str]] = []
    src = src_info or overlay.inspect(source)
    out = overlay.inspect(output)
    fps = src.get("fps") or 30.0

    checks.append(_length(r, out["duration"]))

    f_in, f_out = overlay.frame_count(source), overlay.frame_count(output)
    whole_needed = r["full_clip"] or not a["trim"]
    if f_in is None or f_out is None:
        checks.append(_item("full_clip", "Whole clip, start to end", "warn" if whole_needed else "pass",
                            "Couldn't count the frames."))
    else:
        same = f_in == f_out and abs(src["duration"] - out["duration"]) <= max(0.1, 1.5 / fps)
        status = "pass" if same else ("fail" if whole_needed else "warn")
        checks.append(_item("full_clip", "Whole clip, start to end", status,
                            f"All {f_out} frames, same as the original." if same else
                            f"{f_out} frames vs {f_in} in the original."))

    footage_rules = not (a["crop"] and a["zoom"]) or r["full_clip"]
    drawn = made.get("logo")
    try:
        drift = overlay.footage_drift(source, output, made["layout"], made.get("hook_ass", ""),
                                      out["duration"], f_out or 0,
                                      covered=[drawn["box"]] if drawn else None)
    except Exception as exc:                                  # measurement trouble is a flag, not a pass
        drift = {"psnr_min": None, "error": str(exc)[:160]}
    psnr = drift.get("psnr_min")
    how = "fitted inside a 9:16 frame, nothing cropped" if made["layout"]["scaled"] else "at its own size"
    if psnr is None:
        # Not being able to show the footage is untouched isn't the same as it being fine.
        checks.append(_item("footage", "Footage untouched", "fail" if footage_rules else "warn",
                            "Couldn't confirm the footage matches the original. " + drift.get("error", "")))
    else:
        shift, spread = drift.get("colour_shift", 0.0), drift.get("contrast_change", 0.0)
        if psnr < PSNR_FLOOR:
            ok, why = False, f"Doesn't line up with the original outside the hook ({psnr:.0f} dB) — cropped, zoomed, moved or out of step."
        elif shift > COLOUR_SHIFT_MAX or spread > CONTRAST_MAX:
            ok, why = False, (f"Colours differ from the original (level shift {shift:.1f}, contrast "
                              f"{spread * 100:.1f}%) — looks graded or filtered.")
        else:
            ok, why = True, (f"Matches the original outside the hook{' and logo' if drawn else ''} "
                             f"({psnr:.0f} dB), {how}.")
        checks.append(_item("footage", "Footage untouched",
                            "pass" if ok else ("fail" if footage_rules else "warn"), why))

    if src["has_audio"]:
        if not made.get("audio_copied", True):
            # Re-encoded because a post can't carry the original codec: what matters
            # is whether the sound itself came through — same level, same waveform.
            try:
                match = overlay.audio_match(source, output)
            except Exception:
                match = None
            raw = (src.get("audio_codec") or "audio").lower()
            codec = "PCM" if raw.startswith("pcm") else {"vorbis": "Vorbis", "opus": "Opus", "flac": "FLAC",
                                                         "alac": "ALAC"}.get(raw, raw.upper())
            if match and match["snr_db"] >= AUDIO_SNR_FLOOR and abs(match["gain_db"]) <= AUDIO_GAIN_MAX:
                checks.append(_item("audio", "Audio untouched", "pass",
                                    f"Same sound, same level — converted from {codec} to AAC because TikTok and "
                                    f"Instagram don't take {codec} (matches the original to {match['snr_db']:.0f} dB)."))
            else:
                why = (f"The sound differs from the original (level {match['gain_db']:+.1f} dB, match "
                       f"{match['snr_db']:.0f} dB)." if match else "Couldn't compare the sound with the original.")
                checks.append(_item("audio", "Audio untouched", "fail" if not a["audio"] else "warn", why))
        else:
            fp_in, fp_out = overlay.audio_fingerprint(source), overlay.audio_fingerprint(output)
            same = bool(fp_in) and fp_in == fp_out
            if not same:
                pk_in, pk_out = overlay.audio_packets_hash(source), overlay.audio_packets_hash(output)
                same = bool(pk_in) and pk_in == pk_out
            checks.append(_item("audio", "Audio untouched",
                                "pass" if same else ("fail" if not a["audio"] else "warn"),
                                "Identical to the original, sample for sample." if same else
                                "The sound doesn't match the original."))
    else:
        checks.append(_item("audio", "Audio untouched", "pass", "The original has no sound."))

    if (hook or "").strip() and not a["hook"]:
        checks.append(_item("hook", "Text on screen", "fail", "The brief doesn't allow on-screen text."))
    seen = None
    if drawn:
        try:
            prep = brandlogo.prepare(Path(drawn["src"]), tuple(made["layout"]["canvas"]),
                                     brandlogo.OVERLAY_MAX_W, brandlogo.OVERLAY_MAX_H)
            seen = brandlogo.visible(output, prep, tuple(drawn["box"][:2]), out["duration"], f_out or 0)
        except Exception as exc:
            seen = {"ok": None, "error": str(exc)[:160]}
    logo_item = _brand_logo(r, drawn, seen, made["layout"]["canvas"][0])
    if logo_item:
        checks.append(logo_item)
    checks += _caption(r, post)
    checks.append(_platform(r, post.get("platform", "")))
    checks.append(_tone(tone, hook))
    result = summarize(checks)
    result["measured"] = {"frames": f_out, "frames_source": f_in, "duration": round(out["duration"], 3),
                          "psnr_min": psnr, "psnr": drift.get("psnr"), "hook_area": drift.get("covered"),
                          "colour_shift": drift.get("colour_shift"), "contrast_change": drift.get("contrast_change"),
                          "logo": seen}
    return result


def check_source(rb: Dict[str, Any], clip: Dict[str, Any], edits: Dict[str, Any], output: Path,
                 post: Dict[str, Any], hook: str, tone: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """A clip cut from longer footage: length, plus every switch the brief forbids
    is confirmed off in what was actually rendered."""
    r = campaign.resolve(rb)
    a = r["allowed"]
    checks: List[Dict[str, str]] = []
    try:
        seconds = overlay.inspect(output)["duration"]
    except Exception:
        seconds = float(clip.get("end", 0)) - float(clip.get("start", 0)) - float(clip.get("saved") or 0)
    checks.append(_length(r, seconds))

    parts = clip.get("parts") or []
    smart = edits.get("smart") or {}                  # Smart Stitch: what the render really added
    joins = len(parts) if len(parts) > 1 else 1
    joins += (1 if smart.get("teaser") else 0) + len(smart.get("inserts") or [])
    if not a["stitch"]:
        checks.append(_item("stitch", "One unbroken moment", "pass" if joins <= 1 else "fail",
                            "A single stretch of the video." if joins <= 1 else
                            f"Joins {joins} moments (a teaser or inserts count) — the brief doesn't allow that."))
    if (smart.get("rewind") or {}).get("sound") and not a["music"]:
        checks.append(_item("music", "No added sound", "fail", "The teaser's rewind has a sound effect — "
                                                               "the brief doesn't allow added sound."))
    if smart.get("rewind") and not a["speed"]:
        checks.append(_item("speed", "No speed changes", "fail", "The teaser's rewind plays fast in reverse — "
                                                                 "the brief doesn't allow speed changes."))
    if not a["cut"]:
        cut = bool(edits.get("tighten")) or float(clip.get("saved") or 0) > 0.05
        checks.append(_item("cut", "Nothing cut from the middle", "fail" if cut else "pass",
                            "Dead air was cut out." if cut else "Plays straight through."))
    if not a["crop"]:
        cropped = edits.get("layout") not in ("blur",)
        checks.append(_item("crop", "Whole frame, no crop", "fail" if cropped else "pass",
                            "The picture is cropped to fill the frame." if cropped else
                            "The whole frame is kept, with bars."))
    if not (a["zoom"] and a["crop"]):
        checks.append(_item("zoom", "No zooms or camera moves", "fail" if edits.get("motion") else "pass",
                            "Camera motion is on." if edits.get("motion") else "Camera stays still."))
    if not a["audio"]:
        changed = edits.get("normalize_audio", True) is not False
        checks.append(_item("audio", "Audio levels untouched", "fail" if changed else "pass",
                            "Loudness is being levelled." if changed else "Sound as recorded."))
    if r["captions_required"]:
        on = bool(edits.get("captions_on", True))
        checks.append(_item("captions", "Captions burned in", "pass" if on else "fail",
                            "On." if on else "The brief requires captions."))
    elif not a["captions"] and edits.get("captions_on", True):
        checks.append(_item("captions", "No captions", "fail", "The brief doesn't allow burned-in captions."))
    if not a["hook"] and (edits.get("hook_on", True) and (hook or "").strip() or edits.get("headline_on")):
        checks.append(_item("hook", "Text on screen", "fail", "The brief doesn't allow on-screen text."))
    if not a["watermark"] and edits.get("logo"):
        checks.append(_item("watermark", "No watermark", "fail", "Your logo is on — the brief doesn't allow it."))
    drawn, seen = None, None
    logo_src = edits.get("brand_logo") or ""
    if logo_src and Path(logo_src).exists():
        try:
            canvas = overlay.frame_size(output) or (1080, 1920)
            prep = brandlogo.prepare(Path(logo_src), canvas, brandlogo.SOURCE_MAX_W, brandlogo.SOURCE_MAX_H)
            box = brandlogo.source_box(canvas, prep["w"], prep["h"])
            drawn = {"src": logo_src, "box": [box[0], box[1], prep["w"], prep["h"]], "why": "at the top"}
            seen = brandlogo.visible(output, prep, box, seconds, overlay.frame_count(output) or 0)
        except Exception as exc:
            drawn = drawn or {"src": logo_src, "box": [0, 0, 0, 0], "why": ""}
            seen = {"ok": None, "error": str(exc)[:160]}
    logo_item = _brand_logo(r, drawn, seen)
    if logo_item:
        checks.append(logo_item)

    checks += _caption(r, post)
    checks.append(_platform(r, post.get("platform", "")))
    checks.append(_tone(tone, hook if edits.get("hook_on", True) else ""))
    result = summarize(checks)
    result["measured"] = {"duration": round(seconds, 3)}
    return result


def check_edit(rb: Dict[str, Any], timeline: Dict[str, Any], output: Path, post: Dict[str, Any],
               hook: str, tone: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """A music edit (several moments, cut to a song): measured on what was rendered —
    the length, the song, every effect the brief forbids — plus the caption and the tone."""
    r = campaign.resolve(rb)
    a = r["allowed"]
    fx = timeline.get("effects") or {}
    segs = timeline.get("segments") or []
    checks: List[Dict[str, str]] = []
    try:
        seconds = overlay.inspect(output)["duration"]
    except Exception:
        seconds = float(timeline.get("length") or 0)
    checks.append(_length(r, seconds))
    moments = len({s["moment"] for s in segs})
    if not a["stitch"]:
        checks.append(_item("stitch", "One unbroken moment", "fail" if moments > 1 else "pass",
                            f"Joins {moments} moments — the brief doesn't allow that." if moments > 1 else
                            "A single moment."))
    if not a["crop"]:
        checks.append(_item("crop", "Whole frame, no crop", "fail", "An edit crops the picture to vertical."))
    music = timeline.get("music") or {}
    if music and float(music.get("level") or 0) > 0:
        checks.append(_item("music", "Added music", "pass" if a["music"] else "fail",
                            "The brief allows music." if a["music"] else "A song is added — the brief doesn't allow music."))
    retimed = any(abs(float(v) - 1.0) > 0.01 for s in segs for _, v in (s.get("curve") or []))
    if not a["speed"]:
        checks.append(_item("speed", "No speed changes", "fail" if retimed else "pass",
                            "Slow-mo or speed ramps are on." if retimed else "Plays at normal speed."))
    moved = any(s.get("pulses") or s.get("shakes") or abs(float(s.get("zoom") or 1) - 1) > 0.01 for s in segs) \
        or bool(fx.get("push"))
    if not a["zoom"]:
        checks.append(_item("zoom", "No zooms or camera moves", "fail" if moved else "pass",
                            "Zoom punches, shakes or push-ins are on." if moved else "Camera stays still."))
    words_on = bool(hook.strip()) or any(s.get("text") for s in segs) or \
        (timeline.get("text") in ("subtitle", "build") and any(s.get("words") for s in segs) and fx.get("text"))
    if not a["hook"] and words_on:
        checks.append(_item("hook", "Text on screen", "fail", "The brief doesn't allow on-screen text."))
    captions = timeline.get("text") in ("subtitle", "build") and bool(fx.get("text"))
    if r["captions_required"]:
        checks.append(_item("captions", "Captions burned in", "pass" if captions else "fail",
                            "His words are on screen." if captions else
                            "The brief requires captions — Cinematic and Motivation edits show his words."))
    elif captions and not a["captions"]:
        checks.append(_item("captions", "No captions", "fail", "The brief doesn't allow burned-in captions."))
    if fx.get("letterbox") and not a["borders"]:
        checks.append(_item("borders", "No bars", "fail", "Cinema bars are on — the brief doesn't allow bars."))
    if not a["audio"] and float(timeline.get("voice") or 0) > 0:
        checks.append(_item("audio", "Audio levels untouched", "fail", "An edit mixes and levels his voice."))
    logo = _brand_logo(r, None, None)
    if logo:
        if r.get("brand_logo") == "required":
            logo["detail"] = "The brief requires the brand's logo on screen — edits can't add it yet. Make clips instead."
        checks.append(logo)
    checks += _caption(r, post)
    checks.append(_tone(tone, hook))
    result = summarize(checks)
    result["measured"] = {"duration": round(seconds, 3)}
    return result
