"""Playlist sync: fetch from the service, match local files, place them, write the .m3u."""
import json
import logging
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

from . import apple, db, library, spotify, youtube
from .config import Config
from .folders import place, rename_all, safe_name, song_file_name, write_m3u

SOURCES = {"apple": "Apple Music", "spotify": "Spotify", "youtube": "YouTube", "local": "My music"}

log = logging.getLogger(__name__)
SONG_FIELDS = ("isrc", "title", "artist", "album", "album_artist", "track_number", "disc_number",
               "duration_ms", "content_rating", "release_date", "genre", "artwork_url", "thumb_url", "url",
               "preview_url")
RETRY_AFTER = 6 * 3600   # a Sync retries songs not found on Soulseek after this long
_locks: dict[str, threading.Lock] = {}


# ------------------------------------------------------------------ helpers

def playlist(pid: str):
    with db.connect() as c:
        return c.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()


def folder_of(cfg: Config, p) -> Path:
    return cfg.music_root / p["folder"]


def m3u_path(cfg: Config, p) -> Path:
    return folder_of(cfg, p) / f"{safe_name(p['name'])}.m3u"


def _client(cfg: Config, source: str):
    if source == "youtube":
        return youtube.Playlists(cfg)
    return apple.Client(cfg) if source == "apple" else spotify.Client(cfg)


def _unique_folder(cfg: Config, source: str, name: str, pid: str | None = None) -> str:
    base = f"{cfg.source_dir(source)}/{safe_name(name)}"
    with db.connect() as c:
        taken = {r["folder"] for r in c.execute("SELECT id, folder FROM playlists") if r["id"] != pid}
    folder, n = base, 2
    while folder in taken:
        folder, n = f"{base} ({n})", n + 1
    return folder


def run_job(pid: str, label: str, fn, *args) -> bool:
    """Run fn in the background, one job per playlist at a time."""
    lock = _locks.setdefault(pid, threading.Lock())
    if not lock.acquire(blocking=False):
        return False

    def target():
        try:
            with db.connect() as c:
                c.execute("UPDATE playlists SET busy = ?, error = NULL WHERE id = ?", (label, pid))
            fn(*args)
        except Exception as e:
            log.exception("%s failed for %s", label, pid)
            with db.connect() as c:
                c.execute("UPDATE playlists SET error = ? WHERE id = ?", (f"{label} failed: {e}", pid))
        finally:
            with db.connect() as c:
                c.execute("UPDATE playlists SET busy = NULL WHERE id = ?", (pid,))
            lock.release()

    threading.Thread(target=target, daemon=True, name=f"job-{pid}").start()
    return True


# ------------------------------------------------------------------ add / fetch

def add_playlist(cfg: Config, source: str, remote_id: str, name: str) -> str:
    if source not in ("apple", "spotify", "youtube"):
        raise ValueError(f"unknown source {source!r}")
    pid = f"{source}:{remote_id}"
    with db.connect() as c:
        c.execute("INSERT OR IGNORE INTO playlists (id, source, remote_id, name, folder, numbering, library_mode) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (pid, source, remote_id, name, _unique_folder(cfg, source, name),
                   1 if cfg.default_numbering else 0, cfg.default_library_mode))
    return pid


def add_local_playlist(cfg: Config, name: str, rule: dict | None = None) -> str:
    """A playlist made from your own music: a fixed list you add songs to, or, with a rule,
    every song matching a filter, kept up to date as the library changes."""
    name = (name or "").strip() or "My playlist"
    pid = f"local:{secrets.token_hex(4)}"
    with db.connect() as c:
        c.execute("INSERT INTO playlists (id, source, remote_id, name, folder, numbering, library_mode, rule) "
                  "VALUES (?, 'local', ?, ?, ?, ?, 'reference', ?)",
                  (pid, pid[6:], name, _unique_folder(cfg, "local", name), 1 if cfg.default_numbering else 0,
                   json.dumps(rule) if rule is not None else None))
    return pid


