"""Artist pages and suggestions.

Two halves, like the hits playlists, so a page never waits on the network:

- research() fills the artist tables from open sources in the background: MusicBrainz, Wikidata, Wikipedia,
  Wikimedia Commons, ListenBrainz Labs, Discogs, and with keys Last.fm and TheAudioDB. It only fetches what's
  missing or stale, keeps going when one source refuses, and puts any artist someone is waiting for first.
- page(), suggestions() and suggested_hits() build what the pages show from those tables. They make no
  requests.

Suggestions are artists you don't have that your artists are similar to, or influenced, or were influenced
by. Get their hits picks a suggested artist's songs with the hits playlist rules and adds them to that
artist's playlist and to one Suggestions playlist, which download through Soulseek like any other.
"""
import json
import logging
import math
import os
import re
import statistics
import threading
import time
import uuid
from collections import Counter, defaultdict
from io import BytesIO
from urllib.parse import quote

import httpx
from PIL import Image

from . import db, hits, library, lookups, profiles
from .config import Config
from .lookups import Errors, GeniusRefused, StepStopped
from .match import norm, norm_title
from .sources import audiodb, commons, discogs, lastfm, lb_labs, musicbrainz, wikidata, wikipedia

log = logging.getLogger(__name__)

STATUS_KEY = "artists_status"
PENDING_KEY = "suggestions_pending"
DAY = 86400
FACTS_EVERY = 180 * DAY           # facts, influences, bios, and a suggested artist's albums
SIMILAR_EVERY = 60 * DAY          # similar artists: listening data moves faster
RETRY_NAMES = 30 * DAY            # a name that matched nothing is searched again after this
IMAGE_WIDTH = 500
AHEAD = 25                        # top suggestions whose albums are fetched ahead of time
SUGGESTIONS_SHOWN = 30            # suggestions the Suggestions page lists before "Show more"
TOP = 100                         # suggestions kept per profile
SHOWN_REASONS = 5
INFLUENCE = 0.5                   # an influence link counts like a similarity of 0.5
RULES = {"max": 5, "cutoff": 1.25, "total": 20}
VIEWS_NEED_LISTENERS = 0.5        # Genius views count for a suggested song only with half the usual listeners
# Suggestions lean toward artists you probably haven't heard: a match's score is divided by
# (listeners / FAME_FLOOR) ** FAME_POWER, and everyone under FAME_FLOOR listeners counts the same.
FAME_FLOOR = 5000
FAME_POWER = 0.75
CANDIDATES = 500                  # best matches per profile whose listener counts are looked up
POPULARITY_EVERY = 60 * DAY
POPULARITY_KEY = "artists_popularity_at"
# Other versions of a song on a suggested artist's singles and EPs: "(Rasta mix)", "(English version)",
# "(radio edit)", "(in progress)". The hits rules already leave out live, demo and remix versions.
_OTHER_VERSION = re.compile(r"[\(\[][^\)\]]*\b(mix|version|edit|in progress|take|session|alternate|early|rough|"
                            r"mono|stereo|instrumental|a cappella|acoustic|unplugged|video)\b[^\)\]]*[\)\]]", re.I)
BIO_ORDER = ("wikipedia", "lastfm", "discogs", "audiodb")
BIO_SOURCES = {
    "wikipedia": ("Wikipedia", wikipedia.LICENSE),
    "lastfm": ("Last.fm", lastfm.LICENSE),
    "discogs": ("Discogs", discogs.CREDIT),
    "audiodb": ("TheAudioDB", None),
}

_lock = threading.Lock()
_status = lookups.Status(STATUS_KEY)
_Http = lookups.Http              # tests swap in a fake
_priority: list[str] = []         # artists someone is waiting for: MusicBrainz IDs, or "name:<key>"
_plock = threading.Lock()
_pending_lock = threading.Lock()
_cache: dict = {}


# ------------------------------------------------------------------ status

def status() -> dict:
    return _status.get()


def running() -> bool:
    return _lock.locked()


def waiting(mbid: str) -> bool:
    """Whether the job is about to look this artist up, or is looking it up now."""
    with _plock:
        return mbid in _priority or mbid in _status.get().get("serving", [])


def cache_counts() -> dict:
    idx = library.artist_index()
    with db.connect() as c:
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        lib = list(idx["by_mbid"])
        have = set()
        for i in range(0, len(lib), 500):
            chunk = lib[i:i + 500]
            have |= {r[0] for r in c.execute(f"SELECT mbid FROM artists WHERE mbid IN ({','.join('?' * len(chunk))}) "
                                              "AND fetched LIKE '%\"musicbrainz\"%'", chunk)}
        return {
            "artists": len(idx["artists"]), "identified": len(lib), "with_data": len(have),
            "bios": one("SELECT COUNT(DISTINCT mbid) FROM artist_bios"),
            "images": one("SELECT COUNT(*) FROM artists WHERE image_file IS NOT NULL"),
            "similar": one("SELECT COUNT(DISTINCT mbid) FROM artist_links WHERE kind = 'similar'"),
            "influences": one("SELECT COUNT(DISTINCT mbid) FROM artist_links WHERE kind IN ('influenced_by', 'influenced')"),
            "unmatched": one("SELECT COUNT(*) FROM artist_names WHERE mbid = ''"),
        }


# ------------------------------------------------------------------ starting

def start(cfg: Config, full: bool = True) -> bool:
    """Look artists up in the background. full=False looks up only the artists someone is waiting for.
    False if a lookup is already running (it serves the waiting artists first)."""
    if not _lock.acquire(blocking=False):
        return False

    def target():
        try:
            research(cfg, full)
        finally:
            _lock.release()
            with _plock:
                more = bool(_priority)
            if more:                    # asked for while the run was finishing
                start(cfg, full=False)

    threading.Thread(target=target, daemon=True, name="artist-lookup").start()
    return True


def start_after_hits(cfg: Config) -> None:
    """For the scheduled refresh: the artist lookup after the hits lookup, never at the same time."""
    def target():
        while hits.running() or running():
            time.sleep(5)
        start(cfg)
    threading.Thread(target=target, daemon=True, name="artist-lookup-wait").start()


def lookup_first(cfg: Config, target: str) -> None:
    """Look one artist up ahead of the others (a MusicBrainz ID, or "name:<key>" for a name with no ID yet),
    starting the job if it's idle."""
    with _plock:
        if target in _priority:
            _priority.remove(target)
        _priority.insert(0, target)
    start(cfg, full=False)


# ------------------------------------------------------------------ the lookup

def research(cfg: Config, full: bool = True) -> None:
    _status.set(state="running", started=time.time(), finished=None, error=None, notes=[], step="Starting",
                done=0, total=0, full=full, serving=[])
    run = _Run(cfg)
    try:
        run.serve()
        if full:
            run.everything()
        run.serve()
        done = {"state": "done", "finished": time.time(), "step": None, "done": 0, "total": 0, "serving": []}
        if full:
            done["full_finished"] = time.time()
        _status.set(**done)
    except Exception as e:
        log.exception("artist lookup failed")
        _status.set(state="failed", finished=time.time(), error=str(e)[:300], serving=[])
    finally:
        run.close()


def _now() -> float:
    return time.time()


def _fetched_all() -> dict[str, dict]:
    with db.connect() as c:
        return {r[0]: json.loads(r[1] or "{}") for r in c.execute("SELECT mbid, fetched FROM artists")}


def _stale(fetched: dict, mbid: str, source: str, every: float) -> bool:
    return _now() - (fetched.get(mbid) or {}).get(source, 0) > every


