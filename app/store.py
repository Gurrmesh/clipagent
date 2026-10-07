"""Tiny SQLite layer: jobs, clips, presets, cached transcripts and campaigns."""
import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    source      TEXT,
    source_path TEXT,
    duration    REAL,
    status      TEXT,
    stage       TEXT,
    progress    INTEGER DEFAULT 0,
    error       TEXT,
    transcript  TEXT,
    settings    TEXT,
    created_at  REAL
);
CREATE TABLE IF NOT EXISTS transcripts (
    source_hash TEXT PRIMARY KEY,
    payload     TEXT,
    duration    REAL,
    created_at  REAL
);
CREATE TABLE IF NOT EXISTS presets (
    name       TEXT PRIMARY KEY,
    settings   TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS sounds (
    id         TEXT PRIMARY KEY,
    name       TEXT,
    file       TEXT,
    duration   REAL,
    analysis   TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS edits (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    status      TEXT,
    stage       TEXT,
    progress    INTEGER DEFAULT 0,
    settings    TEXT,
    plan        TEXT,
    file        TEXT,
    thumb       TEXT,
    error       TEXT,
    caption     TEXT,
    hashtags    TEXT,
    campaign_id TEXT,
    created_at  REAL,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS requests (
    id         TEXT PRIMARY KEY,
    job_id     TEXT,
    text       TEXT,
    reply      TEXT,
    status     TEXT,
    source     TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS campaigns (
    id         TEXT PRIMARY KEY,
    name       TEXT,
    mode       TEXT,
    brief      TEXT,
    rulebook   TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS clips (
    id         TEXT PRIMARY KEY,
    job_id     TEXT,
    rank       INTEGER,
    start      REAL,
    end        REAL,
    score      INTEGER,
    title      TEXT,
    hook       TEXT,
    reason     TEXT,
    tags       TEXT,
    words      TEXT,
    edits      TEXT,
    file       TEXT,
    thumb      TEXT,
    status     TEXT,
    created_at REAL,
    caption    TEXT,
    hashtags   TEXT,
    verdict    TEXT,
    saved      REAL,
    framing    TEXT
);
"""

# Columns added after the first release. SQLite has no "add column if missing",
# so an existing database is brought up to date here rather than rebuilt.
MIGRATIONS = {
    "clips": {
        "caption": "TEXT", "hashtags": "TEXT", "verdict": "TEXT",
        "saved": "REAL", "framing": "TEXT",
        # structure: what kind of moment, which parts of the source it uses,
        # which version won the judge, and the persistent context headline
        "clip_type": "TEXT", "parts": "TEXT", "variant": "TEXT", "judge": "TEXT",
        "headline": "TEXT", "alt_of": "TEXT",
        # campaign mode: the gate's verdict, the post kit, and (clip-bank clips)
        # which uploaded file this clip was made from
        "compliance": "TEXT", "post": "TEXT", "source_path": "TEXT",
        # the clip doctor's report: what it checked, fixed, and left to look at
        "doctor": "TEXT",
        # why the last re-render from the editor failed ("" when it didn't)
        "render_error": "TEXT",
        # the version before the last change, so it can be undone (instruct.snapshot)
        "undo": "TEXT",
    },
    "jobs": {"source_hash": "TEXT", "framing": "TEXT", "batch_id": "TEXT", "campaign_id": "TEXT",
             # downloads: what the site said about the video (upload date, channel, … and for a long
             # stream the parts that were downloaded), why a queued link is waiting ("youtube" while
             # YouTube downloads are paused, "restart" after ClipAgent restarted), yt-dlp's own error
             "source_meta": "TEXT", "paused": "TEXT", "error_raw": "TEXT"},
    # the edit maker: the campaign gate's verdict, and the version before the last change (for Undo)
    "edits": {"compliance": "TEXT", "undo": "TEXT"},
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, kind in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# --- jobs -----------------------------------------------------------------

def create_job(title: str, source: str, settings: Dict[str, Any]) -> str:
    job_id = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id,title,source,status,stage,progress,settings,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (job_id, title, source, "queued", "Queued", 0, json.dumps(settings), time.time()),
        )
    return job_id


def update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(limit: int = 30) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,title,status,stage,progress,duration,created_at FROM jobs"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def waiting_jobs() -> List[Dict[str, Any]]:
    """Queued links that are waiting (YouTube paused, or ClipAgent restarted), oldest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,title,source,status,stage,paused,created_at FROM jobs"
            " WHERE status='queued' AND COALESCE(paused,'')!='' ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


def source_meta(job: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A job's source facts (jobs.source_meta), {} when there are none."""
    try:
        meta = json.loads((job or {}).get("source_meta") or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


# --- clips ----------------------------------------------------------------

def create_clip(job_id: str, clip: Dict[str, Any]) -> str:
    clip_id = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO clips (id,job_id,rank,start,end,score,title,hook,reason,tags,words,"
            "edits,status,created_at,caption,hashtags,verdict,saved,framing,"
            "clip_type,parts,variant,judge,headline,alt_of,compliance,post,source_path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                clip_id, job_id, clip.get("rank", 0), clip["start"], clip["end"],
                clip.get("score", 0), clip.get("title", ""), clip.get("hook", ""),
                clip.get("reason", ""), json.dumps(clip.get("tags", [])),
                json.dumps(clip.get("words", [])), json.dumps(clip.get("edits", {})),
                "pending", time.time(), clip.get("caption", ""),
                json.dumps(clip.get("hashtags", [])), clip.get("verdict", ""),
                clip.get("saved", 0.0), json.dumps(clip.get("framing", {})),
                clip.get("type", ""), json.dumps(clip.get("parts", [])),
                clip.get("variant", ""), json.dumps(clip.get("judge", {})),
                clip.get("headline", ""), clip.get("alt_of", ""),
                json.dumps(clip["compliance"]) if clip.get("compliance") else "",
                json.dumps(clip["post"]) if clip.get("post") else "",
                clip.get("source_path", ""),
            ),
        )
    return clip_id


def update_clip(clip_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE clips SET {cols} WHERE id=?", (*fields.values(), clip_id))


def get_clip(clip_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    return dict(row) if row else None


def list_clips(job_id: str) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM clips WHERE job_id=?"
            " ORDER BY rank ASC, COALESCE(alt_of, '') != '' ASC, created_at ASC", (job_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- transcript cache -----------------------------------------------------

def cached_transcript(source_hash: str) -> Optional[Dict[str, Any]]:
    """A transcript we already paid for, if this exact source came through before."""
    if not source_hash:
        return None
    with connect() as conn:
        row = conn.execute("SELECT payload FROM transcripts WHERE source_hash=?",
                           (source_hash,)).fetchone()
    return json.loads(row["payload"]) if row else None


def cache_transcript(source_hash: str, payload: Dict[str, Any], duration: float) -> None:
    if not source_hash:
        return
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO transcripts (source_hash,payload,duration,created_at)"
                     " VALUES (?,?,?,?)",
                     (source_hash, json.dumps(payload), duration, time.time()))


# --- presets --------------------------------------------------------------

def save_preset(name: str, settings: Dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO presets (name,settings,created_at) VALUES (?,?,?)",
                     (name.strip()[:60], json.dumps(settings), time.time()))


def list_presets() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT name,settings FROM presets ORDER BY name").fetchall()
    return [{"name": r["name"], "settings": json.loads(r["settings"])} for r in rows]


def delete_preset(name: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM presets WHERE name=?", (name,))


# --- campaigns ------------------------------------------------------------

def save_campaign(name: str, mode: str, brief: str, rulebook: Dict[str, Any],
                  campaign_id: Optional[str] = None) -> str:
    now = time.time()
    with connect() as conn:
        if campaign_id and conn.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
            conn.execute("UPDATE campaigns SET name=?, mode=?, brief=?, rulebook=?, updated_at=? WHERE id=?",
                         (name, mode, brief, json.dumps(rulebook), now, campaign_id))
            return campaign_id
        campaign_id = new_id()
        conn.execute("INSERT INTO campaigns (id,name,mode,brief,rulebook,created_at,updated_at)"
                     " VALUES (?,?,?,?,?,?,?)",
                     (campaign_id, name, mode, brief, json.dumps(rulebook), now, now))
    return campaign_id


def get_campaign(campaign_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["rulebook"] = json.loads(out.get("rulebook") or "{}")
    return out


def list_campaigns() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT id,name,mode,rulebook,updated_at FROM campaigns"
                            " ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        rb = json.loads(r["rulebook"] or "{}")
        out.append({"id": r["id"], "name": r["name"], "mode": r["mode"], "updated_at": r["updated_at"],
                    "brand": rb.get("brand", ""), "platform": rb.get("platform", ""),
                    "platforms": rb.get("platforms", []), "pay": rb.get("pay", {})})
    return out


def delete_campaign(campaign_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))


def jobs_in_batch(batch_id: str) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,title,status,stage,progress FROM jobs WHERE batch_id=? ORDER BY created_at",
            (batch_id,)).fetchall()
    return [dict(r) for r in rows]


# --- typed change requests ("tell ClipAgent what to change") ---------------

def create_request(job_id: str, text: str, source: str = "web") -> str:
    req_id = new_id()
    with connect() as conn:
        conn.execute("INSERT INTO requests (id,job_id,text,reply,status,source,created_at) VALUES (?,?,?,?,?,?,?)",
                     (req_id, job_id, text, "{}", "reading", source, time.time()))
    return req_id


def update_request(req_id: str, **fields) -> None:
    if "reply" in fields and not isinstance(fields["reply"], str):
        fields["reply"] = json.dumps(fields["reply"])
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE requests SET {cols} WHERE id=?", (*fields.values(), req_id))


def get_request(req_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["reply"] = json.loads(d.get("reply") or "{}")
    return d


def list_requests(job_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM requests WHERE job_id=? ORDER BY created_at DESC LIMIT ?",
                            (job_id, limit)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["reply"] = json.loads(d.get("reply") or "{}")
        out.append(d)
    return out


# --- sounds and edits (the edit maker) -------------------------------------

def _row(row: Any, json_fields: tuple = ()) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    for f in json_fields:
        try:
            d[f] = json.loads(d.get(f) or "{}")
        except (TypeError, ValueError):
            d[f] = {}
    return d


def add_sound(name: str, file: str, duration: float, analysis: Dict[str, Any]) -> str:
    sid = new_id()
    with connect() as conn:
        conn.execute("INSERT INTO sounds (id,name,file,duration,analysis,created_at) VALUES (?,?,?,?,?,?)",
                     (sid, name, file, duration, json.dumps(analysis), time.time()))
    return sid


def get_sound(sound_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM sounds WHERE id=?", (sound_id,)).fetchone(), ("analysis",))


def list_sounds() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM sounds ORDER BY created_at DESC").fetchall()
    return [_row(r, ("analysis",)) for r in rows]


def delete_sound(sound_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sounds WHERE id=?", (sound_id,))


def create_edit(title: str, settings: Dict[str, Any], campaign_id: str = "") -> str:
    eid = new_id()
    now = time.time()
    with connect() as conn:
        conn.execute("INSERT INTO edits (id,title,status,stage,progress,settings,plan,campaign_id,created_at,updated_at)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (eid, title, "queued", "Waiting", 0, json.dumps(settings), "{}", campaign_id, now, now))
    return eid


def update_edit(edit_id: str, **fields) -> None:
    for f in ("settings", "plan", "compliance", "undo"):
        if f in fields and not isinstance(fields[f], str):
            fields[f] = json.dumps(fields[f])
    if "hashtags" in fields and not isinstance(fields["hashtags"], str):
        fields["hashtags"] = json.dumps(fields["hashtags"])
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE edits SET {cols} WHERE id=?", (*fields.values(), edit_id))


def get_edit(edit_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        d = _row(conn.execute("SELECT * FROM edits WHERE id=?", (edit_id,)).fetchone(),
                 ("settings", "plan", "compliance", "undo"))
    if d:
        try:
            d["hashtags"] = json.loads(d.get("hashtags") or "[]")
        except (TypeError, ValueError):
            d["hashtags"] = []
    return d


def list_edits(limit: int = 60) -> List[Dict[str, Any]]:
    with connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM edits ORDER BY created_at DESC LIMIT ?", (limit,))]
    return [e for e in (get_edit(i) for i in ids) if e]


def delete_edit(edit_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM edits WHERE id=?", (edit_id,))
