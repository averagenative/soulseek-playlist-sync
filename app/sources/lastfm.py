"""Last.fm: bios, tags and similar artists, with LASTFM_API_KEY. Bios are CC BY-SA: shown with credit and a
link."""
import html
import re

from ..lookups import Http, StepStopped

API = "https://ws.audioscrobbler.com/2.0/"
LICENSE = "CC BY-SA"
_NOT_FOUND = 6


def _call(http: Http, key: str, method: str, mbid: str | None, name: str | None, **extra) -> dict | None:
    params = {"method": method, "api_key": key, "format": "json", "autocorrect": 1, **extra}
    params.update({"mbid": mbid} if mbid else {"artist": name})
    r = http.send("GET", API, params=params)
    try:
        d = r.json()
    except ValueError:
        d = {}
    err = d.get("error") if isinstance(d, dict) else None
    if err in (10, 26) or r.status_code in (401, 403):
        raise StepStopped("Last.fm refused LASTFM_API_KEY. Check the key on https://www.last.fm/api/accounts.")
    if err == _NOT_FOUND or r.status_code == 404:
        return None
    if err:
        raise ValueError(f"Last.fm error {err}: {d.get('message')}")
    r.raise_for_status()
    return d


def _either(http, key, method, mbid, name, **extra):
    """By MusicBrainz ID, and by name when Last.fm doesn't know the ID."""
    d = _call(http, key, method, mbid, None, **extra) if mbid else None
    if d is None and name:
        d = _call(http, key, method, None, name, **extra)
    return d


def info(http: Http, key: str, mbid: str | None, name: str | None) -> dict | None:
    d = _either(http, key, "artist.getinfo", mbid, name)
    return parse_info(d) if d else None


def parse_info(d: dict) -> dict | None:
    a = d.get("artist") or {}
    bio = plain((a.get("bio") or {}).get("content") or "")
    return {"name": a.get("name"), "url": a.get("url"), "bio": bio or None,
            "tags": [t.get("name") for t in ((a.get("tags") or {}).get("tag") or []) if t.get("name")]}


def similar(http: Http, key: str, mbid: str | None, name: str | None) -> list[dict]:
    d = _either(http, key, "artist.getsimilar", mbid, name, limit=100)
    return parse_similar(d) if d else []


def parse_similar(d: dict) -> list[dict]:
    out = []
    for a in (d.get("similarartists") or {}).get("artist") or []:
        try:
            score = float(a.get("match") or 0)
        except ValueError:
            score = 0.0
        out.append({"mbid": a.get("mbid") or None, "name": a.get("name"), "score": round(score, 4), "url": a.get("url")})
    return out


_READ_MORE = re.compile(r'<a href="[^"]*">Read more on Last\.fm</a>\.?', re.I)
_LICENSE_NOTE = re.compile(r"User-contributed text is available under the Creative Commons By-SA License;?"
                           r"\s*additional terms may apply\.?", re.I)


def plain(text: str) -> str:
    t = _LICENSE_NOTE.sub("", _READ_MORE.sub("", text or ""))
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()