def _mark(c, mbid: str, *sources: str, name: str | None = None) -> None:
    """Record that sources were fetched for an artist (making its row if it has none)."""
    row = c.execute("SELECT fetched FROM artists WHERE mbid = ?", (mbid,)).fetchone()
    fetched = json.loads(row[0] or "{}") if row else {}
    t = _now()
    fetched.update({s: t for s in sources})
    if row:
        c.execute("UPDATE artists SET fetched = ? WHERE mbid = ?", (json.dumps(fetched), mbid))
    else:
        c.execute("INSERT INTO artists (mbid, name, fetched) VALUES (?, ?, ?)", (mbid, name, json.dumps(fetched)))


def _rows(mbids: list[str]) -> dict[str, dict]:
    out = {}
    with db.connect() as c:
        for i in range(0, len(mbids), 500):
            chunk = mbids[i:i + 500]
            for r in c.execute(f"SELECT * FROM artists WHERE mbid IN ({','.join('?' * len(chunk))})", chunk):
                d = dict(r)
                for k in ("links", "facts", "fetched", "image_credit"):
                    d[k] = json.loads(d[k] or "{}")
                d["genres"] = json.loads(d["genres"] or "[]")
                out[d["mbid"]] = d
    return out


def _save_links(c, mbid: str, kinds: tuple, source: str, links: list[dict]) -> None:
    """Replace one source's links of some kinds for an artist."""
    c.execute(f"DELETE FROM artist_links WHERE mbid = ? AND source = ? AND kind IN ({','.join('?' * len(kinds))})",
              (mbid, source, *kinds))
    seen = set()
    for x in links:
        key = x.get("mbid") or x.get("qid") or norm(x.get("name"))
        if not key or (x["kind"], key) in seen or key == mbid:
            continue
        seen.add((x["kind"], key))
        c.execute("INSERT INTO artist_links (mbid, kind, other_key, other_mbid, other_wikidata, other_name, source, "
                  "score, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (mbid, x["kind"], key, x.get("mbid"), x.get("qid"), x.get("name"), source, x.get("score"),
                   json.dumps(x["detail"]) if x.get("detail") else None))


def _save_bio(c, mbid: str, source: str, text: str | None, url: str | None, license: str | None) -> None:
    if text:
        c.execute("INSERT INTO artist_bios (mbid, source, text, url, license, fetched_at) VALUES (?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT(mbid, source) DO UPDATE SET text = excluded.text, url = excluded.url, "
                  "license = excluded.license, fetched_at = excluded.fetched_at",
                  (mbid, source, text, url, license, _now()))
    else:
        c.execute("DELETE FROM artist_bios WHERE mbid = ? AND source = ?", (mbid, source))


