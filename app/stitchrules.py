"""The honesty rules every stitched clip follows, checked in code.

Smart Stitch (docs/SMART_STITCH_PLAN.md, section 0) joins pieces of video —
a teaser of the best part up front, a callback to an earlier line, a laugh
after a punchline, a chart shown while he talks about it. Claude proposes the
pieces; nothing it proposes is trusted until it passes these checks:

  quotes      every piece quotes its words, and the quote must match what the
              transcript says at those times (fuzzy, >= 0.8 once case and
              punctuation are set aside). No match: the piece is dropped.
  sentences   a piece with words starts on the first word of a sentence and
              ends on the last word of one, so nobody seems to say something
              they didn't.
  reactions   a reaction only follows the line it really reacted to: it comes
              after that line in the video, and soon after.
  labels      an on-screen time label is true: a number in it is backed by
              the real jump in the video or by the words said; otherwise it
              becomes a neutral word ("LATER").
  length      the platform's and the campaign's length caps still hold.
  campaign    the brief decides: joining moments, added sound, speed changes,
              a background around the picture.

Part 1 (inside one video) uses these now; Part 2 (across videos) reuses
them, which is why they live here on their own and take plain data.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

QUOTE_MIN = 0.8              # fuzzy match a quote needs, after normalising
EDGE_SLACK = 2               # words a quote's edge may be off by and still match
WORD_CUT_SLACK = 0.3         # a span may end this long before its last word finishes

LAUGH = re.compile(r"^(\[?(laugh(s|ing|ter)?|chuckles?|applause|cheering|crowd)\]?|(ha){2,}h?|(he){2,}h?|lol|lmao)$",
                   re.I)


# --- words ---------------------------------------------------------------------------

def norm_tokens(text: str) -> List[str]:
    """Words for comparing: lower case, no punctuation, "50,000" == "50000"."""
    t = (text or "").lower().replace("’", "'").replace("‘", "'")
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)
    t = re.sub(r"[^\w'\s]", " ", t)
    return [w.strip("'") for w in t.split() if w.strip("'")]


def _sorted(words: Sequence[Dict[str, Any]], start: float, end: float, pad: float = 30.0) -> List[Dict[str, Any]]:
    from .captions import sanitize_words            # crosstalk timings resolved the same way everywhere
    near = [w for w in words if start - pad <= float(w["start"]) <= end + pad]
    return sanitize_words(sorted(near, key=lambda w: float(w["start"])))


def _inside(ws: Sequence[Dict[str, Any]], start: float, end: float) -> List[int]:
    return [i for i, w in enumerate(ws) if start - 0.05 <= float(w["start"]) < end - 0.05]


def span_text(words: Sequence[Dict[str, Any]], start: float, end: float) -> str:
    return " ".join(w["w"] for w in words if start - 0.05 <= float(w["start"]) < end - 0.05)


def is_laughter(words: Sequence[Dict[str, Any]]) -> bool:
    toks = [t for w in words for t in norm_tokens(w["w"])]
    return bool(toks) and all(LAUGH.match(t) for t in toks)


# --- rule 4: quotes are verified ---------------------------------------------------------

def _ratio(a: Sequence[str], b: Sequence[str]) -> float:
    """Words in common over the longer of the two: a changed word always
    counts against the match, whichever side it is on."""
    if not a or not b:
        return 0.0
    m = sum(block.size for block in SequenceMatcher(None, list(a), list(b), autojunk=False).get_matching_blocks())
    return m / max(len(a), len(b))


def _edge_match(q: List[str], ext: List[str], lo: int, hi: int, at_start: bool) -> float:
    """Best match of q against the span's first (or last) words, letting the
    edge move a couple of words either way. `ext` is the span with up to
    EDGE_SLACK neighbouring words on each side; [lo, hi) is the span in it."""
    best = 0.0
    n = len(q)
    for shift in range(-EDGE_SLACK, EDGE_SLACK + 1):
        for extra in range(-EDGE_SLACK, EDGE_SLACK + 1):
            size = n + extra
            if size <= 0:
                continue
            if at_start:
                a = lo + shift
                b = a + size
            else:
                b = hi + shift
                a = b - size
            if a < 0 or b > len(ext) or a >= b:
                continue
            best = max(best, _ratio(q, ext[a:b]))
    return best


def quote_score(quote: str, words: Sequence[Dict[str, Any]], start: float, end: float) -> Tuple[float, str]:
    """How well a quote matches the words said between `start` and `end` (0-1),
    and why when it is low.

    A quote may be the whole span's words, or its first words and last words
    joined by "…" (for a long part). A quote far shorter than the span, with
    no "…", can't vouch for the whole part and scores 0."""
    pieces = [norm_tokens(p) for p in re.split(r"\s*(?:…|\.\.\.)\s*", quote or "")]
    pieces = [p for p in pieces if p]
    ws = _sorted(words, start, end)
    inside = _inside(ws, start, end)
    span = [t for i in inside for t in norm_tokens(ws[i]["w"])]
    if not pieces:
        return (1.0, "") if not span else (0.0, "no quote given")
    if not span:
        laugh = all(LAUGH.match(t) for p in pieces for t in p)
        return (1.0, "") if laugh else (0.0, "nothing is said there")
    before = [t for i in range(max(0, inside[0] - EDGE_SLACK), inside[0]) for t in norm_tokens(ws[i]["w"])]
    after = [t for i in range(inside[-1] + 1, min(len(ws), inside[-1] + 1 + EDGE_SLACK))
             for t in norm_tokens(ws[i]["w"])]
    ext = before + span + after
    lo, hi = len(before), len(before) + len(span)
    if len(pieces) >= 2:
        head, tail = pieces[0], pieces[-1]
        score = min(_edge_match(head, ext, lo, hi, True), _edge_match(tail, ext, lo, hi, False))
        return score, "" if score >= QUOTE_MIN else "the quoted words aren't what is said at its start or end"
    q = pieces[0]
    if len(q) < 0.5 * len(span) and len(q) < 12:
        return 0.0, "the quote is too short to check against the whole part"
    whole = max(_ratio(q, ext[a:b])
                for a in range(max(0, lo - EDGE_SLACK), min(len(ext), lo + EDGE_SLACK) + 1)
                for b in range(max(a + 1, hi - EDGE_SLACK), min(len(ext), hi + EDGE_SLACK) + 1))
    if whole < QUOTE_MIN and len(q) < len(span):
        # A long part quoted by its opening words only: they must open it.
        whole = max(whole, _edge_match(q, ext, lo, hi, True) if len(q) >= 12 else 0.0)
    return whole, "" if whole >= QUOTE_MIN else "the quoted words aren't what is said there"


