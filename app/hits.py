"""Hits playlists: the standout songs of each album, picked from what listeners, the charts and you favor.

Two halves, so a playlist rebuild never waits on the network:

- research() looks songs up and keeps what it learns in the hit_* tables. It only fetches what's
  missing or stale, so after the first run it costs little: new albums, and a slice of old counts
  each time they come due.
- plan() and collection() pick a playlist's songs from those tables. They make no requests.

Signals, by song:

- ListenBrainz listeners. Compared with the album's median, so a band with 300 listeners per song
  ranks its songs as well as one with 300,000. The count for a song is the most for any recording
  of that title by the artist, because listens collect on one version and a file can carry another.
- Genius page views, compared the same way. They lean toward hip-hop and pop, where ListenBrainz is thin.
- MusicBrainz singles: a song released as a single within two years of its album.
- Billboard Hot 100 peak and weeks, 1958 on.
- Your Apple Music Favorite Songs and Spotify Liked Songs, which always go in.
"""
import csv
import io
import json
import logging
import re
import statistics
import threading
import time
from collections import defaultdict

import httpx

from . import apple, db, library, spotify
from .config import Config
from .match import artist_matches, credited_names, norm, norm_title, primary_artist, title_matches

log = logging.getLogger(__name__)

DEFAULTS = {"min": 2, "max": 5, "cutoff": 1.25}
STATUS_KEY = "hits_status"
UA = "soulseek-playlist-sync/1.0 ( https://github.com/averagenative/soulseek-playlist-sync )"
CHARTS_URL = "https://raw.githubusercontent.com/utdata/rwd-billboard-data/main/data-out/hot-100-current.csv"
LB = "https://api.listenbrainz.org/1"
MB = "https://musicbrainz.org/ws/2"
GENIUS = "https://api.genius.com"

CHARTS_EVERY = 7 * 86400          # a new Hot 100 every week
STATS_EVERY = 90 * 86400          # listener counts, page views
SINGLES_EVERY = 180 * 86400
RETRY_IDS = 30 * 86400            # a song no lookup could identify is tried again after this
MB_PACE = 1.1                     # MusicBrainz allows one request a second
GENIUS_PACE = 0.25
GENIUS_PAGES = 4                  # 50 songs a page, most viewed first
GENIUS_RESERVE = 50               # requests left in Genius's daily allowance that research leaves alone
LB_BATCH = 500
MIN_LISTENERS = 30                # an album whose median is lower has too little data to compare
MIN_VIEWS = 300
SHORT = 90                        # seconds: shorter songs are skits and intros
SINGLE_YEARS = 2                  # a single counts when it came out this close to the album

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Not songs: skits and the like, whatever their length.
_JUNK = re.compile(r"^\W*(intro|outro|interlude|skit|prelude|segue|untitled|hidden track|silence)\b"
                   r"|\b(skit|interlude)\b", re.I)
# Other versions of a song: left out so the original gets the place.
_VERSION = re.compile(r"[\(\[][^\)\]]*\b(live|demo|instrumental|a ?cappella|acapella|karaoke|commentary|interview|"
                      r"rehearsal|remix|remixed)\b[^\)\]]*[\)\]]"
                      r"|\s-\s[^-]*\b(live|demo|instrumental|remix|remixed)\b", re.I)
_COMPILATION = re.compile(r"\b(greatest hits|best of|the best|essential|anthology|collection|retrospective|"
                          r"definitive|the singles|hits)\b", re.I)
_VARIOUS = {"various artists", "various", "va"}
BASE_KINDS = {"album", "ep", "soundtrack"}        # kinds that always get min songs
KIND_ORDER = {"album": 0, "ep": 1, "soundtrack": 2, "other": 3, "single": 4, "compilation": 5, "live": 6}

_lock = threading.Lock()


class GeniusRefused(Exception):
    pass


class StepStopped(Exception):
    """One research step can't go on (a service refused, or kept failing). The other steps still run."""


MAX_ERRORS = 10         # errors in a row before a step gives up for this run


# ------------------------------------------------------------------ options and status

def options(rule: dict | None) -> dict | None:
    """A hits playlist's options, or None for any other playlist."""
    h = (rule or {}).get("hits")
    if h is None:
        return None
    o = dict(DEFAULTS)
    o.update({k: v for k, v in h.items() if k in DEFAULTS})
    o["min"] = max(0, min(int(o["min"]), 10))
    o["max"] = max(o["min"], min(int(o["max"]), 20))
    o["cutoff"] = max(1.0, min(float(o["cutoff"]), 5.0))
    o["whole"] = sorted({str(x) for x in h.get("whole") or [] if x})   # albums you want all of (album_id)
    return o


