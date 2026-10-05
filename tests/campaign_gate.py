"""Campaign Mode, offline: the rulebook from a brief, the overlay render, and the gate.

usage: python tests/campaign_gate.py <folder with vert.mp4, land.mp4, short_noaudio.mp4> [brief.txt]

No Claude calls: the reader's answer for the Ketone-IQ brief is stood in by
KETONE_ANSWER below (real quotes from the brief, plus one made-up quote and
one made-up caption line, which the checks must catch). Then every gate
check is shown failing on a deliberately broken file — a gate that never
fails proves nothing.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import campaign, compliance, overlay  # noqa: E402

KETONE_ANSWER = {
    "name": "Ketone-IQ — Shark Tank edits", "brand": "Ketone-IQ", "platform": "vyro", "mode": "overlay",
    "mode_quote": "An approved clip bank will be provided to you. You must post the full clip as provided.",
    "pay": {"per_1k_views": None, "min_views": 5000, "max_per_post": 1000, "budget": None,
            "quote": "Each post must reach 5,000 views to be eligible for payout."},
    "permissions": {
        "trim": {"value": "no", "quote": "Do not trim, cut, or remove any portion of the provided edit."},
        "cut": {"value": "no", "quote": "Do not crop, trim, recut, remix, or otherwise alter the provided clips."},
        "crop": {"value": "no", "quote": "Do not crop, trim, recut, remix, or otherwise alter the provided clips."},
        "zoom": {"value": "no", "quote": "Do not crop, trim, recut, remix, or otherwise alter the provided clips."},
        "stitch": {"value": "no", "quote": "Do not add unrelated footage or outside visuals."},
        "speed": {"value": "unstated", "quote": ""},
        "audio": {"value": "no", "quote": "Do not alter the audio of the provided clips."},
        "music": {"value": "no", "quote": "Do not alter the audio of the provided clips."},
        "outside": {"value": "no", "quote": "Do not add unrelated footage or outside visuals."},
        "hook": {"value": "yes", "quote": "you are encouraged to add creative enhancements such as on-screen hooks, text, borders, visual effects"},
        "captions": {"value": "unstated", "quote": ""},
        "borders": {"value": "yes", "quote": "on-screen hooks, text, borders, visual effects, or anything else"},
        # a quote that is NOT in the brief: must come out unverified, and so "no"
        "watermark": {"value": "yes", "quote": "Feel free to add your own watermark to every clip."},
    },
    "full_clip": {"value": True, "quote": "You must post the full clip as provided."},
    "length": {"min_seconds": 15, "max_seconds": None, "quote": "Minimum video length: 15 seconds"},
    "caption": {
        "required_one_of": ["Crazy how far Ketone-IQ has come", "Ketone-IQ really started like THIS",
                            "The nerds were onto something 👀", "Ketone-IQ before it was everywhere 👀",
                            "Ketone-IQ changed my life forever"],          # the last one is invented
        "required_all": [], "hashtags": ["#KetoneIQPartner"], "mentions": [],
        "extra_text_allowed": True, "other_hashtags": "no",
        "quote": "Every submission must include one of the following mandatory text in the caption:"},
    "hooks": {"examples": ["Sharks go OFF on these nerds", "Shark Tank absolutely COOKED these nerds",
                           "Joe Rogan’s 6-hour brain cheat code??"],
              "rules": ["Keep on-screen text aligned with the campaign messaging."]},
    "tone_avoid": ["unrelated topics", "controversial discussion", "negative sentiment"],
    "platforms": ["tiktok"],
    "posting": {"public": True, "comments_on": True, "no_paid_boost": True, "no_duplicates": True, "collab": "yes"},
    "footage": {"kind": "clip_bank", "links": ["https://f.io/v5fbvabK"]},
    "other_rules": ["Do not use logos, hashtags or watermarks not affiliated with the campaign."],
    "grey_areas": [{"permission": "zoom", "question": "The brief encourages 'visual effects' but forbids altering the clips. Are zoom effects allowed?",
                    "quote": "you are encouraged to add creative enhancements such as on-screen hooks, text, borders, visual effects"}],
}

FAILS = []


def expect(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def status_of(result, cid):
    return next((c["status"] for c in result["checks"] if c["id"] == cid), None)


def main(folder: Path, brief_path: Path) -> None:
    brief = brief_path.read_text(encoding="utf-8")
    rb = campaign.normalize(KETONE_ANSWER, brief)
    r = campaign.resolve(rb)
    print("== rulebook")
    for row in campaign.card(rb)["permissions"]:
        print(f"  {row['label']:34} {'yes' if row['allowed'] else 'no ':3}  [{row['source']}] {row['why']}")
    for n in rb["notes"]:
        print("  note:", n)
    expect(not r["allowed"]["watermark"], "invented watermark quote is not trusted")
    expect(not r["allowed"]["zoom"], "grey area resolves to no")
    expect(r["allowed"]["hook"] and r["allowed"]["borders"], "hooks and borders allowed (verified)")
    expect("Ketone-IQ changed my life forever" not in r["one_of"], "invented caption line kept out of captions")
    expect("Ketone-IQ really started like THIS 💀" in r["one_of"], "dropped emoji restored from the brief")
    expect(r["hashtags"] == ["#KetoneIQPartner"], "hashtag in the brief's spelling")
    expect(r["min_len"] == 15 and r["full_clip"], "15s minimum, whole clip")
    expect(rb["perms"]["trim"]["line"] == 8, "trim rule cited at line 8")

    post = campaign.build_post(rb, 1, "tiktok")
    print("== caption\n" + post["text"])
    expect(post["text"].startswith("Ketone-IQ really started like THIS 💀"), "caption uses the brief's line verbatim")
    expect("#fyp" not in post["text"], "no own hashtags when the brief bans them")
    print("  platforms for 3 versions:", campaign.assign_platforms(rb, 3))

    work = folder / "out"
    work.mkdir(exist_ok=True)

    look = {"hook_style": "neon", "hook_color": "pink", "hook_position": "top", "hook_hold": "whole"}

    for name in ("vert.mp4", "land.mp4", "short_noaudio.mp4"):
        src = folder / name
        print(f"\n== {name}")
        info = overlay.inspect(src)
        made = overlay.render(src, f"test_{src.stem}", "Shark Tank absolutely COOKED these nerds", look, info)
        print("  layout:", made["layout"], "audio copied:", made["audio_copied"])
        res = compliance.check_overlay(rb, src, made["file"], made, post,
                                       "Shark Tank absolutely COOKED these nerds",
                                       {"status": "ok", "reason": ""}, info)
        for c in res["checks"]:
            print(f"  {c['status']:5} {c['label']:28} {c['detail']}")
        print("  =>", res["status"], res["measured"])
        shutil.copy(made["file"], work / f"{src.stem}_hooked.mp4")
        if name == "short_noaudio.mp4":
            expect(status_of(res, "length") == "fail", "8s clip fails the 15s minimum")
            continue
        expect(res["status"] == "ready", f"{name}: clean render passes every check")

        # ---- now break it on purpose, one way at a time
        out = made["file"]
        W, H = made["layout"]["canvas"]
        enc = ["-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p", "-c:a", "copy"]
        broken = {
            "trimmed": (["-ss", "1", "-i", str(out)] + enc, "full_clip"),
            "louder": (["-i", str(out), "-c:v", "copy", "-af", "volume=2dB", "-c:a", "aac", "-b:a", "128k"], "audio"),
            "zoomed": (["-i", str(out), "-vf", f"crop=iw/1.04:ih/1.04,scale={W}:{H}"] + enc, "footage"),
            "shifted": (["-i", str(out), "-vf", f"crop=iw-16:ih:16:0,pad={W}:{H}:0:0"] + enc, "footage"),
            "resized": (["-i", str(out), "-vf", f"scale={W - 40}:{H - 72}"] + enc, "footage"),
            "regraded": (["-i", str(out), "-vf", "eq=saturation=1.12:contrast=1.04"] + enc, "footage"),
            "brighter": (["-i", str(out), "-vf", "eq=brightness=0.02"] + enc, "footage"),
            "reencoded": (["-i", str(out)] + enc, None),       # nothing changed: must NOT fail
        }
        for label, (args, want) in broken.items():
            bad = work / f"{src.stem}_{label}.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error", *args, "-fps_mode", "passthrough", str(bad)], check=True)
            res = compliance.check_overlay(rb, src, bad, made, post, "x", {"status": "ok", "reason": ""}, info)
            failed = [c["id"] for c in res["checks"] if c["status"] == "fail"]
            m = res["measured"]
            print(f"  {label:9} -> {res['status']:7} failed: {failed}  psnr={m['psnr_min']} "
                  f"shift={m.get('colour_shift')} contrast={m.get('contrast_change')}")
            if want:
                expect(want in failed, f"{name}: '{label}' caught by the {want} check")
            else:
                expect(not failed, f"{name}: a plain re-encode of a good clip still passes")

        bad_post = {**post, "text": post["text"].replace("#KetoneIQPartner", "#fyp")}
        res = compliance.check_overlay(rb, src, out, made, bad_post, "x", {"status": "ok", "reason": ""}, info)
        expect(status_of(res, "hashtags") == "fail", f"{name}: missing disclosure hashtag caught")
        dup = {**post, "platform": ""}
        res = compliance.check_overlay(rb, src, out, made, dup, "x", {"status": "ok", "reason": ""}, info)
        expect(status_of(res, "platform") == "fail", f"{name}: second version on the same account caught")
        res = compliance.check_overlay(rb, src, out, made, post, "x", {"status": "breaks_rule", "reason": "negative"}, info)
        expect(res["status"] == "blocked", f"{name}: a hook that breaks the tone rules blocks the clip")

    # A clip-bank file cut without re-encoding carries lead-in frames its edit
    # list hides. Posted whole, it must still pass the whole-clip check.
    cut = folder / "land_copycut.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "3", "-i", str(folder / "land.mp4"),
                    "-t", "16", "-c", "copy", str(cut)], check=True)
    info = overlay.inspect(cut)
    made = overlay.render(cut, "test_copycut", "Sharks go OFF on these nerds", look, info)
    res = compliance.check_overlay(rb, cut, made["file"], made, post, "Sharks go OFF on these nerds",
                                   {"status": "ok", "reason": ""}, info)
    print(f"\n== stream-copied cut: {res['status']} frames {res['measured']['frames']} vs {res['measured']['frames_source']}")
    expect(status_of(res, "full_clip") == "pass" and res["status"] == "ready",
           "a stream-copied clip with hidden lead-in frames still counts as whole")

    # Auto placement: a clip that already has words at the top gets the hook
    # somewhere else; a clean one gets it at the top.
    vert = folder / "vert.mp4"
    vinfo = overlay.inspect(vert)
    vlayout = overlay.layout_for(vinfo)
    pos, why, _ = overlay.choose_position(vert, vinfo, vlayout)
    expect(pos == "top", f"clean clip: hook at the top ({pos} — {why})")
    texted = folder / "vert_texted.mp4"
    ass = overlay.hook_ass("Brand title already on this clip", {"hook_style": "bold", "hook_position": "top"},
                           vlayout, vinfo["duration"], folder / "brand_title.ass")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(vert), "-vf",
                    f"subtitles='{overlay._escape(ass)}':fontsdir='{overlay._escape(overlay.FONTS_DIR)}'",
                    "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy", str(texted)], check=True)
    pos, why, _ = overlay.choose_position(texted, overlay.inspect(texted), vlayout)
    expect(pos != "top", f"clip with its own title at the top: hook moved ({pos} — {why})")

    print("\nFAILED:" if FAILS else "\nall checks behaved", FAILS or "")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else Path("brief.txt"))
