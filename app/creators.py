"""Creator Scan's data: creators, their catalogs, the words of every video,
the moments found in them, and the scans that do the work.

Same SQLite file as the rest of ClipAgent (store.connect), own tables and own
init() — like money.py — so it never collides with the core tables. JSON
columns are decoded on read.

One decision on top of the plan: a catalog row is unique per creator
(creator_id, platform, video_id), not per video. Two creators may share a
channel (a podcast and its guest, a main and a clips channel); each gets its
own row and its own moments. The expensive work is still never paid twice:
the words of a video already read for another creator are copied
(words_elsewhere), and a screening with the same guidance is reused from
scan_cache.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from . import store

KINDS = ["crazy", "funny", "success", "money", "quote", "reaction", "hype", "story"]
CATALOG_STATUSES = ("listed", "words", "scored", "done", "skipped", "failed")
MOMENT_STATUSES = ("candidate", "fetched", "checked", "used", "dropped")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "kinds": list(KINDS),      # which kinds of moment to look for
    "since": "",               # "YYYY-MM-DD": only videos uploaded on or after this day
    "min_views": 0,            # only videos with at least this many views
    "include_shorts": False,   # shorts (and Reels/TikToks) are clips already
    "include_streams": True,   # YouTube's Live tab, Twitch and Kick past broadcasts
    "fetch_top": 40,           # how many of the best moments get their part of the video downloaded
    "min_minutes": 0,          # skip videos shorter than this
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS creators (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    links        TEXT,
    campaign_id  TEXT,
    settings     TEXT,
    status       TEXT,
    created_at   REAL,
    last_scan_at REAL
);
CREATE TABLE IF NOT EXISTS catalog (
    id           TEXT PRIMARY KEY,
    creator_id   TEXT,
    platform     TEXT,
    video_id     TEXT,
    url          TEXT,
    title        TEXT,
    duration     REAL,
    upload_date  TEXT,
    views        INTEGER,
    likes        INTEGER,
    kind         TEXT,
    status       TEXT,
    words_source TEXT,
    heatmap      TEXT,
    outlier      REAL,
    error        TEXT,
    updated_at   REAL,
    UNIQUE(creator_id, platform, video_id)
);
CREATE INDEX IF NOT EXISTS catalog_by_creator ON catalog(creator_id, status);
CREATE INDEX IF NOT EXISTS catalog_by_video ON catalog(platform, video_id);
CREATE TABLE IF NOT EXISTS catalog_words (
    catalog_id   TEXT PRIMARY KEY,
    transcript   TEXT,
    source       TEXT,
    created_at   REAL
);
CREATE TABLE IF NOT EXISTS catalog_chunks (
    catalog_id   TEXT,
    start        REAL,
    end          REAL,
    text         TEXT
);
CREATE INDEX IF NOT EXISTS catalog_chunks_by_video ON catalog_chunks(catalog_id);
CREATE TABLE IF NOT EXISTS moments (
    id             TEXT PRIMARY KEY,
    creator_id     TEXT,
    catalog_id     TEXT,
    start          REAL,
    end            REAL,
    hit            REAL,
    kind           TEXT,
    score          REAL,
    signals        TEXT,
    text           TEXT,
    hook           TEXT,
    reason         TEXT,
    speaker        TEXT,
    story_key      TEXT,
    status         TEXT,
    section_path   TEXT,
    section_offset REAL,
    drop_reason    TEXT,
    used_in        TEXT,
    created_at     REAL
);
CREATE INDEX IF NOT EXISTS moments_by_creator ON moments(creator_id, status);
CREATE INDEX IF NOT EXISTS moments_by_video ON moments(catalog_id);
CREATE TABLE IF NOT EXISTS scan_jobs (
    id           TEXT PRIMARY KEY,
    creator_id   TEXT,
    stage        TEXT,
    status       TEXT,
    progress     REAL,
    counters     TEXT,
    message      TEXT,
    wait_until   REAL,
    started_at   REAL,
    finished_at  REAL,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS scan_jobs_by_creator ON scan_jobs(creator_id, started_at);
CREATE TABLE IF NOT EXISTS scan_cache (
    key          TEXT PRIMARY KEY,
    payload      TEXT,
    created_at   REAL
);
"""