class _Run:
    """One lookup run: its HTTP client, its step runner, and every step. Each step takes the artists to look
    at and fetches only what's missing or stale for them."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = _Http(cfg)
        self.steps = lookups.Steps(_status, "artists")
        self.labs = lb_labs.Labs(self.http, cfg.listenbrainz_token, bool(cfg.lastfm_api_key))
        self.serving = False
        self.genius_ok = bool(cfg.genius_token)
        self.lb_ok = True

    def close(self):
        self.http.close()

    def step(self, label: str, done: int = 0, total: int = 0) -> None:
        _status.step(label, done, total)

    # ---------------------------------------------------------- order

    def everything(self) -> None:
        self.steps(self.resolve_names, None)
        mbids = _library_mbids()
        self.facts(mbids)
        self.steps(self.similar, mbids)
        self.steps(self.discogs, mbids)
        if self.cfg.lastfm_api_key:
            self.steps(self.lastfm, mbids)
        self.steps(self.popularity)
        ahead = self.ahead()
        self.facts(ahead, influences=False)
        self.steps(self.discographies, ahead)
        self.finish_pending()

    def facts(self, mbids: list[str], influences: bool = True) -> None:
        self.steps(self.musicbrainz, mbids)
        self.steps(self.wikidata, mbids)
        if influences:
            self.steps(self.influenced, mbids)
        self.steps(self.wikipedia, mbids)
        self.steps(self.images, mbids)

    def serve(self) -> None:
        """Look up the artists someone is waiting for, ahead of the rest. Steps call this as they go."""
        if self.serving:
            return
        with _plock:
            todo, _priority[:] = list(_priority), []
        if not todo:
            return
        self.serving = True
        try:
            names = [t[5:] for t in todo if t.startswith("name:")]
            mbids = [t for t in todo if not t.startswith("name:")]
            _status.set(serving=mbids)
            if names:
                self.steps(self.resolve_names, names)
                idx = library.artist_index()
                mbids += [idx["by_key"][k]["mbid"] for k in names if k in idx["by_key"] and idx["by_key"][k]["mbid"]]
            if not mbids:
                return
            mbids = list(dict.fromkeys(mbids))
            _status.set(serving=mbids)
            in_library = set(library.artist_index()["by_mbid"])
            self.facts(mbids)
            self.steps(self.similar, [m for m in mbids if m in in_library])
            self.steps(self.discogs, mbids)
            if self.cfg.lastfm_api_key:
                self.steps(self.lastfm, mbids)
            pending = {m for _, m in _pending()}
            wanted = [m for m in mbids if m in pending or m not in in_library]
            if wanted:
                self.steps(self.discographies, wanted)
            self.finish_pending()
        finally:
            self.serving = False
            _status.set(serving=[])

    # ---------------------------------------------------------- names

    def resolve_names(self, keys: list[str] | None) -> None:
        """MusicBrainz IDs for library names whose files carry none, found through one of their albums."""
        idx = library.artist_index()
        with db.connect() as c:
            checked = {r["name"]: dict(r) for r in c.execute("SELECT * FROM artist_names")}
        pool = [idx["by_key"][k] for k in keys if k in idx["by_key"]] if keys is not None else idx["artists"]
        todo = []
        for a in pool:
            if a["mbid"] or not a["album_titles"]:
                continue
            prev = checked.get(a["key"])
            if keys is None and prev and (prev["mbid"] or _now() - prev["checked_at"] < RETRY_NAMES):
                continue
            todo.append(a)
        errors = Errors("Finding artists on MusicBrainz")
        for i, a in enumerate(todo):
            if i % 10 == 0:
                self.step("Finding artists on MusicBrainz", i, len(todo))
                self.serve()
            found = None
            try:
                for album in a["album_titles"][:2]:
                    found = musicbrainz.find_artist(self.http, a["name"], album)
                    if found:
                        break
            except (httpx.HTTPError, ValueError) as e:
                errors.failed(e)
                continue
            errors.ok()
            with db.connect() as c:
                for k in a["keys"]:
                    prev = checked.get(k)
                    if prev and prev["how"] == "manual":
                        continue
                    c.execute("INSERT INTO artist_names (name, mbid, how, checked_at) VALUES (?, ?, 'search', ?) "
                              "ON CONFLICT(name) DO UPDATE SET mbid = excluded.mbid, how = excluded.how, "
                              "checked_at = excluded.checked_at", (k, found or "", _now()))

    # ---------------------------------------------------------- MusicBrainz

    def musicbrainz(self, mbids: list[str]) -> None:
        fetched = _fetched_all()
        todo = [m for m in mbids if _stale(fetched, m, "musicbrainz", FACTS_EVERY)]
        errors = Errors("Getting artist facts from MusicBrainz")
        for i, m in enumerate(todo):
            if i % 10 == 0:
                self.step("Getting artist facts from MusicBrainz", i, len(todo))
            self.serve()
            if not _stale(_fetched_one(m), m, "musicbrainz", FACTS_EVERY):
                continue                    # looked up just now, for someone waiting
            try:
                a = musicbrainz.artist(self.http, m)
            except (httpx.HTTPError, ValueError) as e:
                errors.failed(e)
                continue
            errors.ok()
            with db.connect() as c:
                if a:
                    _save_mb(c, m, a)
                _mark(c, m, "musicbrainz")

    # ---------------------------------------------------------- Wikidata

    def wikidata(self, mbids: list[str]) -> None:
        rows = _rows(mbids)
        fetched = {m: r["fetched"] for m, r in rows.items()}
        todo = [m for m in mbids if m in rows and rows[m]["wikidata"] and _stale(fetched, m, "wikidata", FACTS_EVERY)]
        for i in range(0, len(todo), wikidata.BATCH):
            self.step("Reading Wikidata", i, len(todo))
            self.serve()
            batch = todo[i:i + wikidata.BATCH]
            by_qid = {rows[m]["wikidata"]: m for m in batch}
            ents = wikidata.entities(self.http, list(by_qid))
            related = sorted({q for e in ents.values() for q in e["influenced_by"] + e["genres"] +
                              ([e["formed_in"]] if e["formed_in"] else [])})
            names = wikidata.labels(self.http, related) if related else {}
            with db.connect() as c:
                for qid, m in by_qid.items():
                    e = ents.get(qid)
                    if e:
                        _save_wikidata(c, m, e, names, rows[m])
                    _mark(c, m, "wikidata")

    def influenced(self, mbids: list[str]) -> None:
        """Who each artist influenced: the items that name it under "influenced by"."""
        rows = _rows(mbids)
        fetched = {m: r["fetched"] for m, r in rows.items()}
        todo = [m for m in mbids if m in rows and rows[m]["wikidata"] and _stale(fetched, m, "influenced", FACTS_EVERY)]
        for i in range(0, len(todo), wikidata.BATCH):
            self.step("Finding who each artist influenced on Wikidata", i, len(todo))
            self.serve()
            batch = todo[i:i + wikidata.BATCH]
            by_qid = {rows[m]["wikidata"]: m for m in batch}
            got = wikidata.influenced(self.http, list(by_qid))
            with db.connect() as c:
                for qid, m in by_qid.items():
                    _save_links(c, m, ("influenced",), "wikidata",
                                [{"kind": "influenced", "mbid": x["mbid"], "qid": x["qid"], "name": x["label"]}
                                 for x in got.get(qid) or []])
                    _mark(c, m, "influenced")

    # ---------------------------------------------------------- Wikipedia and images

    def wikipedia(self, mbids: list[str]) -> None:
        rows = _rows(mbids)
        fetched = {m: r["fetched"] for m, r in rows.items()}
        todo = [m for m in mbids if m in rows and _wiki_title(rows[m]) and _stale(fetched, m, "wikipedia", FACTS_EVERY)]
        for i in range(0, len(todo), wikipedia.BATCH):
            self.step("Reading Wikipedia", i, len(todo))
            self.serve()
            batch = todo[i:i + wikipedia.BATCH]
            titles = {m: _wiki_title(rows[m]) for m in batch}
            got = wikipedia.intros(self.http, list(dict.fromkeys(titles.values())))
            with db.connect() as c:
                for m, t in titles.items():
                    x = got.get(t)
                    _save_bio(c, m, "wikipedia", x and x["text"], x and x["url"], wikipedia.LICENSE)
                    _mark(c, m, "wikipedia")

    def images(self, mbids: list[str]) -> None:
        """One picture per artist: the Wikidata image from Commons, or else TheAudioDB's."""
        rows = _rows(mbids)
        fetched = {m: r["fetched"] for m, r in rows.items()}
        want = {m: _commons_file(r) for m, r in rows.items()}
        # A new Wikidata image replaces the old one. One that failed to download is tried again when it's stale.
        todo = [m for m, f in want.items() if f and (f != rows[m]["image_source"] or (
            not rows[m]["image_file"] and _stale(fetched, m, "commons", FACTS_EVERY)))]
        for i in range(0, len(todo), commons.BATCH):
            self.step("Getting artist pictures from Wikimedia Commons", i, len(todo))
            self.serve()
            batch = todo[i:i + commons.BATCH]
            got = commons.images(self.http, [want[m] for m in batch])
            for m in batch:
                info = got.get(want[m])
                saved = info and info["thumb"] and self._save_image(m, info["thumb"], want[m], {
                    "source": "Wikimedia Commons", "author": info["author"], "license": info["license"],
                    "license_url": info["license_url"], "page": info["page"]})
                with db.connect() as c:
                    if not saved:
                        c.execute("UPDATE artists SET image_source = ? WHERE mbid = ? AND image_file IS NULL",
                                  (want[m], m))
                    _mark(c, m, "commons")
        rows = _rows(mbids)
        key = self.cfg.theaudiodb_api_key
        todo = [m for m in mbids if m in rows and (key or not rows[m]["image_file"])
                and _stale({m: rows[m]["fetched"]}, m, "audiodb", FACTS_EVERY)]
        errors = Errors("Getting artist pictures from TheAudioDB")
        for i, m in enumerate(todo):
            if i % 10 == 0:
                self.step("Getting artist pictures from TheAudioDB", i, len(todo))
            self.serve()
            try:
                a = audiodb.artist(self.http, m, key)
            except (httpx.HTTPError, ValueError) as e:
                errors.failed(e)
                continue
            errors.ok()
            if a and a["thumb"] and not rows[m]["image_file"]:
                self._save_image(m, a["thumb"], a["thumb"], {"source": "TheAudioDB", "page": a["url"]})
            with db.connect() as c:
                if key:
                    _save_bio(c, m, "audiodb", a and a["bio"], a and a["url"], None)
                _mark(c, m, "audiodb")

    def _save_image(self, mbid: str, url: str, source: str, credit: dict) -> bool:
        try:
            r = self.http.send("GET", url)
            r.raise_for_status()
            im = Image.open(BytesIO(r.content))
            im.thumbnail((IMAGE_WIDTH, IMAGE_WIDTH * 2))
            folder = self.cfg.artwork_dir / "artists"
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / f".{uuid.uuid4().hex}.jpg"
            im.convert("RGB").save(tmp, "JPEG", quality=84)
            os.replace(tmp, folder / f"{mbid}.jpg")
        except (httpx.HTTPError, OSError, ValueError) as e:
            log.info("artists: no usable picture for %s from %s: %s", mbid, url, e)
            return False
        with db.connect() as c:
            c.execute("UPDATE artists SET image_file = ?, image_source = ?, image_credit = ? WHERE mbid = ?",
                      (f"{mbid}.jpg", source, json.dumps(credit), mbid))
        return True

    # ---------------------------------------------------------- similar artists and other bios

    def similar(self, mbids: list[str]) -> None:
        fetched = _fetched_all()
        todo = [m for m in mbids if _stale(fetched, m, "listenbrainz", SIMILAR_EVERY)]
        errors = Errors("Getting similar artists from ListenBrainz")
        for i, m in enumerate(todo):
            if i % 10 == 0:
                self.step("Getting similar artists from ListenBrainz", i, len(todo))
            self.serve()
            try:
                got = self.labs.similar(m)
            except (httpx.HTTPError, ValueError) as e:
                errors.failed(e)
                continue
            errors.ok()
            with db.connect() as c:
                _save_links(c, m, ("similar",), "listenbrainz",
                            [{"kind": "similar", "mbid": x["mbid"], "name": x["name"], "score": x["score"]} for x in got])
                _mark(c, m, "listenbrainz")

    def discogs(self, mbids: list[str]) -> None:
        rows = _rows(mbids)
        todo = [m for m in mbids if m in rows and musicbrainz.discogs_id(rows[m]["links"])
                and _stale({m: rows[m]["fetched"]}, m, "discogs", FACTS_EVERY)]
        errors = Errors("Reading Discogs")
        for i, m in enumerate(todo):
            if i % 10 == 0:
                self.step("Reading Discogs", i, len(todo))
            self.serve()
            try:
                d = discogs.artist(self.http, musicbrainz.discogs_id(rows[m]["links"]), self.cfg.discogs_token)
            except (httpx.HTTPError, ValueError) as e:
                errors.failed(e)
                continue
            errors.ok()
            with db.connect() as c:
                _save_bio(c, m, "discogs", d and d["profile"], d and d["url"], discogs.CREDIT)
                _mark(c, m, "discogs")

    def lastfm(self, mbids: list[str]) -> None:
        rows = _rows(mbids)
        key = self.cfg.lastfm_api_key
        todo = [m for m in mbids if m in rows and _stale({m: rows[m]["fetched"]}, m, "lastfm", SIMILAR_EVERY)]
        errors = Errors("Reading Last.fm")
        for i, m in enumerate(todo):
            if i % 10 == 0:
                self.step("Reading Last.fm", i, len(todo))
            self.serve()
            name = rows[m]["name"]
            try:
                info = lastfm.info(self.http, key, m, name)
                sim = lastfm.similar(self.http, key, m, name)
            except (httpx.HTTPError, ValueError) as e:
                errors.failed(e)
                continue
            errors.ok()
            with db.connect() as c:
                _save_bio(c, m, "lastfm", info and info["bio"], info and info["url"], lastfm.LICENSE)
                if info and info["tags"]:
                    facts = rows[m]["facts"]
                    facts["tags"] = info["tags"][:8]
                    c.execute("UPDATE artists SET facts = ? WHERE mbid = ?", (json.dumps(facts), m))
                _save_links(c, m, ("similar",), "lastfm",
                            [{"kind": "similar", "mbid": x["mbid"], "name": x["name"], "score": x["score"]} for x in sim])
                _mark(c, m, "lastfm")

    # ---------------------------------------------------------- suggested artists

    def popularity(self) -> None:
        """How well known each profile's best matches are: ListenBrainz listeners, 500 artists a request."""
        want: dict[str, str] = {}
        for p in profiles.all_profiles():
            for s in _matches(p["id"]):
                want.setdefault(s["mbid"], s["name"])
        fetched = _fetched_all()
        todo = [m for m in want if _stale(fetched, m, "popularity", POPULARITY_EVERY)]
        for i in range(0, len(todo), 500):
            self.step("Finding out how well known suggested artists are", i, len(todo))
            batch = todo[i:i + 500]
            res = self.http.lb("POST", "/popularity/artist", json={"artist_mbids": batch}) or []
            got = {r.get("artist_mbid"): r.get("total_user_count") for r in res if isinstance(r, dict)}
            with db.connect() as c:
                for m in batch:
                    _mark(c, m, "popularity", name=want[m])
                    c.execute("UPDATE artists SET listeners = ? WHERE mbid = ?", (got.get(m), m))
        if todo:
            db.set_kv(POPULARITY_KEY, str(_now()))

    def ahead(self) -> list[str]:
        """The artists to fetch albums for ahead of time: each profile's top suggestions, and any artist someone
        asked for the hits of."""
        out = []
        for p in profiles.all_profiles():
            out += [s["mbid"] for s in suggestions(p["id"])[:AHEAD]]
        out += [m for _, m in _pending()]
        return list(dict.fromkeys(out))

    def discographies(self, mbids: list[str]) -> None:
        fetched = _fetched_all()
        todo = [m for m in mbids if _stale(fetched, m, "discography", FACTS_EVERY)]
        errors = Errors("Finding suggested artists' hits")
        for i, m in enumerate(todo):
            self.step("Finding suggested artists' hits", i, len(todo))
            self.serve()
            if not _stale(_fetched_one(m), m, "discography", FACTS_EVERY):
                continue
            try:
                self._discography(m)
            except GeniusRefused as e:
                self.genius_ok = False
                self.steps.note(str(e))
            except (httpx.HTTPError, ValueError) as e:
                errors.failed(e)
                continue
            errors.ok()

    def _discography(self, mbid: str) -> None:
        d = musicbrainz.discography(self.http, mbid)
        name = (_rows([mbid]).get(mbid) or {}).get("name")
        t = _now()
        with db.connect() as c:
            old = [r[0] for r in c.execute("SELECT rgid FROM artist_releases WHERE mbid = ?", (mbid,))]
            c.executemany("DELETE FROM artist_tracks WHERE rgid = ?", [(g,) for g in old])
            c.execute("DELETE FROM artist_releases WHERE mbid = ?", (mbid,))
            for r in d["releases"]:
                c.execute("INSERT OR REPLACE INTO artist_releases (rgid, mbid, title, kind, year, release_id, fetched_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?)", (r["rgid"], mbid, r["title"], r["kind"], r["year"],
                                                           r["release_id"], t))
                c.executemany("INSERT OR REPLACE INTO artist_tracks (rgid, disc, track, recording, title, length) "
                              "VALUES (?, ?, ?, ?, ?, ?)",
                              [(r["rgid"], x["disc"] or 1, x["track"] or n, x["recording"], x["title"], x["length"])
                               for n, x in enumerate(r["tracks"], 1)])
        hits._save_artist("single", mbid, hits.singles_from_groups(d["groups"]))
        recs = sorted({x["recording"] for r in d["releases"] for x in r["tracks"] if x["recording"]})
        with db.connect() as c:
            fresh = {r[0] for r in c.execute("SELECT mbid FROM hit_recordings WHERE fetched_at > ?",
                                             (t - hits.STATS_EVERY,))}
        todo = [x for x in recs if x not in fresh] if self.lb_ok else []
        for i in range(0, len(todo), hits.LB_BATCH):
            batch = todo[i:i + hits.LB_BATCH]
            try:
                res = self.http.lb("POST", "/popularity/recording", json={"recording_mbids": batch}) or []
            except StepStopped as e:            # picks then go by charts and singles alone
                self.lb_ok = False
                self.steps.note(str(e))
                break
            got = {r.get("recording_mbid"): r for r in res if isinstance(r, dict)}
            with db.connect() as c:
                c.executemany(
                    "INSERT INTO hit_recordings (mbid, listeners, listens, fetched_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(mbid) DO UPDATE SET listeners = excluded.listeners, listens = excluded.listens, "
                    "fetched_at = excluded.fetched_at",
                    [(x, (got.get(x) or {}).get("total_user_count"), (got.get(x) or {}).get("total_listen_count"), _now())
                     for x in batch])
        if self.http.lb_token and self.lb_ok:
            try:
                hits._save_artist("listenbrainz", mbid, hits._lb_artist(self.http, mbid))
            except StepStopped as e:
                self.lb_ok = False
                self.steps.note(str(e))
        if self.genius_ok and name:
            titles = [x["title"] for r in d["releases"] if r["kind"] in ("album", "ep") for x in r["tracks"]][:3]
            if titles:
                hits._save_artist("genius", norm(name), hits._genius_artist(self.http, {"artist": name, "titles": titles}))
        with db.connect() as c:
            _mark(c, mbid, "discography", name=name)

    def finish_pending(self) -> None:
        """Add the songs of every Get their hits that was waiting for its artist's albums."""
        fetched = _fetched_all()
        ready = [x for x in _pending() if not _stale(fetched, x[1], "discography", FACTS_EVERY)]
        for profile, mbid in ready:
            try:
                add_hits(self.cfg.for_profile(profile), mbid)
            except Exception:
                log.exception("artists: adding the hits of %s for profile %s failed", mbid, profile)
        _update_pending(drop=set(ready))


