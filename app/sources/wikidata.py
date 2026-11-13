"""Wikidata: influences (P737) in both directions, genres, dates, place of formation, the image, and the
English Wikipedia article. Data CC0."""
import logging

import httpx

from ..lookups import Http

log = logging.getLogger(__name__)
API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
BATCH = 50
LABEL_BATCH = 200
TIMEOUT = 60                      # seconds of query time Wikidata allows a query


def url(qid: str) -> str:
    return f"https://www.wikidata.org/wiki/{qid}"


def _items(claims: dict, prop: str) -> list[str]:
    out = []
    for cl in claims.get(prop) or []:
        v = (cl.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and v.get("id") and v["id"] not in out and cl.get("rank") != "deprecated":
            out.append(v["id"])
    return out


def _strings(claims: dict, prop: str) -> list[str]:
    return [v for v in ((cl.get("mainsnak") or {}).get("datavalue", {}).get("value") for cl in claims.get(prop) or [])
            if isinstance(v, str)]


def _year(claims: dict, prop: str) -> int | None:
    for cl in claims.get(prop) or []:
        v = (cl.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and v.get("time"):
            y = v["time"].lstrip("+").split("-")[0]
            if y.isdigit():
                return int(y)
    return None


def entities(http: Http, qids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(qids), BATCH):
        d = http.get(API, {"action": "wbgetentities", "ids": "|".join(qids[i:i + BATCH]),
                           "props": "claims|labels|sitelinks", "languages": "en", "sitefilter": "enwiki",
                           "format": "json"})
        out.update(parse_entities(d or {}))
    return out


def parse_entities(d: dict) -> dict[str, dict]:
    out = {}
    for qid, e in (d.get("entities") or {}).items():
        if "missing" in e:
            continue
        claims = e.get("claims") or {}
        out[qid] = {
            "qid": qid,
            "label": ((e.get("labels") or {}).get("en") or {}).get("value"),
            "enwiki": ((e.get("sitelinks") or {}).get("enwiki") or {}).get("title"),
            "influenced_by": _items(claims, "P737"),
            "genres": _items(claims, "P136"),
            "inception": _year(claims, "P571"),
            "dissolved": _year(claims, "P576"),
            "formed_in": (_items(claims, "P740") or [None])[0],
            "image": (_strings(claims, "P18") or [None])[0],
            "mbid": (_strings(claims, "P434") or [None])[0],
            "allmusic": (_strings(claims, "P1728") or [None])[0],
        }
    return out


def _bindings(d: dict) -> list[dict]:
    return [{k: v.get("value") for k, v in b.items()} for b in (d.get("results") or {}).get("bindings") or []]


def _qid(uri: str | None) -> str | None:
    return uri.rsplit("/", 1)[1] if uri and "/entity/Q" in uri else None


def _sparql(http: Http, query: str) -> dict:
    """Run a query. One that runs out of time answers 500 or times out, which raises httpx.HTTPError for the
    caller to split the batch; only 429 (slow down) is asked again."""
    r = http.send("POST", SPARQL, retry=(429,), data={"query": query}, timeout=TIMEOUT + 10,
                  headers={"Accept": "application/sparql-results+json"})
    r.raise_for_status()
    return r.json()


def labels(http: Http, qids: list[str]) -> dict[str, dict]:
    """Each item's English label and MusicBrainz artist ID, when it has one."""
    out: dict[str, dict] = {}
    for i in range(0, len(qids), LABEL_BATCH):
        q = ("SELECT ?item ?itemLabel ?mbid WHERE { VALUES ?item { " +
             " ".join(f"wd:{x}" for x in qids[i:i + LABEL_BATCH]) +
             ' } OPTIONAL { ?item wdt:P434 ?mbid } SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }')
        out.update(parse_labels(_sparql(http, q)))
    return out


def parse_labels(d: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for b in _bindings(d):
        q = _qid(b.get("item"))
        if not q:
            continue
        label = b.get("itemLabel")
        x = out.setdefault(q, {"label": None if label == q else label, "mbid": None})
        x["mbid"] = x["mbid"] or b.get("mbid")
    return out


def influenced(http: Http, qids: list[str]) -> dict[str, list[dict]]:
    """For each item, the items that name it under "influenced by" (the reverse of P737). Batches that run out
    of time are split in half and tried again."""
    out: dict[str, list[dict]] = {q: [] for q in qids}
    todo = [qids[i:i + BATCH] for i in range(0, len(qids), BATCH)]
    while todo:
        batch = todo.pop(0)
        q = ("SELECT ?target ?source ?sourceLabel ?mbid WHERE { VALUES ?target { " +
             " ".join(f"wd:{x}" for x in batch) + " } ?source wdt:P737 ?target . "
             'OPTIONAL { ?source wdt:P434 ?mbid } SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }')
        try:
            d = _sparql(http, q)
        except httpx.HTTPError:
            if len(batch) == 1:
                raise
            log.info("wikidata: influence query for %d items ran out of time, splitting it", len(batch))
            half = len(batch) // 2
            todo[:0] = [batch[:half], batch[half:]]
            continue
        for target, rows in parse_influenced(d).items():
            out.setdefault(target, []).extend(rows)
    return out


def parse_influenced(d: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    seen = set()
    for b in _bindings(d):
        t, s = _qid(b.get("target")), _qid(b.get("source"))
        if not t or not s or (t, s) in seen:
            continue
        seen.add((t, s))
        label = b.get("sourceLabel")
        out.setdefault(t, []).append({"qid": s, "label": None if label == s else label, "mbid": b.get("mbid")})
    return out