CHUNK_SECONDS = 30.0        # search looks at the words in stretches this long


def init() -> None:
    with store.connect() as conn:
        conn.executescript(SCHEMA)


def _loads(text: Any, default: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _statuses(status: Union[None, str, Sequence[str]]) -> Optional[List[str]]:
    if status is None:
        return None
    return [status] if isinstance(status, str) else list(status)


# --- creators -------------------------------------------------------------------------

def clean_settings(settings: Optional[Dict[str, Any]], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Settings with every key present and every value the right type."""
    out = dict(DEFAULT_SETTINGS)
    out.update({k: v for k, v in (base or {}).items() if k in DEFAULT_SETTINGS})
    out.update({k: v for k, v in (settings or {}).items() if k in DEFAULT_SETTINGS})
    kinds = [k for k in (out.get("kinds") or []) if k in KINDS]
    out["kinds"] = kinds or list(KINDS)
    since = str(out.get("since") or "").strip()[:10]
    out["since"] = since if len(since) == 10 and since[4] == "-" and since[7] == "-" else ""
    for key, lo, hi in (("min_views", 0, 10 ** 12), ("fetch_top", 0, 500), ("min_minutes", 0, 600)):
        try:
            out[key] = int(max(lo, min(hi, float(out.get(key) or 0))))
        except (TypeError, ValueError):
            out[key] = DEFAULT_SETTINGS[key]
    for key in ("include_shorts", "include_streams"):
        out[key] = out.get(key) in (True, 1, "1", "true", "True", "on", "yes")
    return out


def _links(links: Iterable[Any]) -> List[str]:
    seen, out = set(), []
    for link in links or []:
        u = str(link or "").strip().rstrip("/")
        if u and u.lower() not in seen:
            seen.add(u.lower())
            out.append(u)
    return out


def _creator(row: Any) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    d["links"] = _loads(d.get("links"), [])
    d["settings"] = clean_settings(_loads(d.get("settings"), {}))
    return d


def create_creator(name: str, links: Iterable[Any], campaign_id: str = "",
                   settings: Optional[Dict[str, Any]] = None) -> str:
    cid = store.new_id()
    with store.connect() as conn:
        conn.execute("INSERT INTO creators (id,name,links,campaign_id,settings,status,created_at,last_scan_at)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (cid, (name or "").strip()[:80] or "Creator", _dumps(_links(links)), campaign_id or "",
                      _dumps(clean_settings(settings)), "new", time.time(), None))
    return cid


def update_creator(creator_id: str, **fields) -> None:
    """Change a creator. `settings` is merged into what's there; `links` replaces the list."""
    if "settings" in fields:
        current = get_creator(creator_id)
        fields["settings"] = _dumps(clean_settings(fields["settings"] or {}, (current or {}).get("settings")))
    if "links" in fields:
        fields["links"] = _dumps(_links(fields["links"]))
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()[:80] or "Creator"
    fields = {k: v for k, v in fields.items()
              if k in ("name", "links", "campaign_id", "settings", "status", "last_scan_at")}
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with store.connect() as conn:
        conn.execute(f"UPDATE creators SET {cols} WHERE id=?", (*fields.values(), creator_id))


def get_creator(creator_id: str) -> Optional[Dict[str, Any]]:
    with store.connect() as conn:
        return _creator(conn.execute("SELECT * FROM creators WHERE id=?", (creator_id,)).fetchone())


def list_creators() -> List[Dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM creators ORDER BY created_at DESC").fetchall()
    return [_creator(r) for r in rows]


def delete_creator(creator_id: str) -> None:
    """The creator and its rows. Never touches files: downloaded parts of videos stay on disk."""
    with store.connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM catalog WHERE creator_id=?", (creator_id,))]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            conn.execute(f"DELETE FROM catalog_words WHERE catalog_id IN ({marks})", part)
            conn.execute(f"DELETE FROM catalog_chunks WHERE catalog_id IN ({marks})", part)
        conn.execute("DELETE FROM catalog WHERE creator_id=?", (creator_id,))
        conn.execute("DELETE FROM moments WHERE creator_id=?", (creator_id,))
        conn.execute("DELETE FROM scan_jobs WHERE creator_id=?", (creator_id,))
        conn.execute("DELETE FROM creators WHERE id=?", (creator_id,))


# --- the catalog ------------------------------------------------------------------------

def _catalog(row: Any) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    d["heatmap"] = _loads(d.get("heatmap"), [])
    return d


def upsert_catalog(creator_id: str, rows: Iterable[Dict[str, Any]]) -> int:
    """Add listed videos; refresh what a new listing knows about ones already there.
    A video's progress (status, words) is never reset by a new listing — except that
    a video the filters skipped before and now let in (or the other way round) moves.
    Returns how many videos are new."""
    new = 0
    now = time.time()
    with store.connect() as conn:
        for r in rows:
            platform, vid = str(r.get("platform") or ""), str(r.get("video_id") or "")
            if not platform or not vid:
                continue
            old = conn.execute("SELECT id,status,words_source,upload_date FROM catalog"
                               " WHERE creator_id=? AND platform=? AND video_id=?",
                               (creator_id, platform, vid)).fetchone()
            status = r.get("status") or "listed"
            if not old:
                conn.execute(
                    "INSERT INTO catalog (id,creator_id,platform,video_id,url,title,duration,upload_date,views,likes,"
                    "kind,status,words_source,heatmap,outlier,error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (store.new_id(), creator_id, platform, vid, r.get("url") or "", (r.get("title") or "")[:300],
                     r.get("duration"), r.get("upload_date") or "", r.get("views"), r.get("likes"),
                     r.get("kind") or "video", status, "", _dumps(r.get("heatmap") or []), r.get("outlier"),
                     r.get("error") or "", now))
                new += 1
                continue
            fields: Dict[str, Any] = {"updated_at": now}
            for key in ("url", "title", "duration", "views", "likes", "kind"):
                if r.get(key) not in (None, ""):
                    fields[key] = r[key] if key != "title" else str(r[key])[:300]
            if "outlier" in r:
                fields["outlier"] = r.get("outlier")
            if r.get("upload_date") and not old["upload_date"]:
                fields["upload_date"] = r["upload_date"]       # an exact date is never replaced by a guess
            never_read = not (old["words_source"] or "")
            if old["status"] == "listed" and status == "skipped":
                fields.update(status="skipped", error=r.get("error") or "")
            elif old["status"] == "skipped" and status == "listed" and never_read:
                fields.update(status="listed", error="")
            cols = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE catalog SET {cols} WHERE id=?", (*fields.values(), old["id"]))
    return new


def update_catalog(catalog_id: str, **fields) -> None:
    if "heatmap" in fields and not isinstance(fields["heatmap"], str):
        fields["heatmap"] = _dumps(fields["heatmap"] or [])
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with store.connect() as conn:
        conn.execute(f"UPDATE catalog SET {cols} WHERE id=?", (*fields.values(), catalog_id))


def get_catalog(catalog_id: str) -> Optional[Dict[str, Any]]:
    with store.connect() as conn:
        return _catalog(conn.execute("SELECT * FROM catalog WHERE id=?", (catalog_id,)).fetchone())


def list_catalog(creator_id: str, status: Union[None, str, Sequence[str]] = None,
                 limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """A creator's videos, newest first. `status` is one status or a list of them."""
    q = "SELECT * FROM catalog WHERE creator_id=?"
    args: List[Any] = [creator_id]
    sts = _statuses(status)
    if sts:
        q += f" AND status IN ({','.join('?' * len(sts))})"
        args += sts
    q += " ORDER BY upload_date DESC, title"
    if limit:
        q += " LIMIT ?"
        args.append(int(limit))
    with store.connect() as conn:
        return [_catalog(r) for r in conn.execute(q, args).fetchall()]


PRIORITY = ("CASE WHEN platform='youtube' THEN 0 ELSE 1 END, CASE WHEN outlier IS NULL THEN 1 ELSE 0 END,"
            " outlier DESC, upload_date DESC, id")


def next_catalog(creator_id: str, status: Union[str, Sequence[str]],
                 exclude: Iterable[str] = ()) -> Optional[Dict[str, Any]]:
    """The video to work on next: YouTube first (subtitles are quick), then the ones that
    outperformed their channel, then the newest."""
    sts = _statuses(status) or []
    skip = list(exclude)
    q = f"SELECT * FROM catalog WHERE creator_id=? AND status IN ({','.join('?' * len(sts))})"
    args: List[Any] = [creator_id, *sts]
    if skip:
        q += f" AND id NOT IN ({','.join('?' * len(skip))})"
        args += skip
    with store.connect() as conn:
        return _catalog(conn.execute(q + f" ORDER BY {PRIORITY} LIMIT 1", args).fetchone())


def catalog_counts(creator_id: str) -> Dict[str, int]:
    with store.connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM catalog WHERE creator_id=? GROUP BY status",
                            (creator_id,)).fetchall()
    out = {s: 0 for s in CATALOG_STATUSES}
    out.update({r["status"]: int(r["n"]) for r in rows})
    return out


def catalog_totals(creator_id: str, status: Union[None, str, Sequence[str]] = None) -> List[Dict[str, Any]]:
    """Light rows (no heatmap) for sums over a big catalog."""
    q = "SELECT id,platform,kind,duration,words_source,status FROM catalog WHERE creator_id=?"
    args: List[Any] = [creator_id]
    sts = _statuses(status)
    if sts:
        q += f" AND status IN ({','.join('?' * len(sts))})"
        args += sts
    with store.connect() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def reset_failed(creator_id: str) -> int:
    """A fresh scan tries again what failed for a passing reason (network, a busy site)."""
    with store.connect() as conn:
        cur = conn.execute("UPDATE catalog SET status='listed', error='' WHERE creator_id=? AND status='failed'",
                           (creator_id,))
        return cur.rowcount or 0


def mark_done(creator_id: str) -> int:
    """At the end of a scan: every screened video is done (re-scans only read new uploads)."""
    with store.connect() as conn:
        cur = conn.execute("UPDATE catalog SET status='done', updated_at=? WHERE creator_id=? AND status='scored'",
                           (time.time(), creator_id))
        return cur.rowcount or 0


# --- words ---------------------------------------------------------------------------------

def _chunks(transcript: Dict[str, Any]) -> List[Tuple[float, float, str]]:
    out: List[Tuple[float, float, str]] = []
    cur: List[str] = []
    start = end = None
    for seg in transcript.get("segments") or []:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        s, e = float(seg.get("start") or 0), float(seg.get("end") or 0)
        if start is None:
            start = s
        elif s - start >= CHUNK_SECONDS or (end is not None and s - end > 8):
            out.append((start, end, " ".join(cur)))
            cur, start = [], s
        cur.append(text)
        end = max(e, s)
    if cur and start is not None:
        out.append((start, end, " ".join(cur)))
    return out


def save_words(catalog_id: str, transcript: Dict[str, Any], source: str) -> None:
    """Keep a video's words (and the stretches search looks through)."""
    with store.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO catalog_words (catalog_id,transcript,source,created_at) VALUES (?,?,?,?)",
                     (catalog_id, _dumps(transcript), source, time.time()))
        conn.execute("DELETE FROM catalog_chunks WHERE catalog_id=?", (catalog_id,))
        conn.executemany("INSERT INTO catalog_chunks (catalog_id,start,end,text) VALUES (?,?,?,?)",
                         [(catalog_id, round(s, 2), round(e, 2), t) for s, e, t in _chunks(transcript)])


def get_words(catalog_id: str) -> Optional[Dict[str, Any]]:
    with store.connect() as conn:
        row = conn.execute("SELECT transcript FROM catalog_words WHERE catalog_id=?", (catalog_id,)).fetchone()
    return _loads(row["transcript"], {}) if row else None


def words_elsewhere(platform: str, video_id: str, not_id: str = "") -> Optional[Tuple[Dict[str, Any], str]]:
    """The words of this video if another creator's scan already read them."""
    with store.connect() as conn:
        row = conn.execute(
            "SELECT w.transcript, w.source FROM catalog c JOIN catalog_words w ON w.catalog_id=c.id"
            " WHERE c.platform=? AND c.video_id=? AND c.id<>? LIMIT 1", (platform, video_id, not_id)).fetchone()
    return (_loads(row["transcript"], {}), row["source"]) if row else None


def search_chunks(creator_id: str, terms: Sequence[str], limit: int = 400) -> List[Dict[str, Any]]:
    """Stretches of a creator's videos holding any of the terms (case-insensitive)."""
    terms = [t for t in terms if t][:12]
    if not terms:
        return []
    likes = " OR ".join("LOWER(k.text) LIKE ?" for _ in terms)
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT k.catalog_id, k.start, k.end, k.text, c.title, c.upload_date FROM catalog_chunks k"
            f" JOIN catalog c ON c.id=k.catalog_id WHERE c.creator_id=? AND ({likes}) LIMIT ?",
            (creator_id, *[f"%{t.lower()}%" for t in terms], int(limit) * 5)).fetchall()
    return [dict(r) for r in rows]