def rule_of(p) -> dict | None:
    return json.loads(p["rule"]) if p and p["rule"] else None


def remove_playlist(pid: str) -> None:
    """Stop tracking a playlist. Files on disk are left alone."""
    with db.connect() as c:
        for t in ("entries", "items", "mirrors"):
            c.execute(f"DELETE FROM {t} WHERE playlist_id = ?", (pid,))
        c.execute("DELETE FROM playlists WHERE id = ?", (pid,))


def _save_songs(c, tracks: list[dict]) -> None:
    c.executemany(
        f"INSERT INTO songs (key, {', '.join(SONG_FIELDS)}) VALUES (?{', ?' * len(SONG_FIELDS)}) "
        f"ON CONFLICT(key) DO UPDATE SET {', '.join(f'{f}=excluded.{f}' for f in SONG_FIELDS)}",
        [(s["key"], *(s.get(f) for f in SONG_FIELDS)) for s in tracks])


def fetch(cfg: Config, pid: str) -> None:
    p = playlist(pid)
    if p["source"] == "local":
        _fetch_local(cfg, p)
        return
    with _client(cfg, p["source"]) as api:
        name = api.playlist_name(p["remote_id"])
        tracks = api.playlist_tracks(p["remote_id"])
    _replace_entries(pid, tracks)
    if name != p["name"]:
        _rename_playlist(cfg, p, name)


def _replace_entries(pid: str, tracks: list[dict]) -> None:
    t = db.now()
    with db.connect() as c:
        _save_songs(c, tracks)
        c.execute("DELETE FROM entries WHERE playlist_id = ?", (pid,))
        c.executemany("INSERT INTO entries (playlist_id, position, song_key) VALUES (?, ?, ?)",
                      [(pid, i, s["key"]) for i, s in enumerate(tracks, 1)])
        c.executemany("INSERT OR IGNORE INTO items (playlist_id, song_key, updated_at) VALUES (?, ?, ?)",
                      [(pid, s["key"], t) for s in tracks])
        c.execute("UPDATE playlists SET fetched_at = ? WHERE id = ?", (t, pid))


def _fetch_local(cfg: Config, p) -> None:
    """An auto-updating playlist lists what matches its filter now; a fixed one keeps its songs
    but picks up tag changes."""
    rule = rule_of(p)
    if rule is not None:
        _replace_entries(p["id"], library.collection(cfg, rule))
        return
    with db.connect() as c:
        keys = [r[0] for r in c.execute("SELECT DISTINCT song_key FROM entries WHERE playlist_id = ?", (p["id"],))]
    with db.connect() as c:
        _save_songs(c, library.songs_for_keys(keys))
        c.execute("UPDATE playlists SET fetched_at = ? WHERE id = ?", (db.now(), p["id"]))


def add_songs(cfg: Config, pid: str, keys: list[str]) -> int:
    """Append library songs to a fixed playlist. Songs already in it are left where they are."""
    tracks = library.songs_for_keys(keys)
    t = db.now()
    with db.connect() as c:
        have = {r[0] for r in c.execute("SELECT song_key FROM entries WHERE playlist_id = ?", (pid,))}
        new = [s for s in tracks if s["key"] not in have]
        pos = c.execute("SELECT COALESCE(MAX(position), 0) FROM entries WHERE playlist_id = ?", (pid,)).fetchone()[0]
        _save_songs(c, new)
        c.executemany("INSERT INTO entries (playlist_id, position, song_key) VALUES (?, ?, ?)",
                      [(pid, pos + i, s["key"]) for i, s in enumerate(new, 1)])
        c.executemany("INSERT OR IGNORE INTO items (playlist_id, song_key, updated_at) VALUES (?, ?, ?)",
                      [(pid, s["key"], t) for s in new])
        c.execute("UPDATE playlists SET fetched_at = ? WHERE id = ?", (t, pid))
    return len(new)


