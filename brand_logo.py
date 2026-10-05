"""Brand logos on campaign clips, and the hook's size — offline.

usage: python tests/brand_logo.py <folder with vert.mp4, land.mp4>

Campaigns like Gamebred FC's require the brand's own logo on every post
("Gamebred FC logo must appear somewhere visible on screen", "Missing or
hidden logo" = rejected). These checks show the rulebook picks that up, the
renders draw the logo where it stays clear of the hook and the footage, and
the gate passes a clip only when the logo really shows start to end — with
the gate failing on purpose-broken files, since a gate that never fails
proves nothing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import brandlogo, campaign, captions, compliance, overlay, render  # noqa: E402

GAMEBRED_BRIEF = """Gamebred Bareknuckle MMA Event Clipping
You do not need a dedicated page for this campaign to participate, you can post on any page
WHAT TO DO
Download and clip content from the Gamebred Fighting Championships event. Post highlights including knockouts, crazy moments, funny moments, celebrations, and memes from the prelim fights.
TAGGING (REQUIRED)
You MUST tag the official Gamebred Fighting Championships accounts in the caption of your post.
Instagram: @gamebredfightingchampionships
ON-SCREEN TEXT (OPTIONAL)
If you add on-screen text, it should relate directly to what is happening in the clip.
LOGO (OPTIONAL)
You can if you want include the official Gamebred Fighting Championships logo somewhere visible on screen.
Logo placement guidelines:
Place in a corner or natural area of the frame
Must be clearly visible — do not hide it or make it too small
Do not distort or stretch the logo
REQUIREMENTS
Gamebred FC logo must appear somewhere visible on screen
Video must be AT LEAST 10 seconds long
NOT ALLOWED
Missing or hidden logo
"""

GAMEBRED_ANSWER = {
    "name": "Gamebred Bareknuckle MMA", "brand": "Gamebred FC", "platform": "whop", "mode": "overlay",
    "mode_quote": "Download and clip content from the Gamebred Fighting Championships event.",
    "permissions": {k: {"value": "unstated", "quote": ""} for k in campaign.PERMISSIONS},
    "full_clip": {"value": False, "quote": ""},
    "length": {"min_seconds": 10, "max_seconds": None, "quote": "Video must be AT LEAST 10 seconds long"},
    "caption": {"required_one_of": [], "required_all": [], "hashtags": [],
                "mentions": ["@gamebredfightingchampionships"], "extra_text_allowed": True,
                "other_hashtags": "unstated", "quote": "Instagram: @gamebredfightingchampionships"},
    # The brief says "(OPTIONAL)" in one place and "must appear" in another: the stricter reading.
    "brand_logo": {"value": "required", "quote": "Gamebred FC logo must appear somewhere visible on screen",
                   "rules": ["Place it in a corner or natural area of the frame.", "Never stretch or distort it."],
                   "link": "https://drive.google.com/example"},
    "hooks": {"examples": [], "rules": ["On-screen text must relate directly to the clip."]},
    "platforms": ["tiktok", "instagram"],
    "grey_areas": [],
}
GAMEBRED_ANSWER["permissions"]["hook"] = {"value": "yes", "quote": "If you add on-screen text, it should relate directly to what is happening in the clip."}

FAILS = []


def expect(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def status_of(result, cid):
    return next((c["status"] for c in result["checks"] if c["id"] == cid), None)


def detail_of(result, cid):
    return next((c["detail"] for c in result["checks"] if c["id"] == cid), "")


def make_logo(path: Path, dark: bool = True) -> Path:
    """A brand file the way brands hand them out: a full 9:16 transparent sheet
    with a wordmark in the middle (black with a red edge, like Gamebred's)."""
    img = np.zeros((1920, 1080, 4), np.uint8)
    fill = (20, 20, 20) if dark else (245, 245, 245)
    for thick, colour in ((22, (30, 30, 210)), (12, fill)):
        cv2.putText(img, "GAMEBRED", (120, 990), cv2.FONT_HERSHEY_DUPLEX, 4.2, (*colour, 255), thick, cv2.LINE_AA)
    cv2.imwrite(str(path), img)
    return path


def main(folder: Path) -> None:
    work = folder / "logo_test"
    work.mkdir(exist_ok=True)
    dark = make_logo(work / "brand_dark.png", True)
    light = make_logo(work / "brand_light.png", False)

    print("== rulebook")
    rb = campaign.normalize(GAMEBRED_ANSWER, GAMEBRED_BRIEF)
    r = campaign.resolve(rb)
    expect(rb["brand_logo"]["verified"] and rb["brand_logo"]["line"], "the logo rule is quoted from the brief")
    expect(r["brand_logo"] == "required" and campaign.needs_brand_logo(rb), "a required brand logo is read as required")
    expect(not r["allowed"]["watermark"], "the clipper's own watermark stays a separate, unallowed thing")
    expect(rb["brand_logo"]["link"].startswith("https://"), "where to get the logo is kept")
    edited = campaign.merge_user_edits(rb, {"brand_logo": "allowed"}, GAMEBRED_BRIEF)
    expect(campaign.resolve(edited)["brand_logo"] == "allowed" and not campaign.needs_brand_logo(edited),
           "the card can change the logo rule")
    no_logo_brief = "\n".join(l for l in GAMEBRED_BRIEF.splitlines() if "logo" not in l.lower())
    old = campaign.normalize({**GAMEBRED_ANSWER, "brand_logo": None}, no_logo_brief)
    expect(campaign.resolve(old)["brand_logo"] == "unstated" and campaign.wants_brand_logo(old),
           "a brief that says nothing: logo drawn only if you add one")
    banned = campaign.merge_user_edits(rb, {"brand_logo": "forbidden"}, GAMEBRED_BRIEF)
    expect(campaign.clamp_edits({"brand_logo": str(dark)}, banned)["brand_logo"] == "",
           "a brief that bans logos keeps one off in the editor too")

    # The real reader called it optional (it quoted "LOGO (OPTIONAL)… You can if you want");
    # the brief requires it elsewhere, and a missing logo is a rejection.
    lenient = campaign.normalize({**GAMEBRED_ANSWER, "brand_logo": {
        "value": "allowed", "quote": "You can if you want include the official Gamebred Fighting Championships logo somewhere visible on screen.",
        "rules": []}}, GAMEBRED_BRIEF)
    expect(campaign.resolve(lenient)["brand_logo"] == "required" and any("requires it" in n for n in lenient["notes"]),
           "an 'optional' reading is overruled by the line that requires the logo, and the card says so")

    print("\n== preparing the logo")
    prep = brandlogo.prepare(dark, (1080, 1920), brandlogo.OVERLAY_MAX_W, brandlogo.OVERLAY_MAX_H)
    expect(prep["tone"] == "dark", "a black logo is recognised as dark (gets a light halo)")
    expect(prep["w"] % 2 == 0 and prep["h"] % 2 == 0, "even-sized, so 4:2:0 video can place it exactly")
    expect(prep["w"] <= 1080 * 0.5 and prep["h"] <= 1920 * 0.1, f"cut out of its transparent sheet ({prep['w']}x{prep['h']})")
    rgba = cv2.imread(str(dark), cv2.IMREAD_UNCHANGED)
    ys, xs = np.where(rgba[:, :, 3] > 8)
    src_ratio = (xs.max() - xs.min() + 1) / (ys.max() - ys.min() + 1)
    py, px = np.where(prep["solid"] > 0)
    out_ratio = (px.max() - px.min() + 1) / (py.max() - py.min() + 1)
    expect(abs(out_ratio / src_ratio - 1) < 0.04, f"scaled evenly, not stretched ({src_ratio:.2f} → {out_ratio:.2f})")
    expect(brandlogo.prepare(light, (1080, 1920), 0.46, 0.085)["tone"] == "light", "a white logo is recognised as light")

    print("\n== hook size")
    lay = {"vertical": True, "canvas": [1080, 1920], "content": [0, 0, 1080, 1920], "bars": "none", "scaled": False}
    ass = overlay.hook_ass("Knockout", {"hook_style": "bold", "hook_position": "top"}, lay, 3.0,
                           work / "size.ass")
    box = brandlogo.hook_box(str(ass), (1080, 1920), overlay.FONTS_DIR)
    cap = box[3] - box[1] if box else 0
    print(f"  one word of hook: capitals {cap}px tall on a 1080x1920 frame")
    expect(46 <= cap <= 56, "hook capitals ~4.6% of the width (they were ~3% before the line-height fix)")

    print("\n== clip-bank clips with the logo")
    look = {"hook_style": "neon", "hook_color": "pink", "hook_position": "top"}
    tone_ok = {"status": "ok", "reason": ""}
    for name in ("vert.mp4", "land.mp4"):
        src = folder / name
        info = overlay.inspect(src)
        hook = "He did not see that coming"
        post = campaign.build_post(rb, 0, "instagram")
        made = overlay.render(src, f"logo_{src.stem}", hook, look, info, logo=dark)
        res = compliance.check_overlay(rb, src, made["file"], made, post, hook, tone_ok, info)
        lb = made["logo"]["box"]
        print(f"  {name}: {res['status']} · logo {lb} {made['logo']['why']} · {detail_of(res, 'brand_logo')}")
        print(f"    footage: {detail_of(res, 'footage')} · seen {res['measured'].get('logo')}")
        expect(status_of(res, "brand_logo") == "pass", f"{name}: the logo is seen start to end")
        expect(status_of(res, "footage") == "pass", f"{name}: the footage outside hook and logo still matches the original")
        expect(status_of(res, "mentions") == "pass", f"{name}: the Instagram tag is in the caption")
        hb = brandlogo.hook_box(made["hook_ass"], tuple(made["layout"]["canvas"]), overlay.FONTS_DIR)
        overlap = hb and not (lb[1] >= hb[3] or lb[1] + lb[3] <= hb[1])
        expect(not overlap, f"{name}: the logo doesn't sit on the hook")
        if not made["layout"]["vertical"]:
            x0, y0, fw, fh = made["layout"]["content"]
            expect(lb[1] >= y0 + fh or lb[1] + lb[3] <= y0, f"{name}: in a bar, covering none of the footage")

        # The same clip without the logo, while the brief requires one: blocked.
        bare = overlay.render(src, f"logo_{src.stem}_bare", hook, look, info, logo=None)
        res = compliance.check_overlay(rb, src, bare["file"], bare, post, hook, tone_ok, info)
        expect(status_of(res, "brand_logo") == "fail" and res["status"] == "blocked",
               f"{name}: no logo on a logo-required campaign is blocked")
        # A file that says it drew the logo but doesn't show it.
        res = compliance.check_overlay(rb, src, bare["file"], {**bare, "logo": made["logo"]}, post, hook, tone_ok, info)
        expect(status_of(res, "brand_logo") == "fail", f"{name}: a logo that isn't really there is caught")
        # Covered part-way through: still not 'visible on screen'.
        covered = work / f"covered_{src.stem}.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(made["file"]), "-vf",
                        f"drawbox=x={lb[0]}:y={lb[1]}:w={lb[2]}:h={lb[3]}:color=black:t=fill:enable='gte(t,{info['duration'] / 3:.2f})'",
                        "-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p", "-c:a", "copy", str(covered)], check=True)
        res = compliance.check_overlay(rb, src, covered, made, post, hook, tone_ok, info)
        expect(status_of(res, "brand_logo") == "fail", f"{name}: a logo hidden part of the way through is caught")
        # A brief that bans logos, with one drawn on.
        res = compliance.check_overlay(banned, src, made["file"], made, post, hook, tone_ok, info)
        expect(status_of(res, "brand_logo") == "fail", f"{name}: a logo on a no-logos campaign is caught")

    print("\n== camera audio (PCM) can't go in a post as it is")
    pcm = work / "vert_pcm.mov"
    loud = work / "vert_pcm_loud.mov"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(folder / "vert.mp4"), "-c:v", "copy",
                    "-c:a", "pcm_s24le", str(pcm)], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(pcm), "-c:v", "copy", "-af", "volume=1.06",
                    "-c:a", "pcm_s24le", str(loud)], check=True)
    info = overlay.inspect(pcm)
    made = overlay.render(pcm, "logo_pcm", "He did not see that coming", look, info, logo=dark)
    acodec = overlay.inspect(made["file"])["audio_codec"]
    res = compliance.check_overlay(rb, pcm, made["file"], made, campaign.build_post(rb, 0, "tiktok"),
                                   "He did not see that coming", tone_ok, info)
    print(f"  {acodec}: {detail_of(res, 'audio')}")
    expect(acodec == "aac" and not made["audio_copied"], "PCM goes out as AAC, which every platform plays")
    expect(status_of(res, "audio") == "pass", "…and the gate measures it's the same sound at the same level")
    res = compliance.check_overlay(rb, loud, made["file"], made, campaign.build_post(rb, 0, "tiktok"),
                                   "He did not see that coming", tone_ok, overlay.inspect(loud))
    expect(status_of(res, "audio") == "fail", "a half-dB level change still fails")

    print("\n== clips cut from longer footage")
    ass_txt = captions.build_ass([], 4.0, hook="Hook here", headline="Headline", out_path=work / "off.ass",
                                 top_offset=200).read_text(encoding="utf-8")
    expect(",350,1" in ass_txt.split("Style: Hook")[1].split("\n")[0] and ",270,1" in ass_txt.split("Style: Headline")[1].split("\n")[0],
           "the hook and headline move down to make room for the logo")
    src = folder / "land.mp4"
    srb = campaign.merge_user_edits(rb, {"mode": "source"}, GAMEBRED_BRIEF)
    edits = {"layout": "blur", "hook": "He did not see that coming", "hook_on": True, "captions_on": False,
             "motion": False, "tighten": False, "brand_logo": str(dark), "headline_on": False}
    made = render.render_clip(src, "logo_source", 2.0, 14.0, [], edits, True, None, (1280, 720))
    post = campaign.build_post(srb, 0, "instagram")
    res = compliance.check_source(srb, {"start": 2.0, "end": 14.0, "saved": 0, "parts": []}, edits,
                                  Path(made["file"]), post, edits["hook"], tone_ok)
    print(f"  {res['status']} · {detail_of(res, 'brand_logo')}")
    expect(status_of(res, "brand_logo") == "pass", "source clip: the logo heads the frame, seen start to end")
    no_logo = {**edits, "brand_logo": ""}
    made = render.render_clip(src, "logo_source_bare", 2.0, 14.0, [], no_logo, True, None, (1280, 720))
    res = compliance.check_source(srb, {"start": 2.0, "end": 14.0, "saved": 0, "parts": []}, no_logo,
                                  Path(made["file"]), post, edits["hook"], tone_ok)
    expect(status_of(res, "brand_logo") == "fail", "source clip: missing required logo blocked")

    print("\nFAILED:" if FAILS else "\nall checks behaved", FAILS or "")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