# --- moments ---------------------------------------------------------------------------------

def _moment(row: Any) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    d["signals"] = _loads(d.get("signals"), {})
    d["used_in"] = _loads(d.get("used_in"), [])
    return d


def _insert_moments(conn: Any, creator_id: str, catalog_id: str, rows: Iterable[Dict[str, Any]]) -> List[str]:
    ids = []
    now = time.time()
    for r in rows:
        mid = store.new_id()
        conn.execute(
            "INSERT INTO moments (id,creator_id,catalog_id,start,end,hit,kind,score,signals,text,hook,reason,"
            "speaker,story_key,status,section_path,section_offset,drop_reason,used_in,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, creator_id, catalog_id, float(r["start"]), float(r["end"]),
             float(r.get("hit") if r.get("hit") is not None else (float(r["start"]) + float(r["end"])) / 2),
             r.get("kind") or "quote", float(r.get("score") or 0), _dumps(r.get("signals") or {}),
             r.get("text") or "", r.get("hook") or "", r.get("reason") or "", r.get("speaker") or "unclear",
             r.get("story_key") or "", r.get("status") or "candidate", r.get("section_path") or "",
             r.get("section_offset"), r.get("drop_reason") or "", _dumps(r.get("used_in") or []), now))
        ids.append(mid)
    return ids


