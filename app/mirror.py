"""Copy a playlist to the other streaming service.

Apple Music -> Spotify keeps the copy identical: same songs, same order, on every run.
Spotify -> Apple Music can only add songs: Apple's API has no way to remove or reorder
tracks in a library playlist, so later runs add what's new.
"""
import json
import logging

from . import apple, db, spotify, sync
from .config import Config

log = logging.getLogger(__name__)
RECHECK_MISSING = 7 * 86400      # look again for songs that weren't on the target after a week
TARGETS = {"spotify": "Spotify", "apple": "Apple Music"}


def target_for(source: str) -> str:
    return "spotify" if source == "apple" else "apple"


def get(pid: str, target: str) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT * FROM mirrors WHERE playlist_id = ? AND target = ?", (pid, target)).fetchone()
    if not r:
        return None
    m = dict(r)
    m["missing"] = json.loads(m["missing"] or "[]")
    m["url"] = remote_url(target, m["remote_id"])
    return m


def remote_url(target: str, remote_id: str | None) -> str | None:
    if not remote_id:
        return None
    if target == "spotify":
        return f"https://open.spotify.com/playlist/{remote_id}"
    return f"https://music.apple.com/library/playlist/{remote_id}"


def _client(cfg: Config, target: str):
    return spotify.Client(cfg) if target == "spotify" else apple.Client(cfg)


def run(cfg: Config, pid: str) -> None:
    p = sync.playlist(pid)
    target = target_for(p["source"])
    with db.connect() as c:
        entries = c.execute("SELECT e.song_key FROM entries e WHERE e.playlist_id = ? ORDER BY e.position",
                            (pid,)).fetchall()
        songs = {r["key"]: dict(r) for r in c.execute(
            "SELECT s.* FROM songs s JOIN entries e ON e.song_key = s.key WHERE e.playlist_id = ?", (pid,))}
        links = {r["song_key"]: dict(r) for r in c.execute(
            "SELECT * FROM song_links WHERE target = ? AND song_key IN "
            "(SELECT song_key FROM entries WHERE playlist_id = ?)", (target, pid))}
    order = [e["song_key"] for e in entries]
    unique = list(dict.fromkeys(order))
    now = db.now()
    try:
        with _client(cfg, target) as api:
            for i, key in enumerate(unique, 1):
                link = links.get(key)
                if link and (link["remote_id"] or now - (link["checked_at"] or 0) < RECHECK_MISSING):
                    continue
                if i % 10 == 0:
                    _busy(pid, f"Matching on {TARGETS[target]} ({i}/{len(unique)})")
                found = api.find_track(songs[key]) if target == "spotify" else api.find_song(songs[key])
                links[key] = {"remote_id": found or "", "checked_at": now}
                with db.connect() as c:
                    c.execute("INSERT OR REPLACE INTO song_links (song_key, target, remote_id, checked_at) "
                              "VALUES (?, ?, ?, ?)", (key, target, found or "", now))

            ids = [links[k]["remote_id"] for k in order if links.get(k, {}).get("remote_id")]
            missing = [f"{songs[k]['artist']} - {songs[k]['title']}" for k in unique if not links[k]["remote_id"]]
            _busy(pid, f"Updating the {TARGETS[target]} copy")
            m = get(pid, target)
            remote = m["remote_id"] if m and m["remote_id"] and api.playlist_exists(m["remote_id"]) else None
            desc = f"Copied from {sync.SOURCES[p['source']]} by soulseek-playlist-sync."
            if target == "spotify":
                remote = remote or api.create_playlist(p["name"], desc)
                api.set_tracks(remote, ids)
            elif remote:
                have = api.playlist_song_ids(remote)
                api.add_tracks(remote, [i for i in dict.fromkeys(ids) if i not in have])
            else:
                remote = api.create_playlist(p["name"], desc, ids)
        _save(pid, target, remote, matched=len(unique) - len(missing), total=len(unique), missing=missing)
    except (spotify.SpotifyError, apple.AppleError) as e:
        _save(pid, target, (get(pid, target) or {}).get("remote_id"), error=str(e)[:300])
        raise


def _busy(pid: str, label: str) -> None:
    with db.connect() as c:
        c.execute("UPDATE playlists SET busy = ? WHERE id = ?", (label, pid))


def _save(pid, target, remote, matched=None, total=None, missing=None, error=None):
    with db.connect() as c:
        old = c.execute("SELECT * FROM mirrors WHERE playlist_id = ? AND target = ?", (pid, target)).fetchone()
        c.execute(
            "INSERT OR REPLACE INTO mirrors (playlist_id, target, remote_id, matched, total, missing, updated_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, target, remote,
             matched if matched is not None else (old["matched"] if old else None),
             total if total is not None else (old["total"] if old else None),
             json.dumps(missing) if missing is not None else (old["missing"] if old else None),
             db.now() if error is None else (old["updated_at"] if old else None), error))
