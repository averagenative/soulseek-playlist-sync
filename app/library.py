"""Index of the music already on disk: so we don't download what we have, and so you can browse it
and make playlists from it."""
import hashlib
import logging
import os
import threading
from collections import Counter
from pathlib import Path

import mutagen
from mutagen.easymp4 import EasyMP4Tags

from . import db
from .config import Config
from .match import artist_matches, norm, norm_title, primary_artist

log = logging.getLogger(__name__)
AUDIO = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wma", ".aac"}
_scan_lock = threading.Lock()


def _first(tags, *names):
    for n in names:
        v = tags.get(n) if tags else None
        if v:
            return str(v[0]) if isinstance(v, list) else str(v)
    return None


def _all(tags, *names) -> str | None:
    """Every value of the first tag that has any, joined: 'album; compilation'."""
    for n in names:
        v = tags.get(n) if tags else None
        if v:
            vals = v if isinstance(v, list) else [v]
            return "; ".join(str(x).strip().lower() for x in vals if str(x).strip()) or None
    return None


# Picard's MusicBrainz tags. MP4 files keep the release group in a free-form atom mutagen doesn't map.
try:
    EasyMP4Tags.RegisterFreeformKey("musicbrainz_releasegroupid", "MusicBrainz Release Group Id")
except Exception:       # already registered
    pass


def _num(v: str | None) -> int | None:
    """'3/12' -> 3."""
    try:
        return int(str(v).split("/")[0]) if v else None
    except ValueError:
        return None


def song_key(artist: str | None, title: str | None, path: str) -> str:
    """One key per song, whatever album or folder it's in, so a collection lists each song once."""
    a, t = norm(primary_artist(artist)), norm_title(title)
    basis = f"{a}|{t}" if a and t else f"path|{path}"
    return "local:" + hashlib.sha1(basis.encode()).hexdigest()[:16]


def art_key(path: str, album: str | None) -> str:
    """Files in one folder with the same album share a cover."""
    return hashlib.sha1(f"{os.path.dirname(path)}|{norm(album)}".encode()).hexdigest()[:16]


def read_tags(path: Path) -> dict:
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        f = None
    if f is None:
        return {}
    tags = f.tags or {}
    return {
        "title": _first(tags, "title"),
        "artist": _first(tags, "artist", "albumartist"),
        "album": _first(tags, "album"),
        "album_artist": _first(tags, "albumartist"),
        "track_number": _num(_first(tags, "tracknumber")),
        "disc_number": _num(_first(tags, "discnumber")),
        "year": (_first(tags, "originaldate", "date") or "")[:4] or None,
        "genre": _first(tags, "genre"),
        "isrc": (_first(tags, "isrc") or "").upper() or None,
        "duration": getattr(f.info, "length", None),
        "mb_recording": _first(tags, "musicbrainz_trackid"),
        "mb_artist": _first(tags, "musicbrainz_artistid", "musicbrainz_albumartistid"),
        "mb_release_group": _first(tags, "musicbrainz_releasegroupid"),
        "release_type": _all(tags, "musicbrainz_albumtype", "releasetype"),
    }


def scan(cfg: Config) -> dict:
    """Index new and changed files, drop vanished ones. Safe to call repeatedly."""
    if not _scan_lock.acquire(blocking=False):
        return {"skipped": "scan already running"}
    try:
        db.set_kv("library_scan", "running")
        backfill_keys()
        with db.connect() as c:
            known = {r["path"]: (r["size"], r["mtime"]) for r in c.execute("SELECT path, size, mtime FROM library")}
        seen, changed = set(), []
        # A folder that's missing or empty is more likely an unmounted disk than deleted music.
        offline = [r for r in cfg.library_dirs if not _has_entries(r)]
        for r in offline:
            log.warning("library folder %s is missing or empty; keeping what was indexed there", r)
        for root in cfg.library_dirs:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if os.path.splitext(name)[1].lower() not in AUDIO:
                        continue
                    p = os.path.join(dirpath, name)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    seen.add(p)
                    if known.get(p) != (st.st_size, st.st_mtime):
                        changed.append((p, st.st_size, st.st_mtime))
        rows = []
        for i, (p, size, mtime) in enumerate(changed, 1):
            rows.append({**read_tags(Path(p)), "path": p, "size": size, "mtime": mtime})
            if len(rows) >= 500:
                _write(rows)
                rows = []
                db.set_kv("library_scan", f"running: read {i:,} of {len(changed):,} changed files")
        _write(rows)
        gone = [p for p in known if p not in seen and not any(p.startswith(str(r) + os.sep) for r in offline)]
        with db.connect() as c:
            c.executemany("DELETE FROM library WHERE path = ?", [(p,) for p in gone])
        result = {"files": len(seen), "updated": len(changed), "removed": len(gone)}
        db.set_kv("library_scan", f"done: {len(seen)} files, {len(changed)} updated, {len(gone)} removed")
        db.set_kv("library_scanned_at", str(db.now()))
        log.info("library scan %s", result)
        return result
    except Exception as e:
        db.set_kv("library_scan", f"failed: {e}")
        raise
    finally:
        _scan_lock.release()


