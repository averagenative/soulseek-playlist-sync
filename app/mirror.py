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

import httpx

from . import apple, db, spotify, sync
from .config import Config

log = logging.getLogger(__name__)
RECHECK_MISSING = 7 * 86400      # look again for songs that weren't on the target after a week
TARGETS = {"spotify": "Spotify", "apple": "Apple Music"}
SPOTIFY_MAX = 10_000
PACE = {"spotify": 0.35, "apple": 0.15}   # seconds between searches when matching a big playlist
GIVE_UP = 10        # searches failing in a row before a copy stops: the service is down, not one song
ADD_BATCH = 100     # songs per request when adding to an Apple Music copy


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
    m["not_found"] = len(not_found(pid, target))
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
        # Songs you took out of a My music playlist stay out of its copies.
        entries = c.execute("SELECT e.song_key FROM entries e JOIN items i "
                            "ON i.playlist_id = e.playlist_id AND i.song_key = e.song_key "
                            "WHERE e.playlist_id = ? AND (? OR i.skip = 0) ORDER BY e.position",
                            (pid, 0 if p["source"] == "local" else 1)).fetchall()
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
    failed, in_a_row = [], 0
    desc = ("Made from my music library" if p["source"] == "local"
            else f"Copied from {sync.SOURCES[p['source']]}") + " by soulseek-playlist-sync."
    try:
        with _client(cfg, target) as api:
            # Apple Music copies only ever gain songs, so songs go in as they're matched: the playlist
            # grows while matching runs, and a copy that stops keeps what it added. A Spotify copy is
            # replaced as a whole at the end instead, since it can be a random sample.
            remote, have, pending, added = None, set(), [], 0
            if target == "apple":
                m = get(pid, target)
                if m and m["remote_id"] and api.playlist_exists(m["remote_id"]):
                    remote = m["remote_id"]
                    _busy(pid, f"Reading the {label} copy")
                    # What the copy lists, and what the app added to it: a song added in a version you
                    # already had is listed under the other version's ID. A song you remove from the
                    # copy in the Music app isn't added back.
                    have = api.playlist_song_ids(remote) | _added(pid, target)

            def flush():
                nonlocal remote, added
                if not pending:
                    return
                if remote is None:
                    remote = api.create_playlist(p["name"], desc, pending)
                    _forget_added(pid, target)          # a new playlist: what went into an old one doesn't count
                else:
                    api.add_tracks(remote, pending)
                _remember_added(pid, target, pending)
                have.update(pending)
                added += len(pending)
                pending.clear()
                # Remember the playlist and how far it got, so a restart adds to this one.
                _save(pid, target, remote, matched=len(chosen), total=len(unique))

            for i, key in enumerate(pool, 1):
                if capped and len(chosen) >= SPOTIFY_MAX:
                    break
                looked.append(key)
                link = links.get(key)
                if not (link and (link["remote_id"] or now - (link["checked_at"] or 0) < RECHECK_MISSING)):
                    if searched and len(pool) > 100:
                        time.sleep(PACE[target])
                    searched += 1
                    try:
                        found = api.find_track(songs[key]) if target == "spotify" else api.find_song(songs[key])
                    except (httpx.HTTPError, apple.AppleError, spotify.SpotifyError) as e:
                        # Even after retries one search can fail. Skip the song this time rather than
                        # stop hours of matching; it isn't remembered as missing, so an update tries again.
                        if isinstance(e, (apple.NotConnected, spotify.NotConnected)):
                            raise
                        in_a_row += 1
                        if in_a_row >= GIVE_UP:
                            raise
                        log.warning("%s search failed for %s, skipping it this time: %s", label, key, e)
                        looked.pop()
                        failed.append(key)
                        continue
                    in_a_row = 0
                    links[key] = {"remote_id": found or "", "checked_at": now}
                    with db.connect() as c:
                        c.execute("INSERT OR REPLACE INTO song_links (song_key, target, remote_id, checked_at) "
                                  "VALUES (?, ?, ?, ?)", (key, target, found or "", now))
                if links[key]["remote_id"]:
                    chosen.append(key)
                    rid = links[key]["remote_id"]
                    if target == "apple" and rid not in have and rid not in pending:
                        pending.append(rid)
                        if len(pending) >= ADD_BATCH:
                            flush()
                if i % 25 == 0:
                    _busy(pid, f"Matching on {label}: {len(chosen):,} found" if capped else
                          f"Matching on {label} ({i:,}/{len(pool):,}), {added:,} added" if target == "apple" else
                          f"Matching on {label} ({i:,}/{len(pool):,})")

            keep = set(chosen)
            ids = [links[k]["remote_id"] for k in order if k in keep]
            if p["source"] == "local":
                ids = list(dict.fromkeys(ids))      # two versions of a song can be one track on the service
            missing = [f"{songs[k]['artist']} - {songs[k]['title']}" for k in looked if not links[k]["remote_id"]]
            note = (f"Spotify playlists hold up to {SPOTIFY_MAX:,} songs, so this copy has a random {len(ids):,} "
                    f"of the {len(unique):,}. Update it to pick a new mix.") if capped else None
            if failed:
                n = len(failed)
                note = ((note + " ") if note else "") + (
                    f"{n:,} {'song' if n == 1 else 'songs'} couldn't be looked up because {label} had problems. "
                    "Update the copy to try again.")
            _busy(pid, f"Updating the {label} copy")
            if target == "spotify":
                m = get(pid, target)
                remote = m["remote_id"] if m and m["remote_id"] and api.playlist_exists(m["remote_id"]) else None
                remote = remote or api.create_playlist(p["name"], desc)
                api.set_tracks(remote, ids)
            else:
                flush()
                if remote is None:
                    remote = api.create_playlist(p["name"], desc, [])     # nothing matched: an empty copy
                    _forget_added(pid, target)
        _save(pid, target, remote, matched=len(chosen), total=len(unique), missing=missing, note=note or "")
    except (spotify.SpotifyError, apple.AppleError, httpx.HTTPError) as e:
        _save(pid, target, (get(pid, target) or {}).get("remote_id"), error=str(e)[:300])
        raise