def album_id(artist: str | None, album_artist: str | None, album: str | None) -> str:
    """The key an album goes by in a hits playlist: its artist and title, normalized."""
    return f"{norm(album_artist or artist)}|{norm(album)}" if album else f"{norm(artist)}|"


def album_of(song_key: str) -> str | None:
    """The album a song is picked from in a hits playlist, from the song as the playlist lists it."""
    with db.connect() as c:
        s = c.execute("SELECT artist, album_artist, album FROM songs WHERE key = ?", (song_key,)).fetchone()
    return album_id(s["artist"], s["album_artist"], s["album"]) if s else None


def status() -> dict:
    return json.loads(db.get_kv(STATUS_KEY) or "{}")


def _set_status(**kw) -> None:
    s = status()
    s.update(kw)
    db.set_kv(STATUS_KEY, json.dumps(s))


def running() -> bool:
    return _lock.locked()


def playlists() -> list[dict]:
    with db.connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM playlists WHERE source = 'local' AND rule IS NOT NULL")]
    return [p for p in rows if options(json.loads(p["rule"])) is not None]


def cache_counts() -> dict:
    with db.connect() as c:
        one = lambda sql: c.execute(sql).fetchone()[0]
        return {
            "recordings": one("SELECT COUNT(*) FROM hit_recordings WHERE listeners IS NOT NULL"),
            "artists": one("SELECT COUNT(DISTINCT artist) FROM hit_artists WHERE source = 'listenbrainz'"),
            "singles": one("SELECT COUNT(*) FROM hit_artist_songs WHERE source = 'single'"),
            "genius": one("SELECT COUNT(DISTINCT artist) FROM hit_artists WHERE source = 'genius' AND found = 1"),
            "charts": one("SELECT COUNT(*) FROM hit_charts"),
            "favorites": one("SELECT COUNT(*) FROM hit_favorites"),
            "identified": one("SELECT COUNT(*) FROM hit_ids WHERE recording != ''"),
            "unidentified": one("SELECT COUNT(*) FROM hit_ids WHERE recording = ''"),
        }


# ------------------------------------------------------------------ research

def start(cfg: Config) -> bool:
    """Look up what's missing, in the background, then rebuild the hits playlists. False if a run is going."""
    if not _lock.acquire(blocking=False):
        return False

    def target():
        try:
            research(cfg)
        finally:
            _lock.release()

    threading.Thread(target=target, daemon=True, name="hits-research").start()
    return True


def start_if_needed(cfg: Config) -> bool:
    """For the scheduled refresh: research only when there's a hits playlist to serve."""
    return bool(playlists()) and start(cfg)


def research(cfg: Config) -> None:
    _set_status(state="running", started=time.time(), finished=None, error=None, notes=[], step="Starting",
                done=0, total=0)
    http = _Http(cfg)
    notes: list[str] = []

    def step(fn, *args):
        """Run one step. If it stops, say why and go on with the rest."""
        try:
            return fn(*args)
        except StepStopped as e:
            log.warning("hits: %s", e)
            notes.append(str(e))
        except Exception as e:
            log.exception("hits: %s failed", fn.__name__)
            notes.append(f"A step stopped: {str(e)[:200]}")
        _set_status(notes=notes)
        return None

    try:
        step(_charts, http)
        step(_favorites, cfg)
        step(_recordings, http)
        _rebuild(cfg)
        step(_artist_lists, http, "listenbrainz")
        _rebuild(cfg)
        step(_artist_lists, http, "single")
        _rebuild(cfg)
        if cfg.genius_token:
            step(_genius, http)
            _rebuild(cfg)
        if step(_identify, http):
            step(_recordings, http)
            step(_artist_lists, http, "listenbrainz")
            step(_artist_lists, http, "single")
        _set_status(state="done", finished=time.time(), step=None, done=0, total=0, notes=notes)
    except Exception as e:
        log.exception("hits research failed")
        _set_status(state="failed", finished=time.time(), error=str(e)[:300], notes=notes)
    finally:
        http.close()
        _rebuild(cfg)


def _rebuild(cfg: Config) -> None:
    from . import sync          # sync imports this module
    for p in playlists():
        sync.run_job(p["id"], "Picking songs", sync.refresh_local, cfg, p["id"], False)


