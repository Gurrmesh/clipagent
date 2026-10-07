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

import re
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
    # a check may carry a "short" line that says what's wrong better than its name does
    if fails:
        status = "blocked"
        summary = "Blocked: " + "; ".join(c.get("short") or c["label"].lower() for c in fails)
    elif warns:
        status = "check"
        summary = "Check before posting: " + "; ".join(c.get("short") or c["label"].lower() for c in warns)
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


def _upload_date(r: Dict[str, Any], meta: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """The source video is recent enough for a brief with a date rule ("2026 onwards")."""
    need = r.get("min_upload_date") or ""
    if not need:
        return None
    rule = campaign.date_rule_words(need)
    posted = campaign.upload_date_of(meta)
    label = "When the video was posted"
    if not posted:
        return _item("upload_date", label, "warn",
                     f"Check first: couldn't confirm when this video was posted (the campaign only takes videos "
                     f"{'from ' + need[:4] + ' on' if need.endswith('-01-01') else 'posted ' + rule}).")
    if posted < need:
        return _item("upload_date", label, "fail",
                     f"Posted on {campaign.date_words(posted)} — the campaign only takes videos posted {rule}.")
    return _item("upload_date", label, "pass", f"Posted on {campaign.date_words(posted)} — {rule}, as the brief asks.")


def _must_mention(r: Dict[str, Any], on_screen: List[str], caption: str,
                  hook_allowed: bool = True) -> Optional[Dict[str, str]]:
    """The name the brief says the caption and/or the text on screen must carry."""
    mm = r.get("must_mention")
    if not mm:
        return None
    names, where = mm["names"], mm["where"]
    who = " or ".join(names)
    screen = any(campaign.mentions_name(t, names, hashtags_count=False) for t in on_screen if (t or "").strip())
    cap = campaign.mentions_name(re.sub(r"#\w+", " ", caption or ""), names, hashtags_count=False)
    need = {"caption": cap, "overlay": screen, "either": cap or screen, "both": cap and screen}[where]
    label = f"Mentions {who}"
    if need:
        found = [w for w, ok in (("the caption", cap), ("the text on screen", screen)) if ok]
        return _item("must_mention", label, "pass", f"Named in {' and '.join(found)}, as the brief asks.")
    if where in ("overlay", "both") and not screen and not hook_allowed:
        return _item("must_mention", label, "fail",
                     f"The brief wants {who} named in text on screen, but it doesn't allow text on screen — "
                     "check the campaign's rules.")
    fix = {"caption": "the caption", "overlay": "the hook", "either": "the hook or the caption",
           "both": " and ".join(w for w, ok in (("the hook", screen), ("the caption", cap)) if not ok)}[where]
    return _item("must_mention", label, "fail",
                 f"The brief says {who} must be named {campaign.MENTION_WHERE[where]}, and it isn't. "
                 f"Add {names[0]} to {fix} in the editor.")


def _screen_texts(edits: Dict[str, Any], hook: str) -> List[str]:
    """Every piece of text a source clip shows: the hook, a card, the headline."""
    texts = [hook or ""]
    texts += [c.get("text") or "" for c in (edits.get("cards") or []) if isinstance(c, dict)]
    if edits.get("headline_on"):
        texts.append(edits.get("headline") or "")
    return texts


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


# --- the campaign look: who is in it, logos, offensive words, AI footage (lookcheck.py) ------------

def _short(item: Dict[str, str], short: str) -> Dict[str, str]:
    if short:
        item["short"] = short[:90]
    return item


def _pct(x: Optional[float]) -> str:
    return f"{(x or 0) * 100:.0f}%"


def _evidence(who: Dict[str, Any], name: str, other: str) -> str:
    bits = []
    if who.get("creator_share") is not None:
        bit = f"{name} is on screen {_pct(who['creator_share'])} of the clip"
        if who.get("creator_talk") is not None:
            bit += f" and does {_pct(who['creator_talk'])} of the talking"
        bits.append(bit)
    if who.get("other_talk"):
        bits.append(f"{other} does {_pct(who['other_talk'])} of the talking")
    if who.get("local") == "offcam":
        bits.append("most of the talking comes from someone off camera")
    if who.get("conflict"):
        bits.append(f"the face check and Claude disagree about which person is {name}")
    how = {"faces": f"{name} recognised from the reference faces — an approximate match",
           "context": "who is who worked out from the context"}.get(who.get("creator_how") or "")
    if how and bits:
        bits.append(how)
    return "; ".join(bits)


def _cant_tell(look: Dict[str, Any], name: str) -> str:
    claude = look.get("claude") or {}
    faces = look.get("faces") or {}
    bits = []
    if not claude.get("ran"):
        bits.append(claude.get("why") or "Claude didn't look")
    if not faces.get("ok"):
        bits.append(faces.get("note") or "faces weren't looked for")
    elif not (look.get("refs") or {}).get("photos") and not (look.get("refs") or {}).get("learned"):
        bits.append(f"ClipAgent doesn't know what {name} looks like yet — add 1–3 clear photos of {name}'s face "
                    "on the campaign page")
    elif (look.get("match") or {}).get("status") == "unclear":
        bits.append(f"nobody in the clip clearly matches {name}'s reference faces")
    return "; ".join(bits) or "the signals were too weak"


def _identity_check(r: Dict[str, Any], look: Dict[str, Any], hook: str, caption: str) -> Optional[Dict[str, str]]:
    from . import highlights
    who = look.get("who")
    if not who:
        return None
    name = r["focus"] or r["creator"] or "the creator"
    other = who.get("other_name") or "Someone else"
    credits_now = bool(r["creator"]) and (highlights.credits_creator(hook, r["creator"])
                                          or highlights.credits_creator(caption, r["creator"]))
    hs = who.get("hook_speaker")
    wrong_credit = credits_now and hs in ("other", "nobody_on_screen")
    unsure_credit = credits_now and hs not in ("creator", "other", "nobody_on_screen")
    fixed = [n for n in look.get("fixes") or [] if n.startswith("Rewrote the")]
    evidence = _evidence(who, name, other)
    credit_line = ""
    if wrong_credit:
        credit_line = f"The hook or caption credits {r['creator']} with words {other.lower() if other == 'Someone else' else other} says — change it."
    elif unsure_credit:
        credit_line = (f"The hook or caption says {r['creator']} said this, but it couldn't be confirmed that "
                       f"{r['creator']} is the one talking ({_cant_tell(look, r['creator'])}).")
    if r["focus"]:
        label = "Main person on screen"
        main = who.get("main")
        if main == "creator":
            item = _item("identity", label, "pass", f"{name} is the main person" + (f": {evidence}." if evidence else "."))
        elif main == "other":
            item = _short(_item("identity", label, "fail",
                                f"{other} is the one talking in this clip, and the {r['campaign']} campaign needs "
                                f"{name} to be the main person." + (f" ({evidence}.)" if evidence else "")),
                          f"{other} is the main person, not {name}")
            return item
        elif main == "nobody":
            item = _short(_item("identity", label, "warn",
                                f"Nobody's face shows in this clip (a screen share or a chart?). The brief needs {name} "
                                f"to be the main person — check it's {name} talking before posting."),
                          "nobody on screen — check it's " + name)
        elif main == "unknown":
            item = _short(_item("identity", label, "warn",
                                f"Couldn't check that {name} is the main person: {_cant_tell(look, name)}."),
                          f"couldn't check {name} is the main person")
        else:
            item = _short(_item("identity", label, "warn",
                                f"Couldn't tell for sure that {name} is the main person — "
                                f"{evidence or _cant_tell(look, name)}. Watch it before posting."),
                          f"not sure {name} is the main person")
        if credit_line:
            item["status"] = "warn" if item["status"] == "pass" else item["status"]
            item["detail"] += " " + credit_line
            if item["status"] == "warn" and not item.get("short"):
                _short(item, f"hook credits {r['creator']} with someone else's words")
        if fixed:
            item["detail"] += " " + " ".join(f + "." for f in fixed)
        return item
    label = "Words credited to the right person"
    if wrong_credit:
        return _short(_item("identity", label, "warn", credit_line + (f" ({evidence}.)" if evidence else "")),
                      f"hook credits {r['creator']} with someone else's words")
    if unsure_credit:
        return _short(_item("identity", label, "warn", credit_line + " Check before posting."),
                      f"couldn't confirm {r['creator']} says the hook's words")
    if fixed:
        return _item("identity", label, "pass", "Fixed: " + " ".join(f + "." for f in fixed))
    return _item("identity", label, "pass",
                 "The hook and caption don't credit anyone with words they didn't say." if (hook or caption)
                 else "No hook or caption words to check.")


def _when(a: float, b: float) -> str:
    from .lookcheck import mmss
    return f"at {mmss(a)}" if b - a < 1.0 else f"from {mmss(a)} to {mmss(b)}"


def _where(where: str) -> str:
    where = (where or "").strip().rstrip(".")
    if not where:
        return ""
    return f" in the {where}" if re.match(r"(top|bottom|middle|centre|center|left|right|upper|lower)", where, re.I) \
        else f" {where}"


def _spoken(look: Dict[str, Any], key_local: str, key_claude: str) -> List[Dict[str, Any]]:
    """Phrases heard, from the word lists and from Claude, without saying one twice."""
    out: List[Dict[str, Any]] = []
    for x in ((look.get("local") or {}).get(key_local) or []) + ((look.get("claude") or {}).get(key_claude) or []):
        if not any(abs(float(x.get("at") or 0) - float(o.get("at") or 0)) < 1.5 for o in out):
            out.append({"quote": x.get("quote") or "", "at": float(x.get("at") or 0)})
    return out


def _logos_check(r: Dict[str, Any], look: Dict[str, Any], kind: str) -> Optional[Dict[str, str]]:
    from .lookcheck import mmss
    if not r["no_logos"]:
        return None
    label = "Logos and sponsor banners"
    claude = look.get("claude") or {}
    seen = [x for x in claude.get("logos") or [] if x.get("kind") != "drawn_by_clipagent"]
    own = [x for x in seen if x.get("kind") == "campaign_own"]
    banned = [x for x in seen if x.get("kind") != "campaign_own"]
    exempt = r["brand_logo"] in ("required", "allowed") or (kind == "overlay" and r["brand_logo"] != "forbidden")
    spoken = _spoken(look, "promos", "spoken_promos")
    said = (f" Someone says “{spoken[0]['quote']}” at {mmss(spoken[0]['at'])} — a spoken promo." if spoken else "")
    if banned:
        lines = [f"{x['what'][:1].upper() + x['what'][1:]} shows{_where(x.get('where'))} {_when(x['from'], x['to'])}"
                 for x in banned[:3]]
        more = f" (and {len(banned) - 3} more)" if len(banned) > 3 else ""
        quote = f" The brief: “{r['no_logos_quote'][:120]}”" if r["no_logos_quote"] else ""
        return _short(_item("logos", label, "fail", "; ".join(lines) + more + "." + quote), lines[0])
    if own and not exempt:
        x = own[0]
        return _short(_item("logos", label, "warn",
                            f"{x['what'][:1].upper() + x['what'][1:]} shows{_where(x.get('where'))} "
                            f"{_when(x['from'], x['to'])}. The brief says no logos — check whether "
                            f"{r['creator'] or 'the brand'}'s own logo counts." + said),
                      "the campaign's own logo shows — check the brief allows it")
    if not claude.get("ran"):
        return _short(_item("logos", label, "warn",
                            f"Couldn't look at the picture for logos, sponsor banners or promo codes — "
                            f"{claude.get('why') or 'Claude did not look'}. Look through it before posting."
                            + (said or (" No promo codes are said in it." if kind != "overlay" else ""))),
                      "logos and banners not checked")
    if spoken:
        return _short(_item("logos", label, "warn", said.strip() + " The brief bans promotions — check before posting."),
                      f"a spoken promo at {mmss(spoken[0]['at'])}")
    return _item("logos", label, "pass", f"No logos, sponsor banners, promo codes or watermarks seen in "
                                         f"{look.get('frames') or 'the'} frames" +
                 (", and none said." if kind != "overlay" else "."))


def _ai_check(r: Dict[str, Any], look: Dict[str, Any], kind: str) -> Optional[Dict[str, str]]:
    from .lookcheck import mmss
    if not r["no_ai"]:
        return None
    label = "AI-made footage"
    claude = look.get("claude") or {}
    shown = claude.get("ai_visuals") or []
    spoken = _spoken(look, "ai", "ai_mentions")
    if shown:
        x = shown[0]
        quote = f" The brief: “{r['no_ai_quote'][:120]}”" if r["no_ai_quote"] else " The brief bans AI-generated video."
        return _short(_item("ai_footage", label, "fail",
                            f"AI-made footage shows {_when(x['from'], x['to'])} ({x['what']}).{quote}"),
                      f"AI-made footage {_when(x['from'], x['to'])}")
    if not claude.get("ran"):
        said = (f" AI is mentioned at {mmss(spoken[0]['at'])} (“{spoken[0]['quote']}”)." if spoken else "")
        return _short(_item("ai_footage", label, "warn",
                            f"Couldn't look at the picture for AI-made footage — {claude.get('why') or 'Claude did not look'}."
                            + said + " Look through it before posting."), "AI footage not checked")
    if spoken:
        return _short(_item("ai_footage", label, "warn",
                            f"AI video is mentioned at {mmss(spoken[0]['at'])} (“{spoken[0]['quote']}”) but none was "
                            "seen in the frames — check it before posting."), "AI mentioned — check the footage")
    return _item("ai_footage", label, "pass", "No AI-made visuals seen" + (", and none mentioned." if kind != "overlay"
                                                                           else "."))


def _offensive_check(r: Dict[str, Any], look: Dict[str, Any], edits: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    from .lookcheck import mmss
    if look.get("kind") == "overlay":
        return None
    label = "Offensive or negative content"
    claude = look.get("claude") or {}
    found = look.get("offensive") or []
    applied = [list(map(float, c)) for c in ((edits or {}).get("cut_applied") or [])]

    def was_cut(f: Dict[str, Any]) -> bool:
        src = f.get("src")
        return bool(src) and any(abs(a - src[0]) < 0.06 and abs(b - src[1]) < 0.06 for a, b in applied)

    left = [f for f in found if not was_cut(f)]
    name = r["creator"] or "the creator"
    if left:
        f = left[0]
        what = "An offensive joke" if f["kind"] == "offensive_joke" else "An offensive word"
        why = f["why_not"] or "it wasn't cut out"
        bad = (f" It's said by {name}, so it would also show {name} in a bad light — the brief: “{r['negative'][:100]}”."
               if r["negative"] and f.get("said_by") == "creator" else "")
        return _short(_item("offensive", label, "fail",
                            f"{what} (“{f['quote']}”) {_when(f['from'], f['to'])} can't be cut out: {why}.{bad} "
                            "Pick another moment, or download anyway if you're sure."
                            + (f" ({len(left) - 1} more found.)" if len(left) > 1 else "")),
                      f"{what.lower()} {_when(f['from'], f['to'])}")
    if claude.get("bad_light") and r["negative"]:
        return _short(_item("offensive", label, "fail",
                            f"This clip shows {name} in a bad light ({claude.get('bad_light_why') or 'Claude flagged it'}) "
                            f"— the brief: “{r['negative'][:120]}”."), f"shows {name} in a bad light")
    if found:
        return _item("offensive", label, "pass", "Cut out " + ", ".join(
            f"{'an offensive joke' if f['kind'] == 'offensive_joke' else 'an offensive word'} at {mmss(f['from'])}"
            for f in found) + ".")
    if not claude.get("ran"):
        return _short(_item("offensive", label, "warn",
                            f"Only checked the words for the most common slurs ({claude.get('why') or 'Claude did not look'})"
                            " — not for offensive jokes. Listen to it before posting."), "only checked for common slurs")
    return _item("offensive", label, "pass", "No slurs or offensive jokes in what's said.")


def look_checks(rb: Dict[str, Any], look: Optional[Dict[str, Any]], *, kind: str = "clip", hook: str = "",
                caption: str = "", edits: Optional[Dict[str, Any]] = None,
                spans: Optional[List[Any]] = None) -> List[Dict[str, str]]:
    """The look's findings as check lines: identity, logos, offensive, ai_footage. Anything that
    couldn't be looked at says so (Check first) — it never counts as a pass."""
    from . import lookcheck
    r = lookcheck.rules_of(rb)
    if not isinstance(look, dict) or not look:
        out = []
        if r["focus"] and kind != "overlay":
            out.append(_short(_item("identity", "Main person on screen", "warn",
                                    f"Not checked: who is on screen and talking wasn't looked at for this clip — make "
                                    f"sure {r['focus']} is the main person before posting."),
                              f"not checked that {r['focus']} is the main person"))
        if r["no_logos"]:
            out.append(_short(_item("logos", "Logos and sponsor banners", "warn",
                                    "Not checked: nobody looked at this clip for logos, sponsor banners or promo codes."),
                              "logos and banners not checked"))
        if r["no_ai"]:
            out.append(_short(_item("ai_footage", "AI-made footage", "warn",
                                    "Not checked: nobody looked at this clip for AI-made footage."),
                              "AI footage not checked"))
        return out
    items = [x for x in (_identity_check(r, look, hook, caption) if kind != "overlay" else None,
                         _logos_check(r, look, kind), _offensive_check(r, look, edits), _ai_check(r, look, kind)) if x]
    missed = lookcheck.uncovered(look, spans) if spans else 0.0
    if missed > 0.5:
        note = f" ({missed:.0f}s of this version were added after the check and weren't looked at.)"
        for it in items:
            it["detail"] += note
            if missed > lookcheck.UNCHECKED_OK and it["status"] == "pass":
                it["status"] = "warn"
    if look.get("fix_error"):
        for it in items:
            if it["status"] != "pass":
                it["detail"] += f" {look['fix_error']}"
    return items


def check_overlay(rb: Dict[str, Any], source: Path, output: Path, made: Dict[str, Any],
                  post: Dict[str, Any], hook: str, tone: Optional[Dict[str, str]],
                  src_info: Optional[Dict[str, Any]] = None,
                  look: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A clip-bank clip: the footage, sound and length must be the source's own.
    `look` (lookcheck.review_overlay) adds what the brief bans in the picture."""
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
    mention = _must_mention(r, [hook], post.get("text") or post.get("caption") or "", a["hook"])
    if mention:
        checks.append(mention)
    checks.append(_platform(r, post.get("platform", "")))
    checks.append(_tone(tone, hook))
    checks += look_checks(rb, look, kind="overlay", hook=hook)
    result = summarize(checks)
    result["measured"] = {"frames": f_out, "frames_source": f_in, "duration": round(out["duration"], 3),
                          "psnr_min": psnr, "psnr": drift.get("psnr"), "hook_area": drift.get("covered"),
                          "colour_shift": drift.get("colour_shift"), "contrast_change": drift.get("contrast_change"),
                          "logo": seen}
    return result


def check_source(rb: Dict[str, Any], clip: Dict[str, Any], edits: Dict[str, Any], output: Path,
                 post: Dict[str, Any], hook: str, tone: Optional[Dict[str, str]],
                 source_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A clip cut from longer footage: length, plus every switch the brief forbids
    is confirmed off in what was actually rendered. `source_meta` is what the
    download learned about the video (its upload date), for a brief's date rule."""
    r = campaign.resolve(rb)
    a = r["allowed"]
    checks: List[Dict[str, str]] = []
    try:
        seconds = overlay.inspect(output)["duration"]
    except Exception:
        seconds = float(clip.get("end", 0)) - float(clip.get("start", 0)) - float(clip.get("saved") or 0)
    checks.append(_length(r, seconds))
    dated = _upload_date(r, source_meta)
    if dated:
        checks.append(dated)

    parts = clip.get("parts") or []
    if not a["stitch"]:
        checks.append(_item("stitch", "One unbroken moment", "pass" if len(parts) <= 1 else "fail",
                            "A single stretch of the video." if len(parts) <= 1 else
                            f"Joins {len(parts)} moments — the brief doesn't allow that."))
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
    shown = hook if edits.get("hook_on", True) else ""
    mention = _must_mention(r, _screen_texts(edits, shown), post.get("text") or post.get("caption") or "",
                            a["hook"])
    if mention:
        checks.append(mention)
    checks.append(_platform(r, post.get("platform", "")))
    checks.append(_tone(tone, shown))
    # what the campaign look found (kept with the clip by lookcheck.review_clip)
    from . import lookcheck
    checks += look_checks(rb, edits.get("campaign_look"), kind="clip", hook=lookcheck._on_screen(edits),
                          caption=post.get("caption") or "", edits=edits, spans=lookcheck.clip_spans(clip))
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
    mention = _must_mention(r, [hook] + [s.get("text") or "" for s in segs],
                            post.get("text") or post.get("caption") or "", a["hook"])
    if mention:
        checks.append(mention)
    checks.append(_tone(tone, hook))
    # the campaign look: who is in it, logos and banners, offensive words, AI footage
    from . import lookcheck
    try:
        look = lookcheck.review_edit(rb, timeline, output, hook, post)
    except Exception as exc:                           # a look that couldn't run is a flag, not a pass
        look = {"kind": "edit", "claude": {"ran": False, "why": str(exc)[:140]}, "faces": {"ok": False}}
    checks += look_checks(rb, look, kind="edit", hook=hook, caption=(post or {}).get("caption") or "")
    result = summarize(checks)
    result["measured"] = {"duration": round(seconds, 3)}
    result["look"] = {"who": look.get("who"), "faces": look.get("faces")}
    return result