def add_moments(creator_id: str, catalog_id: str, rows: Iterable[Dict[str, Any]]) -> List[str]:
    with store.connect() as conn:
        return _insert_moments(conn, creator_id, catalog_id, rows)


def save_screened(creator_id: str, catalog_id: str, rows: Iterable[Dict[str, Any]]) -> List[str]:
    """A video's moments from screening, and its status → scored, in one transaction: a scan
    cut off half-way and run again never ends up with the same moments twice. Moments that
    were downloaded, used or found by a search are kept."""
    with store.connect() as conn:
        conn.execute("DELETE FROM moments WHERE creator_id=? AND catalog_id=? AND status='candidate'"
                     " AND COALESCE(section_path,'')='' AND COALESCE(signals,'') NOT LIKE '%\"search\"%'",
                     (creator_id, catalog_id))
        ids = _insert_moments(conn, creator_id, catalog_id, rows)
        conn.execute("UPDATE catalog SET status='scored', error='', updated_at=? WHERE id=?",
                     (time.time(), catalog_id))
    return ids


def update_moment(moment_id: str, **fields) -> None:
    for key in ("signals", "used_in"):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = _dumps(fields[key] if fields[key] is not None else ({} if key == "signals" else []))
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with store.connect() as conn:
        conn.execute(f"UPDATE moments SET {cols} WHERE id=?", (*fields.values(), moment_id))