class _Http:
    """One client per research run, keeping each service's pace and rate limits."""

    def __init__(self, cfg: Config):
        self.c = httpx.Client(timeout=30, headers={"User-Agent": UA}, follow_redirects=True)
        self.genius_token = cfg.genius_token
        self.lb_token = cfg.listenbrainz_token
        self.genius_left: int | None = None
        self._next_mb = 0.0
        self._next_genius = 0.0

    def close(self):
        self.c.close()

    def send(self, method: str, url: str, **kw) -> httpx.Response:
        r = None
        for attempt in range(5):
            try:
                r = self.c.request(method, url, **kw)
            except httpx.TransportError:
                if attempt == 4:
                    raise
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (429, 502, 503, 504):
                wait = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset-In")
                try:
                    wait = float(wait)
                except (TypeError, ValueError):
                    wait = 5.0 * (attempt + 1)
                time.sleep(min(max(wait, 1.0), 120.0))
                continue
            return r
        return r

    def lb(self, method: str, path: str, **kw):
        if self.lb_token:
            kw["headers"] = {**kw.get("headers", {}), "Authorization": f"Token {self.lb_token}"}
        r = self.send(method, LB + path, **kw)
        if r.status_code in (401, 403):
            raise StepStopped(
                "ListenBrainz refused the token in LISTENBRAINZ_TOKEN." if self.lb_token else
                "ListenBrainz asks for a user token for this. Set LISTENBRAINZ_TOKEN to add it "
                "(it's on https://listenbrainz.org/settings/).")
        try:
            if int(r.headers.get("X-RateLimit-Remaining", "9")) <= 1:
                time.sleep(float(r.headers.get("X-RateLimit-Reset-In", "1")) + 0.5)
        except ValueError:
            pass
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return r.json()

    def mb(self, path: str, params: dict):
        wait = self._next_mb - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_mb = time.monotonic() + MB_PACE
        r = self.send("GET", MB + path, params={**params, "fmt": "json"})
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return r.json()

    def genius(self, path: str, params: dict):
        wait = self._next_genius - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_genius = time.monotonic() + GENIUS_PACE
        r = self.send("GET", GENIUS + path, params=params, headers={"Authorization": f"Bearer {self.genius_token}"})
        left = r.headers.get("x-ratelimit-remaining")
        if left and left.isdigit():
            self.genius_left = int(left)
        if r.status_code in (401, 403):
            raise GeniusRefused(f"Genius refused the token ({r.status_code})")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("response") or {}


def _parts(title: str) -> list[str]:
    """'Dear Mama/Old School' -> both songs. '24/7' stays whole."""
    parts = [x.strip() for x in title.split("/")]
    if len(parts) > 1 and all(re.search(r"[A-Za-z]", x) for x in parts):
        return parts
    return [title.strip()]


def _songs() -> dict[str, dict]:
    """Each library song once: its names, its files' recording IDs, its artist ID."""
    out: dict[str, dict] = {}
    with db.connect() as c:
        rows = c.execute("SELECT song_key, artist, title, album, duration, isrc, mb_recording, mb_artist "
                         "FROM library WHERE song_key IS NOT NULL AND title IS NOT NULL")
        for r in rows:
            s = out.setdefault(r["song_key"], {"key": r["song_key"], "artist": r["artist"], "title": r["title"],
                                               "album": r["album"], "duration": r["duration"], "isrc": None,
                                               "recordings": set(), "artists": set()})
            if r["isrc"] and not s["isrc"]:
                s["isrc"] = r["isrc"]
            if r["mb_recording"] and _UUID.match(r["mb_recording"]):
                s["recordings"].add(r["mb_recording"])
            if r["mb_artist"] and _UUID.match(r["mb_artist"]):
                s["artists"].add(r["mb_artist"])
        for r in c.execute("SELECT * FROM hit_ids WHERE recording != ''"):
            s = out.get(r["song_key"])
            if s:
                s["recordings"].add(r["recording"])
                if r["artist"]:
                    s["artists"].add(r["artist"])
    return out


def _step(step: str, done: int = 0, total: int = 0) -> None:
    _set_status(step=step, done=done, total=total)


