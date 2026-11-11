"""Index of the music already on disk, so we don't download what we have."""
import logging
import os
import threading
from pathlib import Path

import mutagen

from . import db
from .config import Config
from .match import artist_matches, norm_title

log = logging.getLogger(__name__)
AUDIO = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wma", ".aac"}
_scan_lock = threading.Lock()


def _first(tags, *names):
    for n in names:
        v = tags.get(n) if tags else None
        if v:
            return str(v[0]) if isinstance(v, list) else str(v)
    return None


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
        "isrc": (_first(tags, "isrc") or "").upper() or None,
        "duration": getattr(f.info, "length", None),
    }


def scan(cfg: Config) -> dict:
    """Index new and changed files, drop vanished ones. Safe to call repeatedly."""
    if not _scan_lock.acquire(blocking=False):
        return {"skipped": "scan already running"}
    try:
        db.set_kv("library_scan", "running")
        with db.connect() as c:
            known = {r["path"]: (r["size"], r["mtime"]) for r in c.execute("SELECT path, size, mtime FROM library")}
        seen, changed = set(), []
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
        for p, size, mtime in changed:
            t = read_tags(Path(p))
            rows.append((p, size, mtime, t.get("isrc"), t.get("artist"), t.get("title"), t.get("album"),
                         t.get("duration"), norm_title(t.get("title"))))
            if len(rows) >= 500:
                _write(rows)
                rows = []
                db.set_kv("library_scan", f"running ({len(seen)} files seen)")
        _write(rows)
        gone = [p for p in known if p not in seen]
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


def _write(rows):
    if not rows:
        return
    with db.connect() as c:
        c.executemany(
            "INSERT INTO library (path, size, mtime, isrc, artist, title, album, duration, norm_title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
            "mtime=excluded.mtime, isrc=excluded.isrc, artist=excluded.artist, title=excluded.title, "
            "album=excluded.album, duration=excluded.duration, norm_title=excluded.norm_title", rows)


def add_file(path: Path, song: dict) -> None:
    """Index a file we just wrote, without waiting for the next scan."""
    st = path.stat()
    _write([(str(path), st.st_size, st.st_mtime, song.get("isrc"), song.get("artist"), song.get("title"),
             song.get("album"), (song.get("duration_ms") or 0) / 1000, norm_title(song.get("title")))])


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
