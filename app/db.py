"""SQLite storage. Each call opens its own connection; WAL lets the worker and web share it."""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS playlists (
    id            TEXT PRIMARY KEY,          -- source:remote id, e.g. apple:p.PkxVVPVt2R91O3
    source        TEXT NOT NULL,
    remote_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    folder        TEXT NOT NULL,             -- relative to MUSIC_ROOT
    numbering     INTEGER NOT NULL DEFAULT 1,
    library_mode  TEXT NOT NULL DEFAULT 'copy',   -- copy | reference
    fetched_at    REAL,
    synced_at     REAL,
    busy          TEXT,                      -- what a running job is doing, or NULL
    error         TEXT
);

CREATE TABLE IF NOT EXISTS songs (
    key            TEXT PRIMARY KEY,         -- apple:<catalog id> or apple-lib:<library id>
    isrc           TEXT,
    title          TEXT,
    artist         TEXT,
    album          TEXT,
    album_artist   TEXT,
    track_number   INTEGER,
    disc_number    INTEGER,
    duration_ms    INTEGER,
    content_rating TEXT,
    release_date   TEXT,
    genre          TEXT,
    artwork_url    TEXT,
    url            TEXT
);

-- The playlist as the service has it now, one row per position.
CREATE TABLE IF NOT EXISTS entries (
    playlist_id TEXT NOT NULL,
    position    INTEGER NOT NULL,
    song_key    TEXT NOT NULL,
    PRIMARY KEY (playlist_id, position)
);

-- One row per song per playlist: what we have locally for it.
CREATE TABLE IF NOT EXISTS items (
    playlist_id  TEXT NOT NULL,
    song_key     TEXT NOT NULL,
    file         TEXT,          -- file name inside the playlist folder
    origin       TEXT,          -- download | library | reference
    owned        INTEGER NOT NULL DEFAULT 0,   -- this app created the file
    library_path TEXT,          -- matched library file (absolute)
    skip         INTEGER NOT NULL DEFAULT 0,
    want         INTEGER NOT NULL DEFAULT 0,   -- download requested
    updated_at   REAL,
    PRIMARY KEY (playlist_id, song_key)
);

-- One row per song: the Soulseek search and download.
CREATE TABLE IF NOT EXISTS downloads (
    song_key     TEXT PRIMARY KEY,
    state        TEXT NOT NULL,  -- wanted | searching | found | not_found | queued | downloading | done | failed
    detail       TEXT,
    query_index  INTEGER NOT NULL DEFAULT 0,
    search_id    TEXT,
    candidates   TEXT,           -- JSON list, best first
    attempt      INTEGER NOT NULL DEFAULT 0,
    username     TEXT,
    filename     TEXT,
    size         INTEGER,
    percent      REAL,
    path         TEXT,           -- the finished, tagged file
    queued_at    REAL,
    created_at   REAL,
    updated_at   REAL
);

CREATE TABLE IF NOT EXISTS library (
    path       TEXT PRIMARY KEY,
    size       INTEGER,
    mtime      REAL,
    isrc       TEXT,
    artist     TEXT,
    title      TEXT,
    album      TEXT,
    duration   REAL,
    norm_title TEXT
);
CREATE INDEX IF NOT EXISTS library_isrc ON library(isrc);

-- A copy of a playlist on the other service.
CREATE TABLE IF NOT EXISTS mirrors (
    playlist_id TEXT NOT NULL,
    target      TEXT NOT NULL,        -- spotify | apple
    remote_id   TEXT,
    matched     INTEGER,
    total       INTEGER,
    missing     TEXT,                 -- JSON list of "Artist - Title" not found on the target
    updated_at  REAL,
    error       TEXT,
    PRIMARY KEY (playlist_id, target)
);

-- A song's match on another service ('' = looked, not there).
CREATE TABLE IF NOT EXISTS song_links (
    song_key   TEXT NOT NULL,
    target     TEXT NOT NULL,
    remote_id  TEXT NOT NULL,
    checked_at REAL,
    PRIMARY KEY (song_key, target)
);
CREATE INDEX IF NOT EXISTS library_norm_title ON library(norm_title);
"""

_path: Path | None = None


def init(path: Path) -> None:
    global _path
    _path = path
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)
        c.execute("PRAGMA journal_mode=WAL")
        song_cols = {r["name"] for r in c.execute("PRAGMA table_info(songs)")}
        for col in ("thumb_url", "preview_url"):
            if col not in song_cols:
                c.execute(f"ALTER TABLE songs ADD COLUMN {col} TEXT")
        have = {r["name"] for r in c.execute("PRAGMA table_info(downloads)")}
        for col, kind in (("speed", "REAL"), ("bytes_done", "INTEGER"), ("place_in_queue", "INTEGER"),
                          ("source", "TEXT NOT NULL DEFAULT 'soulseek'")):
            if col not in have:
                c.execute(f"ALTER TABLE downloads ADD COLUMN {col} {kind}")
        # A YouTube download runs inside the app, so one in flight when it stopped has to start over.
        c.execute("UPDATE downloads SET state = 'pending' WHERE source = 'youtube' AND state IN ('searching', 'downloading')")
        # A job that was running when the app stopped is not running any more.
        c.execute("UPDATE playlists SET busy = NULL")


@contextmanager
def connect():
    c = sqlite3.connect(_path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout = 30000")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def get_kv(key: str, default: str | None = None) -> str | None:
    with connect() as c:
        row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_kv(key: str, value: str | None) -> None:
    with connect() as c:
        c.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def now() -> float:
    return time.time()
