"""Keep Spotify Liked Songs and Apple Music Favorite Songs in step.

Each direction is switched on separately. The sync only adds: it carries a song across once and
remembers the pair (favorite_pairs), so a song you later remove on either service stays removed.
A check works out what a sync would add without changing anything except the match cache.

Matching reuses song_links, the same cache playlist copies use. Before searching, a song is compared
with the other side's favorites by ISRC and by normalized title and artist, so a song that's already a
favorite on both services under different versions isn't added twice.
"""
import functools
import json
import logging
import threading
import time

from . import apple, db, spotify
from .config import Config
from .match import norm, norm_title, primary_artist

log = logging.getLogger(__name__)
OPTIONS_KEY = "favorites_options"
STATUS_KEY = "favorites_status"
RECHECK_MISSING = 7 * 86400
PACE = {"spotify": 0.35, "apple": 0.15}      # seconds between searches when many songs need matching
LIST_MAX = 2000                              # names kept for the page's lists
_locks: dict[int, threading.Lock] = {}      # one sync at a time per profile


class Busy(Exception):
    pass


def _key(base: str, profile: int) -> str:
    """Each profile's options and status. The first profile keeps the keys from before profiles."""
    return base if profile == 1 else f"{base}:{profile}"


def kv_keys(profile: int) -> list[str]:
    return [_key(OPTIONS_KEY, profile), _key(STATUS_KEY, profile)]


def _lock(profile: int) -> threading.Lock:
    return _locks.setdefault(profile, threading.Lock())


def options(profile: int) -> dict:
    o = {"to_apple": False, "to_spotify": False, "auto": False}
    o.update(json.loads(db.get_kv(_key(OPTIONS_KEY, profile)) or "{}"))
    return o


def save_options(profile: int, to_apple: bool, to_spotify: bool, auto: bool) -> None:
    db.set_kv(_key(OPTIONS_KEY, profile), json.dumps({"to_apple": to_apple, "to_spotify": to_spotify, "auto": auto}))


def status(profile: int) -> dict:
    s = json.loads(db.get_kv(_key(STATUS_KEY, profile)) or "{}")
    if s.get("state") == "running" and not _lock(profile).locked():
        s["state"] = "stopped"          # the app restarted during a run
    return s


def _set_status(profile: int, **kw) -> None:
    s = json.loads(db.get_kv(_key(STATUS_KEY, profile)) or "{}")
    s.update(kw)
    db.set_kv(_key(STATUS_KEY, profile), json.dumps(s))


def running(profile: int) -> bool:
    return _lock(profile).locked()


def pair_count(profile: int) -> int:
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) FROM favorite_pairs WHERE profile = ?", (profile,)).fetchone()[0]


def _clients(cfg: Config):
    return spotify.Client(cfg), apple.Client(cfg)


def _label(s: dict) -> str:
    return f"{s.get('artist') or '?'} - {s.get('title') or '?'}"


def _id(s: dict) -> str:
    return s["key"].split(":", 1)[1]


def _same_key(s: dict) -> tuple[str, str]:
    return norm_title(s.get("title")), norm(primary_artist(s.get("artist")))


class _Side:
    """One service's favorites, indexed for spotting the same song on the other side."""

    def __init__(self, songs: list[dict]):
        self.songs = songs
        self.ids = {_id(s) for s in songs}
        self.by_isrc = {s["isrc"].upper(): _id(s) for s in songs if s.get("isrc")}
        self.by_name = {_same_key(s): _id(s) for s in songs}

    def same(self, s: dict) -> str | None:
        if s.get("isrc") and s["isrc"].upper() in self.by_isrc:
            return self.by_isrc[s["isrc"].upper()]
        return self.by_name.get(_same_key(s))


def run(cfg: Config, apply: bool) -> dict:
    """Check (apply=False) or sync (apply=True) the directions that are switched on."""
    lock = _lock(cfg.profile)
    if not lock.acquire(blocking=False):
        raise Busy("A favorites sync is already running")
    try:
        return _run(cfg, apply)
    except Exception as e:
        log.exception("favorites sync failed")
        _set_status(cfg.profile, state="error", error=str(e)[:500], finished=time.time())
        raise
    finally:
        lock.release()