def make_stale(mbid: str) -> None:
    """Fetch an artist's data again at its next lookup, keeping what's there until then."""
    with db.connect() as c:
        r = c.execute("SELECT fetched FROM artists WHERE mbid = ?", (mbid,)).fetchone()
        if r:
            c.execute("UPDATE artists SET fetched = ? WHERE mbid = ?",
                      (json.dumps({k: 0 for k in json.loads(r[0] or "{}")}), mbid))


def _fetched_one(mbid: str) -> dict:
    with db.connect() as c:
        r = c.execute("SELECT fetched FROM artists WHERE mbid = ?", (mbid,)).fetchone()
    return {mbid: json.loads(r[0] or "{}")} if r else {}


def _library_mbids() -> list[str]:
    """Library artists with an ID, the ones with the most songs first."""
    return [a["mbid"] for a in sorted(library.artist_index()["artists"], key=lambda a: -a["songs"]) if a["mbid"]]


def _save_mb(c, mbid: str, a: dict) -> None:
    c.execute("INSERT INTO artists (mbid) VALUES (?) ON CONFLICT(mbid) DO NOTHING", (mbid,))
    c.execute("UPDATE artists SET name = ?, sort_name = ?, type = ?, area = ?, begin_area = ?, life_begin = ?, "
              "life_end = ?, ended = ?, disambiguation = ?, genres = ?, wikidata = ?, links = ? WHERE mbid = ?",
              (a["name"], a["sort_name"], a["type"], a["area"], a["begin_area"], a["begin"], a["end"],
               1 if a["ended"] else 0, a["disambiguation"], json.dumps(a["genres"]),
               musicbrainz.wikidata_id(a["links"]), json.dumps(a["links"]), mbid))
    member = lambda kind, m: {"kind": kind, "mbid": m["mbid"], "name": m["name"],
                              "detail": {"begin": m["begin"], "end": m["end"], "instruments": m["instruments"],
                                         "original": m["original"]}}
    _save_links(c, mbid, ("member", "member_of"), "musicbrainz",
                [member("member", m) for m in a["members"]] + [member("member_of", m) for m in a["member_of"]])