def remove_songs(cfg: Config, pid: str, keys: list[str]) -> None:
    """Take songs out of a fixed playlist, and delete the copies this app made of them."""
    with db.connect() as c:
        order = [r[0] for r in c.execute("SELECT song_key FROM entries WHERE playlist_id = ? ORDER BY position", (pid,))]
        drop = set(keys)
        c.execute("DELETE FROM entries WHERE playlist_id = ?", (pid,))
        c.executemany("INSERT INTO entries (playlist_id, position, song_key) VALUES (?, ?, ?)",
                      [(pid, i, k) for i, k in enumerate((k for k in order if k not in drop), 1)])
    prune(cfg, pid)
    reconcile(cfg, pid, apply=True)


def refresh_local(cfg: Config, pid: str, scan: bool = True) -> None:
    """Rescan the library for changes, then bring the playlist and its .m3u up to date."""
    if scan:
        library.scan(cfg)
    fetch(cfg, pid)
    prune(cfg, pid)
    reconcile(cfg, pid, apply=True)


def refresh_auto_playlists(cfg: Config) -> None:
    """Rescan the library, then update every auto-updating playlist (not their copies on the services)."""
    library.scan(cfg)
    for p in tracked():
        if p["source"] == "local" and p["rule"] is not None:
            run_job(p["id"], "Refreshing", refresh_local, cfg, p["id"], False)


def _rename_playlist(cfg: Config, p, name: str) -> None:
    """The playlist was renamed on the service: rename its .m3u, and its folder unless you chose the folder."""
    old_m3u = m3u_path(cfg, p)
    auto = f"{cfg.source_dir(p['source'])}/{safe_name(p['name'])}"
    folder = p["folder"]
    if folder == auto or folder.startswith(auto + " ("):
        folder = _unique_folder(cfg, p["source"], name, p["id"])
        if not (cfg.music_root / folder).exists():
            _move_files(cfg, p["folder"], folder)
        else:
            folder = p["folder"]
    with db.connect() as c:
        c.execute("UPDATE playlists SET name = ?, folder = ? WHERE id = ?", (name, folder, p["id"]))
    stale = cfg.music_root / folder / old_m3u.name
    if stale.exists():
        stale.unlink()


def rename_playlist(cfg: Config, pid: str, name: str) -> None:
    _rename_playlist(cfg, playlist(pid), name)
    write_playlist_m3u(cfg, pid)


def _move_files(cfg: Config, old: str, new: str) -> None:
    """Move a playlist folder and fix the paths we remember for files inside it."""
    old_dir, new_dir = cfg.music_root / old, cfg.music_root / new
    if old_dir.exists():
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_dir), str(new_dir))
    a, b = str(old_dir) + os.sep, str(new_dir) + os.sep
    with db.connect() as c:
        for table, col in (("library", "path"), ("downloads", "path")):
            c.execute(f"UPDATE OR REPLACE {table} SET {col} = ? || substr({col}, ?) WHERE substr({col}, 1, ?) = ?",
                      (b, len(a) + 1, len(a), a))


def move_playlist_folder(cfg: Config, pid: str, folder: str) -> None:
    """Point a playlist at a different folder under the music root, moving what's there."""
    p = playlist(pid)
    if folder == p["folder"]:
        return
    with db.connect() as c:
        if c.execute("SELECT 1 FROM playlists WHERE folder = ? AND id != ?", (folder, pid)).fetchone():
            raise ValueError(f"Another playlist already uses {folder}")
    target = cfg.music_root / folder
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"{folder} already exists and isn't empty")
    if target.exists():
        target.rmdir()
    _move_files(cfg, p["folder"], folder)
    with db.connect() as c:
        c.execute("UPDATE playlists SET folder = ? WHERE id = ?", (folder, pid))
    write_playlist_m3u(cfg, pid)


# ------------------------------------------------------------------ reconcile

