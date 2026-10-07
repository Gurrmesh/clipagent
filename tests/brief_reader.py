"""The campaign brief reader: the rules it used to miss or misread, offline.

Run: python tests/brief_reader.py

Claude is stood in by fixed replies to a TJR-like brief (a "What to look for"
list, a 2026-or-later date rule, "caption or text overlay must mention TJR",
"TJR must be the primary focus", no logos, no AI video, "no reposts or collab
posts"). Checks: (a) a correct reply becomes every new rulebook key with its
quote; (b) a reply that reads "no reposts or collab posts" as "no joining
moments" is put right in code; (c) the name the brief requires is added to
hooks and captions that forget it, and the clip check passes or blocks;
(d) a source video older than the date rule stops the run before
transcription, a newer one goes on, an unknown date asks you to check;
(e) "Read the brief again" adds only what's missing and keeps your choices;
(f) an old rulebook with none of the new keys behaves as before.
The real reading quality depends on Claude and can't be tested here.
"""
from __future__ import annotations

import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):  # Windows: print safely even when output goes to a file
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="brief_reader_")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("WHISPER_API_KEY", "x")
os.environ["TELEGRAM_BOT_TOKEN"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TJR_BRIEF = """TJR — Clipping Campaign (Reach)

About
TJR is a full-time trader and educator. We want short clips from his YouTube videos, streams and Kick broadcasts.

Pay
$0.75 per 1,000 views. A post must reach 10,000 views before it pays. Minimum payout $7.50, maximum $750 per post.

Where to post
TikTok, Instagram Reels and YouTube Shorts.

What to look for
- Big wins and green days
- Trading lessons and tips
- Funny moments with friends
- Reactions to the market open

Rules
- Only clips from videos posted in 2026 or later. Older content will be rejected.
- Your caption or text overlay must mention TJR.
- TJR must be the primary focus of every clip.
- Caption must include #TJR.
- No logos.
- No AI-generated video.
- Strictly no reposts or collab posts.
- Never portray TJR negatively.
- Posts must be public.

Sources
youtube.com/@TJRTrades, youtube.com/@TRichesTrades, kick.com/tjr, instagram.com/tjr
"""

UNSTATED = {"value": "unstated", "quote": ""}
PERMS = {k: dict(UNSTATED) for k in ("trim", "cut", "crop", "zoom", "stitch", "speed", "audio", "music",
                                     "outside", "hook", "captions", "borders", "watermark")}

# What a careful reading returns.
GOOD = {
    "name": "TJR — Reach", "brand": "TJR", "platform": "whop", "mode": "source",
    "mode_quote": "We want short clips from his YouTube videos, streams and Kick broadcasts.",
    "pay": {"per_1k_views": 0.75, "min_views": 10000, "max_per_post": 750, "budget": None,
            "quote": "$0.75 per 1,000 views."},
    "permissions": PERMS,
    "captions_required": False,
    "length": {"min_seconds": None, "max_seconds": None, "quote": ""},
    "caption": {"required_one_of": [], "required_all": [], "hashtags": ["#TJR"], "mentions": [],
                "extra_text_allowed": True, "other_hashtags": "unstated", "quote": "Caption must include #TJR."},
    "hooks": {"examples": [], "rules": []},
    "brand_logo": {"value": "forbidden", "quote": "No logos.", "rules": [], "link": ""},
    "tone_avoid": ["portraying TJR negatively"],
    "platforms": ["tiktok", "instagram", "youtube"],
    "posting": {"public": True, "comments_on": False, "no_paid_boost": False, "no_duplicates": True, "collab": "no"},
    "footage": {"kind": "long_form", "links": []},
    "other_rules": ["Never portray TJR negatively."],
    "grey_areas": [],
    "creator": "TJR",
    "look_for": ["Big wins and green days", "Trading lessons and tips", "Funny moments with friends",
                 "Reactions to the market open"],
    "min_upload_date": {"date": "2026-01-01", "quote": "Only clips from videos posted in 2026 or later."},
    "must_mention": {"names": ["TJR"], "where": "either", "quote": "Your caption or text overlay must mention TJR."},
    "primary_focus": {"name": "TJR", "quote": "TJR must be the primary focus of every clip."},
    "no_logos": {"value": "yes", "quote": "No logos."},
    "no_ai": {"value": "yes", "quote": "No AI-generated video."},
}

# The misreading seen on the real TJR brief: "no reposts or collab posts" taken
# as "no joining moments", and the caption's hashtag rule taken as "no burned-in captions".
MISREAD = copy.deepcopy(GOOD)
MISREAD["permissions"]["stitch"] = {"value": "no", "quote": "Strictly no reposts or collab posts."}
MISREAD["permissions"]["captions"] = {"value": "no", "quote": "Caption must include #TJR."}
MISREAD["posting"] = {"public": True, "comments_on": False, "no_paid_boost": False, "no_duplicates": False,
                      "collab": "unstated"}

# A reading that misses every new rule (what the old reader returned).
BARE = copy.deepcopy(MISREAD)
for _k in ("creator", "look_for", "min_upload_date", "must_mention", "primary_focus", "no_logos", "no_ai"):
    BARE.pop(_k)
BARE["permissions"]["captions"] = dict(UNSTATED)

from fastapi.testclient import TestClient  # noqa: E402

from app import campaign, compliance, highlights, main, media, pipeline, store, transcribe  # noqa: E402

FAILS = []


def expect(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def item(result, cid):
    return next((c for c in result["checks"] if c["id"] == cid), None)


def main_test() -> None:
    # ------------------------------------------------------------------ (a)
    print("== (a) a correct reading becomes every new key, each with the brief's quote")
    rb = campaign.normalize(GOOD, TJR_BRIEF)
    expect(rb["creator"] == "TJR", "creator: TJR")
    expect(rb["look_for"] == GOOD["look_for"], f"look_for, word for word ({rb['look_for']})")
    mud = rb["min_upload_date"]
    expect(mud["date"] == "2026-01-01" and mud["verified"] and mud["line"] == 19,
           f"min_upload_date 2026-01-01, quoted from line 19 ({mud})")
    mm = rb["must_mention"]
    expect(mm["names"] == ["TJR"] and mm["where"] == "either" and mm["verified"], f"must_mention TJR, either ({mm})")
    expect(rb["primary_focus"]["name"] == "TJR" and rb["primary_focus"]["verified"], "primary_focus TJR")
    expect(rb["no_logos"]["value"] == "yes" and rb["no_logos"]["quote"] == "No logos.", "no_logos yes, quoted")
    expect(rb["no_ai"]["value"] == "yes" and rb["no_ai"]["verified"], "no_ai yes, quoted")
    expect(rb["perms"]["stitch"]["value"] == "unstated" and not rb["grey"], "stitch unstated, no grey area")
    expect(rb["posting"]["no_duplicates"] and rb["posting"]["collab"] == "no", "posting rules where they belong")
    expect(not [n for n in rb["notes"] if "left this out" in n], f"no backup notes needed ({rb['notes']})")
    expect(rb["by_you"] == [], "nothing marked as changed by you yet")
    # what reaches the moment picker
    g = campaign.picker_guidance(rb)
    expect("What this campaign wants to see" in g and "Funny moments with friends" in g,
           "the look-for list reaches the moment picker")
    expect(g.index("hard filter") < g.index("What this campaign wants to see"),
           "the look-for list is a preference, not under the hard-filter rule")
    expect("TJR must be the main person" in g and "must name TJR" in g, "primary focus and the name reach the picker")
    r = campaign.resolve(rb)
    expect(r["min_upload_date"] == "2026-01-01" and r["no_logos"] == "yes" and r["look_for"], "resolve carries them")
    card = campaign.card(rb)
    expect(card["mention_where"]["either"] and card["ban_options"]["yes"], "the card gets the labels it needs")
    expect(not campaign.wants_brand_logo(rb), "no logos: the brand logo isn't drawn either")
    s, _ = campaign.source_settings(rb, {"logo": True})
    expect(s["logo"] is False, "no logos: your own logo is off")

    # ------------------------------------------------------------------ (b)
    print("\n== (b) “no reposts or collab posts” is a posting rule, not “no joining moments”")
    rb_m = campaign.normalize(MISREAD, TJR_BRIEF)
    st = rb_m["perms"]["stitch"]
    expect(st["value"] == "unstated" and st.get("misread") == "Strictly no reposts or collab posts.",
           "stitch set back to unstated")
    grey = [g for g in rb_m["grey"] if g["perm"] == "stitch"]
    expect(len(grey) == 1 and "join different moments of TJR's videos" in grey[0]["question"]
           and "Allowed?" in grey[0]["question"] and grey[0]["line"] == 25,
           f"a question for you instead ({grey[0]['question'] if grey else None})")
    expect(rb_m["posting"]["no_duplicates"] and rb_m["posting"]["collab"] == "no",
           f"posting: once per account, no collab posts ({rb_m['posting']})")
    expect(campaign.explain(rb_m, "stitch")[1] == "grey", "joining stays off until you decide (campaign rules win)")
    cap = rb_m["perms"]["captions"]
    expect(cap["value"] == "unstated" and not any(g["perm"] == "captions" for g in rb_m["grey"])
           and campaign.allowed(rb_m, "captions"),
           "“Caption must include #TJR” isn't “no burned-in captions”: back to the usual (on), no question needed")
    expect(any("about posting" in n for n in rb_m["notes"]), "the card says what was put right")
    # a real ban on joining stays a ban
    real = "Clip rules:\nDon't combine moments from different videos into one clip. No reposts.\n" + "x" * 40
    rb_r = campaign.normalize({**GOOD, "permissions": {**PERMS, "stitch": {
        "value": "no", "quote": "Don't combine moments from different videos into one clip. No reposts."}}}, real)
    expect(rb_r["perms"]["stitch"]["value"] == "no", "a real “don't combine” ban is kept")
    # "collab posts are allowed" read as stitch yes: back to unstated, no question needed
    allow = "Collab posts are allowed.\nOther lines of the brief go here, long enough to count.\n"
    rb_y = campaign.normalize({**GOOD, "posting": {}, "permissions": {**PERMS, "stitch": {"value": "yes",
                                                                         "quote": "Collab posts are allowed."}}}, allow)
    expect(rb_y["perms"]["stitch"]["value"] == "unstated" and not any(g["perm"] == "stitch" for g in rb_y["grey"])
           and rb_y["posting"]["collab"] == "yes", "“collab posts are allowed” isn't “joining allowed”")
    # "no reuploads of the full video" isn't "no cropping"
    reup = "No reuploads of the full video.\nOther lines of the brief go here, long enough to count.\n"
    rb_c = campaign.normalize({**GOOD, "permissions": {**PERMS, "crop": {"value": "no",
                                                                       "quote": "No reuploads of the full video."}}}, reup)
    expect(rb_c["perms"]["crop"]["value"] == "unstated" and "No reuploads of the full video." in rb_c["other_rules"],
           "“no reuploads of the full video” isn't “no cropping”")
    # what the edit maker says when joining is off because the brief is silent
    from app import edits as edits_mod
    _, _, refusal = edits_mod.campaign_fit(rb_m, "velocity")
    expect("doesn't say" in refusal and "Rules" in refusal and "Join different moments" in refusal,
           f"the edit refusal says where to switch it on ({refusal[:90]}…)")

    print("\n== backup check: a reading that missed every new rule")
    rb_b = campaign.normalize(BARE, TJR_BRIEF)
    expect(rb_b["look_for"] == GOOD["look_for"], f"look_for taken from the list in the brief ({rb_b['look_for']})")
    expect(rb_b["min_upload_date"]["date"] == "2026-01-01" and rb_b["min_upload_date"]["line"] == 19, "date rule found")
    expect(rb_b["must_mention"]["names"] == ["TJR"] and rb_b["must_mention"]["where"] == "either", "must mention found")
    expect(rb_b["primary_focus"]["name"] == "TJR", "primary focus found")
    expect(rb_b["no_logos"]["value"] == "yes" and rb_b["no_ai"]["value"] == "yes", "no logos, no AI found")
    expect(rb_b["creator"] == "TJR", "creator from the primary focus")
    expect(sum("left this out" in n for n in rb_b["notes"]) == 6, "each one is flagged on the card for you to check")
    quiet = ("Campaign runs from March 1 to April 30.\nPost on TikTok. Use any of his videos.\n"
             "Submissions after March 1 count. Mention our brand if you like.\n")
    rb_q = campaign.normalize({**BARE, "posting": {}}, quiet)
    expect(not rb_q["min_upload_date"]["date"] and not rb_q["must_mention"]["names"] and not rb_q["look_for"],
           "campaign dates and loose wording aren't taken as rules")
    expect((campaign._backup_date("Only videos posted after March 1, 2026 can be used.\n") or [""])[0] == "2026-03-02",
           "“posted after March 1, 2026” -> from 2 March 2026")

    # ------------------------------------------------------------------ (c)
    print("\n== (c) the name the brief requires: added when the writer forgets, then checked")
    expect(campaign.fix_hook(rb, "He turned $500 into $50K") == "TJR: He turned $500 into $50K",
           "a hook without TJR gets it")
    expect(campaign.fix_hook(rb, "TJR's best trade ever") == "TJR's best trade ever", "a hook with TJR is left alone")
    expect(campaign.fix_hook(rb, "") == "", "no hook: nothing invented")
    e = campaign.clamp_edits({"hook": "Green day on the open", "cards": [{"kind": "title", "text": "[Huge] win"}]}, rb)
    expect(e["hook"].startswith("TJR: ") and e["cards"][0]["text"] == "TJR: [Huge] win",
           "the editor's hook and card get it too")
    post = campaign.build_post(rb, 0, "tiktok", extra="Green day on the open #fyp")
    expect(post["caption"] == "TJR: Green day on the open" and "#TJR" in post["text"],
           f"the caption names TJR, not just #TJR ({post['text']!r})")
    clip = {"start": 0, "end": 30, "saved": 0, "parts": []}
    edits_ok = {"hook_on": True, "headline_on": False}
    meta_new = {"upload_date": "20260203"}
    res = compliance.check_source(rb, clip, edits_ok, Path("missing.mp4"), post, "TJR: Green day", None,
                                  source_meta=meta_new)
    expect(item(res, "must_mention")["status"] == "pass", "named in both: passes")
    bare_post = {"text": "Green day on the open\n\n#TJR", "caption": "Green day on the open"}
    res = compliance.check_source(rb, clip, edits_ok, Path("missing.mp4"), bare_post, "TJR: Green day", None,
                                  source_meta=meta_new)
    expect(item(res, "must_mention")["status"] == "pass", "“caption or overlay”: the hook alone is enough")
    res = compliance.check_source(rb, clip, edits_ok, Path("missing.mp4"), bare_post, "Green day", None,
                                  source_meta=meta_new)
    mi = item(res, "must_mention")
    expect(mi["status"] == "fail" and res["status"] == "blocked" and "Add TJR" in mi["detail"],
           f"in neither (only #TJR): blocked ({mi['detail']})")
    both = {**rb, "must_mention": {**rb["must_mention"], "where": "both"}}
    res = compliance.check_source(both, clip, edits_ok, Path("missing.mp4"), bare_post, "TJR: Green day", None,
                                  source_meta=meta_new)
    expect(item(res, "must_mention")["status"] == "fail", "“both”: missing from the caption blocks")
    tl = {"segments": [{"moment": 0, "text": ""}], "effects": {}, "length": 20, "music": {}}
    res = compliance.check_edit(rb, tl, Path("missing.mp4"), bare_post, "Green day", None)
    expect(item(res, "must_mention")["status"] == "fail", "an edit is checked for the name too")
    hooks, _ = campaign.plan_hooks({**rb, "hooks": {"examples": [{"text": "Green day", "verified": True}]}}, 2,
                                   generate=False)
    expect(hooks == ["TJR: Green day", "TJR: Green day"], f"clip-bank hooks get the name ({hooks})")
    tone = campaign.check_text({**rb, "hooks": {"examples": [{"text": "Green day", "verified": True}]}},
                               [{"id": 0, "hook": "TJR: Green day", "extra": ""}])
    expect(tone[0]["status"] == "ok", "the brand's own hook with the name added still counts as the brand's")

    # ------------------------------------------------------------------ (d)
    print("\n== (d) a source older than the date rule stops before transcription")
    msg = campaign.too_old(rb, {"upload_date": "20250312"}, "TJR — Reach")
    expect(msg == "This video was posted on 12 March 2025. The TJR campaign only takes clips from videos posted "
                  "in 2026 or later. Pick a newer video.", f"plain refusal ({msg})")
    expect(main.friendly_error(msg) == msg, "shown to you word for word")
    expect(campaign.too_old(rb, {"upload_date": "20260203"}) == "", "a 2026 video is fine")
    expect(campaign.too_old(rb, {}) == "" and campaign.too_old(rb, None) == "", "an unknown date doesn't refuse")
    later = {**rb, "min_upload_date": {"date": "2026-03-02", "quote": "", "line": None, "verified": False}}
    expect("on or after 2 March 2026" in campaign.too_old(later, {"upload_date": "2026-03-01"}),
           "a mid-year rule reads as a day")
    res = compliance.check_source(rb, clip, edits_ok, Path("missing.mp4"), post, "TJR: Green day", None,
                                  source_meta={"upload_date": ""})
    ud = item(res, "upload_date")
    expect(ud["status"] == "warn" and ud["detail"] == "Check first: couldn't confirm when this video was posted "
                                                       "(the campaign only takes videos from 2026 on).",
           f"unknown date: check first ({ud['detail']})")
    res = compliance.check_source(rb, clip, edits_ok, Path("missing.mp4"), post, "TJR: Green day", None,
                                  source_meta={"upload_date": "20250312"})
    expect(item(res, "upload_date")["status"] == "fail", "an old one that got through anyway is blocked")
    res = compliance.check_source(rb, clip, edits_ok, Path("missing.mp4"), post, "TJR: Green day", None,
                                  source_meta=meta_new)
    expect(item(res, "upload_date")["status"] == "pass" and "3 February 2026" in item(res, "upload_date")["detail"],
           "a new one passes, with its date")

    # the real run: the date is read after the download, before transcribing
    store.init()
    with store.connect() as conn:          # the downloads work adds this column; stand it in when it isn't there
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "source_meta" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN source_meta TEXT")
    dummy = Path(os.environ["DATA_DIR"]) / "source.mp4"
    dummy.write_bytes(b"\0" * 4096)
    calls = []

    def fake_info(url):                    # what the site says about the video, before downloading it
        return {"upload_date": DATES[url], "title": "A TJR stream", "duration": 600.0,
                "webpage_url": url, "extractor_key": "Youtube"}

    def fake_download(url, job_id, progress=None, **kw):
        return dummy, "A TJR stream"

    def no_audio_step(*a, **kw):
        calls.append("audio")
        raise RuntimeError("stopped after the date check")

    DATES = {"https://youtu.be/old": "20250312", "https://youtu.be/new": "20260203", "https://youtu.be/unk": ""}
    media.read_info = fake_info
    media.download = fake_download
    media.probe = lambda p: {"duration": 600.0, "has_audio": True, "width": 1920, "height": 1080}
    media.extract_audio = no_audio_step
    transcribe.transcribe = lambda *a, **kw: calls.append("transcribe")
    pipeline._frame_rate = lambda p: 30.0
    settings = {"campaign": {"id": "c1", "name": "TJR — Reach", "mode": "source", "rules": rb}}
    for url, want in (("https://youtu.be/old", "refused"), ("https://youtu.be/new", "goes on"),
                      ("https://youtu.be/unk", "goes on")):
        calls.clear()
        jid = store.create_job(url, "url", settings)
        pipeline._run_job(jid, url)
        job = store.get_job(jid)
        if want == "refused":
            expect(job["status"] == "failed" and job["error"].startswith("This video was posted on 12 March 2025")
                   and not calls, "an old video: refused before any audio or transcription work")
        else:
            expect(job["error"] == "stopped after the date check" and calls == ["audio"],
                   f"{url.rsplit('/', 1)[1]}: past the date check")

    # ------------------------------------------------------------------ (e)
    print("\n== (e) “Read the brief again” adds what's missing and keeps your choices")
    # gs's TJR campaign as the old reader saved it, with his own decisions on it
    old = campaign.normalize(BARE, TJR_BRIEF)
    for k in campaign.CONTENT_KEYS + ("by_you",):
        old.pop(k, None)
    old["perms"]["stitch"] = {"value": "no", "quote": "Strictly no reposts or collab posts.", "line": 25,
                              "verified": True}
    old["posting"] = {"public": True, "comments_on": False, "no_paid_boost": False, "no_duplicates": False,
                      "collab": "unstated"}
    old["grey"] = [{"perm": "speed", "question": "Speed ramps?", "quote": "", "line": None, "verified": False}]
    old["notes"] = []
    old["overrides"] = {"zoom": "yes", "speed": "yes"}          # a permission changed, a grey area answered
    old["name"] = "TJR — my campaign"

    class FakeMessages:
        reply = GOOD

        def create(self, **kw):
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=FakeMessages.reply)],
                                   stop_reason="tool_use")

    highlights._client = lambda: SimpleNamespace(messages=FakeMessages())
    client = TestClient(main.app)
    cid = store.save_campaign(old["name"], "source", TJR_BRIEF, old)
    r = client.post(f"/api/campaigns/{cid}/reread", json={})
    expect(r.status_code == 200, f"re-read accepted ({r.status_code} {r.text[:100]})")
    out = r.json()
    nb = out["rulebook"]
    print("   message:", out["message"])
    expect(out["message"].startswith("Found 6 new rules:") and "only videos posted in 2026 or later" in out["message"],
           "says in plain words what was added")
    expect("Fixed 1 misread" in out["message"], "and that the reposts line was put right")
    expect(nb["overrides"] == {"zoom": "yes", "speed": "yes"} and nb["name"] == "TJR — my campaign",
           "your changed permission, answered grey area and name are kept")
    expect(any(g["perm"] == "speed" for g in nb["grey"]) and any(g["perm"] == "stitch" for g in nb["grey"]),
           "your grey areas stay; the joining question is added")
    expect(nb["min_upload_date"]["date"] == "2026-01-01" and nb["must_mention"]["names"] == ["TJR"]
           and nb["look_for"] == GOOD["look_for"] and nb["no_ai"]["value"] == "yes", "the new rules are saved")
    expect(store.get_campaign(cid)["rulebook"]["min_upload_date"]["date"] == "2026-01-01", "…in the database too")
    # gs changes the look-for list and clears the date rule by hand; a re-read doesn't undo that
    r = client.put(f"/api/campaigns/{cid}", json={"edits": {"look_for": ["Big wins and green days"],
                                                            "min_upload_date": "", "overrides": nb["overrides"]}})
    mine = r.json()["rulebook"]
    expect(sorted(mine["by_you"]) == ["look_for", "min_upload_date"] and mine["min_upload_date"]["date"] == "",
           f"your changes are marked yours ({mine['by_you']})")
    r = client.post(f"/api/campaigns/{cid}/reread", json={})
    again = r.json()
    expect(again["rulebook"]["look_for"] == ["Big wins and green days"]
           and again["rulebook"]["min_upload_date"]["date"] == "", "a second re-read keeps what you changed")
    expect(again["message"].startswith("Nothing new"), f"…and says nothing is new ({again['message']})")
    # unsaved card edits sent along are saved, not lost
    r = client.post(f"/api/campaigns/{cid}/reread", json={"edits": {"name": "TJR renamed",
                                                                    "overrides": {"zoom": "yes", "speed": "yes"}}})
    expect(r.json()["rulebook"]["name"] == "TJR renamed", "unsaved changes on the card come along")
    # a stitch you allowed by hand stays allowed after the fix
    yours = {**old, "overrides": {"stitch": "yes"}}
    fixed_rb, _, fixed = campaign.merge_reread(yours, campaign.normalize(GOOD, TJR_BRIEF))
    expect(campaign.allowed(fixed_rb, "stitch") and fixed, "joining you allowed yourself stays allowed")
    r = client.post("/api/campaigns/nope/reread", json={})
    expect(r.status_code == 404, "unknown campaign: 404")
    empty_id = store.save_campaign("No brief", "source", "", {"mode": "source", "name": "No brief"})
    r = client.post(f"/api/campaigns/{empty_id}/reread", json={})
    expect(r.status_code == 400 and "no saved brief" in r.json()["detail"], "no saved brief: said plainly")
    # the edit form round-trips the new fields
    r = client.put(f"/api/campaigns/{cid}", json={"edits": {
        "must_mention": {"names": "TJR, Tyler Riches", "where": "both"}, "primary_focus": "Tyler Riches",
        "no_logos": "unstated", "no_ai": "no", "creator": "TJR"}})
    rb_e = r.json()["rulebook"]
    expect(rb_e["must_mention"]["names"] == ["TJR", "Tyler Riches"] and rb_e["must_mention"]["where"] == "both"
           and rb_e["primary_focus"]["name"] == "Tyler Riches" and rb_e["no_ai"]["value"] == "no",
           "names, where, focus and the bans can be changed on the card")

    # ------------------------------------------------------------------ (f)
    print("\n== (f) an old rulebook with none of the new keys")
    legacy = copy.deepcopy(old)
    legacy["perms"]["stitch"] = dict(UNSTATED, line=None, verified=False)
    c = campaign.content_rules(legacy)
    expect(c == {"creator": "TJR", "look_for": [], "min_upload_date": "", "must_mention": None, "primary_focus": "",
                 "no_logos": "unstated", "no_ai": "unstated"}, f"defaults ({c})")
    expect("wants to see" not in campaign.picker_guidance(legacy), "no look-for line for the picker")
    expect(campaign.too_old(legacy, {"upload_date": "20200101"}) == "", "no date rule: nothing refused")
    expect(campaign.fix_hook(legacy, "He turned $500 into $50K") == "He turned $500 into $50K", "hooks untouched")
    p = campaign.build_post(legacy, 0, "tiktok", extra="Green day")
    expect(p["caption"] == "Green day", "captions untouched")
    res = compliance.check_source(legacy, clip, edits_ok, Path("missing.mp4"), p, "Green day", None)
    expect(not item(res, "upload_date") and not item(res, "must_mention"), "no new checks on its clips")
    expect(campaign.card(legacy)["resolved"]["look_for"] == [], "the card still renders")
    expect(campaign.clamp_edits({"hook": "x", "logo": True}, {**legacy, "perms": {
        **legacy["perms"], "watermark": {"value": "yes", "quote": "", "verified": True}}})["logo"] is True,
        "no no-logos rule: your logo isn't forced off")

    print("\nFAILED:" if FAILS else "\nall checks behaved", FAILS or "")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main_test()