COLUMNS = ("path", "size", "mtime", "isrc", "artist", "title", "album", "duration", "norm_title", "song_key",
           "art_key", "album_artist", "track_number", "disc_number", "year", "genre", "search", "mb_recording",
           "mb_artist", "mb_release_group", "release_type")


def _derived(r: dict) -> dict:
    return {"norm_title": norm_title(r.get("title")),
            "song_key": song_key(r.get("artist"), r.get("title"), r["path"]),
            "art_key": art_key(r["path"], r.get("album")),
            "search": " ".join(norm(r.get(f)) for f in ("artist", "album_artist", "album", "title")
                               if r.get(f)) or norm(Path(r["path"]).stem)}


def _has_entries(folder: Path) -> bool:
    try:
        with os.scandir(folder) as it:
            return any(True for _ in it)
    except OSError:
        return False


def _write(rows: list[dict]):
    if not rows:
        return
    with db.connect() as c:
        c.executemany(
            f"INSERT INTO library ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))}) "
            f"ON CONFLICT(path) DO UPDATE SET {', '.join(f'{k}=excluded.{k}' for k in COLUMNS[1:])}",
            [tuple({**r, **_derived(r)}.get(k) for k in COLUMNS) for r in rows])


def backfill_keys() -> None:
    """Bring the derived columns up to date with the current matching rules (and fill them for rows
    indexed before they existed), without reading the files again."""
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT path, artist, album_artist, title, album, norm_title, song_key, art_key, search FROM library")]
        stale = []
        for r in rows:
            d = _derived(r)
            if any(r[k] != d[k] for k in d):
                stale.append((d["norm_title"], d["song_key"], d["art_key"], d["search"], r["path"]))
        c.executemany("UPDATE library SET norm_title = ?, song_key = ?, art_key = ?, search = ? WHERE path = ?", stale)


def add_file(path: Path, song: dict) -> None:
    """Index a file we just wrote, without waiting for the next scan."""
    st = path.stat()
    _write([{"path": str(path), "size": st.st_size, "mtime": st.st_mtime, "duration": (song.get("duration_ms") or 0) / 1000,
             "year": (song.get("release_date") or "")[:4] or None,
             **{k: song.get(k) for k in ("isrc", "artist", "title", "album", "album_artist", "track_number",
                                         "disc_number", "genre")}}])


def find(song: dict, tolerance: float, mp3_only: bool = True, exclude_dir: Path | None = None) -> str | None:
    """Best local file for a song: same ISRC, or same title and artist with a matching length."""
    want_len = (song.get("duration_ms") or 0) / 1000
    with db.connect() as c:
        rows = []
        if song.get("isrc"):
            rows = c.execute("SELECT * FROM library WHERE isrc = ?", (song["isrc"].upper(),)).fetchall()
        rows += c.execute("SELECT * FROM library WHERE norm_title = ?", (norm_title(song.get("title")),)).fetchall()

    def ok(r) -> bool:
        if mp3_only and not r["path"].lower().endswith(".mp3"):
            return False
        if exclude_dir and r["path"].startswith(str(exclude_dir) + os.sep):
            return False
        if want_len and r["duration"] and abs(r["duration"] - want_len) > tolerance:
            return False
        if song.get("isrc") and r["isrc"] == song["isrc"].upper():
            return True
        return artist_matches(song.get("artist"), r["artist"])

    for r in rows:
        if ok(r) and os.path.exists(r["path"]):
            return r["path"]
    return None


def stats() -> dict:
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM library").fetchone()[0]
    return {"files": n, "scan": db.get_kv("library_scan"), "scanned_at": db.get_kv("library_scanned_at")}


# ------------------------------------------------------------------ browsing

SORTS = {
    "artist": "(COALESCE(album_artist, artist) IS NULL), COALESCE(album_artist, artist) COLLATE NOCASE, "
              "year, album COLLATE NOCASE, disc_number, track_number, title COLLATE NOCASE, path",
    "title": "(title IS NULL), title COLLATE NOCASE, artist COLLATE NOCASE, path",
    "album": "(album IS NULL), album COLLATE NOCASE, disc_number, track_number, path",
    "added": "mtime DESC, path",
}
_folder_cache: dict = {}


def folder_label(cfg: Config, path: str) -> str | None:
    """The folder a file is filed under: the top folder of the music root, or for the app's own
    playlist folders, the playlist's folder (AppleMusic/Road Trip)."""
    prefix = str(cfg.music_root) + os.sep
    if not path.startswith(prefix):
        root = next((r for r in cfg.library_dirs if path.startswith(str(r) + os.sep)), None)
        return str(root) if root else None
    parts = path[len(prefix):].split(os.sep)
    if len(parts) < 2:
        return None
    if parts[0] in (cfg.apple_dir, cfg.spotify_dir, cfg.local_dir) and len(parts) >= 3:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def folder_path(cfg: Config, label: str) -> Path:
    return Path(label) if label.startswith("/") else cfg.music_root / label