def _charts(http: _Http) -> None:
    with db.connect() as c:
        have = c.execute("SELECT COUNT(*) FROM hit_charts").fetchone()[0]
    if have and time.time() - float(db.get_kv("hits_charts_at") or 0) < CHARTS_EVERY:
        return
    _step("Downloading the Billboard Hot 100 history")
    etag = db.get_kv("hits_charts_etag")
    r = http.send("GET", CHARTS_URL, headers={"If-None-Match": etag} if etag and have else {})
    if r.status_code == 304:
        db.set_kv("hits_charts_at", str(time.time()))
        return
    r.raise_for_status()
    best: dict[tuple[str, str], list] = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            pos = int(row["current_week"])
        except (KeyError, ValueError):
            continue
        for part in _parts(row.get("title") or ""):
            k = (norm_title(part), (row.get("performer") or "").strip())
            if not k[0] or not k[1]:
                continue
            b = best.get(k)
            if b is None:
                best[k] = [pos, 1, row.get("chart_week")]
            else:
                b[0], b[1] = min(b[0], pos), b[1] + 1
                b[2] = min(b[2] or "", row.get("chart_week") or "") or b[2]
    with db.connect() as c:
        c.execute("DELETE FROM hit_charts")
        c.executemany("INSERT INTO hit_charts (title, performer, peak, weeks, first_week) VALUES (?, ?, ?, ?, ?)",
                      [(t, p, v[0], v[1], v[2]) for (t, p), v in best.items()])
    db.set_kv("hits_charts_at", str(time.time()))
    db.set_kv("hits_charts_etag", r.headers.get("ETag"))
    log.info("hits: %d chart songs", len(best))


def _favorites(cfg: Config) -> None:
    """Your favorites on both services, as library song keys. Kept as they were if neither can be read."""
    keys, read = set(), False
    sources = []
    if apple.configured(cfg) and apple.user_token(cfg):
        sources.append(("Apple Music Favorite Songs", lambda: _read(apple.Client(cfg), lambda a: a.favorite_songs())))
    if spotify.connected(cfg):
        sources.append(("Spotify Liked Songs",
                        lambda: _read(spotify.Client(cfg), lambda s: s.playlist_tracks(spotify.LIKED))))
    for label, fn in sources:
        _step(f"Reading your {label}")
        try:
            for s in fn():
                if s.get("artist") and s.get("title"):
                    keys.add(library.song_key(s["artist"], s["title"], ""))
            read = True
        except Exception as e:
            log.warning("hits: couldn't read %s: %s", label, e)
    if read:
        with db.connect() as c:
            c.execute("DELETE FROM hit_favorites")
            c.executemany("INSERT OR IGNORE INTO hit_favorites (song_key) VALUES (?)", [(k,) for k in keys])


def _read(client, fn):
    with client as api:
        return fn(api)


def _recordings(http: _Http) -> None:
    ids = set().union(*(s["recordings"] for s in _songs().values()))
    cutoff = time.time() - STATS_EVERY
    with db.connect() as c:
        fresh = {r[0] for r in c.execute("SELECT mbid FROM hit_recordings WHERE fetched_at > ?", (cutoff,))}
    todo = sorted(ids - fresh)
    for i in range(0, len(todo), LB_BATCH):
        _step("Getting listener counts from ListenBrainz", i, len(todo))
        batch = todo[i:i + LB_BATCH]
        res = http.lb("POST", "/popularity/recording", json={"recording_mbids": batch}) or []
        t = time.time()
        got = {r.get("recording_mbid"): r for r in res if isinstance(r, dict)}
        with db.connect() as c:
            c.executemany(
                "INSERT INTO hit_recordings (mbid, listeners, listens, fetched_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(mbid) DO UPDATE SET listeners = excluded.listeners, listens = excluded.listens, "
                "fetched_at = excluded.fetched_at",
                [(m, (got.get(m) or {}).get("total_user_count"), (got.get(m) or {}).get("total_listen_count"), t)
                 for m in batch])


def _artist_ids() -> list[str]:
    return sorted(set().union(*(s["artists"] for s in _songs().values())))


def _artist_lists(http: _Http, source: str) -> None:
    every = SINGLES_EVERY if source == "single" else STATS_EVERY
    fetch = _mb_singles if source == "single" else _lb_artist
    label = ("Looking up singles on MusicBrainz" if source == "single"
             else "Getting each artist's listener counts from ListenBrainz")
    with db.connect() as c:
        fresh = {r[0] for r in c.execute("SELECT artist FROM hit_artists WHERE source = ? AND fetched_at > ?",
                                         (source, time.time() - every))}
    todo = [a for a in _artist_ids() if a not in fresh]
    if source == "listenbrainz" and todo and not http.lb_token:
        raise StepStopped("ListenBrainz asks for a user token for each artist's counts. Set LISTENBRAINZ_TOKEN "
                          "to add them (it's on https://listenbrainz.org/settings/).")
    errors = 0
    for i, a in enumerate(todo):
        if i % 10 == 0:
            _step(label, i, len(todo))
        try:
            rows = fetch(http, a)
        except (httpx.HTTPError, ValueError) as e:      # this artist again next run
            errors += 1
            if errors >= MAX_ERRORS:
                raise StepStopped(f"{label} stopped after {MAX_ERRORS} errors in a row: {str(e)[:150]}") from e
            continue
        errors = 0
        _save_artist(source, a, rows)