def check_quote(quote: str, words: Sequence[Dict[str, Any]], start: float, end: float) -> Tuple[bool, str]:
    score, why = quote_score(quote, words, start, end)
    return score >= QUOTE_MIN, why or ""


# --- rule 1: complete thoughts ------------------------------------------------------------

def _edges(ws: List[Dict[str, Any]]) -> Tuple[set, set]:
    from .highlights import _sentence_edges
    return _sentence_edges(ws) if ws else (set(), set())


def check_sentences(start: float, end: float, words: Sequence[Dict[str, Any]]) -> Tuple[bool, str]:
    """A span with words opens on a sentence's first word and closes on a
    sentence's last word (punctuation and pauses decide where sentences are).
    A span with no words at all — a laugh, a look — passes."""
    ws = _sorted(words, start, end)
    inside = _inside(ws, start, end)
    if not inside:
        return True, ""
    opens, closes = _edges(ws)
    i, j = inside[0], inside[-1]
    if i not in opens:
        return False, f"it starts in the middle of a sentence (on “{ws[i]['w']}”)"
    if j not in closes:
        return False, f"it stops in the middle of a sentence (after “{ws[j]['w']}”)"
    if float(ws[j]["end"]) > end + WORD_CUT_SLACK:
        return False, f"it cuts off the word “{ws[j]['w']}”"
    return True, ""


def sentence_spans(words: Sequence[Dict[str, Any]], lo: float, hi: float,
                   min_len: float, max_len: float) -> List[Tuple[float, float, int, int]]:
    """Every run of whole sentences inside [lo, hi] lasting min_len-max_len
    seconds: (start, end, first word index, last word index) with a little
    breathing room at each end, never into the neighbouring words."""
    ws = _sorted(words, lo, hi)
    if not ws:
        return []
    opens, closes = _edges(ws)
    out = []
    for i in sorted(opens):
        if float(ws[i]["start"]) < lo - 0.05:
            continue
        for j in sorted(closes):
            if j < i:
                continue
            if float(ws[j]["end"]) > hi + 0.1:
                break
            s = float(ws[i]["start"]) - 0.08
            if i > 0:
                s = max(s, float(ws[i - 1]["end"]) + 0.02)
            e = float(ws[j]["end"]) + 0.15
            if j + 1 < len(ws):
                e = min(e, float(ws[j + 1]["start"]) - 0.02)
            if e - s > max_len:
                break
            if e - s >= min_len:
                out.append((round(max(lo - 0.05, s), 2), round(min(hi + 0.1, e), 2), i, j))
    return out


def snap_sentences(start: float, end: float, words: Sequence[Dict[str, Any]], lo: float, hi: float,
                   min_len: float, max_len: float) -> Optional[Tuple[float, float]]:
    """The whole-sentence span inside [lo, hi] that best matches what was
    proposed (start, end), or None when no span of the right length overlaps it."""
    best, best_score = None, 0.0
    for s, e, _, _ in sentence_spans(words, lo, hi, min_len, max_len):
        inter = min(e, end) - max(s, start)
        if inter <= 0:
            continue
        score = inter / (max(e, end) - min(s, start))
        if score > best_score:
            best, best_score = (s, e), score
    return best