def get_moment(moment_id: str) -> Optional[Dict[str, Any]]:
    with store.connect() as conn:
        return _moment(conn.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone())


def list_moments(creator_id: str, kind: Optional[str] = None, min_score: Optional[float] = None,
                 q: Optional[str] = None, catalog_id: Optional[str] = None,
                 status: Union[None, str, Sequence[str]] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """A creator's moments, best first. `status=None` leaves out dropped moments;
    `status="all"` includes them; otherwise one status or a list."""
    sql = "SELECT * FROM moments WHERE creator_id=?"
    args: List[Any] = [creator_id]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if min_score is not None:
        sql += " AND score>=?"
        args.append(float(min_score))
    if catalog_id:
        sql += " AND catalog_id=?"
        args.append(catalog_id)
    if status is None:
        sql += " AND status<>'dropped'"
    elif status != "all":
        sts = _statuses(status) or []
        sql += f" AND status IN ({','.join('?' * len(sts))})"
        args += sts
    if q and q.strip():
        for word in q.strip().lower().split()[:6]:
            sql += " AND (LOWER(text) LIKE ? OR LOWER(hook) LIKE ? OR LOWER(reason) LIKE ?)"
            args += [f"%{word}%"] * 3
    sql += " ORDER BY score DESC, created_at ASC LIMIT ?"
    args.append(int(limit))
    with store.connect() as conn:
        return [_moment(r) for r in conn.execute(sql, args).fetchall()]


def moment_counts(creator_id: str, great: float = 60.0) -> Dict[str, int]:
    with store.connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n, SUM(CASE WHEN score>=? THEN 1 ELSE 0 END) AS g,"
                            " SUM(CASE WHEN COALESCE(section_path,'')<>'' THEN 1 ELSE 0 END) AS s"
                            " FROM moments WHERE creator_id=? GROUP BY status", (great, creator_id)).fetchall()
    out = {s: 0 for s in MOMENT_STATUSES}
    great_n = sections = 0
    for r in rows:
        out[r["status"]] = int(r["n"])
        if r["status"] != "dropped":
            great_n += int(r["g"] or 0)
            sections += int(r["s"] or 0)
    out["kept"] = sum(v for k, v in out.items() if k in MOMENT_STATUSES and k != "dropped")
    out["great"] = great_n
    out["sections"] = sections
    return out