def _save_artist(source: str, artist: str, rows: dict[str, int | None]) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM hit_artist_songs WHERE source = ? AND artist = ?", (source, artist))
        c.executemany("INSERT INTO hit_artist_songs (source, artist, title, value) VALUES (?, ?, ?, ?)",
                      [(source, artist, t, v) for t, v in rows.items() if t])
        c.execute("INSERT INTO hit_artists (source, artist, fetched_at, found) VALUES (?, ?, ?, ?) "
                  "ON CONFLICT(source, artist) DO UPDATE SET fetched_at = excluded.fetched_at, found = excluded.found",
                  (source, artist, time.time(), 1 if rows else 0))


def _lb_artist(http: _Http, mbid: str) -> dict[str, int]:
    """Title -> the most listeners for any of the artist's recordings with that title."""
    res = http.lb("GET", f"/popularity/top-recordings-for-artist/{mbid}") or []
    out: dict[str, int] = {}
    for r in res if isinstance(res, list) else []:
        t, n = norm_title(r.get("recording_name")), r.get("total_user_count") or 0
        if t and n > out.get(t, 0):
            out[t] = n
    return out


def _mb_singles(http: _Http, mbid: str) -> dict[str, int | None]:
    """Title -> the year it first came out as a single. Live, remix and DJ-mix singles don't count, and
    of a single with more than two songs only the first does."""
    out: dict[str, int | None] = {}
    offset = 0
    while True:
        d = http.mb("/release-group", {"artist": mbid, "type": "single", "limit": 100, "offset": offset})
        if not d:
            break
        for g in d.get("release-groups") or []:
            if set(g.get("secondary-types") or []) - {"Soundtrack"}:
                continue
            date = (g.get("first-release-date") or "")[:4]
            year = int(date) if date.isdigit() else None
            parts = _parts(g.get("title") or "")
            for part in parts[:1] if len(parts) > 2 else parts:
                t = norm_title(part)
                if t and (t not in out or (year and (out[t] is None or year < out[t]))):
                    out[t] = year
        offset += 100
        if offset >= (d.get("release-group-count") or 0) or offset >= 500:
            break
    return out


def _genius(http: _Http) -> None:
    """Page views of each artist's most viewed songs on Genius, while the daily allowance lasts."""
    artists: dict[str, dict] = {}
    for s in _songs().values():
        name = norm(primary_artist(s["artist"]))
        if name and name not in _VARIOUS:
            a = artists.setdefault(name, {"artist": primary_artist(s["artist"]), "titles": []})
            if len(a["titles"]) < 3:
                a["titles"].append(s["title"])
    with db.connect() as c:
        fresh = {r[0] for r in c.execute("SELECT artist FROM hit_artists WHERE source = 'genius' AND fetched_at > ?",
                                         (time.time() - STATS_EVERY,))}
    todo = sorted(n for n in artists if n not in fresh)
    errors = 0
    for i, name in enumerate(todo):
        if http.genius_left is not None and http.genius_left < GENIUS_RESERVE:
            log.info("hits: Genius allowance used up for today; %d artists left for the next run", len(todo) - i)
            break
        if i % 10 == 0:
            _step("Getting page views from Genius", i, len(todo))
        try:
            rows = _genius_artist(http, artists[name])
        except GeniusRefused as e:
            raise StepStopped(str(e)) from e
        except (httpx.HTTPError, ValueError) as e:
            errors += 1
            if errors >= MAX_ERRORS:
                raise StepStopped(f"Genius stopped after {MAX_ERRORS} errors in a row: {str(e)[:150]}") from e
            continue
        errors = 0
        _save_artist("genius", name, rows)


