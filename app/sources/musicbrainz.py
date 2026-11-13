"""MusicBrainz: artist facts, memberships, links to other sites, and discographies. Data CC0."""
import re

from ..match import norm
from ..lookups import Http

# MusicBrainz URL relation types -> the link kinds artist pages show.
LINK_KINDS = {
    "wikidata": "wikidata",
    "wikipedia": "wikipedia",
    "allmusic": "allmusic",
    "discogs": "discogs",
    "last.fm": "lastfm",
    "official homepage": "official",
    "bandcamp": "bandcamp",
    "image": "image",
}
# Membership attributes that aren't instruments.
_NOT_INSTRUMENTS = {"original", "founder", "founding", "additional", "guest", "minor"}
_GROUP_KINDS = {"Album": "album", "EP": "ep", "Single": "single"}
GROUP_PAGES = 3                    # 300 release groups is more than any artist worth suggesting has
SINGLE_PAGES = 3


def url(mbid: str) -> str:
    return f"https://musicbrainz.org/artist/{mbid}"


def mbid_from_link(text: str) -> str | None:
    """The artist ID in a pasted MusicBrainz link (or a bare ID)."""
    m = re.search(r"(?:musicbrainz\.org/artist/)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  (text or "").strip().lower())
    return m.group(1) if m else None


# ------------------------------------------------------------------ artists

def artist(http: Http, mbid: str) -> dict | None:
    d = http.mb(f"/artist/{mbid}", {"inc": "url-rels+artist-rels+genres+aliases"})
    return parse_artist(d) if d else None


def parse_artist(d: dict) -> dict:
    life = d.get("life-span") or {}
    genres = sorted(d.get("genres") or [], key=lambda g: (-(g.get("count") or 0), g.get("name") or ""))
    links: dict[str, str] = {}
    members: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    for r in d.get("relations") or []:
        if r.get("target-type") == "url":
            kind = LINK_KINDS.get(r.get("type"))
            res = (r.get("url") or {}).get("resource") or ""
            if r.get("type") == "lyrics" and "genius.com/artists/" in res:
                kind = "genius"
            if kind == "wikipedia" and "://en.wikipedia.org/" not in res:
                continue
            if kind and res and kind not in links:
                links[kind] = res
        elif r.get("target-type") == "artist" and r.get("type") == "member of band":
            other = r.get("artist") or {}
            # "backward": the other artist is a member of this band. "forward": this artist is in the other band.
            into = members if r.get("direction") == "backward" else groups
            m = into.setdefault(other.get("id"), {"mbid": other.get("id"), "name": other.get("name"),
                                                  "begin": None, "end": None, "current": False, "instruments": [],
                                                  "original": False})
            attrs = r.get("attributes") or []
            m["original"] = m["original"] or "original" in attrs
            for a in attrs:
                if a not in _NOT_INSTRUMENTS and a not in m["instruments"]:
                    m["instruments"].append(a)
            begin, end = (r.get("begin") or "")[:4] or None, (r.get("end") or "")[:4] or None
            if begin and (not m["begin"] or begin < m["begin"]):
                m["begin"] = begin
            if not r.get("ended") and not r.get("end"):
                m["current"] = True
            if end and (not m["end"] or end > m["end"]):
                m["end"] = end
    for m in list(members.values()) + list(groups.values()):
        if m["current"]:
            m["end"] = None
    order = lambda m: (m["begin"] or "9999", m["name"] or "")
    return {
        "mbid": d.get("id"), "name": d.get("name"), "sort_name": d.get("sort-name"), "type": d.get("type"),
        "area": (d.get("area") or {}).get("name"), "begin_area": (d.get("begin-area") or {}).get("name"),
        "begin": life.get("begin"), "end": life.get("end"), "ended": bool(life.get("ended")),
        "disambiguation": d.get("disambiguation") or "",
        "genres": [g["name"] for g in genres if g.get("name")][:8],
        "aliases": [a.get("name") for a in d.get("aliases") or [] if a.get("name")][:10],
        "links": links,
        "members": sorted(members.values(), key=order),
        "member_of": sorted(groups.values(), key=order),
    }


def wikidata_id(links: dict) -> str | None:
    m = re.search(r"/(Q\d+)$", links.get("wikidata") or "")
    return m.group(1) if m else None


def discogs_id(links: dict) -> str | None:
    m = re.search(r"discogs\.com/artist/(\d+)", links.get("discogs") or "")
    return m.group(1) if m else None


def commons_file(links: dict) -> str | None:
    m = re.search(r"commons\.wikimedia\.org/wiki/File:(.+)$", links.get("image") or "")
    return m.group(1).replace("_", " ") if m else None


# ------------------------------------------------------------------ finding an artist

def _lucene(s: str) -> str:
    return re.sub(r'([\\"])', r"\\\1", s or "")


def find_artist(http: Http, name: str, album: str) -> str | None:
    """The artist's ID, from a release group search for one of its albums. Only a release group credited to an
    artist with the same name counts, so same-named artists with other albums don't."""
    d = http.mb("/release-group", {"query": f'releasegroup:"{_lucene(album)}" AND artist:"{_lucene(name)}"',
                                   "limit": 10})
    return parse_find(d or {}, name)


def parse_find(d: dict, name: str) -> str | None:
    want = norm(name)
    for g in d.get("release-groups") or []:
        for credit in g.get("artist-credit") or []:
            a = credit.get("artist") or {}
            if a.get("id") and want in (norm(credit.get("name")), norm(a.get("name"))):
                return a["id"]
    return None


# ------------------------------------------------------------------ discographies

def discography(http: Http, mbid: str) -> dict:
    """An artist's albums, EPs and singles, each with one tracklist: an album's or EP's earliest official
    release (so a deluxe edition's bonus tracks don't count), and a single's title songs.

    Returns {"groups": every release group (for hits.singles_from_groups), "releases": [...]}."""
    groups: list[dict] = []
    for page in range(GROUP_PAGES):
        d = http.mb("/release-group", {"artist": mbid, "limit": 100, "offset": page * 100})
        if not d:
            break
        groups += d.get("release-groups") or []
        if len(groups) >= (d.get("release-group-count") or 0):
            break
    wanted = [g for g in groups if _group_kind(g)]
    releases = []
    for g in wanted:
        kind = _group_kind(g)
        if kind == "single":
            continue
        d = http.mb("/release", {"release-group": g["id"], "status": "official", "inc": "recordings+media",
                                 "limit": 100})
        rel = earliest((d or {}).get("releases") or [])
        releases.append(release_entry(g, kind, rel))
    singles = {g["id"]: g for g in wanted if _group_kind(g) == "single"}
    if singles:
        found: dict[str, list] = {}
        for page in range(SINGLE_PAGES):
            d = http.mb("/release", {"artist": mbid, "type": "single", "status": "official",
                                     "inc": "recordings+media+release-groups", "limit": 100, "offset": page * 100})
            if not d:
                break
            for r in d.get("releases") or []:
                rg = (r.get("release-group") or {}).get("id")
                if rg in singles:
                    found.setdefault(rg, []).append(r)
            if (page + 1) * 100 >= (d.get("release-count") or 0):
                break
        for rg, g in singles.items():
            releases.append(release_entry(g, "single", earliest(found.get(rg) or [])))
    return {"groups": groups, "releases": releases}


def _group_kind(g: dict) -> str | None:
    """album, ep, single or soundtrack; None for compilations, live albums, remixes, DJ mixes and demos."""
    kind = _GROUP_KINDS.get(g.get("primary-type"))
    secondary = set(g.get("secondary-types") or [])
    if not kind or secondary - {"Soundtrack"}:
        return None
    return "soundtrack" if "Soundtrack" in secondary and kind == "album" else kind


def earliest(releases: list[dict]) -> dict | None:
    """The first official release: by date (a year alone counts as its start), then fewest tracks."""
    dated = [r for r in releases if r.get("date")]
    pool = dated or releases
    if not pool:
        return None
    return min(pool, key=lambda r: (r.get("date") or "9999", sum(len(m.get("tracks") or []) for m in r.get("media") or [])))


def release_entry(g: dict, kind: str, rel: dict | None) -> dict:
    year = (g.get("first-release-date") or "")[:4]
    tracks = []
    for disc, m in enumerate((rel or {}).get("media") or [], 1):
        for t in m.get("tracks") or []:
            rec = t.get("recording") or {}
            tracks.append({"disc": m.get("position") or disc, "track": t.get("position"), "recording": rec.get("id"),
                           "title": t.get("title") or rec.get("title"), "length": t.get("length") or rec.get("length")})
    if kind == "single" and tracks:
        tracks = single_songs(g.get("title") or "", tracks)
    return {"rgid": g["id"], "title": g.get("title"), "kind": kind, "year": int(year) if year.isdigit() else None,
            "release_id": (rel or {}).get("id"), "tracks": tracks}


def single_songs(title: str, tracks: list[dict]) -> list[dict]:
    """A single's title songs: the tracks named in its title ("A / B" names two), or else its first track."""
    from ..hits import _parts
    from ..match import norm_title
    names = {norm_title(p) for p in _parts(title)}
    named = [t for t in tracks if norm_title(t["title"]) in names]
    return named or tracks[:1]
