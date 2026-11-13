"""What the background lookups share: an HTTP client that waits out rate limits, one pace per host that every
job keeps together, a status kept in the database, and a step runner where one service refusing stops only
its own step.

The hits lookup (app/hits.py) and the artist lookup (app/artists.py) both use MusicBrainz, which allows one
request a second from a client. Their requests go through the same Pacer, so together they stay under it.
"""
import json
import logging
import threading
import time
from urllib.parse import urlsplit

import httpx

from . import db

log = logging.getLogger(__name__)

UA = "soulseek-playlist-sync/1.0 ( https://github.com/averagenative/soulseek-playlist-sync )"
MB = "https://musicbrainz.org/ws/2"
LB = "https://api.listenbrainz.org/1"
GENIUS = "https://api.genius.com"
MAX_ERRORS = 10         # errors in a row before a step gives up for this run
RETRY = (429, 500, 502, 503, 504)      # answers worth asking again, after Retry-After

# Seconds between requests to each host, from each service's published limits or, where there are none,
# a polite pace. Discogs allows 25 requests a minute, or 60 with a token (see discogs_pace()).
PACES = {
    "musicbrainz.org": 1.1,
    "api.genius.com": 0.25,
    "api.discogs.com": 2.5,
    "www.wikidata.org": 0.5,
    "query.wikidata.org": 1.0,
    "en.wikipedia.org": 0.5,
    "commons.wikimedia.org": 0.5,
    "upload.wikimedia.org": 0.25,
    "ws.audioscrobbler.com": 0.25,
    "www.theaudiodb.com": 2.1,
    "labs.api.listenbrainz.org": 0.5,
}


class StepStopped(Exception):
    """One step can't go on (a service refused, or kept failing). The other steps still run."""


class GeniusRefused(Exception):
    pass


# ------------------------------------------------------------------ pacing

class Pacer:
    """At most one request every `every` seconds, across every thread that shares it. Each caller takes the
    next free slot, then sleeps until it comes."""

    def __init__(self, every: float):
        self.every = every
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.every
        if start > now:
            time.sleep(start - now)


_pacers: dict[str, Pacer] = {}
_pacers_lock = threading.Lock()


def pacer(host: str) -> Pacer:
    """The process-wide pacer for a host."""
    with _pacers_lock:
        p = _pacers.get(host)
        if p is None:
            p = _pacers[host] = Pacer(PACES.get(host, 0.0))
        return p


def discogs_pace(token: str | None) -> None:
    pacer("api.discogs.com").every = 1.05 if token else 2.5


# ------------------------------------------------------------------ HTTP

class Http:
    """One client per lookup run. Paces each host, retries 429 and 5xx answers after their Retry-After, and
    stops the step when a service refuses (401 or 403)."""

    def __init__(self, cfg=None):
        self.c = httpx.Client(timeout=30, headers={"User-Agent": UA}, follow_redirects=True)
        self.genius_token = getattr(cfg, "genius_token", None)
        self.lb_token = getattr(cfg, "listenbrainz_token", None)
        self.genius_left: int | None = None

    def close(self):
        self.c.close()

    def send(self, method: str, url: str, retry: tuple = RETRY, **kw) -> httpx.Response:
        p = pacer(urlsplit(url).netloc)
        r = None
        for attempt in range(5):
            p.wait()
            try:
                r = self.c.request(method, url, **kw)
            except httpx.TransportError:
                if attempt == 4 or not retry:
                    raise
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in retry and attempt < 4:
                wait = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset-In")
                try:
                    wait = float(wait)
                except (TypeError, ValueError):
                    wait = 5.0 * (attempt + 1)
                time.sleep(min(max(wait, 1.0), 120.0))
                continue
            return r
        return r

    def json(self, method: str, url: str, refused: str | None = None, **kw):
        """The answer's JSON, or None for 400 and 404. A 401 or 403 stops the step, saying `refused` (or which
        service refused)."""
        r = self.send(method, url, **kw)
        if r.status_code in (401, 403):
            raise StepStopped(refused or f"{urlsplit(url).netloc} refused the request ({r.status_code})")
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return r.json()

    def get(self, url: str, params: dict | None = None, refused: str | None = None, **kw):
        return self.json("GET", url, refused, params=params, **kw)

    # The services the hits lookup has used from the start.

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
        r = self.send("GET", MB + path, params={**params, "fmt": "json"})
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return r.json()

    def genius(self, path: str, params: dict):
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


# ------------------------------------------------------------------ status and steps

class Status:
    """A job's progress, kept in the database so every page and process sees it."""

    def __init__(self, key: str):
        self.key = key

    def get(self) -> dict:
        return json.loads(db.get_kv(self.key) or "{}")

    def set(self, **kw) -> None:
        s = self.get()
        s.update(kw)
        db.set_kv(self.key, json.dumps(s))

    def step(self, label: str, done: int = 0, total: int = 0) -> None:
        self.set(step=label, done=done, total=total)


class Steps:
    """Runs a job's steps. A step that stops says why in the status notes, and the job goes on."""

    def __init__(self, status: Status, name: str):
        self.status, self.name, self.notes = status, name, []

    def __call__(self, fn, *args):
        try:
            return fn(*args)
        except StepStopped as e:
            log.warning("%s: %s", self.name, e)
            self.note(str(e))
        except Exception as e:
            log.exception("%s: %s failed", self.name, getattr(fn, "__name__", fn))
            self.note(f"A step stopped: {str(e)[:200]}")
        return None

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)
        self.status.set(notes=self.notes)


class Errors:
    """Counts errors in a row for one step, and stops the step at MAX_ERRORS."""

    def __init__(self, label: str):
        self.label, self.n = label, 0

    def ok(self) -> None:
        self.n = 0

    def failed(self, e: Exception) -> None:
        self.n += 1
        if self.n >= MAX_ERRORS:
            raise StepStopped(f"{self.label} stopped after {MAX_ERRORS} errors in a row: {str(e)[:150]}") from e