def _genius_artist(http: _Http, a: dict) -> dict[str, int]:
    aid = None
    for title in a["titles"][:2]:
        res = http.genius("/search", {"q": f"{a['artist']} {title}"}) or {}
        for h in res.get("hits") or []:
            pa = (h.get("result") or {}).get("primary_artist") or {}
            if pa.get("id") and norm(pa.get("name")) == norm(a["artist"]):
                aid = pa["id"]
                break
        if aid:
            break
    out: dict[str, int] = {}
    if not aid:
        return out
    for page in range(1, GENIUS_PAGES + 1):
        d = http.genius(f"/artists/{aid}/songs", {"sort": "popularity", "per_page": 50, "page": page}) or {}
        songs = d.get("songs") or []
        views = 0
        for s in songs:
            views = (s.get("stats") or {}).get("pageviews") or 0
            if (s.get("primary_artist") or {}).get("id") != aid or not views:
                continue
            t = norm_title(s.get("title"))
            out[t] = max(out.get(t, 0), views)
        if not d.get("next_page") or views < MIN_VIEWS:
            break
    return out


def _identify(http: _Http) -> int:
    """MusicBrainz IDs for songs whose files carry none: by ISRC, then by searching. Returns how many it found."""
    cutoff = time.time() - RETRY_IDS
    with db.connect() as c:
        checked = {r[0] for r in c.execute("SELECT song_key FROM hit_ids WHERE checked_at > ?", (cutoff,))}
    todo = [s for s in _songs().values() if not s["recordings"] and s["key"] not in checked and s["artist"]]
    found = errors = 0
    for i, s in enumerate(todo):
        if i % 10 == 0:
            _step("Identifying songs on MusicBrainz", i, len(todo))
        rec, art, how = None, None, None
        try:
            if s["isrc"]:
                d = http.mb(f"/isrc/{s['isrc']}", {"inc": "artist-credits"}) or {}
                rec, art = _best_recording(s, d.get("recordings") or [], need_title=False)
                how = "isrc" if rec else None
            if not rec:
                q = f'recording:"{_lucene(s["title"])}" AND artist:"{_lucene(primary_artist(s["artist"]))}"'
                d = http.mb("/recording", {"query": q, "limit": 10}) or {}
                rec, art = _best_recording(s, d.get("recordings") or [])
                how = "search" if rec else None
        except (httpx.HTTPError, ValueError) as e:      # this song again next run
            errors += 1
            if errors >= MAX_ERRORS:
                raise StepStopped(f"Identifying songs on MusicBrainz stopped after {MAX_ERRORS} errors in a row: "
                                  f"{str(e)[:150]}") from e
            continue
        errors = 0
        found += 1 if rec else 0
        with db.connect() as c:
            c.execute("INSERT INTO hit_ids (song_key, recording, artist, how, checked_at) VALUES (?, ?, ?, ?, ?) "
                      "ON CONFLICT(song_key) DO UPDATE SET recording = excluded.recording, artist = excluded.artist, "
                      "how = excluded.how, checked_at = excluded.checked_at",
                      (s["key"], rec or "", art or "", how, time.time()))
    return found


def _lucene(s: str) -> str:
    return re.sub(r'([\\"])', r"\\\1", s or "")


def _credit(r: dict) -> tuple[str, str | None]:
    credits = r.get("artist-credit") or []
    name = "".join((x.get("name") or "") + (x.get("joinphrase") or "") for x in credits)
    first = ((credits[0] if credits else {}).get("artist") or {}).get("id")
    return name, first


def _best_recording(s: dict, recs: list[dict], need_title: bool = True) -> tuple[str | None, str | None]:
    """The recording that is this song: same title and artist, closest in length."""
    best, best_off = None, None
    for r in recs:
        name, first = _credit(r)
        if need_title and not title_matches(r.get("title"), s["title"]):
            continue
        if not artist_matches(s["artist"], name):
            continue
        off = abs((r.get("length") or 0) / 1000 - (s["duration"] or 0)) if r.get("length") and s["duration"] else 30
        if off > 15 and r.get("length") and s["duration"]:
            continue
        if best_off is None or off < best_off:
            best, best_off = (r.get("id"), first), off
    return best if best else (None, None)


# ------------------------------------------------------------------ picking

def _kind(release_type: str | None, album: str | None, album_artist: str | None) -> str:
    parts = {x.strip() for x in (release_type or "").split(";") if x.strip()}
    if "live" in parts:
        return "live"
    if "compilation" in parts:
        return "compilation"
    if parts & {"remix", "dj-mix"}:
        return "other"
    if "soundtrack" in parts:
        return "soundtrack"
    if "single" in parts:
        return "single"
    if "ep" in parts:
        return "ep"
    if "album" in parts or parts & {"mixtape/street", "broadcast", "other"}:
        return "album"
    if norm(album_artist) in _VARIOUS or _COMPILATION.search(album or ""):
        return "compilation"
    return "album"