def _load(pid: str):
    with db.connect() as c:
        entries = c.execute("SELECT position, song_key FROM entries WHERE playlist_id = ? ORDER BY position",
                            (pid,)).fetchall()
        items = {r["song_key"]: dict(r) for r in c.execute("SELECT * FROM items WHERE playlist_id = ?", (pid,))}
        songs = {r["key"]: dict(r) for r in c.execute(
            "SELECT s.* FROM songs s JOIN items i ON i.song_key = s.key WHERE i.playlist_id = ?", (pid,))}
        downloads = {r["song_key"]: dict(r) for r in c.execute(
            "SELECT d.* FROM downloads d JOIN items i ON i.song_key = d.song_key WHERE i.playlist_id = ?", (pid,))}
    return entries, items, songs, downloads


def _placed(folder: Path, it: dict) -> bool:
    if it.get("origin") in ("download", "library") and it.get("file"):
        return (folder / it["file"]).exists()
    if it.get("origin") == "reference" and it.get("library_path"):
        return os.path.exists(it["library_path"])
    return False


def reconcile(cfg: Config, pid: str, apply: bool, want_downloads: bool = False,
              retry_after: float = RETRY_AFTER) -> None:
    """Match every song to a local file.

    apply: place library copies and finished downloads, renumber, write the .m3u.
    want_downloads: queue missing songs for Soulseek search and download.
    """
    p = playlist(pid)
    folder = folder_of(cfg, p)
    entries, items, songs, downloads = _load(pid)
    local = p["source"] == "local"
    local_paths = library.key_paths(exclude_dir=folder) if local else {}
    first_pos: dict[str, int] = {}
    for e in entries:
        first_pos.setdefault(e["song_key"], e["position"])
    width = max(3, len(str(len(entries))))

    renames, placements, updates, wanted = {}, [], [], []
    used: set[str] = set()
    for key, pos in first_pos.items():
        it, s = items[key], songs[key]
        if it["skip"]:
            continue
        stem = song_file_name(s, pos if p["numbering"] else None, width)[:-4]
        if stem.lower() in used:     # two songs with the same artist and title, no numbering
            n = 2
            while f"{stem} ({n})".lower() in used:
                n += 1
            stem = f"{stem} ({n})"
        used.add(stem.lower())

        if _placed(folder, it) and not (it["origin"] == "reference" and p["library_mode"] == "copy"):
            name = stem + Path(it["file"] or ".mp3").suffix.lower()
            if it["origin"] != "reference" and it["file"] != name:
                renames[it["file"]] = name
                updates.append((key, {"file": name}))
            continue

        d = downloads.get(key)
        src = d["path"] if d and d["state"] == "done" and d["path"] and os.path.exists(d["path"]) else None
        origin = "download" if src else "library"
        if not src and local_paths.get(key) and os.path.exists(local_paths[key]):
            src = local_paths[key]
        # Playlists from the streaming services stay MP3-only for MP3 players; your own music is what it is.
        src = src or library.find(s, cfg.length_tolerance, mp3_only=not local, exclude_dir=folder)
        if src:
            if p["library_mode"] == "reference" and origin == "library":
                updates.append((key, {"origin": "reference", "library_path": src, "file": None}))
            else:
                placements.append((key, src, stem + Path(src).suffix.lower(), origin))
                updates.append((key, {"library_path": src if origin == "library" else it["library_path"]}))
            continue
        updates.append((key, {"library_path": None}))
        if want_downloads:
            wanted.append(key)

    if apply:
        folder.mkdir(parents=True, exist_ok=True)
        rename_all(folder, renames)
        for key, src, name, origin in placements:
            place(Path(src), folder / name)
            updates.append((key, {"file": name, "origin": origin, "owned": 1}))
    else:
        # A preview records matches but must not rename anything on disk.
        updates = [(k, u) for k, u in updates if "file" not in u or "origin" in u]

    t = db.now()
    with db.connect() as c:
        for key, fields in updates:
            fields["updated_at"] = t
            c.execute(f"UPDATE items SET {', '.join(f'{k} = ?' for k in fields)} WHERE playlist_id = ? AND song_key = ?",
                      (*fields.values(), pid, key))
        for key in wanted:
            c.execute("UPDATE items SET want = 1 WHERE playlist_id = ? AND song_key = ?", (pid, key))
            c.execute("INSERT OR IGNORE INTO downloads (song_key, state, created_at, updated_at) "
                      "VALUES (?, 'pending', ?, ?)", (key, t, t))
            c.execute("UPDATE downloads SET state = 'pending', source = 'soulseek', query_index = 0, attempt = 0, candidates = NULL, "
                      "detail = NULL, updated_at = ? WHERE song_key = ? AND state IN ('not_found', 'failed') "
                      "AND updated_at <= ?", (t, key, t - retry_after))
        if apply:
            c.execute("UPDATE playlists SET synced_at = ? WHERE id = ?", (t, pid))
    if apply:
        write_playlist_m3u(cfg, pid)


