"""Wikipedia: an article's introduction as plain text. Text CC BY-SA 4.0: shown with credit and a link."""
from urllib.parse import quote

from ..lookups import Http

API = "https://en.wikipedia.org/w/api.php"
BATCH = 20                        # the most introductions one request returns
LICENSE = "CC BY-SA 4.0"


def url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="()_,'!:")


def intros(http: Http, titles: list[str]) -> dict[str, dict]:
    """Requested title -> {"text", "url"}; titles with no article are left out."""
    out: dict[str, dict] = {}
    for i in range(0, len(titles), BATCH):
        batch = titles[i:i + BATCH]
        d = http.get(API, {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "redirects": 1,
                           "exlimit": BATCH, "titles": "|".join(batch), "format": "json", "formatversion": 2})
        out.update(parse_intros(d or {}, batch))
    return out


def parse_intros(d: dict, asked: list[str]) -> dict[str, dict]:
    q = d.get("query") or {}
    # Follow the renames back to what was asked: "Nirvana_(band)" -> "Nirvana (band)" -> a redirect's target.
    back = {t: t for t in asked}
    for step in (q.get("normalized") or []) + (q.get("redirects") or []):
        for orig, now in list(back.items()):
            if now == step.get("from"):
                back[orig] = step.get("to")
    by_title = {p.get("title"): p for p in q.get("pages") or [] if not p.get("missing")}
    out = {}
    for orig, title in back.items():
        p = by_title.get(title)
        text = (p or {}).get("extract", "").strip()
        if text:
            out[orig] = {"text": text, "url": url(title)}
    return out