def _save_wikidata(c, mbid: str, e: dict, names: dict, row: dict) -> None:
    label = lambda q: (names.get(q) or {}).get("label")
    facts = dict(row["facts"])
    facts.update(inception=e["inception"], dissolved=e["dissolved"], image=e["image"],
                 formed_in=label(e["formed_in"]) if e["formed_in"] else None,
                 genres=[g for g in (label(q) for q in e["genres"]) if g])
    c.execute("UPDATE artists SET enwiki = ?, facts = ? WHERE mbid = ?", (e["enwiki"], json.dumps(facts), mbid))
    _save_links(c, mbid, ("influenced_by",), "wikidata",
                [{"kind": "influenced_by", "qid": q, "mbid": (names.get(q) or {}).get("mbid"), "name": label(q)}
                 for q in e["influenced_by"]])


def _wiki_title(row: dict) -> str | None:
    if row.get("enwiki"):
        return row["enwiki"]
    link = (row.get("links") or {}).get("wikipedia") or ""
    return link.rsplit("/wiki/", 1)[1].replace("_", " ") if "/wiki/" in link else None


def _commons_file(row: dict) -> str | None:
    return (row.get("facts") or {}).get("image") or musicbrainz.commons_file(row.get("links") or {})


# ------------------------------------------------------------------ pages

def _link_rows(sql: str, args) -> list[dict]:
    with db.connect() as c:
        return [dict(r) for r in c.execute(sql, args)]


def _person(x: dict, idx: dict) -> dict:
    """A related artist as a page shows it: linked to its page when it's in the library, and otherwise to
    MusicBrainz (or Wikidata, for an artist MusicBrainz doesn't know)."""
    lib = idx["by_mbid"].get(x.get("mbid")) if x.get("mbid") else None
    if not lib and x.get("name") and not x.get("mbid"):
        lib = idx["by_key"].get(norm(x["name"]))
    name = x.get("name") or (lib or {}).get("name")
    if lib and lib.get("mbid"):
        url = f"/artists/{lib['mbid']}"
    elif lib:
        url = "/artists/by-name?n=" + _q(name)
    elif x.get("mbid"):
        url = musicbrainz.url(x["mbid"])
    else:
        url = wikidata.url(x["qid"]) if x.get("qid") else None
    return {"name": name, "mbid": x.get("mbid") or (lib or {}).get("mbid"), "in_library": bool(lib), "url": url}


def _related(mbid: str, kind: str, inverse: str) -> list[dict]:
    """An artist's links of one kind, plus the other direction recorded on the other artist's side."""
    own = _link_rows("SELECT other_mbid AS mbid, other_wikidata AS qid, other_name AS name FROM artist_links "
                     "WHERE mbid = ? AND kind = ?", (mbid, kind))
    back = _link_rows("SELECT l.mbid AS mbid, NULL AS qid, a.name AS name FROM artist_links l "
                      "LEFT JOIN artists a ON a.mbid = l.mbid WHERE l.other_mbid = ? AND l.kind = ?", (mbid, inverse))
    out, seen = [], set()
    for x in own + back:
        key = x["mbid"] or x["qid"] or norm(x["name"])
        if key and key not in seen:
            seen.add(key)
            out.append(x)
    return out


def _years(row: dict) -> str | None:
    b, e = (row.get("life_begin") or "")[:4], (row.get("life_end") or "")[:4]
    facts = row.get("facts") or {}
    b = b or str(facts.get("inception") or "")
    e = e or str(facts.get("dissolved") or "")
    if row.get("type") == "Person":
        if b and e:
            return f"Born {b}, died {e}"
        return f"Born {b}" if b else None
    if b and e:
        return b if b == e else f"{b} to {e}"
    if b:
        return f"{b} to {'unknown' if row.get('ended') else 'now'}"
    return None


def _origin(row: dict) -> str | None:
    parts = [x for x in (row.get("begin_area"), row.get("area")) if x]
    if len(parts) == 2 and parts[0] == parts[1]:
        parts = parts[:1]
    return ", ".join(parts) or (row.get("facts") or {}).get("formed_in")


LINKS_OUT = (("musicbrainz", "MusicBrainz"), ("wikipedia", "Wikipedia"), ("wikidata", "Wikidata"),
             ("allmusic", "AllMusic"), ("discogs", "Discogs"), ("lastfm", "Last.fm"), ("genius", "Genius"),
             ("bandcamp", "Bandcamp"), ("official", "Official site"))


