"""Wikimedia Commons: an image's thumbnail, license and author, for artist pictures."""
import html
import re

from ..lookups import Http

API = "https://commons.wikimedia.org/w/api.php"
BATCH = 50
WIDTH = 500


def _text(value: str | None) -> str:
    """Commons metadata is HTML: the author is often a link."""
    t = html.unescape(re.sub(r"<[^>]+>", "", value or ""))
    return " ".join(t.split())[:160]


def images(http: Http, files: list[str]) -> dict[str, dict]:
    """File name (as Wikidata gives it, without "File:") -> thumbnail and credit."""
    out: dict[str, dict] = {}
    for i in range(0, len(files), BATCH):
        batch = files[i:i + BATCH]
        d = http.get(API, {"action": "query", "titles": "|".join("File:" + f for f in batch), "prop": "imageinfo",
                           "iiprop": "url|extmetadata", "iiurlwidth": WIDTH, "format": "json", "formatversion": 2})
        out.update(parse_images(d or {}))
    return out


def _key(title: str) -> str:
    return title.split(":", 1)[-1].replace("_", " ")


def parse_images(d: dict) -> dict[str, dict]:
    out = {}
    for p in (d.get("query") or {}).get("pages") or []:
        info = (p.get("imageinfo") or [None])[0]
        if not info:
            continue
        meta = info.get("extmetadata") or {}
        get = lambda k: (meta.get(k) or {}).get("value")
        out[_key(p["title"])] = {
            "thumb": info.get("thumburl") or info.get("url"),
            "page": info.get("descriptionurl"),
            "license": _text(get("LicenseShortName")) or None,
            "license_url": get("LicenseUrl"),
            "author": _text(get("Artist")) or None,
            "attribution": (get("AttributionRequired") or "").lower() == "true",
        }
    return out
