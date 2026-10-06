"""The campaign look (lookcheck.py + identity.py + compliance), offline with drawn footage.

Run: python tests/campaign_checks.py

Two drawn people (A = the campaign's creator "TJR", B = his friend "Timmy") take
turns talking: the mouth of whoever talks moves with a voice track's words.
A's face is learned from a reference photo and from "solo" clips; a clip where B
does the talking must be blocked under a "TJR must be the main person" brief, and
its hook ("TJR says…") rewritten without one. Claude's frame look is stood in by
canned answers (by tool name), so logos, AI footage, offensive words and the
no-key path are all exercised. One clip goes through the real pipeline: rendered,
looked at, and re-rendered with an offensive word cut out of the middle.
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import base64
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="campaign_checks_")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ["TELEGRAM_BOT_TOKEN"] = ""
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

FAILS = []


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


# --- drawn footage ---------------------------------------------------------------------------

def face(img, cx, cy, r, mouth_open, look="A"):
    """A head YuNet finds. A: light skin, dark hair. B: darker skin, red hair, glasses, chin beard."""
    skin = (140, 170, 215) if look == "A" else (95, 135, 185)
    hair = (30, 40, 60) if look == "A" else (40, 90, 200)
    cv2.rectangle(img, (cx - int(r * 0.25), cy + int(r * 0.8)), (cx + int(r * 0.25), cy + int(r * 1.3)), skin, -1)
    cv2.ellipse(img, (cx, cy + int(r * 2.3)), (int(r * 1.5), int(r * 1.2)), 0, 180, 360,
                (60, 50, 42) if look == "A" else (40, 90, 40), -1)
    cv2.ellipse(img, (cx, cy - int(r * 0.55)), (int(r * 0.95), int(r * 0.7)), 0, 180, 360, hair, -1)
    cv2.ellipse(img, (cx, cy), (int(r * 0.78), r), 0, 0, 360, skin, -1)
    if look == "B":
        cv2.ellipse(img, (cx, cy + int(0.78 * r)), (int(0.32 * r), int(0.2 * r)), 0, 0, 180, (40, 70, 130), -1)
    for dx in (-0.32, 0.32):
        ex, ey = cx + int(dx * r), cy - int(0.18 * r)
        cv2.ellipse(img, (ex, ey), (int(0.16 * r), int(0.08 * r)), 0, 0, 360, (255, 255, 255), -1)
        cv2.circle(img, (ex, ey), int(0.06 * r), (40, 30, 20), -1)
        if look == "A":
            cv2.line(img, (ex - int(0.17 * r), ey - int(0.16 * r)), (ex + int(0.15 * r), ey - int(0.19 * r)),
                     (40, 40, 60), max(2, int(r * 0.04)))
        else:
            cv2.circle(img, (ex, ey), int(0.2 * r), (20, 20, 20), max(2, int(r * 0.035)))
    if look == "B":
        cv2.line(img, (cx - int(0.12 * r), cy - int(0.18 * r)), (cx + int(0.12 * r), cy - int(0.18 * r)),
                 (20, 20, 20), max(2, int(r * 0.035)))
    cv2.ellipse(img, (cx, cy + int(0.12 * r)), (int(0.07 * r), int(0.12 * r)), 0, 0, 360,
                tuple(int(c * 0.8) for c in skin), -1)
    cv2.ellipse(img, (cx, cy + int(0.45 * r)), (int(0.25 * r), int(0.04 * r + 0.30 * r * mouth_open)), 0, 0, 360,
                (50, 50, 140), -1)


def mouth_at(t, salt=0):
    """How open a talking mouth is: a new random target every 0.12 s, eased between."""
    def level(i):
        return 0.1 + 0.9 * ((((i + salt) * 1103515245 + 12345) >> 9) % 100) / 99.0
    i = int(t / 0.12)
    f = t / 0.12 - i
    return level(i) * (1 - f) + level(i + 1) * f


def talking(t):
    return 0.3 < t % 3.0 < 2.4             # sentences with pauses between them


def make(out, seconds, layout, talker, size):
    W, H = size
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    room = np.clip(np.stack([60 + 70 * np.exp(-((xx - W * .3) / W) ** 2), 70 + 60 * (yy / H), 85 + 50 * (xx / W)],
                            -1), 0, 255).astype(np.uint8)
    enc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                            "-r", "30", "-i", "-", "-f", "lavfi", "-t", str(seconds), "-i", "sine=f=180:sample_rate=48000",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", "-c:a", "aac",
                            "-shortest", str(out)], stdin=subprocess.PIPE)
    r = int(min(W, H) * 0.13)
    for i in range(int(seconds * 30)):
        t = i / 30
        img = room.copy()
        for look, xf in layout:
            m = mouth_at(t, 5 if look == "B" else 0) if (look == talker and talking(t)) else 0.0
            sway = int(math.sin(t * 0.8 + (0 if look == "A" else 1.7)) * W * 0.015)
            face(img, int(W * xf) + sway, int(H * 0.38), r, m, look)
        enc.stdin.write(img.tobytes())
    enc.stdin.close()
    enc.wait()


def words_for(seconds, special=None):
    """Six words per sentence, said while the mouth moves; `special` = {index: word}."""
    out, n = [], 0
    for k in range(int(seconds // 3) + 1):
        for i in range(6):
            a = k * 3.0 + 0.35 + i * 0.33
            if a + 0.3 < seconds:
                out.append({"w": (special or {}).get(n, f"word{n}"), "start": round(a, 3), "end": round(a + 0.28, 3)})
                n += 1
    return out


# --- Claude, stood in by tool name -----------------------------------------------------------

ANSWER = {}
CALLS = []


class FakeMessages:
    def create(self, **kw):
        name = kw["tool_choice"]["name"]
        CALLS.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=ANSWER.get(name, {}))],
                               stop_reason="tool_use")


def answer(**kw):
    base = {"people": [], "main_person": "unclear", "hook_speaker": "unclear", "misattributed": False,
            "logos": [], "ai_visuals": [], "offensive": [], "spoken_promos": [], "ai_mentions": []}
    base.update(kw)
    ANSWER["submit_campaign_check"] = base


def status_of(items, cid):
    return next((c for c in items if c["id"] == cid), None)


def main():
    from app import campaign, compliance, highlights, identity, lookcheck, pipeline, render, store, transcribe
    store.init()
    highlights._client = lambda: SimpleNamespace(messages=FakeMessages())
    campaign.check_text = lambda rb, posts: {p["id"]: {"status": "ok", "reason": ""} for p in posts}
    data = Path(os.environ["DATA_DIR"])
    print("== drawn footage")
    solo_a, solo_a2 = data / "soloA.mp4", data / "soloA2.mp4"
    two_b, two_a = data / "two_B.mp4", data / "two_A.mp4"
    make(solo_a, 8, [("A", 0.5)], "A", (1080, 1920))
    make(solo_a2, 8, [("A", 0.45)], "A", (1080, 1920))
    make(two_b, 9, [("A", 0.27), ("B", 0.73)], "B", (1280, 720))
    make(two_a, 9, [("A", 0.27), ("B", 0.73)], "A", (1280, 720))
    expect(all(p.exists() for p in (solo_a, solo_a2, two_b, two_a)), "four drawn videos made")

    rb_focus = {"mode": "source", "name": "TJR — Reach", "creator": "TJR",
                "primary_focus": {"name": "TJR", "quote": "TJR must be the focus of every clip."},
                "no_logos": {"value": "yes", "quote": "No logos or sponsor banners."},
                "no_ai": {"value": "yes", "quote": "No AI-generated video."},
                "tone_avoid": ["Never portray TJR negatively"]}
    rb_plain = {"mode": "source", "name": "Plain", "creator": "TJR"}
    camp = store.save_campaign("TJR — Reach", "source", "brief", rb_focus)

    print("\n== who talks (mouths against the words)")
    w9 = words_for(9)
    scan_b = identity.scan(two_b, w9)
    left = min(scan_b["people"], key=lambda p: p["x"]) if scan_b.get("people") else {}
    right = max(scan_b["people"], key=lambda p: p["x"]) if scan_b.get("people") else {}
    expect(scan_b.get("ok") and len(scan_b["people"]) == 2, f"two people found ({identity.describe(scan_b)})")
    expect(right.get("talk", 0) >= 0.6 and left.get("talk", 1) <= 0.15,
           f"B (right) is the one talking: B {right.get('talk')}, A {left.get('talk')}")
    scan_a = identity.scan(two_a, w9)
    l2 = min(scan_a["people"], key=lambda p: p["x"]) if scan_a.get("people") else {}
    r2 = max(scan_a["people"], key=lambda p: p["x"]) if scan_a.get("people") else {}
    expect(l2.get("talk", 0) >= 0.6 and r2.get("talk", 1) <= 0.15,
           f"A (left) is the one talking in the other clip: A {l2.get('talk')}, B {r2.get('talk')}")
    silent = identity.scan(two_b, [])
    expect(silent.get("ok") and silent.get("speech_from") == "sound", "no words: speech is read from the sound")

    print("\n== who is TJR (reference faces)")
    st = identity.status(camp)
    expect(not st["ready"] and "Add photos" in st["note"], f"no references yet: “{st['note']}”")
    expect(identity.match(scan_b, identity.references(camp))["status"] == "no_refs", "no references: no match claimed")
    still = data / "tjr.jpg"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(solo_a), "-frames:v", "1", str(still)], check=True)
    st = identity.add_photo(camp, still)
    expect(st["ready"] and len(st["photos"]) == 1 and st["photos"][0]["url"].startswith("/media/identity/"),
           f"a photo of TJR added ({st['note']})")
    blank = data / "blank.jpg"
    cv2.imwrite(str(blank), np.full((400, 400, 3), 128, np.uint8))
    try:
        identity.add_photo(camp, blank)
        expect(False, "a photo with no face is refused")
    except ValueError as exc:
        expect("Couldn't find a face" in str(exc), f"a photo with no face is refused: “{exc}”")
    m = identity.match(scan_b, identity.references(camp))
    expect(m["status"] == "clear" and m["person"] == left.get("id"), f"A matched as TJR from the photo ({m['why']})")
    # learned references: solo clips of A in two different videos
    camp2 = store.save_campaign("Learn", "source", "brief", rb_focus)
    s1, s2 = identity.scan(solo_a, w9), identity.scan(solo_a2, w9)
    expect(s1.get("solo") and s2.get("solo"), "both solo clips count as solo")
    identity.learn(camp2, s1, "job1", "c1")
    expect(identity.references(camp2)["learned"] == 0, "one video alone isn't trusted yet")
    identity.learn(camp2, s2, "job2", "c2")
    refs2 = identity.references(camp2)
    expect(refs2["learned"] == 2 and refs2["videos"] == 2,
           f"the same face in two videos is trusted ({refs2['learned']} learned, {refs2['videos']} videos)")
    m2 = identity.match(scan_b, refs2)
    expect(m2["status"] == "clear" and m2["person"] == left.get("id"), "learned faces pick A out too")
    expect(identity.match(scan_b, {"kind": "lbp", "vectors": [right["vec"]] if right.get("vec") is not None else []})
           ["person"] == right.get("id"), "with B's face as reference, B is matched instead")

    print("\n== the look under 'TJR must be the main person'")
    a_id, b_id = left.get("id"), right.get("id")
    people = [{"box": a_id, "who": "creator", "name": "TJR", "how_known": "title"},
              {"box": b_id, "who": "other", "name": "Timmy", "how_known": "TJR calls him Timmy"}]
    answer(people=people, main_person="other", main_person_name="Timmy", hook_speaker="other",
           hook_speaker_name="Timmy", misattributed=True, fixed_hook="Timmy asks how long it really takes")
    CALLS.clear()
    hook = "TJR says it takes two years"
    look, _ = lookcheck.review(two_b, w9, rb_focus, title="Teaching My Friend How To Day Trade", hook=hook,
                               campaign_id=camp)
    expect(len(CALLS) == 1, f"one Claude call for the whole look ({len(CALLS)})")
    imgs = [c for c in CALLS[0]["messages"][0]["content"] if c["type"] == "image"] if CALLS else []
    expect(len(imgs) == lookcheck.OVERVIEW_N + lookcheck.DETAIL_N or len(imgs) >= 6,
           f"frames sent: {len(imgs)} (boxed overview + full-size)")
    text = " ".join(c["text"] for c in CALLS[0]["messages"][0]["content"] if c["type"] == "text") if CALLS else ""
    expect("Never identify anyone from their face" in CALLS[0]["system"] and "Teaching My Friend" in text
           and f"box {a_id}" in text, "Claude gets the title, the boxes, the face check — and is told: context only")
    keep = data / "sent_frames"
    keep.mkdir(exist_ok=True)
    for i, im in enumerate(imgs[:3]):
        (keep / f"frame{i}.jpg").write_bytes(base64.b64decode(im["source"]["data"]))
    print(f"  (frames sent to Claude saved in {keep})")
    who = look["who"]
    expect(who["local"] == "other" and who["main"] == "other", f"faces + mouths alone say B is the main person ({who})")
    items = compliance.look_checks(rb_focus, look, hook=hook)
    ident = status_of(items, "identity")
    expect(ident and ident["status"] == "fail" and "Timmy is the one talking" in ident["detail"]
           and "TJR" in ident["detail"], f"blocked: “{(ident or {}).get('detail')}”")
    summary = compliance.summarize(items)
    expect(summary["status"] == "blocked" and "Timmy is the main person, not TJR" in summary["summary"],
           f"the card says why: “{summary['summary']}”")
    change, notes = lookcheck.plan_fixes(look, rb_focus, {"start": 0, "end": 9, "parts": "[]", "words": "[]"},
                                         {"hook": hook}, [])
    expect(change == {}, "a blocked clip isn't re-rendered for nothing")

    answer(people=people, main_person="creator", hook_speaker="creator")
    look_a, _ = lookcheck.review(two_a, w9, rb_focus, title="t", hook="Why most traders lose", campaign_id=camp)
    ident = status_of(compliance.look_checks(rb_focus, look_a, hook="Why most traders lose"), "identity")
    expect(ident and ident["status"] == "pass" and "TJR is the main person" in ident["detail"],
           f"A talks: passes — “{(ident or {}).get('detail')}”")

    print("\n== no 'main person' rule: the hook is fixed instead")
    answer(people=people, main_person="other", hook_speaker="other", hook_speaker_name="Timmy", misattributed=True,
           fixed_hook="Timmy asks how long it really takes")
    look_p, _ = lookcheck.review(two_b, w9, rb_plain, hook=hook, campaign_id=camp)
    expect(look_p["who"]["misattributed"], "the hook is seen crediting TJR with Timmy's words")
    change, notes = lookcheck.plan_fixes(look_p, rb_plain, {"start": 0, "end": 9, "parts": "[]", "words": "[]"},
                                         {"hook": hook}, [])
    expect(change.get("hook") == "Timmy asks how long it really takes" and notes, f"hook rewritten: {notes}")
    look_p["fixes"] = notes
    items = compliance.look_checks(rb_plain, look_p, hook=change.get("hook", ""))
    ident = status_of(items, "identity")
    expect(ident["status"] == "pass" and ident["detail"].startswith("Fixed:"), f"noted: “{ident['detail']}”")
    expect(status_of(items, "logos") is None and status_of(items, "ai_footage") is None,
           "no logo or AI lines when the brief doesn't ban them")
    cards = {"cards": [{"kind": "label", "text": "TJR says it takes [TWO YEARS]"}]}
    look_p["hook"] = "TJR says it takes TWO YEARS"
    change, _ = lookcheck.plan_fixes(look_p, rb_plain, {"start": 0, "end": 9, "parts": "[]", "words": "[]"}, cards, [])
    expect(change.get("cards") and change["cards"][0]["text"] == "Timmy asks how long it really takes",
           "a headline card is rewritten the same way")

    print("\n== the picker's speaker rule, in code")
    clip = {"hook": "TJR says start with $500", "caption": "TJR explains why", "speaker": "other",
            "speaker_name": "his friend Timmy"}
    highlights.credit_clip(clip, None, "TJR")
    expect(clip["hook"] == "Timmy says start with $500" and "credit_note" in clip, f"credited to Timmy: {clip['hook']}")
    clip = {"hook": "TJR: never risk more than 1%", "speaker": "unclear"}
    highlights.credit_clip(clip, None, "TJR")
    expect(clip["hook"] == "Never risk more than 1%", f"unclear speaker: no name at all ({clip['hook']})")
    expect(highlights.fix_credit("Timmy asks TJR how to trade", "TJR", "other", "Timmy") == "Timmy asks TJR how to trade",
           "naming TJR without crediting him is left alone")
    ranked = highlights.rank([{"start": 10, "end": 40, "score": 80, "title": "t", "hook": "TJR says wait for it",
                               "speaker": "other", "speaker_name": "Timmy"}], [], 100, 3, creator="TJR")
    expect(ranked[0]["speaker"] == "other" and ranked[0]["hook"] == "Timmy says wait for it",
           "rank() keeps the speaker and holds the hook to it")

    print("\n== logos, AI footage, offensive words")
    answer(people=people, main_person="creator", hook_speaker="creator",
           logos=[{"what": "a “use code TJR” sponsor banner", "kind": "sponsor_banner", "where": "bottom left",
                   "from_s": 1.0, "to_s": 5.0}],
           ai_visuals=[{"what": "an AI-made city flyover", "from_s": 2.0, "to_s": 4.0}])
    look_l, _ = lookcheck.review(two_a, w9, rb_focus, hook="x", campaign_id=camp)
    items = compliance.look_checks(rb_focus, look_l, hook="x")
    logos = status_of(items, "logos")
    expect(logos["status"] == "fail" and "bottom left from 0:01 to 0:05" in logos["detail"],
           f"banner blocks: “{logos['detail']}”")
    ai = status_of(items, "ai_footage")
    expect(ai["status"] == "fail" and "0:02" in ai["detail"], f"AI footage blocks: “{ai['detail']}”")
    expect(not [c for c in compliance.look_checks(rb_plain, look_l, hook="x") if c["status"] == "fail"],
           "the same clip is fine for a brief without those bans")
    promo_words = words_for(9, {7: "use", 8: "code", 9: "TJR"})
    answer(people=people, main_person="creator", hook_speaker="creator")
    look_s, _ = lookcheck.review(two_a, promo_words, rb_focus, hook="x", campaign_id=camp)
    logos = status_of(compliance.look_checks(rb_focus, look_s, hook="x"), "logos")
    expect(logos["status"] == "warn" and "use code" in logos["detail"], f"spoken promo: check first — “{logos['detail']}”")
    ai_words = words_for(9, {3: "this", 4: "is", 5: "Sora"})
    look_s, _ = lookcheck.review(two_a, ai_words, rb_focus, hook="x", campaign_id=camp)
    ai = status_of(compliance.look_checks(rb_focus, look_s, hook="x"), "ai_footage")
    expect(ai["status"] == "warn" and "Sora" in ai["detail"], f"AI only mentioned: check first — “{ai['detail']}”")
    lookcheck.EXTRA_SLURS.add(hashlib.sha256(b"zorblax").hexdigest())
    hook_words = words_for(9, {0: "zorblax"})
    look_o, _ = lookcheck.review(two_a, hook_words, rb_focus, hook="x", campaign_id=camp)
    off = status_of(compliance.look_checks(rb_focus, look_o, hook="x"), "offensive")
    expect(off["status"] == "fail" and "opening" in off["detail"] and "zorblax" not in off["detail"],
           f"offensive word in the hook: blocked, word starred — “{off['detail']}”")
    answer(people=people, main_person="creator", hook_speaker="creator",
           offensive=[{"quote": "word10 word11 word12 word13 word14 word15 word16 word17 word18 word19 word20",
                       "kind": "offensive_joke", "from_s": 6.3, "to_s": 9.0, "said_by": "creator"}])
    look_j, _ = lookcheck.review(two_a, w9, rb_focus, hook="x", campaign_id=camp)
    off = status_of(compliance.look_checks(rb_focus, look_j, hook="x"), "offensive")
    expect(off["status"] == "fail" and ("too long" in off["detail"] or "punchline" in off["detail"])
           and "bad light" in off["detail"], f"a long joke by TJR: blocked — “{off['detail']}”")

    print("\n== no Claude key: says what couldn't be checked")
    lookcheck.ANTHROPIC_API_KEY = ""
    CALLS.clear()
    look_n, _ = lookcheck.review(two_b, w9, rb_focus, hook=hook, campaign_id=camp)
    items = compliance.look_checks(rb_focus, look_n, hook=hook)
    expect(not CALLS, "no call is made")
    for cid in ("logos", "ai_footage", "offensive"):
        it = status_of(items, cid)
        expect(it and it["status"] == "warn" and "Claude isn't set up" in it["detail"], f"{cid}: “{it and it['detail']}”")
    ident = status_of(items, "identity")
    expect(ident["status"] == "fail", "faces and mouths alone still catch Timmy doing the talking")
    camp3 = store.save_campaign("Nobody known", "source", "brief", rb_focus)
    look_u, _ = lookcheck.review(two_a, w9, rb_focus, hook="x", campaign_id=camp3)
    ident = status_of(compliance.look_checks(rb_focus, look_u, hook="x"), "identity")
    expect(ident["status"] == "warn" and "add 1–3 clear photos" in ident["detail"],
           f"no key, no references: “{ident['detail']}”")
    expect(all(c["status"] == "warn" for c in compliance.look_checks(rb_focus, None)), "never looked at: all 'check first'")
    lookcheck.ANTHROPIC_API_KEY = "x"

    print("\n== an Edit Maker edit goes through the same look")
    answer(people=people, main_person="other", main_person_name="Timmy", hook_speaker="other")
    tl = {"segments": [{"moment": "m1", "curve": [], "words": [{"w": w["w"], "t": w["start"], "end": w["end"]}
                                                               for w in w9]}],
          "effects": {}, "text": "", "length": 9.0, "voice": 1.0}
    res = compliance.check_edit(rb_focus, tl, two_b, {"text": "#TJR", "caption": ""}, "", {"status": "ok"})
    ident = status_of(res["checks"], "identity")
    expect(res["status"] == "blocked" and ident and ident["status"] == "fail", f"edit blocked: {res['summary']}")
    n = len(CALLS)
    compliance.check_edit(rb_focus, tl, two_b, {"text": "#TJR", "caption": ""}, "", {"status": "ok"})
    expect(len(CALLS) == n, "the same edit file isn't paid for twice")

    print("\n== through the pipeline: an offensive word cut out of the middle")
    src = data / "sources" / "talk" / "source.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    make(src, 14, [("A", 0.3), ("B", 0.72)], "A", (1280, 720))
    all_words = words_for(14, {13: "zorblax"})
    bad = next(w for w in all_words if w["w"] == "zorblax")
    rb_cut = {**rb_focus, "name": "TJR — Reach"}
    job = store.create_job("Teaching My Friend How To Day Trade", "upload",
                           {"campaign": {"id": camp, "name": "TJR — Reach", "mode": "source", "rules": rb_cut},
                            "platforms": ["tiktok"]})
    store.update_job(job, status="done", source_path=str(src), duration=14.0,
                     transcript=json.dumps({"words": all_words, "segments": []}))
    start, end = 0.9, 12.0
    words = transcribe.words_between(all_words, start, end)
    edits = render.merge_edits({"motion": False, "tighten": False, "hook": "Why most traders lose", "hook_on": True})
    cid = store.create_clip(job, {"start": start, "end": end, "title": "t", "hook": edits["hook"], "score": 80,
                                  "reason": "", "tags": [], "edits": edits, "words": words,
                                  "post": campaign.build_post(rb_cut, 0, "tiktok")})
    out = render.render_clip(src, cid, start, end, words, edits, True, None, (1280, 720))
    store.update_clip(cid, file=str(out["file"]), thumb=str(out["thumb"]), status="ready", words=json.dumps(words))
    before = lookcheck.identity._probe(Path(out["file"]))[2]
    answer(people=[{"box": 1, "who": "creator", "name": "TJR"}], main_person="creator", hook_speaker="creator")
    CALLS.clear()
    result = lookcheck.review_clip(cid, rb_cut)
    row = store.get_clip(cid)
    got = json.loads(row["edits"])
    after = lookcheck.identity._probe(Path(row["file"]))[2]
    new_words = json.loads(row["words"])
    expect(result == "rerendered" and len(got.get("cut_applied") or []) == 1, f"re-rendered with the cut ({result})")
    cut = got["cut_out"][0][1] - got["cut_out"][0][0] if got.get("cut_out") else 0
    expect(abs((before - after) - cut) < 0.12 and cut > 0.25, f"shorter by the cut: {before:.2f}s -> {after:.2f}s "
                                                                f"(cut {cut:.2f}s)")
    names = [w["w"] for w in new_words]
    expect("zorblax" not in names and "word12" in names and "word14" in names, "the word is gone, its neighbours stay")
    w12 = next(w for w in new_words if w["w"] == "word12")
    w14 = next(w for w in new_words if w["w"] == "word14")
    old14 = next(w for w in words if w["w"] == "word14")
    expect(abs(w12["start"] - (bad["start"] - 0.33 - start)) < 0.05 and abs((old14["start"] - w14["start"]) - cut) < 0.05,
           "words before the cut keep their time, words after move up by the cut")
    comp = json.loads(row["compliance"] or "{}")
    off = status_of(comp.get("checks") or [], "offensive")
    expect(off and off["status"] == "pass" and off["detail"].startswith("Cut out an offensive word at 0:0"),
           f"the check says so: “{(off or {}).get('detail')}”")
    expect(len(CALLS) == 1, f"one Claude call for the clip, none for the re-render ({len(CALLS)})")
    # the crop here shows B listening while A talks off screen: even with Claude calling box 1
    # the creator, the faces show nobody on screen talking — that can't pass as "TJR is the main person"
    ident = status_of(comp.get("checks") or [], "identity")
    expect(ident and ident["status"] == "warn" and "off camera" in ident["detail"],
           f"talking off camera isn't 'the main person': “{(ident or {}).get('detail')}”")
    frame = data / "after_cut.jpg"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "4", "-i", row["file"], "-frames:v", "1", str(frame)])
    print(f"  (a frame of the re-rendered clip: {frame})")

    print("\nall checks behaved" if not FAILS else f"\n{len(FAILS)} check(s) failed:\n  " + "\n  ".join(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
