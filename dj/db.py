"""SQLite store for tracks, plays, feedback and learned preferences.

Everything the DJ knows about you lives here, in one file you can back up.
"""
import json
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone

import numpy as np

from .config import DATA

DB_PATH = DATA / "mind-dj.sqlite3"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tracks (
    id            INTEGER PRIMARY KEY,
    video_id      TEXT UNIQUE NOT NULL,
    ident         TEXT NOT NULL,
    title         TEXT NOT NULL,
    artist        TEXT NOT NULL DEFAULT "",
    album         TEXT DEFAULT "",
    duration_sec  INTEGER DEFAULT 0,
    thumbnail     TEXT DEFAULT "",
    year          INTEGER,
    tags          TEXT DEFAULT "[]",
    source        TEXT DEFAULT "ytm",
    first_seen    TEXT NOT NULL,
    last_seen     TEXT,
    banned        INTEGER DEFAULT 0,
    unplayable    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tracks_ident  ON tracks(ident);
CREATE INDEX IF NOT EXISTS ix_tracks_artist ON tracks(artist);

CREATE TABLE IF NOT EXISTS plays (
    id           INTEGER PRIMARY KEY,
    track_id     INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    listened_sec REAL DEFAULT 0,
    duration_sec REAL DEFAULT 0,
    completion   REAL DEFAULT 0,
    outcome      TEXT DEFAULT "unknown",
    sink         TEXT DEFAULT "",
    session_id   TEXT DEFAULT "",
    ctx          TEXT DEFAULT "{}",
    bucket       TEXT DEFAULT "",
    requested    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_plays_track  ON plays(track_id);
CREATE INDEX IF NOT EXISTS ix_plays_bucket ON plays(bucket);
CREATE INDEX IF NOT EXISTS ix_plays_time   ON plays(started_at);

CREATE TABLE IF NOT EXISTS feedback (
    id       INTEGER PRIMARY KEY,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    ts       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    weight   REAL DEFAULT 1.0,
    ctx      TEXT DEFAULT "{}",
    bucket   TEXT DEFAULT ""
);
CREATE INDEX IF NOT EXISTS ix_feedback_track ON feedback(track_id);

CREATE TABLE IF NOT EXISTS embeddings (
    track_id  INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vec       BLOB NOT NULL,
    text_hash TEXT NOT NULL,
    ts        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vibes (
    id       INTEGER PRIMARY KEY,
    centroid BLOB NOT NULL,
    dim      INTEGER NOT NULL,
    label    TEXT DEFAULT "",
    size     INTEGER DEFAULT 0,
    updated  TEXT
);
CREATE TABLE IF NOT EXISTS track_vibe (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    vibe_id  INTEGER NOT NULL,
    sim      REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_track_vibe ON track_vibe(vibe_id);

CREATE TABLE IF NOT EXISTS ctx_pref (
    vibe_id INTEGER NOT NULL,
    bucket  TEXT NOT NULL,
    pos     REAL DEFAULT 0,
    neg     REAL DEFAULT 0,
    updated TEXT,
    PRIMARY KEY (vibe_id, bucket)
);

CREATE TABLE IF NOT EXISTS suggestions (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    bucket   TEXT DEFAULT "",
    score    REAL DEFAULT 0,
    parts    TEXT DEFAULT "{}",
    reason   TEXT DEFAULT "",
    accepted INTEGER
);

CREATE TABLE IF NOT EXISTS features (
    track_id   INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    energy     REAL, brightness REAL, dynamics REAL, punch REAL,
    tempo      REAL, tempo_confidence REAL, loudness REAL,
    ts         TEXT
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

_ARTICLE = re.compile(r"^(the|a|an)\s+", re.I)
_PAREN = re.compile(r"[\(\[].*?[\)\]]")
_NOISE = re.compile(
    r"\b(official|video|audio|lyrics?|hd|4k|remaster(ed)?|mv|music\s*video|"
    r"visualizer|live|feat\.?|ft\.?|prod\.?)\b",
    re.I,
)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_DASH = re.compile(r"\s+[-–—]\s+")
# Bulgarian uploads routinely carry the same name twice, Latin then Cyrillic,
# split by a spaced pipe or slash: "Kasay / Касай | Official Video 2025".
# Spaced separators only, so "AC/DC" survives intact.
_SEP = re.compile(r"\s+[|/]\s+")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def now():
    return datetime.now(timezone.utc).isoformat()


def normalise(text):
    """Fold a title or artist to a comparable form.

    YouTube and YouTube Music hand out different video ids for the same song,
    so identity has to come from the text rather than the id. Also folds
    Cyrillic case and strips the usual "Official Video 2025" cruft.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    t = _SEP.split(t, maxsplit=1)[0]
    t = _PAREN.sub(" ", t)
    t = _NOISE.sub(" ", t)
    t = _YEAR.sub(" ", t)
    t = _PUNCT.sub(" ", t)
    t = _ARTICLE.sub("", t)
    return _WS.sub(" ", t).strip()


def ident_for(artist, title):
    """Stable identity for a song, independent of which id it arrived under."""
    raw_title = str(title or "")
    a, t = normalise(artist), normalise(raw_title)
    if not a:
        # Bare uploads usually carry "Artist - Title" in the title alone.
        parts = _DASH.split(raw_title, maxsplit=1)
        if len(parts) == 2:
            a, t = normalise(parts[0]), normalise(parts[1])
    return (a + "|" + t).strip("|")


_local = threading.local()


def connect():
    """A connection belonging to the calling thread.

    One sqlite3 connection must not be used from several threads at once, even
    with check_same_thread=False: concurrent execute() calls interleave and
    corrupt cursor state, which shows up as fetchone() returning None from a
    COUNT that cannot possibly be empty. The playback loop, the Home Assistant
    bridge, the prefetcher and every web request are all separate threads, so
    each gets its own connection. WAL mode is what makes that cheap.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        _local.conn = conn
    return conn


def init():
    conn = connect()
    with conn:
        conn.executescript(SCHEMA)
    return conn


def pack(vec):
    return np.asarray(vec, dtype=np.float32).tobytes()


def unpack(blob):
    return np.frombuffer(blob, dtype=np.float32)


def upsert_track(conn, video_id, title, artist="", album="", duration_sec=0,
                 thumbnail="", year=None, tags=None, source="ytm"):
    """Insert or refresh a track. Returns its row id."""
    tags_json = json.dumps(tags or [], ensure_ascii=False)
    ident = ident_for(artist, title)
    ts = now()
    with conn:
        row = conn.execute("SELECT id FROM tracks WHERE video_id = ?", (video_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE tracks SET title=?, artist=?, album=?, duration_sec=?, thumbnail=?,"
                " year=COALESCE(?, year), tags=?, ident=?, last_seen=? WHERE id=?",
                (title, artist, album, duration_sec, thumbnail, year, tags_json,
                 ident, ts, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO tracks (video_id, ident, title, artist, album, duration_sec,"
            " thumbnail, year, tags, source, first_seen, last_seen)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (video_id, ident, title, artist, album, duration_sec, thumbnail,
             year, tags_json, source, ts, ts),
        )
        return cur.lastrowid


def find_by_ident(conn, artist, title):
    ident = ident_for(artist, title)
    if not ident:
        return None
    return conn.execute("SELECT * FROM tracks WHERE ident = ? LIMIT 1", (ident,)).fetchone()


def get_track(conn, track_id):
    return conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()


def get_track_by_video(conn, video_id):
    return conn.execute("SELECT * FROM tracks WHERE video_id = ?", (video_id,)).fetchone()


def track_dict(row):
    if row is None:
        return None
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except Exception:
        d["tags"] = []
    return d


def log_play(conn, track_id, started_at, ended_at, listened_sec, duration_sec,
             outcome, sink, session_id, ctx, bucket, requested=0):
    completion = 0.0
    if duration_sec and duration_sec > 0:
        completion = max(0.0, min(1.0, listened_sec / duration_sec))
    with conn:
        cur = conn.execute(
            "INSERT INTO plays (track_id, started_at, ended_at, listened_sec, duration_sec,"
            " completion, outcome, sink, session_id, ctx, bucket, requested)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (track_id, started_at, ended_at, listened_sec, duration_sec, completion,
             outcome, sink, session_id, json.dumps(ctx, ensure_ascii=False), bucket,
             requested),
        )
        return cur.lastrowid


def add_feedback(conn, track_id, kind, ctx=None, bucket="", weight=1.0):
    with conn:
        conn.execute(
            "INSERT INTO feedback (track_id, ts, kind, weight, ctx, bucket)"
            " VALUES (?,?,?,?,?,?)",
            (track_id, now(), kind, weight,
             json.dumps(ctx or {}, ensure_ascii=False), bucket),
        )
        if kind == "ban":
            conn.execute("UPDATE tracks SET banned = 1 WHERE id = ?", (track_id,))


def kv_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except Exception:
        return row["v"]


def kv_set(conn, k, v):
    with conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v, ensure_ascii=False)),
        )


FEATURE_COLS = ("energy", "brightness", "dynamics", "punch",
                "tempo", "tempo_confidence", "loudness")


def save_features(conn, track_id, feat):
    """Store what a track sounds like."""
    if not feat:
        return False
    with conn:
        conn.execute(
            "INSERT INTO features (track_id, energy, brightness, dynamics, punch,"
            " tempo, tempo_confidence, loudness, ts) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(track_id) DO UPDATE SET"
            " energy=excluded.energy, brightness=excluded.brightness,"
            " dynamics=excluded.dynamics, punch=excluded.punch,"
            " tempo=excluded.tempo, tempo_confidence=excluded.tempo_confidence,"
            " loudness=excluded.loudness, ts=excluded.ts",
            (track_id, *(feat.get(c) for c in FEATURE_COLS), now()),
        )
    return True


def get_features(conn, track_id):
    row = conn.execute("SELECT * FROM features WHERE track_id = ?",
                       (track_id,)).fetchone()
    return dict(row) if row else None


def stats(conn):
    def q(sql):
        return conn.execute(sql).fetchone()[0]
    return {
        "tracks": q("SELECT COUNT(*) FROM tracks"),
        "plays": q("SELECT COUNT(*) FROM plays"),
        "heard": q("SELECT COUNT(DISTINCT track_id) FROM plays"),
        "feedback": q("SELECT COUNT(*) FROM feedback"),
        "embedded": q("SELECT COUNT(*) FROM embeddings"),
        "vibes": q("SELECT COUNT(*) FROM vibes"),
        "analysed": q("SELECT COUNT(*) FROM features"),
    }