def _run(cfg: Config, apply: bool) -> dict:
    me = cfg.profile
    _status = functools.partial(_set_status, me)
    opts = options(me)
    if not (opts["to_apple"] or opts["to_spotify"]):
        raise ValueError("Choose at least one direction to sync")
    if apply and opts["to_spotify"] and not spotify.can_save_likes(cfg):
        raise ValueError("Reconnect Spotify so the app can add songs to your Liked Songs")
    db.set_kv(_key(STATUS_KEY, me), json.dumps({"state": "running", "mode": "sync" if apply else "check",
                                      "started": time.time(), "step": "Reading your Spotify Liked Songs"}))
    sp, am = _clients(cfg)
    with sp, am:
        liked = _Side(sp.playlist_tracks(spotify.LIKED))
        _status(step="Reading your Apple Music Favorite Songs")
        favs = _Side(am.favorite_songs())
        with db.connect() as c:
            pairs = [(r["spotify_id"], r["apple_id"]) for r in c.execute("SELECT spotify_id, apple_id FROM favorite_pairs WHERE profile = ?", (me,))]
            links = {(r["song_key"], r["target"]): dict(r) for r in c.execute("SELECT * FROM song_links")}
        paired_sp = {p[0] for p in pairs}
        paired_am = {p[1] for p in pairs}
        new_pairs: list[tuple[str, str, str | None]] = []

        def pair(sid: str, cid: str, added_to: str | None = None) -> None:
            paired_sp.add(sid)
            paired_am.add(cid)
            new_pairs.append((sid, cid, added_to))

        def match(song: dict, target: str, api, n: int) -> str | None:
            key = song["key"]
            link = links.get((key, target))
            now = db.now()
            if link and (link["remote_id"] or now - (link["checked_at"] or 0) < RECHECK_MISSING):
                return link["remote_id"] or None
            if n > 100:
                time.sleep(PACE[target])
            found = api.find_track(song) if target == "spotify" else api.find_song(song)
            links[(key, target)] = {"remote_id": found or "", "checked_at": now}
            with db.connect() as c:
                c.execute("INSERT OR REPLACE INTO song_links (song_key, target, remote_id, checked_at) VALUES (?, ?, ?, ?)",
                          (key, target, found or "", now))
            return found

        out = {"liked": len(liked.songs), "favorites": len(favs.songs), "to_apple": [], "to_spotify": [],
               "missing_apple": [], "missing_spotify": []}
        plan_apple: list[tuple[str, str]] = []       # (spotify id, apple catalog id)
        plan_spotify: list[tuple[str, str]] = []     # (spotify id, apple catalog id)

        if opts["to_apple"]:
            todo = [s for s in liked.songs if _id(s) not in paired_sp]
            for i, s in enumerate(todo, 1):
                if i % 25 == 0:
                    _status(step=f"Matching Spotify Liked Songs on Apple Music ({i:,}/{len(todo):,})")
                sid = _id(s)
                cid = favs.same(s) or match(s, "apple", am, len(todo))
                if not cid:
                    out["missing_apple"].append(_label(s))
                elif cid in favs.ids or cid in paired_am:
                    pair(sid, cid)
                else:
                    plan_apple.append((sid, cid))
                    out["to_apple"].append(_label(s))
                    paired_sp.add(sid)
                    paired_am.add(cid)

        if opts["to_spotify"]:
            todo = [s for s in favs.songs if _id(s) not in paired_am]
            for i, s in enumerate(todo, 1):
                if i % 25 == 0:
                    _status(step=f"Matching Apple Music Favorite Songs on Spotify ({i:,}/{len(todo):,})")
                cid = _id(s)
                sid = liked.same(s)
                if not sid:
                    uri = match(s, "spotify", sp, len(todo))
                    sid = uri.rsplit(":", 1)[1] if uri else None
                if not sid:
                    out["missing_spotify"].append(_label(s))
                elif sid in liked.ids or sid in paired_sp:
                    pair(sid, cid)
                else:
                    plan_spotify.append((sid, cid))
                    out["to_spotify"].append(_label(s))
                    paired_sp.add(sid)
                    paired_am.add(cid)

        # Songs that are favorites on both services now, counted once each (two versions of a song on one
        # service can pair with the same song on the other).
        out["in_sync"] = len({a for s, a, *_ in pairs + new_pairs if a in favs.ids and s in liked.ids})
        out["added_apple"] = out["added_spotify"] = 0
        if apply:
            if plan_apple:
                _status(step=f"Adding {len(plan_apple):,} songs to Apple Music Favorite Songs")
                done = set(am.add_favorites([c for _, c in plan_apple], progress=lambda n: _status(
                    step=f"Adding to Apple Music Favorite Songs ({n:,}/{len(plan_apple):,})")))
                for sid, cid in plan_apple:
                    if cid in done:
                        new_pairs.append((sid, cid, "apple"))
                out["added_apple"] = len(done)
            if plan_spotify:
                _status(step=f"Adding {len(plan_spotify):,} songs to Spotify Liked Songs")
                sp.save_likes([f"spotify:track:{sid}" for sid, _ in plan_spotify], progress=lambda n: _status(
                    step=f"Adding to Spotify Liked Songs ({n:,}/{len(plan_spotify):,})"))
                new_pairs += [(sid, cid, "spotify") for sid, cid in plan_spotify]
                out["added_spotify"] = len(plan_spotify)
            with db.connect() as c:
                c.executemany("INSERT OR IGNORE INTO favorite_pairs (profile, spotify_id, apple_id, added_to, at) "
                              "VALUES (?, ?, ?, ?, ?)", [(me, s, a, t, time.time()) for s, a, t in new_pairs])
    for k in ("to_apple", "to_spotify", "missing_apple", "missing_spotify"):
        out[f"{k}_count"] = len(out[k])
        out[k] = sorted(out[k], key=str.casefold)[:LIST_MAX]
    out.update(state="done", finished=time.time(), step=None, error=None)
    _status(**out)
    log.info("favorites %s: %s", "sync" if apply else "check",
             {k: v for k, v in out.items() if not isinstance(v, list)})
    return out


def start(cfg: Config, apply: bool) -> None:
    """Run in the background; the page shows progress from the status."""
    if running(cfg.profile):
        raise Busy("A favorites sync is already running")

    def go():
        try:
            run(cfg, apply)
        except Busy:
            pass
        except Exception:
            pass                       # recorded in the status
    threading.Thread(target=go, daemon=True, name="favorites").start()


def scheduled(cfg: Config) -> None:
    """The worker's periodic run: sync each profile that asked for it to run on its own, one after another."""
    from . import profiles
    for p in profiles.all_profiles():
        pcfg = cfg.for_profile(p["id"])
        o = options(p["id"])
        if o["auto"] and (o["to_apple"] or o["to_spotify"]) and spotify.connected(pcfg) and apple.user_token(pcfg):
            try:
                run(pcfg, apply=True)
            except Exception:
                pass