# ------------------------------------------------------------------ songs the copy didn't find

def not_found(pid: str, target: str) -> list[dict]:
    """Songs in the playlist the service search didn't find, in playlist order. Songs you left out of a My
    music playlist aren't listed, and neither are songs whose search failed (the next update tries them)."""
    p = sync.playlist(pid)
    with db.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT s.*, MIN(e.position) AS position FROM entries e "
            "JOIN items i ON i.playlist_id = e.playlist_id AND i.song_key = e.song_key "
            "JOIN song_links l ON l.song_key = e.song_key AND l.target = ? "
            "JOIN songs s ON s.key = e.song_key "
            "WHERE e.playlist_id = ? AND l.remote_id = '' AND (? OR i.skip = 0) "
            "GROUP BY s.key ORDER BY position", (target, pid, 0 if p["source"] == "local" else 1))]


def candidates(cfg: Config, target: str, song: dict, q: str = "") -> list[dict]:
    """Songs on the service that could be this one, likeliest first. Without your own search words, it
    searches by artist and title, then by title alone, which finds a song credited to someone else."""
    from .match import artist_matches, core_title, search_artist, title_matches
    title = core_title(song.get("title"))
    terms = [q.strip()] if q.strip() else [f"{search_artist(song.get('artist'))} {title}".strip(), title]
    out, seen = [], set()
    with _client(cfg, target) as api:
        search = api.search_tracks if target == "spotify" else api.search_songs
        for term in dict.fromkeys(t for t in terms if t):
            for r in search(term):
                if r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(r)
    want = (song.get("duration_ms") or 0) / 1000
    for r in out:
        r["off"] = round((r["duration_ms"] or 0) / 1000 - want) if want and r["duration_ms"] else None
        r["same_title"] = title_matches(song.get("title"), r["title"])
        r["same_artist"] = artist_matches(song.get("artist"), r["artist"])
    out.sort(key=lambda r: (not r["same_title"], not r["same_artist"], abs(r["off"]) if r["off"] is not None else 999))
    return out


def link(cfg: Config, pid: str, target: str, key: str, remote_id: str) -> str:
    """Use the song you picked on the service. An Apple Music copy gets it right away; a Spotify copy at its
    next update, since that replaces the playlist as a whole. Returns what happened, for the page."""
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO song_links (song_key, target, remote_id, checked_at) VALUES (?, ?, ?, ?)",
                  (key, target, remote_id, db.now()))
    m = get(pid, target)
    label = TARGETS[target]
    if not (m and m["remote_id"]):
        return f"Saved. It goes in when you copy the playlist to {label}."
    if target == "spotify":
        return "Saved. It goes in at the next Update."
    if remote_id in _added(pid, target):
        return f"Saved. That song is already in the {label} copy."
    try:
        with _client(cfg, target) as api:
            api.add_tracks(m["remote_id"], [remote_id])
    except (apple.AppleError, httpx.HTTPError) as e:
        log.warning("couldn't add %s to %s: %s", remote_id, m["remote_id"], e)
        return f"Saved, but {label} didn't take it just now. It goes in at the next Update."
    _remember_added(pid, target, [remote_id])
    with db.connect() as c:
        c.execute("UPDATE mirrors SET matched = COALESCE(matched, 0) + 1 WHERE playlist_id = ? AND target = ?",
                  (pid, target))
    return f"Added to the {label} copy."


def _added(pid: str, target: str) -> set[str]:
    with db.connect() as c:
        return {r[0] for r in c.execute("SELECT song_id FROM mirror_songs WHERE playlist_id = ? AND target = ?",
                                        (pid, target))}


def _remember_added(pid: str, target: str, ids: list[str]) -> None:
    with db.connect() as c:
        c.executemany("INSERT OR IGNORE INTO mirror_songs (playlist_id, target, song_id) VALUES (?, ?, ?)",
                      [(pid, target, i) for i in ids])


def _forget_added(pid: str, target: str) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM mirror_songs WHERE playlist_id = ? AND target = ?", (pid, target))


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