def write_playlist_m3u(cfg: Config, pid: str) -> None:
    p = playlist(pid)
    folder = folder_of(cfg, p)
    entries, items, songs, _ = _load(pid)
    lines = []
    for e in entries:
        it, s = items[e["song_key"]], songs[e["song_key"]]
        if it["skip"] or not _placed(folder, it):
            continue
        rel = it["file"] if it["origin"] != "reference" else os.path.relpath(it["library_path"], folder)
        lines.append((round((s["duration_ms"] or 0) / 1000), f"{s['artist']} - {s['title']}", rel))
    folder.mkdir(parents=True, exist_ok=True)
    write_m3u(m3u_path(cfg, p), lines)


def preview(cfg: Config, pid: str) -> None:
    fetch(cfg, pid)
    reconcile(cfg, pid, apply=False)


def sync(cfg: Config, pid: str) -> None:
    """Download all: place library songs, download every missing song, retry earlier failures, write the .m3u."""
    fetch(cfg, pid)
    reconcile(cfg, pid, apply=True, want_downloads=True, retry_after=0)


def prune(cfg: Config, pid: str) -> int:
    """Delete files this app created for songs that left the playlist."""
    p = playlist(pid)
    folder = folder_of(cfg, p)
    with db.connect() as c:
        current = {r["song_key"] for r in c.execute("SELECT song_key FROM entries WHERE playlist_id = ?", (pid,))}
        gone = [dict(r) for r in c.execute("SELECT * FROM items WHERE playlist_id = ?", (pid,))
                if r["song_key"] not in current]
        n = 0
        for it in gone:
            if it["owned"] and it["file"] and (folder / it["file"]).exists():
                (folder / it["file"]).unlink()
                n += 1
            c.execute("DELETE FROM items WHERE playlist_id = ? AND song_key = ?", (pid, it["song_key"]))
    write_playlist_m3u(cfg, pid)
    return n


def set_song(pid: str, key: str, **fields) -> None:
    with db.connect() as c:
        c.execute(f"UPDATE items SET {', '.join(f'{k} = ?' for k in fields)} WHERE playlist_id = ? AND song_key = ?",
                  (*fields.values(), pid, key))


# ------------------------------------------------------------------ downloads finishing

def place_download(cfg: Config, key: str, src: Path) -> Path | None:
    """Put a finished, tagged download into every playlist folder that wants it."""
    with db.connect() as c:
        rows = c.execute(
            "SELECT i.playlist_id, MIN(e.position) AS pos FROM items i JOIN entries e "
            "ON e.playlist_id = i.playlist_id AND e.song_key = i.song_key "
            "WHERE i.song_key = ? AND i.skip = 0 GROUP BY i.playlist_id", (key,)).fetchall()
    first = None
    for r in rows:
        pid = r["playlist_id"]
        p = playlist(pid)
        folder = folder_of(cfg, p)
        it = _load(pid)[1][key]
        if _placed(folder, it):
            continue
        with db.connect() as c:
            song = dict(c.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone())
            total = c.execute("SELECT COUNT(*) FROM entries WHERE playlist_id = ?", (pid,)).fetchone()[0]
        name = song_file_name(song, r["pos"] if p["numbering"] else None, max(3, len(str(total))))
        dst = folder / name
        place(first or src, dst, move=first is None)
        first = first or dst
        set_song(pid, key, file=name, origin="download", owned=1, updated_at=db.now())
        write_playlist_m3u(cfg, pid)
    return first


