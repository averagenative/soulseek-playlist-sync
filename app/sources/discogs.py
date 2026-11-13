"""Discogs: an artist's profile, with Discogs' link markup made plain. Shown as "Data provided by Discogs"
with a link back, as Discogs' API terms ask."""
import re

from ..lookups import Http, discogs_pace

API = "https://api.discogs.com"
CREDIT = "Data provided by Discogs"


def artist(http: Http, discogs_id: str, token: str | None = None) -> dict | None:
    discogs_pace(token)
    headers = {"Authorization": f"Discogs token={token}"} if token else {}
    d = http.get(f"{API}/artists/{discogs_id}", headers=headers,
                 refused=f"Discogs refused the request{' (check DISCOGS_TOKEN)' if token else ''}")
    return parse_artist(d) if d else None


def parse_artist(d: dict) -> dict:
    names = {m["id"]: m.get("name") for m in (d.get("members") or []) + (d.get("groups") or []) if m.get("id")}
    return {"id": d.get("id"), "name": d.get("name"), "url": d.get("uri"),
            "profile": plain(d.get("profile") or "", names),
            "members": [m.get("name") for m in d.get("members") or []],
            "groups": [g.get("name") for g in d.get("groups") or []]}


_NAMED = re.compile(r"\[([alg])=([^\]]+)\]")                 # [a=Kurt Cobain], [l=DGC]
_NUMBERED = re.compile(r"\[([almrg])=?(\d+)\]")               # [a12345], [m=13773], [r123]
_URL = re.compile(r"\[url=[^\]]*\](.*?)\[/url\]", re.I | re.S)
_STYLE = re.compile(r"\[/?(b|i|u|s)\]", re.I)


def plain(text: str, names: dict[int, str] | None = None) -> str:
    """Discogs markup as plain text. Numbered artist links become names when the profile lists them (members
    and groups); other numbered links (releases, masters, labels) are dropped."""
    names = names or {}
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _URL.sub(r"\1", t)
    t = _NAMED.sub(lambda m: re.sub(r"\s*\(\d+\)$", "", m.group(2)), t)       # "Name (2)" is Discogs' numbering
    t = _NUMBERED.sub(lambda m: (names.get(int(m.group(2))) or "") if m.group(1) == "a" else "", t)
    t = _STYLE.sub("", t)
    t = re.sub(r"[\"“”]\s*[\"“”]", "", t)                                       # quotes around a dropped title
    t = re.sub(r"[ \t]+([,.;:)])", r"\1", t)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()