def folders(cfg: Config) -> list[tuple[str, int]]:
    """(folder, number of files) for the folder filter."""
    stamp = (db.get_kv("library_scanned_at"), stats()["files"], cfg.music_root, cfg.apple_dir, cfg.spotify_dir,
             cfg.local_dir)
    if _folder_cache.get("stamp") != stamp:
        with db.connect() as c:
            counts = Counter(folder_label(cfg, r[0]) for r in c.execute("SELECT path FROM library"))
        counts.pop(None, None)
        _folder_cache.update(stamp=stamp, folders=sorted(counts.items(), key=lambda kv: kv[0].casefold()))
    return _folder_cache["folders"]


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def where(cfg: Config, f: dict) -> tuple[str, list]:
    """SQL for a filter: {'q': words, 'folders': [labels], 'artist': exact, 'album': exact}."""
    clauses, args = [], []
    labels = [x for x in f.get("folders") or [] if x]
    if labels:
        clauses.append("(" + " OR ".join("path LIKE ? ESCAPE '\\'" for _ in labels) + ")")
        args += [_esc(str(folder_path(cfg, x)) + os.sep) + "%" for x in labels]
    for word in norm(f.get("q")).split():
        clauses.append("search LIKE ? ESCAPE '\\'")
        args.append(f"%{_esc(word)}%")
    if f.get("artist"):
        clauses.append("(artist = ? OR album_artist = ?)")
        args += [f["artist"], f["artist"]]
    if f.get("album"):
        clauses.append("album = ?")
        args.append(f["album"])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def count_songs(cfg: Config, f: dict) -> int:
    """Songs matching a filter, counting a song in two albums or folders once."""
    sql, args = where(cfg, f)
    with db.connect() as c:
        return c.execute(f"SELECT COUNT(DISTINCT song_key) FROM library{sql}", args).fetchone()[0]


def browse(cfg: Config, f: dict, sort: str = "artist", offset: int = 0, limit: int = 100) -> tuple[list[dict], int]:
    """One page of files matching a filter, and how many match in all."""
    sql, args = where(cfg, f)
    with db.connect() as c:
        total = c.execute(f"SELECT COUNT(*) FROM library{sql}", args).fetchone()[0]
        rows = [dict(r) for r in c.execute(
            f"SELECT rowid AS id, * FROM library{sql} ORDER BY {SORTS.get(sort, SORTS['artist'])} LIMIT ? OFFSET ?",
            (*args, limit, offset))]
    for r in rows:
        r["display_title"] = r["title"] or Path(r["path"]).stem
        r["folder"] = folder_label(cfg, r["path"])
    return rows, total


def song(r: dict) -> dict:
    """A library row as a song, the same shape the streaming services give."""
    return {
        "key": r["song_key"], "isrc": r["isrc"], "title": r["title"] or Path(r["path"]).stem,
        "artist": r["artist"], "album": r["album"], "album_artist": r["album_artist"],
        "track_number": r["track_number"], "disc_number": r["disc_number"],
        # Files don't say whether they're the explicit version; copies then prefer the original (explicit) one.
        "duration_ms": int(r["duration"] * 1000) if r["duration"] else None, "content_rating": "unknown",
        "release_date": r["year"], "genre": r["genre"], "artwork_url": None,
        "thumb_url": f"/art/{r['art_key']}" if r["art_key"] else None, "url": None, "preview_url": None,
    }


def collection(cfg: Config, f: dict) -> list[dict]:
    """Every song matching a filter, once each, in artist and album order."""
    sql, args = where(cfg, f)
    seen, out = set(), []
    with db.connect() as c:
        for r in c.execute(f"SELECT * FROM library{sql} ORDER BY {SORTS['artist']}", args):
            if r["song_key"] and r["song_key"] not in seen:
                seen.add(r["song_key"])
                out.append(song(dict(r)))
    return out


def songs_for_keys(keys: list[str]) -> list[dict]:
    """Songs for these keys, in this order; keys the library doesn't have are left out."""
    found: dict[str, dict] = {}
    with db.connect() as c:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            for r in c.execute(f"SELECT * FROM library WHERE song_key IN ({', '.join('?' * len(chunk))}) "
                               f"ORDER BY path", chunk):
                found.setdefault(r["song_key"], song(dict(r)))
    return [found[k] for k in dict.fromkeys(keys) if k in found]


def key_paths(exclude_dir: Path | None = None) -> dict[str, str]:
    """Song key -> a file for it, MP3s first."""
    out: dict[str, str] = {}
    skip = str(exclude_dir) + os.sep if exclude_dir else None
    with db.connect() as c:
        for r in c.execute("SELECT song_key, path FROM library WHERE song_key IS NOT NULL "
                           "ORDER BY (lower(path) NOT LIKE '%.mp3'), path"):
            if skip and r["path"].startswith(skip):
                continue
            out.setdefault(r["song_key"], r["path"])
    return out


def file_by_id(file_id: int) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT rowid AS id, * FROM library WHERE rowid = ?", (file_id,)).fetchone()
    return dict(r) if r else None


def file_for_art(key: str) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT path, album FROM library WHERE art_key = ? ORDER BY path LIMIT 1", (key,)).fetchone()
    return dict(r) if r else None