def anchor_after(quote: str, words: Sequence[Dict[str, Any]], near: float,
                 window: float = 8.0) -> Optional[float]:
    """Where a quoted line ends (the end of its sentence), searched within
    `window` seconds before `near` and a little after. None when the line
    isn't said there — the anchor of a callback or a reaction must be real."""
    q = norm_tokens(quote)
    if not q:
        return None
    ws = _sorted(words, near - window, near + 4.0, pad=0)
    if not ws:
        return None
    toks: List[Tuple[str, int]] = [(t, i) for i, w in enumerate(ws) for t in norm_tokens(w["w"])]
    best, best_end = 0.0, None
    n = len(q)
    for a in range(0, max(1, len(toks) - n + 1)):
        for size in (n - 1, n, n + 1):
            b = a + size
            if size <= 0 or b > len(toks):
                continue
            r = _ratio(q, [t for t, _ in toks[a:b]])
            if r > best:
                best, best_end = r, toks[b - 1][1]
    if best < QUOTE_MIN or best_end is None:
        return None
    _, closes = _edges(ws)
    j = next((k for k in sorted(closes) if k >= best_end), len(ws) - 1)
    return round(float(ws[j]["end"]) + 0.05, 2)


# --- rule 2: reactions follow what they react to ------------------------------------------

def check_reaction_order(reaction_start: float, reacts_to_end: float, max_gap: float = 20.0,
                         words: Optional[Sequence[Dict[str, Any]]] = None,
                         max_between: int = 6) -> Tuple[bool, str]:
    """A reaction comes after the line it reacts to, soon after it, and with
    little else said in between (or it may be reacting to that instead)."""
    if reaction_start < reacts_to_end - 0.25:
        return False, "it happens before the line it's meant to react to"
    if reaction_start - reacts_to_end > max_gap:
        return False, f"it happens {reaction_start - reacts_to_end:.0f}s after the line — too late to be a reaction to it"
    if words is not None:
        between = [w for w in words if reacts_to_end + 0.05 <= float(w["start"]) < reaction_start - 0.05]
        if len(between) > max_between:
            return False, (f"{len(between)} more words are said before it — too late to be a reaction to that "
                           "line, it may be reacting to something else")
    return True, ""


# --- rule 3: true time labels --------------------------------------------------------------

_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
                 "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90, "hundred": 100}
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
                 "month": 2629800, "year": 31557600}
_AMOUNT = re.compile(r"\b(\d+(?:\.\d+)?|" + "|".join(_NUMBER_WORDS) + r")\s+(second|minute|hour|day|week|month|year)s?\b",
                     re.I)


def _number(token: str) -> Optional[float]:
    t = token.lower()
    if t in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[t])
    try:
        return float(t)
    except ValueError:
        return None


def true_label(label: str, gap_seconds: Optional[float] = None, said: str = "") -> str:
    """A label as it may be shown. Words alone ("BEFORE", "THE REVEAL") pass.
    A number must be true: "20 MINUTES LATER" when the jump in the video
    really is about 20 minutes, or any number the speaker says himself in the
    parts it joins ("five months later"). Otherwise a neutral word replaces it
    — never a guessed number."""
    text = " ".join((label or "").upper().split())[:24]
    if not re.search(r"\d", text) and not _AMOUNT.search(text):
        return text
    said_toks = norm_tokens(said)
    said_nums = {_number(t) for t in said_toks} - {None}
    m = _AMOUNT.search(text)
    if m:
        amount = _number(m.group(1))
        unit = _UNIT_SECONDS[m.group(2).lower()]
        if amount and gap_seconds is not None and gap_seconds > 0:
            ratio = gap_seconds / (amount * unit)
            if 0.6 <= ratio <= 1.5:
                return text
    numbers = []
    for tok in re.findall(r"\d+(?:\.\d+)?|[a-z]+", text.lower()):
        if tok[0].isdigit() or (tok in _NUMBER_WORDS and tok not in ("a", "an")):
            numbers.append(_number(tok))
    if numbers and all(n in said_nums for n in numbers):
        return text
    if gap_seconds is not None and gap_seconds < 0:
        return "EARLIER"
    return "LATER"


# --- rules 5 and 6: campaign and length -----------------------------------------------------

def campaign_allows(rules: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    """What the brief allows that Smart Stitch needs. No campaign: everything."""
    keys = ("stitch", "music", "speed", "borders", "zoom", "audio")
    if not rules:
        return {k: True for k in keys}
    from . import campaign
    a = campaign.resolve(rules)["allowed"]
    return {"stitch": bool(a.get("stitch")), "music": bool(a.get("music")), "speed": bool(a.get("speed")),
            "borders": bool(a.get("borders")), "zoom": bool(a.get("zoom")) and bool(a.get("crop")),
            "audio": bool(a.get("audio"))}


def length_cap(settings: Dict[str, Any]) -> float:
    """The longest a clip may run for where it's going (platforms, or the brief's maximum)."""
    from .highlights import length_window
    return float(length_window(settings.get("min_len"), settings.get("max_len"),
                               settings.get("platforms") or None)[1])