def page(mbid: str | None, profile: int = 1, names: list[str] | None = None) -> dict:
    """Everything an artist page shows, from the cache. mbid None: a library artist with no ID (names are
    the library names it goes by)."""
    idx = library.artist_index()
    lib = idx["by_mbid"].get(mbid) if mbid else idx["by_key"].get((names or [""])[0])
    row = (_rows([mbid]).get(mbid) if mbid else None) or {}
    fetched = row.get("fetched") or {}
    out = {"mbid": mbid, "lib": lib, "row": row, "has_data": "musicbrainz" in fetched,
           "name": row.get("name") or (lib or {}).get("name") or (names or ["Unknown artist"])[0],
           "albums": _albums(lib) if lib else [], "in_library": bool(lib), "fetched": fetched}
    if not row:
        return out
    links = dict(row["links"])
    links["musicbrainz"] = musicbrainz.url(mbid)
    if row.get("wikidata"):
        links["wikidata"] = wikidata.url(row["wikidata"])
    if _wiki_title(row):
        links["wikipedia"] = wikipedia.url(_wiki_title(row))
    with db.connect() as c:
        bios = {r["source"]: dict(r) for r in c.execute("SELECT * FROM artist_bios WHERE mbid = ?", (mbid,))}
    bio_list = [{**bios[s], "label": BIO_SOURCES[s][0], "license": bios[s]["license"] or BIO_SOURCES[s][1]}
                for s in BIO_ORDER if s in bios]
    by_year = lambda xs: sorted(xs, key=lambda m: (m.get("begin") or "9999", m["name"].casefold()))
    members = by_year([{**_person(x, idx), **json.loads(x["detail"] or "{}")} for x in _link_rows(
        "SELECT other_mbid AS mbid, other_name AS name, detail FROM artist_links WHERE mbid = ? AND kind = 'member'", (mbid,))
        if x["name"]])
    groups = by_year([{**_person(x, idx), **json.loads(x["detail"] or "{}")} for x in _link_rows(
        "SELECT other_mbid AS mbid, other_name AS name, detail FROM artist_links WHERE mbid = ? AND kind = 'member_of'", (mbid,))
        if x["name"]])
    # Artists Wikidata has no English name for are left out: there's nothing to show for them.
    order = lambda xs: sorted((x for x in xs if x["name"]), key=lambda x: (not x["in_library"], library.sort_key(x["name"])))
    influenced_by = order([_person(x, idx) for x in _related(mbid, "influenced_by", "influenced")])
    influenced = order([_person(x, idx) for x in _related(mbid, "influenced", "influenced_by")])
    similar: dict[str, dict] = {}
    for x in _link_rows("SELECT other_key, other_mbid AS mbid, other_name AS name, source, score FROM artist_links "
                        "WHERE mbid = ? AND kind = 'similar'", (mbid,)):
        s = similar.get(x["other_key"])
        if s is None:
            s = similar[x["other_key"]] = {**_person(x, idx), "score": 0.0, "sources": []}
        s["score"] = max(s["score"], x["score"] or 0)
        s["sources"].append({"listenbrainz": "ListenBrainz", "lastfm": "Last.fm"}.get(x["source"], x["source"]))
    sim = sorted((x for x in similar.values() if x["name"]), key=lambda x: -x["score"])
    suggested = {s["mbid"]: s for s in suggestions(profile)}
    shown = {s["mbid"] for s in suggestions(profile, SUGGESTIONS_SHOWN)}       # the ones the Suggestions page lists
    for x in sim + influenced_by + influenced:
        x["suggested"] = x.get("mbid") in shown
    facts = row.get("facts") or {}
    credit = row.get("image_credit") or {}
    sources = [("MusicBrainz", "Facts, members, and links", "CC0", links["musicbrainz"])]
    if row.get("wikidata"):
        sources.append(("Wikidata", "Influences, and facts MusicBrainz doesn't have", "CC0", links["wikidata"]))
    for b in bio_list:
        sources.append((b["label"], "Bio", b["license"], b["url"]))
    if credit:
        sources.append((credit.get("source"), "Picture", credit.get("license"), credit.get("page")))
    labels = sorted({s for x in sim for s in x["sources"]})
    if "ListenBrainz" in labels:
        sources.append(("ListenBrainz", "Similar artists, from what listeners play together", "CC0",
                        "https://listenbrainz.org/"))
    if "Last.fm" in labels and not any(b["source"] == "lastfm" for b in bio_list):
        sources.append(("Last.fm", "Similar artists", None, links.get("lastfm")))
    out.update(
        type=row.get("type"), years=_years(row), origin=_origin(row), disambiguation=row.get("disambiguation"),
        genres=row["genres"][:6] or facts.get("genres", [])[:6] or facts.get("tags", [])[:6],
        bio=bio_list[0] if bio_list else None, other_bios=bio_list[1:],
        image=f"/artists/{mbid}/image" if row.get("image_file") else None, credit=credit,
        members=members, groups=groups, influenced_by=influenced_by, influenced=influenced,
        similar_have=[x for x in sim if x["in_library"]], similar_not=[x for x in sim if not x["in_library"]][:30],
        similar_sources=labels,
        links_out=[(label, links[k]) for k, label in LINKS_OUT if links.get(k)], sources=sources,
        suggestion=suggested.get(mbid) if not lib else None)
    return out


def _albums(lib: dict) -> list[dict]:
    """The artist's albums in the library, newest first, with the songs a hits playlist picked from each."""
    keys = list(lib["song_keys"])
    rows = []
    with db.connect() as c:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            rows += [dict(r) for r in c.execute(
                f"SELECT song_key, title, album, album_artist, artist, year, release_type, art_key, track_number, "
                f"disc_number FROM library WHERE song_key IN ({','.join('?' * len(chunk))})", chunk)]
        picked = {r[0] for r in c.execute(
            "SELECT DISTINCT e.song_key FROM entries e JOIN playlists p ON p.id = e.playlist_id "
            "JOIN items i ON i.playlist_id = e.playlist_id AND i.song_key = e.song_key "
            "WHERE p.source = 'local' AND p.rule LIKE '%\"hits\"%' AND i.skip = 0")}
    albums: dict[str, dict] = {}
    for r in rows:
        if not r["album"]:
            continue
        k = hits.album_id(r["artist"], r["album_artist"], r["album"])
        a = albums.get(k)
        if a is None:
            a = albums[k] = {"id": k, "title": r["album"], "artist": r["album_artist"] or r["artist"], "year": r["year"],
                             "kind": hits._kind(r["release_type"], r["album"], r["album_artist"]),
                             "art": f"/art/{r['art_key']}" if r["art_key"] else None, "songs": {}}
        a["songs"].setdefault(r["song_key"], (r["disc_number"] or 0, r["track_number"] or 0, r["title"]))
    # A picked song is marked on the album it counts for in a hits playlist: an album before an EP, single or
    # compilation, then the earliest.
    home: dict[str, tuple] = {}
    for k, a in albums.items():
        rank = (hits.KIND_ORDER.get(a["kind"], 3), a["year"] or "9999")
        for s in a["songs"]:
            if s not in home or rank < home[s][0]:
                home[s] = (rank, k)
    out = []
    for k, a in albums.items():
        a["picks"] = [t for s, (_, _, t) in sorted(a["songs"].items(), key=lambda kv: kv[1][:2])
                      if s in picked and home[s][1] == k]
        a["count"] = len(a["songs"])
        a["url"] = "/library?" + "&".join(f"{f}={_q(v)}" for f, v in (("artist", a["artist"]), ("album", a["title"])))
        del a["songs"]
        out.append(a)
    return sorted(out, key=lambda a: (a["year"] or "0000", a["title"].casefold()), reverse=True)


