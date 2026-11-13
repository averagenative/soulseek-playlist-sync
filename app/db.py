"""SQLite storage. Each call opens its own connection; WAL lets the worker and web share it."""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

# Tables with a profile in their primary key: kept apart so an older table can be rebuilt into them.
FAVORITE_PAIRS = """CREATE TABLE IF NOT EXISTS favorite_pairs (
    profile    INTEGER NOT NULL DEFAULT 1,
    spotify_id TEXT NOT NULL,             -- Spotify track ID
    apple_id   TEXT NOT NULL,             -- Apple Music catalog ID
    added_to   TEXT,                      -- 'apple' or 'spotify' when the app made the favorite, NULL when both had it
    at         REAL,
    PRIMARY KEY (profile, spotify_id, apple_id)
)"""
HIT_FAVORITES = """CREATE TABLE IF NOT EXISTS hit_favorites (
    profile  INTEGER NOT NULL DEFAULT 1,
    song_key TEXT NOT NULL,
    PRIMARY KEY (profile, song_key)
)"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

-- The people who use the app. Each has their own playlists and Apple Music and Spotify connections.
CREATE TABLE IF NOT EXISTS profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password        TEXT,                  -- scrypt hash, or NULL for a profile anyone can open
    admin           INTEGER NOT NULL DEFAULT 0,
    folder          TEXT NOT NULL DEFAULT '',   -- under each service folder, for this profile's new playlists
    session_version INTEGER NOT NULL DEFAULT 1, -- raised to sign every browser out of the profile
    created_at      REAL
);

CREATE TABLE IF NOT EXISTS playlists (
    id            TEXT PRIMARY KEY,          -- source:remote id, e.g. apple:p.AbC123, local:9f2c41d0
    source        TEXT NOT NULL,
    remote_id     TEXT NOT NULL,
    name          TEXT NOT NULL,
    folder        TEXT NOT NULL,             -- relative to MUSIC_ROOT
    numbering     INTEGER NOT NULL DEFAULT 1,
    library_mode  TEXT NOT NULL DEFAULT 'copy',   -- copy | reference
    fetched_at    REAL,
    synced_at     REAL,
    busy          TEXT,                      -- what a running job is doing, or NULL
    error         TEXT,
    own_name      INTEGER NOT NULL DEFAULT 0 -- you renamed it here, so the service's name doesn't replace it
);

CREATE TABLE IF NOT EXISTS songs (
    key            TEXT PRIMARY KEY,         -- apple:<catalog id>, apple-lib:<library id>, spotify:<id>, local:<hash>
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

-- Songs this app added to an Apple Music copy. Apple lists a song that was already in your library
-- in another version under that version's ID, so the playlist alone can't say what was added.
CREATE TABLE IF NOT EXISTS mirror_songs (
    playlist_id TEXT NOT NULL,
    target      TEXT NOT NULL,
    song_id     TEXT NOT NULL,        -- the catalog ID the app added
    PRIMARY KEY (playlist_id, target, song_id)
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

-- Favorites sync: a Spotify Liked Song and its Apple Music Favorite Song, per profile. Once a pair is
-- here, the app never adds either one again, so a song you remove on one service stays removed.
""" + FAVORITE_PAIRS + """;

-- Hits playlists: what the app has looked up about your songs, kept so later builds cost nothing.
-- MusicBrainz IDs found for songs whose files don't carry them ('' = looked, nothing matched).
CREATE TABLE IF NOT EXISTS hit_ids (
    song_key   TEXT PRIMARY KEY,
    recording  TEXT NOT NULL DEFAULT '',
    artist     TEXT NOT NULL DEFAULT '',
    how        TEXT,                  -- isrc | search
    checked_at REAL NOT NULL
);
-- ListenBrainz listener counts, per MusicBrainz recording.
CREATE TABLE IF NOT EXISTS hit_recordings (
    mbid       TEXT PRIMARY KEY,
    listeners  INTEGER,
    listens    INTEGER,
    fetched_at REAL NOT NULL
);
-- Lists fetched per artist, one row per song title (norm_title):
--   listenbrainz  value = listeners, the most for any recording with that title
--   single        value = the year it came out as a single (MusicBrainz)
--   genius        value = page views
CREATE TABLE IF NOT EXISTS hit_artist_songs (
    source TEXT NOT NULL,
    artist TEXT NOT NULL,             -- MusicBrainz artist ID; normalized name for genius
    title  TEXT NOT NULL,
    value  INTEGER,
    PRIMARY KEY (source, artist, title)
);
CREATE TABLE IF NOT EXISTS hit_artists (
    source     TEXT NOT NULL,
    artist     TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    found      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source, artist)
);
-- Billboard Hot 100, one row per song and performer.
CREATE TABLE IF NOT EXISTS hit_charts (
    title      TEXT NOT NULL,         -- norm_title
    performer  TEXT NOT NULL,
    peak       INTEGER NOT NULL,
    weeks      INTEGER NOT NULL,
    first_week TEXT,
    PRIMARY KEY (title, performer)
);
-- Song keys of each profile's Apple Music Favorite Songs and Spotify Liked Songs.
""" + HIT_FAVORITES + """;
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
        lib_cols = {r["name"] for r in c.execute("PRAGMA table_info(library)")}
        for col, kind in (("song_key", "TEXT"), ("art_key", "TEXT"), ("album_artist", "TEXT"),
                          ("track_number", "INTEGER"), ("disc_number", "INTEGER"), ("year", "TEXT"),
                          ("genre", "TEXT"), ("search", "TEXT"), ("mb_recording", "TEXT"), ("mb_artist", "TEXT"),
                          ("mb_release_group", "TEXT"), ("release_type", "TEXT")):
            if col not in lib_cols:
                c.execute(f"ALTER TABLE library ADD COLUMN {col} {kind}")
        if "song_key" not in lib_cols or "mb_recording" not in lib_cols:
            c.execute("UPDATE library SET mtime = NULL")     # read every file's tags again for the new columns
        c.execute("CREATE INDEX IF NOT EXISTS library_song_key ON library(song_key)")
        c.execute("CREATE INDEX IF NOT EXISTS library_art_key ON library(art_key)")
        pl_cols = {r["name"] for r in c.execute("PRAGMA table_info(playlists)")}
        if "rule" not in pl_cols:
            c.execute("ALTER TABLE playlists ADD COLUMN rule TEXT")      # JSON filter of an auto-updating playlist
        if "own_name" not in pl_cols:
            c.execute("ALTER TABLE playlists ADD COLUMN own_name INTEGER NOT NULL DEFAULT 0")
        if "note" not in {r["name"] for r in c.execute("PRAGMA table_info(mirrors)")}:
            c.execute("ALTER TABLE mirrors ADD COLUMN note TEXT")
        # Profiles: everything that was here before belongs to the first one.
        if "owner" not in pl_cols:
            c.execute("ALTER TABLE playlists ADD COLUMN owner INTEGER NOT NULL DEFAULT 1")
        _add_profile(c, "favorite_pairs", FAVORITE_PAIRS, "spotify_id, apple_id, added_to, at")
        _add_profile(c, "hit_favorites", HIT_FAVORITES, "song_key")
        c.execute("CREATE INDEX IF NOT EXISTS favorite_pairs_apple ON favorite_pairs(profile, apple_id)")
        c.execute("INSERT INTO profiles (id, name, admin, folder, created_at) SELECT 1, 'Me', 1, '', ? "
                  "WHERE NOT EXISTS (SELECT 1 FROM profiles)", (time.time(),))
        # A YouTube download runs inside the app, so one in flight when it stopped has to start over.
        c.execute("UPDATE downloads SET state = 'pending' WHERE source = 'youtube' AND state IN ('searching', 'downloading')")
        # A job that was running when the app stopped is not running any more.
        c.execute("UPDATE playlists SET busy = NULL")


def _add_profile(c, table: str, create: str, cols: str) -> None:
    """Rebuild a table from before profiles with a profile column in its primary key, in one transaction,
    giving every old row to the first profile."""
    if "profile" in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
        return
    c.executescript(f"BEGIN; ALTER TABLE {table} RENAME TO {table}_old; {create}; "
                    f"INSERT INTO {table} (profile, {cols}) SELECT 1, {cols} FROM {table}_old; "
                    f"DROP TABLE {table}_old; COMMIT;")


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