# --- scans -------------------------------------------------------------------------------------

def _scan(row: Any) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    d["counters"] = _loads(d.get("counters"), {})
    return d


def create_scan(creator_id: str) -> str:
    sid = store.new_id()
    with store.connect() as conn:
        conn.execute("INSERT INTO scan_jobs (id,creator_id,stage,status,progress,counters,message,wait_until,"
                     "started_at,finished_at,error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, creator_id, "list", "running", 0.0, "{}", "", None, time.time(), None, ""))
    return sid


def update_scan(scan_id: str, **fields) -> None:
    if "counters" in fields and not isinstance(fields["counters"], str):
        fields["counters"] = _dumps(fields["counters"] or {})
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with store.connect() as conn:
        conn.execute(f"UPDATE scan_jobs SET {cols} WHERE id=?", (*fields.values(), scan_id))


def get_scan(scan_id: str) -> Optional[Dict[str, Any]]:
    with store.connect() as conn:
        return _scan(conn.execute("SELECT * FROM scan_jobs WHERE id=?", (scan_id,)).fetchone())


def latest_scan(creator_id: str) -> Optional[Dict[str, Any]]:
    with store.connect() as conn:
        return _scan(conn.execute("SELECT * FROM scan_jobs WHERE creator_id=? ORDER BY started_at DESC, rowid DESC"
                                  " LIMIT 1", (creator_id,)).fetchone())


def scans_with_status(statuses: Sequence[str]) -> List[Dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(f"SELECT * FROM scan_jobs WHERE status IN ({','.join('?' * len(statuses))})",
                            list(statuses)).fetchall()
    return [_scan(r) for r in rows]


# --- the cache: work already paid for -----------------------------------------------------------

def cache_get(key: str) -> Any:
    with store.connect() as conn:
        row = conn.execute("SELECT payload FROM scan_cache WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def cache_put(key: str, payload: Any) -> None:
    with store.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO scan_cache (key,payload,created_at) VALUES (?,?,?)",
                     (key, _dumps(payload), time.time()))