def _q(v: str) -> str:
    return quote(v or "", safe="")


# ------------------------------------------------------------------ suggestions

def _stamp(profile: int) -> tuple:
    with db.connect() as c:
        return (tuple(c.execute("SELECT COUNT(*), MAX(rowid) FROM artist_links").fetchone()),
                tuple(c.execute("SELECT COUNT(*), MAX(at) FROM suggestion_dismissed WHERE profile = ?",
                                (profile,)).fetchone()),
                c.execute("SELECT COUNT(*) FROM hit_favorites WHERE profile = ?", (profile,)).fetchone()[0],
                id(library.artist_index()), db.get_kv(POPULARITY_KEY))


def suggestions(profile: int = 1, limit: int = TOP) -> list[dict]:
    """Artists you don't have that your artists point to, leaning toward ones you probably haven't heard.

    Each of the best CANDIDATES matches (see _matches) is scored by its match divided by how well known it is:
    match / (max(listeners, FAME_FLOOR) / FAME_FLOOR) ** FAME_POWER. An artist whose listeners aren't known
    yet counts as under the floor."""
    stamp = _stamp(profile)
    hit = _cache.get(("suggestions", profile))
    if hit and hit[0] == stamp:
        return hit[1][:limit]
    matches = _matches(profile, stamp)
    fame = _listeners([s["mbid"] for s in matches])
    out = []
    for s in matches:
        n = fame.get(s["mbid"])
        out.append({**s, "listeners": n, "score": s["match"] / (max(n or 0, FAME_FLOOR) / FAME_FLOOR) ** FAME_POWER})
    out.sort(key=lambda s: (-s["score"], s["name"]))
    out = out[:TOP]
    _cache[("suggestions", profile)] = (stamp, out)
    return out[:limit]


def _listeners(mbids: list[str]) -> dict[str, int | None]:
    out = {}
    with db.connect() as c:
        for i in range(0, len(mbids), 500):
            chunk = mbids[i:i + 500]
            out.update({r[0]: r[1] for r in c.execute(
                f"SELECT mbid, listeners FROM artists WHERE mbid IN ({','.join('?' * len(chunk))})", chunk)})
    return out


def _matches(profile: int, stamp: tuple | None = None) -> list[dict]:
    """The artists you don't have that your artists point to most strongly, best first, up to CANDIDATES.

    match(X) = the sum over library artists A linked to X of weight(A) * strength(A, X), where
    weight(A) = 1 + ln(1 + A's songs) + 2 ln(1 + A's songs among your favorites), and strength is the best
    similarity score between them, or INFLUENCE for an influence either way."""
    stamp = stamp or _stamp(profile)
    hit = _cache.get(("matches", profile))
    if hit and hit[0] == stamp:
        return hit[1]
    idx = library.artist_index()
    with db.connect() as c:
        favs = {r[0] for r in c.execute("SELECT song_key FROM hit_favorites WHERE profile = ?", (profile,))}
        dismissed = {r[0] for r in c.execute("SELECT mbid FROM suggestion_dismissed WHERE profile = ?", (profile,))}
        links = c.execute("SELECT mbid, kind, other_mbid, other_name, score FROM artist_links "
                          "WHERE kind IN ('similar', 'influenced_by', 'influenced') AND other_mbid IS NOT NULL").fetchall()
    weight = {a["mbid"]: 1 + math.log1p(a["songs"]) + 2 * math.log1p(len(a["song_keys"] & favs))
              for a in idx["artists"] if a["mbid"]}
    names = {a["mbid"]: a["name"] for a in idx["artists"] if a["mbid"]}
    pair: dict[tuple, dict] = {}
    other_names: dict[str, Counter] = defaultdict(Counter)
    for mbid, kind, x, x_name, score in links:
        if mbid not in weight or x in weight or x in dismissed or (x_name and norm(x_name) in idx["by_key"]):
            continue
        strength = (score or 0) if kind == "similar" else INFLUENCE
        # How X relates to A: similar to A, influenced A (A was influenced by X), or influenced by A.
        how = {"similar": "similar", "influenced_by": "influenced", "influenced": "influenced_by"}[kind]
        p = pair.setdefault((x, mbid), {"strength": 0.0, "how": set()})
        p["strength"] = max(p["strength"], strength)
        p["how"].add(how)
        if x_name:
            other_names[x][x_name] += 1
    scored: dict[str, dict] = {}
    for (x, a), p in pair.items():
        s = scored.setdefault(x, {"mbid": x, "match": 0.0, "links": []})
        part = weight[a] * p["strength"]
        s["match"] += part
        s["links"].append((part, names[a], a, p["how"]))
    out = []
    for x, s in scored.items():
        if s["match"] <= 0:
            continue
        s["links"].sort(key=lambda t: -t[0])
        s["name"] = other_names[x].most_common(1)[0][0] if other_names[x] else x
        s["reasons"] = reasons_text(s["links"])
        s["count"] = len(s["links"])
        out.append(s)
    out.sort(key=lambda s: (-s["match"], s["name"]))
    out = out[:CANDIDATES]
    _cache[("matches", profile)] = (stamp, out)
    return out


def _and(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def reasons_text(links: list[tuple]) -> str:
    """'Similar to Nirvana, Pixies, and Mudhoney; influenced Nirvana' from the strongest links, and how many
    more there are."""
    shown = links[:SHOWN_REASONS]
    parts = []
    for how, lead in (("similar", "similar to"), ("influenced", "influenced"), ("influenced_by", "influenced by")):
        who = [name for _, name, _, hows in shown if how in hows]
        if who:
            parts.append(f"{lead} {_and(who)}")
    text = "; ".join(parts)
    more = len(links) - len(shown)
    if text and more > 0:
        text += f"; and {more} more of your artists"
    return (text[0].upper() + text[1:]) if text else ""


def rules_for(profile: int) -> dict:
    r = dict(RULES)
    try:
        r.update(json.loads(db.get_kv(f"suggestions_rules:{profile}") or "{}"))
    except ValueError:
        pass
    return {"max": max(1, min(int(r["max"]), 20)), "cutoff": max(1.0, min(float(r["cutoff"]), 5.0)),
            "total": max(1, min(int(r["total"]), 100))}


def save_rules(profile: int, max_songs: int, cutoff: float, total: int = RULES["total"]) -> dict:
    db.set_kv(f"suggestions_rules:{profile}", json.dumps({"max": max_songs, "cutoff": cutoff, "total": total}))
    return rules_for(profile)


def dismiss(profile: int, mbid: str, name: str | None) -> None:
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO suggestion_dismissed (profile, mbid, name, at) VALUES (?, ?, ?, ?)",
                  (profile, mbid, name, _now()))


def undismiss(profile: int, mbid: str) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM suggestion_dismissed WHERE profile = ? AND mbid = ?", (profile, mbid))


def dismissed(profile: int) -> list[dict]:
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM suggestion_dismissed WHERE profile = ? ORDER BY at DESC",
                                           (profile,))]


