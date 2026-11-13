"""TheAudioDB: an artist image when Wikimedia Commons has none, and a bio with a paid key. The free key
("123") is rate limited, so the lookup asks only for artists still without an image."""
from ..lookups import Http

FREE_KEY = "123"


def artist(http: Http, mbid: str, key: str | None = None) -> dict | None:
    d = http.get(f"https://www.theaudiodb.com/api/v1/json/{key or FREE_KEY}/artist-mb.php", {"i": mbid},
                 refused="TheAudioDB refused the request (check THEAUDIODB_API_KEY)" if key else None)
    return parse_artist(d or {}, paid=bool(key))


def parse_artist(d: dict, paid: bool = False) -> dict | None:
    a = (d.get("artists") or [None])[0]
    if not a:
        return None
    return {"thumb": a.get("strArtistThumb") or None,
            "bio": (a.get("strBiographyEN") or "").strip() or None if paid else None,
            "url": f"https://www.theaudiodb.com/artist/{a['idArtist']}" if a.get("idArtist") else None}
