"""Copy a playlist to the other streaming service.

Apple Music -> Spotify keeps the copy identical: same songs, same order, on every run.
Spotify -> Apple Music can only add songs: Apple's API has no way to remove or reorder
tracks in a library playlist, so later runs add what's new.
Playlists made from your own music can go to either service, or both.

A Spotify playlist holds about 10,000 songs. A bigger playlist gets a random 10,000 of its songs,
picked again on every update, which suits a whole collection you want to shuffle.
"""
import json
import logging
import random
import time

from . import apple, db, spotify, sync
from .config import Config

log = logging.getLogger(__name__)
RECHECK_MISSING = 7 * 86400      # look again for songs that weren't on the target after a week
TARGETS = {"spotify": "Spotify", "apple": "Apple Music"}
SPOTIFY_MAX = 10_000
PACE = {"spotify": 0.35, "apple": 0.15}   # seconds between searches when matching a big playlist


def targets_for(source: str) -> list[str]:
    return {"apple": ["spotify"], "spotify": ["apple"], "local": ["spotify", "apple"],
            "youtube": ["spotify", "apple"]}[source]


def target_for(source: str) -> str:
    return targets_for(source)[0]


def get(pid: str, target: str) -> dict | None:
    with db.connect() as c:
        r = c.execute("SELECT * FROM mirrors WHERE playlist_id = ? AND target = ?", (pid, target)).fetchone()
    if not r:
        return None
    m = dict(r)
    m["missing"] = json.loads(m["missing"] or "[]")
    m["capped"] = bool(m.get("note"))
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


def run(cfg: Config, pid: str, target: str | None = None) -> None:
    p = sync.playlist(pid)
    target = target or target_for(p["source"])
    if target not in targets_for(p["source"]):
        raise ValueError(f"can't copy a {p['source']} playlist to {target}")
    if p["source"] == "local" and sync.rule_of(p) is not None:
        _busy(pid, "Checking your library for changes")
        sync.refresh_local(cfg, pid)
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
    if not unique:
        # Never empty a copy: an empty playlist here is more often a problem than a choice.
        raise ValueError("The playlist has no songs, so the copy was left as it is")
    capped = target == "spotify" and len(unique) > SPOTIFY_MAX
    # A capped copy looks at songs in random order and stops when it has enough.
    pool = random.sample(unique, len(unique)) if capped else unique
    label = TARGETS[target]
    now = db.now()
    chosen, looked, searched = [], [], 0
    try:
        with _client(cfg, target) as api:
            for i, key in enumerate(pool, 1):
                if capped and len(chosen) >= SPOTIFY_MAX:
                    break
                looked.append(key)
                link = links.get(key)
                if not (link and (link["remote_id"] or now - (link["checked_at"] or 0) < RECHECK_MISSING)):
                    if searched and len(pool) > 100:
                        time.sleep(PACE[target])
                    searched += 1
                    found = api.find_track(songs[key]) if target == "spotify" else api.find_song(songs[key])
                    links[key] = {"remote_id": found or "", "checked_at": now}
                    with db.connect() as c:
                        c.execute("INSERT OR REPLACE INTO song_links (song_key, target, remote_id, checked_at) "
                                  "VALUES (?, ?, ?, ?)", (key, target, found or "", now))
                if links[key]["remote_id"]:
                    chosen.append(key)
                if i % 25 == 0:
                    _busy(pid, f"Matching on {label}: {len(chosen):,} found" if capped else
                          f"Matching on {label} ({i:,}/{len(pool):,})")

            keep = set(chosen)
            ids = [links[k]["remote_id"] for k in order if k in keep]
            if p["source"] == "local":
                ids = list(dict.fromkeys(ids))      # two versions of a song can be one track on the service
            missing = [f"{songs[k]['artist']} - {songs[k]['title']}" for k in looked if not links[k]["remote_id"]]
            note = (f"Spotify playlists hold up to {SPOTIFY_MAX:,} songs, so this copy has a random {len(ids):,} "
                    f"of the {len(unique):,}. Update it to pick a new mix.") if capped else None
            _busy(pid, f"Updating the {TARGETS[target]} copy")
            m = get(pid, target)
            remote = m["remote_id"] if m and m["remote_id"] and api.playlist_exists(m["remote_id"]) else None
            desc = ("Made from my music library" if p["source"] == "local"
                    else f"Copied from {sync.SOURCES[p['source']]}") + " by soulseek-playlist-sync."
            if target == "spotify":
                remote = remote or api.create_playlist(p["name"], desc)
                api.set_tracks(remote, ids)
            elif remote:
                have = api.playlist_song_ids(remote)
                api.add_tracks(remote, [i for i in dict.fromkeys(ids) if i not in have])
            else:
                remote = api.create_playlist(p["name"], desc, ids)
        _save(pid, target, remote, matched=len(chosen), total=len(unique), missing=missing, note=note or "")
    except (spotify.SpotifyError, apple.AppleError) as e:
        _save(pid, target, (get(pid, target) or {}).get("remote_id"), error=str(e)[:300])
        raise


def _busy(pid: str, label: str) -> None:
    with db.connect() as c:
        c.execute("UPDATE playlists SET busy = ? WHERE id = ?", (label, pid))


def _save(pid, target, remote, matched=None, total=None, missing=None, error=None, note=None):
    with db.connect() as c:
        old = c.execute("SELECT * FROM mirrors WHERE playlist_id = ? AND target = ?", (pid, target)).fetchone()
        c.execute(
            "INSERT OR REPLACE INTO mirrors (playlist_id, target, remote_id, matched, total, missing, updated_at, "
            "error, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, target, remote,
             matched if matched is not None else (old["matched"] if old else None),
             total if total is not None else (old["total"] if old else None),
             json.dumps(missing) if missing is not None else (old["missing"] if old else None),
             db.now() if error is None else (old["updated_at"] if old else None), error,
             (note or None) if note is not None else (old["note"] if old else None)))