def card(mbid: str) -> dict:
    """What a suggestion shows beyond its reasons: picture, genres, a bio excerpt with its credit."""
    row = _rows([mbid]).get(mbid) or {}
    with db.connect() as c:
        bios = {r["source"]: dict(r) for r in c.execute("SELECT * FROM artist_bios WHERE mbid = ?", (mbid,))}
    bio = next((bios[s] for s in BIO_ORDER if s in bios), None)
    excerpt = None
    if bio:
        first = bio["text"].split("\n")[0].strip()
        excerpt = {"text": first if len(first) <= 320 else first[:300].rsplit(" ", 1)[0] + "...",
                   "label": BIO_SOURCES[bio["source"]][0], "url": bio["url"],
                   "license": bio["license"] or BIO_SOURCES[bio["source"]][1]}
    facts = row.get("facts") or {}
    return {"image": f"/artists/{mbid}/image" if row.get("image_file") else None,
            "credit": row.get("image_credit") or {}, "genres": (row.get("genres") or facts.get("genres") or [])[:4],
            "years": _years(row) if row else None, "origin": _origin(row) if row else None, "bio": excerpt,
            "listeners": row.get("listeners"),
            "has_data": "musicbrainz" in (row.get("fetched") or {})}


# ------------------------------------------------------------------ Get their hits

def albums_known(mbid: str) -> bool:
    return not _stale(_fetched_one(mbid), mbid, "discography", FACTS_EVERY)


def suggested_hits(mbid: str, profile: int = 1, rules: dict | None = None, st: dict | None = None,
                   owned: set | None = None) -> dict:
    """The songs Get their hits would add: each album's songs that pass the hits rules (charted, a single near
    its album, or listeners or Genius views at least the cutoff times the album's median), at most rules["max"]
    an album, each song once on its earliest album, minus songs already in the library."""
    rules = {**rules_for(profile), **(rules or {})}
    if not albums_known(mbid):
        return {"known": False, "songs": [], "owned": 0}
    with db.connect() as c:
        rels = [dict(r) for r in c.execute("SELECT * FROM artist_releases WHERE mbid = ?", (mbid,))]
        tracks: dict[str, list] = defaultdict(list)
        for r in c.execute("SELECT t.* FROM artist_tracks t JOIN artist_releases r ON r.rgid = t.rgid "
                           "WHERE r.mbid = ? ORDER BY t.rgid, t.disc, t.track", (mbid,)):
            tracks[r["rgid"]].append(dict(r))
        name = (c.execute("SELECT name FROM artists WHERE mbid = ?", (mbid,)).fetchone() or [None])[0] or ""
        if owned is None:
            owned = {r[0] for r in c.execute("SELECT DISTINCT song_key FROM library WHERE song_key IS NOT NULL")}
    # A page showing many suggestions loads the numbers once and passes them in.
    st = st or hits._load_stats(profile)
    groups = []
    recordings: dict[str, set] = defaultdict(set)
    for rel in rels:
        songs: dict[str, dict] = {}
        for t in tracks.get(rel["rgid"], []):
            if not t["recording"] or not t["title"]:
                continue
            key = library.song_key(name, t["title"], "")
            recordings[key].add(t["recording"])
            songs.setdefault(key, {"key": key, "title": t["title"], "norm_title": norm_title(t["title"]),
                                   "duration": (t["length"] or 0) / 1000 or None, "disc": t["disc"],
                                   "track": t["track"], "recording": t["recording"], "artists": {mbid}})
        if songs:
            groups.append({**rel, "songs": list(songs.values())})
    home: dict[str, tuple] = {}
    for g in groups:
        rank = (hits.KIND_ORDER.get(g["kind"], 3), g["year"] or 9999, -len(g["songs"]))
        for s in g["songs"]:
            if s["key"] not in home or rank < home[s["key"]][0]:
                home[s["key"]] = (rank, g["rgid"], g["title"])
    opts = {"min": 0, "max": rules["max"], "cutoff": rules["cutoff"]}
    ordered = sorted(groups, key=lambda g: (g["year"] or 9999, g["title"] or ""))
    for g in ordered:
        for s in g["songs"]:
            s["recordings"] = set(recordings[s["key"]])
            hits.signals(s, st, name, g["year"])
            s.update(skipped=False, elsewhere=None, out=None)
            if home[s["key"]][1] != g["rgid"]:
                s["out"] = f"On {home[s['key']][2]}"
            elif _OTHER_VERSION.search(s["title"]):
                s["out"] = "Another version"
    # A song has to beat the artist's typical album song too, not just the other songs on its release, or
    # the one known song on an EP of remixes and B-sides looks like a hit.
    album_songs = [s for g in ordered if g["kind"] in ("album", "soundtrack") for s in g["songs"] if not s["out"]]
    base = {k: statistics.median(v) if v else None for k, v in (
        ("lb", [s["lb"] for s in album_songs if s["lb"]]), ("views", [s["views"] for s in album_songs if s["views"]]))}
    picked, have = [], 0
    for g in ordered:
        hits.pick_album(g["songs"], g["kind"], opts, base=base, views_need=VIEWS_NEED_LISTENERS)
        for s in sorted(g["songs"], key=lambda s: (s["disc"] or 0, s["track"] or 0)):
            if not s["picked"]:
                continue
            if s["key"] in owned:
                have += 1
                continue
            picked.append({
                "key": f"mb:{s['recording']}", "isrc": None, "title": s["title"], "artist": name, "album": g["title"],
                "album_artist": name, "track_number": s["track"], "disc_number": s["disc"],
                "duration_ms": int(s["duration"] * 1000) if s["duration"] else None, "content_rating": None,
                "release_date": str(g["year"]) if g["year"] else None, "genre": None,
                "artwork_url": f"https://coverartarchive.org/release/{g['release_id']}/front-250" if g["release_id"] else None,
                "thumb_url": None, "url": f"https://musicbrainz.org/recording/{s['recording']}", "preview_url": None,
                "reasons": s["reasons"], "score": s["score"], "kind": g["kind"]})
    passing = len(picked)
    if passing > rules["total"]:
        # The strongest songs overall, kept in album order.
        best = {id(x) for x in sorted(picked, key=lambda x: -x["score"])[:rules["total"]]}
        picked = [x for x in picked if id(x) in best]
    return {"known": True, "songs": picked, "owned": have, "name": name, "passing": passing}


def _pending() -> list[tuple[int, str]]:
    try:
        return [(int(p), m) for p, m in json.loads(db.get_kv(PENDING_KEY) or "[]")]
    except (ValueError, TypeError):
        return []


def _update_pending(add: tuple | None = None, drop: set | None = None) -> None:
    """Change the waiting Get their hits list, one change at a time, so the job and a click don't lose one."""
    with _pending_lock:
        now = [x for x in _pending() if x not in (drop or set()) and x != add]
        if add:
            now.append(add)
        db.set_kv(PENDING_KEY, json.dumps([list(x) for x in now]))


def is_pending(profile: int, mbid: str) -> bool:
    return (profile, mbid) in _pending()


def get_hits(cfg: Config, mbid: str) -> int | None:
    """Get their hits: add the songs now when the artist's albums are known (returns how many), or look the
    artist up first and add them when that finishes (returns None)."""
    if albums_known(mbid):
        return add_hits(cfg, mbid)
    _update_pending(add=(cfg.profile, mbid))
    lookup_first(cfg, mbid)
    return None


def add_hits(cfg: Config, mbid: str) -> int:
    """Add a suggested artist's picked songs to its "<Artist> Hits" playlist and to Suggestions, then download
    both. Returns how many songs were new to the artist's playlist."""
    from . import sync
    got = suggested_hits(mbid, cfg.profile)
    if not got["songs"]:
        return 0
    one, every = sync.add_suggested(cfg, mbid, got["name"] or "Suggested artist")
    n = sync.add_suggested_songs(one, got["songs"])
    for pid in (one, every):
        sync.run_job(pid, "Downloading all", sync.sync, cfg, pid)
    return n