def _excluded(title: str, duration: float | None) -> str | None:
    if _JUNK.search(title or ""):
        return "Skit or interlude"
    if _VERSION.search(title or ""):
        return "Live, demo or remix"
    if duration and duration < SHORT:
        return "Under 90 seconds"
    return None


def _lift(values: dict[str, int | None], floor: int) -> dict[str, float]:
    """Each song's count over the album's median, when enough of the album has counts."""
    have = [v for v in values.values() if v]
    if len(have) < 2 or len(have) * 2 < len(values):
        return {}
    med = statistics.median(have)
    if med < floor:
        return {}
    return {k: v / med for k, v in values.items() if v}


def _load_stats() -> dict:
    with db.connect() as c:
        recs = {r[0]: r[1] for r in c.execute("SELECT mbid, listeners FROM hit_recordings WHERE listeners IS NOT NULL")}
        lists: dict[str, dict] = defaultdict(dict)
        for r in c.execute("SELECT source, artist, title, value FROM hit_artist_songs"):
            lists[r[0]][(r[1], r[2])] = r[3]
        charts: dict[str, list] = defaultdict(list)
        for r in c.execute("SELECT title, performer, peak, weeks FROM hit_charts"):
            charts[r[0]].append((r[1], r[2], r[3]))
        favorites = {r[0] for r in c.execute("SELECT song_key FROM hit_favorites")}
        ids = {r[0]: (r[1], r[2]) for r in c.execute("SELECT song_key, recording, artist FROM hit_ids WHERE recording != ''")}
    return {"recs": recs, "lists": lists, "charts": charts, "favorites": favorites, "ids": ids}


def _chart(charts: dict, title_n: str, artist: str | None) -> tuple[int, int] | None:
    best = None
    names = credited_names(artist)
    for performer, peak, weeks in charts.get(title_n, ()):
        if (names & credited_names(performer)) or artist_matches(artist, performer):
            if best is None or peak < best[0]:
                best = (peak, weeks)
    return best