def summary(cfg: Config, pid: str) -> tuple[list[dict], dict]:
    """Rows for the playlist page, one per song in playlist order, and counts per status."""
    p = playlist(pid)
    folder = folder_of(cfg, p)
    entries, items, songs, downloads = _load(pid)
    count: dict[str, int] = {}
    order: list[str] = []
    positions: dict[str, list[int]] = {}
    for e in entries:
        if e["song_key"] not in positions:
            order.append(e["song_key"])
        positions.setdefault(e["song_key"], []).append(e["position"])
    order += [k for k in items if k not in positions]   # removed upstream, at the end

    rows = []
    for key in order:
        it, s, d = items[key], songs.get(key, {}), downloads.get(key)
        status, detail = _status(folder, it, d, key in positions)
        count[status] = count.get(status, 0) + 1
        rows.append({"key": key, "song": s, "item": it, "download": d, "positions": positions.get(key, []),
                     "status": status, "detail": detail})
    return rows, count


def _status(folder: Path, it: dict, d: dict | None, current: bool) -> tuple[str, str | None]:
    if not current:
        return "removed", "No longer in the playlist"
    if it["skip"]:
        return "skipped", None
    if _placed(folder, it):
        if it["origin"] == "reference":
            return "synced", "Linked to library copy"
        return "synced", d["detail"] if d and d.get("source") == "youtube" else None
    if it["library_path"] and os.path.exists(it["library_path"]):
        return "in_library", it["library_path"]
    if d:
        state = d["state"]
        if state == "downloading":
            return "downloading", progress_text(d)
        if state == "queued":
            place = f", number {d['place_in_queue']} in line" if d.get("place_in_queue") else ""
            return "queued", f"Queued at {d['username']}{place}"
        if state == "found":
            return "found", _cand_detail(d)
        if state == "pending":
            return "waiting", d["detail"] if d.get("source") == "youtube" else "Waiting to search Soulseek"
        if state == "searching":
            return "searching", d["detail"]
        if state == "done":
            return "downloading", "Finishing"
        return state, d["detail"]
    return "missing", None


def human_bytes(n: float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def progress_text(d: dict) -> str:
    parts = [f"{d.get('percent') or 0:.0f}%"]
    if d.get("speed"):
        parts.append(f"{human_bytes(d['speed'])}/s")
    if d.get("size"):
        parts.append(f"{human_bytes(d.get('bytes_done'))} of {human_bytes(d['size'])}")
    if d.get("speed") and d.get("size"):
        left = max(d["size"] - (d.get("bytes_done") or 0), 0) / d["speed"]
        parts.append(f"{int(left // 60)}:{int(left % 60):02d} left")
    return " · ".join(parts) + f" from {d['username']}"


def downloads_overview() -> tuple[list[dict], dict]:
    """Every song being searched or downloaded, plus the last day's finished and failed ones."""
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT d.*, s.title, s.artist, s.album, s.thumb_url, s.artwork_url, "
            "(SELECT group_concat(p.name, ', ') FROM items i JOIN playlists p ON p.id = i.playlist_id "
            " WHERE i.song_key = d.song_key) AS playlists, "
            "(SELECT MAX(i.want) FROM items i WHERE i.song_key = d.song_key AND i.skip = 0) AS wanted "
            "FROM downloads d JOIN songs s ON s.key = d.song_key "
            "WHERE d.state IN ('pending', 'searching', 'found', 'queued', 'downloading') "
            "   OR d.updated_at > ? ORDER BY "
            "CASE d.state WHEN 'downloading' THEN 0 WHEN 'queued' THEN 1 WHEN 'searching' THEN 2 "
            "WHEN 'pending' THEN 3 WHEN 'found' THEN 4 ELSE 5 END, d.updated_at DESC", (db.now() - 86400,))]
    counts: dict[str, int] = {}
    for r in rows:
        if r["state"] == "found" and not r["wanted"]:
            r["state"] = "found_only"          # found by a dry-run search, not asked for
        counts[r["state"]] = counts.get(r["state"], 0) + 1
        r["progress"] = progress_text(r) if r["state"] == "downloading" else None
    return rows, counts