def plan(cfg: Config, rule: dict, skip: set[str] | frozenset = frozenset()) -> list[dict]:
    """Every album the filter covers, with each song's numbers and whether it's picked, and why."""
    opts = options(rule) or dict(DEFAULTS)
    sql, args = library.where(cfg, rule)
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(f"SELECT * FROM library{sql} ORDER BY {library.SORTS['artist']}", args)]
    st = _load_stats()
    groups: dict[tuple, dict] = {}
    for r in rows:
        if not r["song_key"]:
            continue
        artist = r["album_artist"] or r["artist"]
        gk = album_id(r["artist"], r["album_artist"], r["album"])
        g = groups.get(gk)
        if g is None:
            g = groups[gk] = {"id": gk, "artist": artist if r["album"] else r["artist"],
                              "album": r["album"], "year": r["year"],
                              "kind": _kind(r["release_type"], r["album"], r["album_artist"]) if r["album"] else "loose",
                              "songs": {}}
        if r["song_key"] not in g["songs"]:
            g["songs"][r["song_key"]] = {"key": r["song_key"], "row": r, "title": r["title"] or "",
                                         "track": r["track_number"], "disc": r["disc_number"],
                                         "duration": r["duration"], "recordings": set(), "artists": set()}
        s = g["songs"][r["song_key"]]
        if r["mb_recording"]:
            s["recordings"].add(r["mb_recording"])
        if r["mb_artist"]:
            s["artists"].add(r["mb_artist"])

    # Each song counts on one album: an album before an EP, single or compilation, then the earliest.
    home: dict[str, tuple] = {}
    for gk, g in groups.items():
        rank = (KIND_ORDER.get(g["kind"], 3), g["year"] or "9999", -len(g["songs"]))
        for k in g["songs"]:
            if k not in home or rank < home[k][0]:
                home[k] = (rank, gk)
    # An artist's live albums only count when they're all the artist has here.
    studio = {norm(g["artist"]) for g in groups.values() if g["kind"] not in ("live", "loose")}

    out = []
    for gk, g in groups.items():
        kind = g["kind"]
        if kind == "live" and norm(g["artist"]) not in studio:
            kind = "album"
        songs = list(g["songs"].values())
        for s in songs:
            r = s["row"]
            tn = r["norm_title"] or norm_title(s["title"])
            hid = st["ids"].get(s["key"])
            if hid:
                s["recordings"].add(hid[0])
                if hid[1]:
                    s["artists"].add(hid[1])
            lb = max([st["recs"].get(m) or 0 for m in s["recordings"]] +
                     [st["lists"]["listenbrainz"].get((a, tn)) or 0 for a in s["artists"]] + [0]) or None
            views = st["lists"]["genius"].get((norm(primary_artist(r["artist"])), tn))
            year = None
            single = False
            for a in s["artists"]:
                key = (a, tn)
                if key in st["lists"]["single"]:
                    year = st["lists"]["single"][key]
                    single = True
            if single and year and g["year"] and str(g["year"]).isdigit() and abs(year - int(g["year"])) > SINGLE_YEARS:
                single = False
            s.update(lb=lb, views=views, single=single, single_year=year,
                     chart=_chart(st["charts"], tn, r["artist"]), favorite=s["key"] in st["favorites"],
                     skipped=s["key"] in skip, elsewhere=None, out=None)
            if home[s["key"]][1] != gk:
                other = groups[home[s["key"]][1]]
                s["elsewhere"] = other["album"] or "your loose songs"
                s["out"] = f"On {s['elsewhere']}"
            elif not (s["favorite"] or s["chart"]):
                s["out"] = _excluded(s["title"], s["duration"])
        live = [s for s in songs if not s["out"]]
        lb_lift = _lift({s["key"]: s["lb"] for s in live}, MIN_LISTENERS)
        gv_lift = _lift({s["key"]: s["views"] for s in live}, MIN_VIEWS)
        has_data = bool(lb_lift or gv_lift)
        for s in live:
            lifts = [x for x in (lb_lift.get(s["key"]), gv_lift.get(s["key"])) if x is not None]
            s["lift_lb"], s["lift_views"] = lb_lift.get(s["key"]), gv_lift.get(s["key"])
            lift = max(lifts) if lifts else None
            s["lift"] = lift
            score = lift if lift is not None else (0.9 if has_data else 1.0)
            if s["chart"]:
                peak = s["chart"][0]
                score += 0.5 + (0.25 if peak <= 40 else 0) + (0.25 if peak <= 10 else 0)
            if s["single"]:
                score += 0.15
            s["score"] = score
            s["standout"] = bool(s["favorite"] or s["chart"] or (lift is not None and lift >= opts["cutoff"])
                                 or (s["single"] and (lift is None or lift >= 0.9)))
        # Ties keep album order, which puts the opener first on an album with no numbers at all.
        ranked = sorted((s for s in live if not s["skipped"]),
                        key=lambda s: (-s["score"], s["disc"] or 0, s["track"] or 0))
        picked: list[dict] = [s for s in ranked if s["favorite"]]
        if kind in BASE_KINDS:
            for s in ranked:
                if len(picked) >= opts["min"]:
                    break
                if s not in picked:
                    picked.append(s)
        for s in ranked:
            if len(picked) >= opts["max"]:
                break
            if s["standout"] and s not in picked:
                picked.append(s)
        chosen = {s["key"] for s in picked}
        whole = gk in opts["whole"]
        if whole:
            # You asked for all of it: every song, skits and interludes too, so it plays through.
            chosen = {s["key"] for s in songs if not s["skipped"]}
        for s in songs:
            s["picked"] = s["key"] in chosen
            s["reasons"] = _reasons(s) if s["picked"] or not s["out"] else []
            if whole and s["picked"]:
                s["reasons"].insert(0, "Whole album")
        g.update(kind=kind, has_data=has_data, songs=songs, picked=len(chosen), whole=whole)
        out.append(g)
    return out


def _reasons(s: dict) -> list[str]:
    why = []
    if s.get("favorite"):
        why.append("Favorite")
    if s.get("chart"):
        why.append(f"Hot 100 #{s['chart'][0]}")
    if s.get("single"):
        why.append(f"Single{' ' + str(s['single_year']) if s.get('single_year') else ''}")
    if s.get("lift_lb") is not None:
        why.append(f"{s['lift_lb']:.2f}x listeners")
    if s.get("lift_views") is not None:
        why.append(f"{s['lift_views']:.2f}x Genius views")
    return why


def collection(cfg: Config, rule: dict, skip: set[str] | frozenset = frozenset()) -> list[dict]:
    """The hits playlist's songs, in artist and album order. Songs you left out stay listed (as left out)
    and the album's next best takes their place."""
    keep, seen = [], set()
    for g in plan(cfg, rule, skip):
        for s in g["songs"]:
            # A song can be picked twice: on its own album, and on a compilation you asked for whole.
            if (s["picked"] or (s["skipped"] and not s["elsewhere"])) and s["key"] not in seen:
                seen.add(s["key"])
                keep.append(s)
    return [library.song(s["row"]) for s in keep]