def _cand_detail(d: dict) -> str | None:
    import json
    cands = json.loads(d["candidates"] or "[]")
    if not cands:
        return None
    c = cands[min(d["attempt"], len(cands) - 1)]
    br = f"{c['bitrate']} kbps" if c.get("bitrate") else "unknown bitrate"
    return f"{br} from {c['username']} ({len(cands)} option{'s' if len(cands) != 1 else ''})"


def song_path(cfg: Config, pid: str, key: str) -> Path | None:
    """The local file for a song in a playlist, if it has one."""
    p = playlist(pid)
    if not p:
        return None
    with db.connect() as c:
        it = c.execute("SELECT * FROM items WHERE playlist_id = ? AND song_key = ?", (pid, key)).fetchone()
    if not it:
        return None
    if it["origin"] == "reference" and it["library_path"]:
        path = Path(it["library_path"])
    elif it["file"]:
        path = folder_of(cfg, p) / it["file"]
    else:
        return None
    return path if path.is_file() else None


def export_files(cfg: Config, pid: str) -> tuple[str, list[tuple[str, Path]], str]:
    """(folder name, [(name in the ZIP, file on disk)], .m3u text) for a self-contained ZIP."""
    p = playlist(pid)
    folder = folder_of(cfg, p)
    entries, items, songs, _ = _load(pid)
    width = max(3, len(str(len(entries))))
    names: dict[str, str] = {}
    files: list[tuple[str, Path]] = []
    lines = ["#EXTM3U"]
    for e in entries:
        key = e["song_key"]
        it, s = items[key], songs[key]
        if it["skip"] or not _placed(folder, it):
            continue
        if key not in names:
            if it["origin"] == "reference":
                src = Path(it["library_path"])
                names[key] = song_file_name(s, e["position"] if p["numbering"] else None, width)[:-4] + src.suffix.lower()
                files.append((names[key], src))
            else:
                names[key] = it["file"]
                files.append((names[key], folder / it["file"]))
        lines += [f"#EXTINF:{round((s['duration_ms'] or 0) / 1000)},{s['artist']} - {s['title']}", names[key]]
    return safe_name(p["name"]), files, "\n".join(lines) + "\n"


def covers(pid: str, n: int = 4) -> list[str]:
    """Artwork of the first few different albums in the playlist, for a mosaic cover."""
    out: list[str] = []
    with db.connect() as c:
        for r in c.execute("SELECT COALESCE(s.thumb_url, s.artwork_url) AS art, s.album FROM entries e "
                           "JOIN songs s ON s.key = e.song_key WHERE e.playlist_id = ? ORDER BY e.position", (pid,)):
            if r["art"] and r["art"] not in out:
                out.append(r["art"])
                if len(out) >= n:
                    break
    return out


def tracked() -> list[dict]:
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM playlists ORDER BY name COLLATE NOCASE")]


def wait_idle(pid: str, timeout: float = 600) -> None:
    """For scripts and tests: block until the playlist's job finishes."""
    end = time.time() + timeout
    lock = _locks.get(pid)          # taken before the job's thread starts, so there's no gap to miss
    while time.time() < end and ((lock and lock.locked()) or playlist(pid)["busy"]):
        time.sleep(0.1)
