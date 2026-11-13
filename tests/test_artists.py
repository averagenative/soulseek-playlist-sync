"""Artist pages and suggestions: the source clients against recorded answers, the lookup job with a fake
client, the pages, suggestions, and the Suggestions playlists."""
import importlib
import json
import os
import threading
import time
import uuid
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from PIL import Image

from app import artists, config, db, hits, library, lookups, settings, sync
from app.lookups import StepStopped
from app.sources import audiodb, commons, discogs, lastfm, lb_labs, musicbrainz, wikidata, wikipedia

F = Path(__file__).parent / "fixtures" / "artists"
NIRVANA = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
NAILS = "a7d9c5f6-751d-4ea5-bf6d-39f1751d44a1"
PIXIES = "b6b2bb8d-54a9-491f-9607-7b546023b433"
STOOGES = "794c6bf2-3241-416f-9b8f-24e2d84a1c4b"
MELVINS = "9ccfbc94-a4f4-42f7-b6f5-d903ab77cccb"


def fx(name: str):
    return json.loads((F / name).read_text())


def rid(n) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_OID, str(n)))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path / "music"))
    base = config.load()
    db.init(base.db_path)
    settings.init(base)
    artists._cache.clear()
    artists._priority.clear()
    library._artist_cache.clear()
    return base


def songs(cfg, artist, album, titles, mbid=None, year=1991, album_artist=None):
    """Index an album of songs by an artist, tagged with its MusicBrainz ID when given."""
    rows = []
    for i, t in enumerate(titles, 1):
        p = cfg.music_root / (album_artist or artist) / album / f"{i:02d} {t}.mp3"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"ID3")
        rows.append({"path": str(p), "size": 3, "mtime": p.stat().st_mtime, "artist": artist,
                     "album_artist": album_artist or artist, "title": t, "album": album, "duration": 200.0,
                     "track_number": i, "year": str(year), "mb_artist": mbid, "mb_recording": rid(f"{artist}|{t}"),
                     "release_type": "album"})
    library._write(rows)


def jpeg() -> bytes:
    b = BytesIO()
    Image.new("RGB", (800, 600), (120, 40, 200)).save(b, "JPEG")
    return b.getvalue()


# ------------------------------------------------------------------ the shared lookup layer

def test_two_jobs_share_the_musicbrainz_pace(monkeypatch):
    p = lookups.pacer("musicbrainz.org")
    assert p.every >= 1.0                                   # MusicBrainz allows one request a second
    monkeypatch.setattr(p, "every", 0.05)
    stamps, lock = [], threading.Lock()

    def job():
        for _ in range(6):
            p.wait()
            with lock:
                stamps.append(time.monotonic())
    threads = [threading.Thread(target=job) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    assert len(stamps) == 12 and min(b - a for a, b in zip(stamps, stamps[1:])) >= 0.045

    # The hits lookup's client and the artist lookup's go through that same pacer.
    waits = []
    monkeypatch.setattr(p, "wait", lambda: waits.append(1))
    for http in (hits._Http(None), artists._Http(None)):
        http.c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        http.mb("/artist/x", {})
    assert len(waits) == 2


def test_a_refusal_stops_the_step_and_429_waits(monkeypatch):
    calls = []

    def handler(r):
        calls.append(r.url.host)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(403)
    http = lookups.Http(None)
    http.c = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(lookups.time, "sleep", lambda s: None)
    with pytest.raises(StepStopped, match="refused"):
        http.get("https://api.discogs.com/artists/1")
    assert len(calls) == 2


# ------------------------------------------------------------------ source clients

def test_musicbrainz_facts_members_and_links():
    a = musicbrainz.parse_artist(fx("mb_artist_nirvana.json"))
    assert (a["type"], a["begin"], a["end"], a["ended"]) == ("Group", "1987", "1994-04-05", True)
    assert (a["begin_area"], a["area"]) == ("Aberdeen", "United States")
    assert "grunge" in a["genres"]
    assert {"wikidata", "allmusic", "discogs", "lastfm", "official", "image"} <= set(a["links"])
    assert musicbrainz.wikidata_id(a["links"]) == "Q11649" and musicbrainz.discogs_id(a["links"]) == "125246"
    assert musicbrainz.commons_file(a["links"]) == "Nirvana around 1992.jpg"
    m = {x["name"]: x for x in a["members"]}
    assert (m["Dave Grohl"]["begin"], m["Dave Grohl"]["end"]) == ("1990", "1994")
    assert "drums (drum set)" in m["Dave Grohl"]["instruments"] and "guitar" in m["Kurt Cobain"]["instruments"]
    assert m["Kurt Cobain"]["original"] and "original" not in m["Kurt Cobain"]["instruments"]
    nails = {x["name"]: x for x in musicbrainz.parse_artist(fx("mb_artist_nails.json"))["members"]}
    # Two relations for one person become one member, still in the band.
    assert nails["Todd Jones"]["end"] is None and set(nails["Todd Jones"]["instruments"]) == {"lead vocals", "electric guitar"}
    assert musicbrainz.mbid_from_link(f"https://musicbrainz.org/artist/{NAILS}/releases") == NAILS
    assert musicbrainz.mbid_from_link("https://example.com/") is None


def test_musicbrainz_finds_an_artist_only_by_its_own_album():
    d = fx("mb_search_nails.json")
    assert musicbrainz.parse_find(d, "Nails") == NAILS
    assert musicbrainz.parse_find(d, "Nail Polish") is None


def test_musicbrainz_discography_uses_the_earliest_release():
    class Http:
        def mb(self, path, params):
            if path == "/release-group":
                return fx("mb_groups_nails.json")
            if params.get("release-group") == "6d2da807-879c-4ce4-b9dc-797162c56456":
                return fx("mb_release_browse_unsilent.json")
            if params.get("type") == "single":
                return fx("mb_singles_nails.json")
            return {"releases": []}
    d = musicbrainz.discography(Http(), NAILS)
    rel = {r["title"]: r for r in d["releases"]}
    assert {r["kind"] for r in d["releases"]} == {"album", "ep", "single"}
    # Six official releases; the 2010 CD's 10 songs, not the 2012 Japanese edition's 17 or 2020's 15.
    assert len(rel["Unsilent Death"]["tracks"]) == 10 and rel["Unsilent Death"]["year"] == 2010
    # A single's title song, even when it isn't the first track.
    assert [t["title"] for t in rel["Fear Based Life"]["tracks"]] == ["Fear Based Life"]
    assert [t["title"] for t in rel["Give Me the Painkiller"]["tracks"]] == ["Give Me the Painkiller"]


def test_wikidata_entities_labels_and_reverse_influences():
    e = wikidata.parse_entities(fx("wd_entities.json"))["Q11649"]
    assert e["enwiki"] == "Nirvana (band)" and e["inception"] == 1987 and e["dissolved"] is None
    assert e["image"] == "Nirvana around 1992.jpg" and e["mbid"] == NIRVANA and len(e["influenced_by"]) == 8
    labels = wikidata.parse_labels(fx("wd_labels.json"))
    assert labels["Q188464"] == {"label": "Pixies", "mbid": PIXIES}
    inf = wikidata.parse_influenced(fx("wd_influenced.json"))
    assert len(inf["Q11649"]) == 33 and inf["Q17325289"][0]["label"] == "Faceplant"


def test_wikidata_splits_a_query_that_runs_out_of_time():
    asked = []

    class Http:
        def send(self, method, url, retry=None, **kw):
            q = kw["data"]["query"]
            n = q.count("wd:Q")
            asked.append(n)
            req = httpx.Request("POST", url)
            if n > 1:
                return httpx.Response(500, request=req)
            return httpx.Response(200, request=req, json={"results": {"bindings": []}})
    got = wikidata.influenced(Http(), ["Q1", "Q2", "Q3"])
    assert set(got) == {"Q1", "Q2", "Q3"} and asked.count(1) == 3


def test_wikipedia_intros_follow_renames():
    d = fx("wp_intros.json")
    d["query"]["normalized"] = [{"from": "Nirvana_(band)", "to": "Nirvana (band)"}]
    got = wikipedia.parse_intros(d, ["Nirvana_(band)", "Nails (band)", "No Such Band"])
    assert got["Nirvana_(band)"]["url"] == "https://en.wikipedia.org/wiki/Nirvana_(band)"
    assert got["Nirvana_(band)"]["text"].startswith("Nirvana was an American rock band")
    assert "Nails (band)" in got and "No Such Band" not in got


def test_commons_credit():
    got = commons.parse_images(fx("commons.json"))["Nirvana around 1992.jpg"]
    assert (got["author"], got["license"], got["attribution"]) == ("P.B. Rage from USA", "CC BY-SA 2.0", True)
    assert got["page"] == "https://commons.wikimedia.org/wiki/File:Nirvana_around_1992.jpg"


def test_listenbrainz_labs_scores_and_the_token_retry():
    got = lb_labs.parse_similar(fx("lb_similar_nirvana.json"), NIRVANA)
    assert got[0] == {"mbid": "8bfac288-ccc5-448d-9573-c33ea2aa5c30", "name": "Red Hot Chili Peppers", "score": 1.0}
    assert all(0 < x["score"] <= 1 for x in got)

    class Http:
        def __init__(self, accept):
            self.accept, self.sent = accept, []

        def send(self, method, url, **kw):
            token = kw.get("headers", {}).get("Authorization")
            self.sent.append(token)
            req = httpx.Request("GET", url)
            if token and self.accept:
                return httpx.Response(200, request=req, json=fx("lb_similar_nirvana.json"))
            return httpx.Response(401, request=req)
    http = Http(accept=True)
    labs = lb_labs.Labs(http, "tok", lastfm=False)
    assert labs.similar(NIRVANA) and labs.similar(NAILS) is not None
    assert http.sent == [None, "Token tok", "Token tok"]          # kept for the rest of the run
    with pytest.raises(StepStopped) as e:
        lb_labs.Labs(Http(accept=False), "tok", lastfm=False).similar(NIRVANA)
    assert "ListenBrainz refused the similar-artists request (401)" in str(e.value)
    assert "LASTFM_API_KEY" in str(e.value) and "https://www.last.fm/api/account/create" in str(e.value)
    with pytest.raises(StepStopped, match="only from Last.fm"):
        lb_labs.Labs(Http(accept=False), None, lastfm=True).similar(NIRVANA)


def test_discogs_markup_becomes_plain_text():
    assert discogs.plain("Formed by [a=Kurt Cobain] and [a203017] on [l=Sub Pop]. LP \"[m=13773]\" out.",
                         {203017: "Dave Grohl"}) == "Formed by Kurt Cobain and Dave Grohl on Sub Pop. LP out."
    assert discogs.plain("[url=http://x.com]their site[/url] [b]loud[/b] [a=Nails (2)]") == "their site loud Nails"
    d = discogs.parse_artist(fx("discogs_nirvana.json"))
    assert d["url"] == "https://www.discogs.com/artist/125246-Nirvana" and "[" not in d["profile"]
    assert "Dave Grohl" in d["members"]


def test_lastfm_bio_without_its_link_and_notice():
    info = lastfm.parse_info(fx("lastfm_info_nirvana.json"))
    assert info["url"] == "https://www.last.fm/music/Nirvana" and "Grunge" in info["tags"]
    assert "Read more on Last.fm" not in info["bio"] and "User-contributed text" not in info["bio"] and "<a" not in info["bio"]
    sim = lastfm.parse_similar(fx("lastfm_similar_nirvana.json"))
    assert sim[0]["score"] == 1.0 and isinstance(sim[1]["score"], float)


def test_audiodb_image_and_no_free_bio():
    a = audiodb.parse_artist(fx("audiodb_nirvana.json"))
    assert a["thumb"].startswith("https://r2.theaudiodb.com/") and a["bio"] is None
    assert audiodb.parse_artist({"artists": None}) is None


# ------------------------------------------------------------------ the lookup job

class Resp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self.data, self.content, self.headers = status, data, content, {}
        self.request = httpx.Request("GET", "http://fake")

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(str(self.status_code), request=self.request,
                                        response=httpx.Response(self.status_code, request=self.request))


class FakeHttp:
    """Every source, answered from the recorded responses."""

    def __init__(self):
        self.calls, self.refuse = [], set()
        self.lb_token, self.genius_token, self.genius_left = None, None, None

    def close(self):
        pass

    def mb(self, path, params):
        self.calls.append(("musicbrainz.org", path))
        if path.startswith("/artist/"):
            return {NIRVANA: fx("mb_artist_nirvana.json"), NAILS: fx("mb_artist_nails.json")}.get(path.split("/")[2])
        if path == "/release-group" and "query" in params:
            return fx("mb_search_nails.json") if 'artist:"Nails"' in params["query"] else {"release-groups": []}
        if path == "/release-group" and params.get("artist") == NAILS:
            return fx("mb_groups_nails.json")
        if path == "/release" and params.get("release-group") == "6d2da807-879c-4ce4-b9dc-797162c56456":
            return fx("mb_release_browse_unsilent.json")
        if path == "/release" and params.get("artist") == NAILS and params.get("type") == "single":
            return fx("mb_singles_nails.json")
        return {"release-groups": [], "release-group-count": 0, "releases": []}

    def get(self, url, params=None, refused=None, **kw):
        host = urlsplit(url).netloc
        self.calls.append((host, url))
        if host in self.refuse:
            raise StepStopped(refused or f"{host} refused the request (403)")
        if "wikidata.org/w/api.php" in url:
            ids = params["ids"].split("|")
            return {"entities": {k: v for k, v in fx("wd_entities.json")["entities"].items() if k in ids}}
        if "wikipedia.org" in url:
            return fx("wp_intros.json")
        if "commons.wikimedia.org" in url:
            return fx("commons.json")
        if "discogs.com" in url:
            return fx("discogs_nirvana.json") if url.endswith("/125246") else fx("discogs_nails.json")
        if "theaudiodb.com" in url:
            return fx("audiodb_nirvana.json") if params["i"] == NIRVANA else fx("audiodb_nails.json")
        raise AssertionError(url)

    def send(self, method, url, retry=None, **kw):
        host = urlsplit(url).netloc
        self.calls.append((host, url))
        if host in self.refuse:
            return Resp(401)
        if host == "query.wikidata.org":
            q = kw["data"]["query"]
            return Resp(data=fx("wd_influenced.json") if "wdt:P737 ?target" in q else fx("wd_labels.json"))
        if host == "labs.api.listenbrainz.org":
            return Resp(data=fx("lb_similar_nirvana.json") if kw["params"]["artist_mbids"] == NIRVANA else [])
        if host == "ws.audioscrobbler.com":
            name = "nirvana" if kw["params"].get("mbid") == NIRVANA else "nails"
            kind = "info" if kw["params"]["method"] == "artist.getinfo" else "similar"
            return Resp(data=fx(f"lastfm_{kind}_{name}.json"))
        return Resp(content=jpeg())                           # pictures

    def lb(self, method, path, **kw):
        self.calls.append(("api.listenbrainz.org", path))
        return []


@pytest.fixture
def fake(cfg, monkeypatch):
    f = FakeHttp()
    monkeypatch.setattr(artists, "_Http", lambda cfg: f)
    return f


def library_of_three(cfg):
    songs(cfg, "Nirvana", "Nevermind", ["Smells Like Teen Spirit", "In Bloom", "Lithium"], mbid=NIRVANA)
    songs(cfg, "Nirvana feat. Someone", "Nevermind Extras", ["Extra"], mbid=NIRVANA)
    songs(cfg, "Pixies", "Doolittle", ["Debaser", "Hey"], mbid=PIXIES, year=1989)
    songs(cfg, "Nails", "Unsilent Death", ["Conform", "Scum Will Rise"], year=2010)     # no ID in the tags


def test_lookup_fills_the_cache_and_a_second_run_asks_nothing(cfg, fake):
    library_of_three(cfg)
    artists.research(cfg)
    st = artists.status()
    assert st["state"] == "done" and st["full_finished"]
    with db.connect() as c:
        names = {r["name"]: (r["mbid"], r["how"]) for r in c.execute("SELECT * FROM artist_names")}
        bios = {(r["mbid"], r["source"]) for r in c.execute("SELECT mbid, source FROM artist_bios")}
        links = {(r["mbid"], r["kind"]): r["n"] for r in c.execute(
            "SELECT mbid, kind, COUNT(*) AS n FROM artist_links GROUP BY mbid, kind")}
        row = dict(c.execute("SELECT * FROM artists WHERE mbid = ?", (NIRVANA,)).fetchone())
    assert names["nails"] == (NAILS, "search")                          # found through its album
    assert {(NIRVANA, "wikipedia"), (NIRVANA, "discogs"), (NAILS, "wikipedia")} <= bios
    assert links[(NIRVANA, "influenced_by")] == 8 and links[(NIRVANA, "influenced")] == 33
    assert links[(NIRVANA, "member")] == 9 and links[(NIRVANA, "similar")] == 25
    assert row["enwiki"] == "Nirvana (band)" and row["image_file"] == f"{NIRVANA}.jpg"
    assert json.loads(row["image_credit"])["author"] == "P.B. Rage from USA"
    with Image.open(cfg.artwork_dir / "artists" / f"{NIRVANA}.jpg") as im:
        assert im.size[0] <= artists.IMAGE_WIDTH
    fake.calls.clear()
    artists.research(cfg)
    assert fake.calls == []


def test_only_stale_data_is_fetched_again(cfg, fake):
    library_of_three(cfg)
    artists.research(cfg)
    with db.connect() as c:
        f = json.loads(c.execute("SELECT fetched FROM artists WHERE mbid = ?", (NIRVANA,)).fetchone()[0])
        f["listenbrainz"] = f["wikipedia"] = time.time() - 61 * 86400
        c.execute("UPDATE artists SET fetched = ? WHERE mbid = ?", (json.dumps(f), NIRVANA))
    fake.calls.clear()
    artists.research(cfg)
    hosts = [h for h, _ in fake.calls]
    assert hosts == ["labs.api.listenbrainz.org"]            # similar artists at 61 days; the bio waits for 180


def test_a_refused_source_stops_only_its_step(cfg, fake):
    library_of_three(cfg)
    fake.refuse = {"labs.api.listenbrainz.org"}
    artists.research(cfg)
    notes = artists.status()["notes"]
    assert any("ListenBrainz refused the similar-artists request (401)" in n and "LASTFM_API_KEY" in n for n in notes)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM artist_bios").fetchone()[0] >= 3        # bios still came
        assert c.execute("SELECT COUNT(*) FROM artist_links WHERE kind = 'similar'").fetchone()[0] == 0
    # Only one refusal: the step stopped instead of asking for every artist.
    assert [h for h, _ in fake.calls].count("labs.api.listenbrainz.org") == 1


def test_names_that_match_nothing_wait_30_days_and_a_pasted_id_wins(cfg, fake):
    songs(cfg, "Obscure Band", "Demo Tape", ["One"])
    artists.research(cfg)
    with db.connect() as c:
        assert dict(c.execute("SELECT mbid, how FROM artist_names WHERE name = 'obscure band'").fetchone()) == \
            {"mbid": "", "how": "search"}
    fake.calls.clear()
    artists.research(cfg)
    assert not [p for _, p in fake.calls if p == "/release-group"]            # not again within 30 days
    with db.connect() as c:
        c.execute("UPDATE artist_names SET checked_at = ?", (time.time() - 31 * 86400,))
    artists.research(cfg)
    assert [p for _, p in fake.calls if p == "/release-group"]
    # A pasted link is used, and later lookups leave it alone.
    with db.connect() as c:
        c.execute("UPDATE artist_names SET mbid = ?, how = 'manual', checked_at = 0 WHERE name = 'obscure band'", (NAILS,))
    artists.research(cfg)
    assert library.artist_for("Obscure Band")["mbid"] == NAILS
    with db.connect() as c:
        assert c.execute("SELECT how FROM artist_names WHERE name = 'obscure band'").fetchone()[0] == "manual"


def test_an_artist_someone_opens_is_looked_up_first(cfg, fake):
    library_of_three(cfg)
    artists._priority[:] = [NAILS]
    artists.research(cfg, full=False)
    with db.connect() as c:
        done = {r[0] for r in c.execute("SELECT mbid FROM artists WHERE fetched LIKE '%\"musicbrainz\"%'")}
    assert done == {NAILS} and not artists._priority


# ------------------------------------------------------------------ pages

@pytest.fixture
def client(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WORKER", "0")
    monkeypatch.setenv("SLSKD_URL", "http://127.0.0.1:9")
    import app.web as web
    importlib.reload(web)
    with TestClient(web.app) as c:
        # The app scans the music folder when it starts; let that finish before a test adds songs.
        end = time.time() + 10
        while time.time() < end and not (db.get_kv("library_scan") or "").startswith("done"):
            time.sleep(0.02)
        yield c


def test_the_artist_page(cfg, fake, client, monkeypatch):
    library_of_three(cfg)
    artists.research(cfg)
    monkeypatch.setattr(artists, "lookup_first", lambda cfg, target: None)
    page = client.get(f"/artists/{NIRVANA}").text
    assert "Group, 1987 to 1994, from Aberdeen, United States" in page
    assert 'Wikipedia</a>, CC BY-SA 4.0' in page and "Nirvana was an American rock band" in page
    assert "The bio from Discogs" in page and "Data provided by Discogs" in page
    assert "Dave Grohl" in page and "1990 to 1994" in page
    assert f'href="/artists/{PIXIES}"' in page                               # in the library: its page
    assert f'href="https://musicbrainz.org/artist/{STOOGES}"' in page and "Not in your library" in page
    assert "https://www.allmusic.com/artist/" in page                        # a link, and no AllMusic text
    assert "Picture: P.B. Rage from USA" in page
    assert "Your albums" in page and "Nevermind" in page
    assert "Fans of Nirvana also have" in page and "Red Hot Chili Peppers" in page
    # The local inverse: Pixies' page lists Nirvana under Influenced, though Wikidata only says it on Nirvana's.
    pixies = client.get(f"/artists/{PIXIES}").text
    assert f'href="/artists/{NIRVANA}"' in pixies.split("<h2>Influenced</h2>")[1].split("</div>")[0]
    # Nails has no influences of its own.
    assert "No influences are known for Nails on Wikidata." in client.get(f"/artists/{NAILS}").text


def test_pages_for_artists_without_data(cfg, client, monkeypatch):
    asked = []
    monkeypatch.setattr(artists, "lookup_first", lambda cfg, target: asked.append(target))
    songs(cfg, "Obscure Band", "Demo Tape", ["One"])
    unknown = rid("nobody")
    page = client.get(f"/artists/{unknown}").text
    assert "Looking up" in page and asked == [unknown]
    page = client.get("/artists/by-name?n=Obscure Band").text
    assert "Demo Tape" in page and "Paste their MusicBrainz link" in page and asked[-1] == "name:obscure band"
    r = client.post("/artists/identify", data={"name": "obscure band", "link": f"https://musicbrainz.org/artist/{NAILS}"},
                    follow_redirects=False)
    assert r.headers["location"] == f"/artists/{NAILS}"
    assert client.get("/artists/by-name?n=Obscure Band", follow_redirects=False).headers["location"] == f"/artists/{NAILS}"


def test_the_artists_index_and_linked_names(cfg, client):
    library_of_three(cfg)
    page = client.get("/artists?q=nir").text
    assert "Nirvana" in page and "Pixies" not in page
    assert client.get("/artists").text.count(">Nirvana</b>") == 1             # "Nirvana feat. Someone" too
    assert "Pixies" in client.get("/artists?letter=P").text
    # Artist names on My music link to their pages.
    assert "/artists/by-name?n=Nirvana" in client.get("/library").text
    r = client.get("/artists/by-name?n=Nirvana feat. Someone", follow_redirects=False)
    assert r.headers["location"] == f"/artists/{NIRVANA}"
    assert client.get("/partials/artists").status_code == 200


# ------------------------------------------------------------------ suggestions

def link(c, a, kind, other, name, score=None, source="listenbrainz"):
    c.execute("INSERT INTO artist_links (mbid, kind, other_key, other_mbid, other_name, source, score) "
              "VALUES (?, ?, ?, ?, ?, ?, ?)", (a, kind, other, other, name, source, score))


def test_suggestions_rank_explain_and_dismiss(cfg):
    a, b, c_, d = rid("a"), rid("b"), rid("c"), rid("d")
    songs(cfg, "Nirvana", "Nevermind", ["One", "Two"], mbid=a)
    songs(cfg, "Pixies", "Doolittle", ["One", "Two"], mbid=b)
    songs(cfg, "Mudhoney", "Superfuzz", ["One", "Two"], mbid=c_)
    songs(cfg, "Tad", "God's Balls", ["One", "Two"], mbid=d)
    with db.connect() as c:
        for x in (a, b, c_):
            link(c, x, "similar", MELVINS, "Melvins", 0.6)
        link(c, a, "influenced_by", MELVINS, "Melvins", source="wikidata")
        link(c, d, "similar", rid("one-fan"), "Only One", 0.6)
        link(c, a, "similar", b, "Pixies", 0.9)                               # in the library: not suggested
    got = artists.suggestions(1)
    assert [s["name"] for s in got] == ["Melvins", "Only One"]
    assert got[0]["reasons"] == "Similar to Nirvana, Pixies, and Mudhoney; influenced Nirvana"
    # Favorites count more: Tad's fan pulls ahead once Tad's songs are your favorites.
    with db.connect() as c:
        c.execute("DELETE FROM artist_links WHERE other_mbid = ?", (MELVINS,))
        link(c, a, "similar", MELVINS, "Melvins", 0.6)
        c.executemany("INSERT INTO hit_favorites (profile, song_key) VALUES (1, ?)",
                      [(library.song_key("Tad", t, ""),) for t in ("One", "Two")])
    assert [s["name"] for s in artists.suggestions(1)] == ["Only One", "Melvins"]
    assert [s["name"] for s in artists.suggestions(2)] == ["Melvins", "Only One"]      # someone else's favorites
    artists.dismiss(1, MELVINS, "Melvins")
    assert [s["name"] for s in artists.suggestions(1)] == ["Only One"]
    assert artists.dismissed(1)[0]["name"] == "Melvins"
    artists.undismiss(1, MELVINS)
    assert "Melvins" in [s["name"] for s in artists.suggestions(1)]
    # Once Melvins songs are in the library, Melvins isn't a suggestion.
    songs(cfg, "Melvins", "Houdini", ["Hooch"], mbid=MELVINS)
    assert "Melvins" not in [s["name"] for s in artists.suggestions(1)]


def discography(mbid, name, releases):
    """Cache a suggested artist's albums: releases are (title, kind, year, [(title, listeners), ...])."""
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO artists (mbid, name, fetched) VALUES (?, ?, ?)",
                  (mbid, name, json.dumps({"discography": time.time(), "musicbrainz": time.time()})))
        for title, kind, year, tracks in releases:
            rg = rid(f"{name}|{title}")
            c.execute("INSERT INTO artist_releases (rgid, mbid, title, kind, year, release_id, fetched_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)", (rg, mbid, title, kind, year, rid(f"rel|{rg}"), time.time()))
            for i, (t, listeners) in enumerate(tracks, 1):
                rec = rid(f"{name}|{t}")
                c.execute("INSERT INTO artist_tracks (rgid, disc, track, recording, title, length) VALUES (?, 1, ?, ?, ?, ?)",
                          (rg, i, rec, t, 200000))
                if listeners is not None:
                    c.execute("INSERT OR REPLACE INTO hit_recordings (mbid, listeners, listens, fetched_at) VALUES (?, ?, ?, ?)",
                              (rec, listeners, listeners, time.time()))


MELVINS_ALBUMS = [
    ("Houdini", "album", 1993, [("Hooch", 9000), ("Night Goat", 2000), ("Lizzy", 2000), ("Hag Me", 2100),
                                ("Set Me Straight", 2000), ("Sky Pup", 1900)]),
    ("Night Goat", "single", 1992, [("Night Goat", 2000)]),
    # An EP of other versions and a B-side: the B-side towers over the EP, but not over the artist's albums.
    ("Hooch EP", "ep", 1993, [("Hooch (Rasta Mix)", 60), ("Hooch (Radio Edit)", 50), ("B-Side", 900),
                              ("Outtake", 40)]),
    ("Stoner Witch", "album", 1994, [("Skweetis", 1000), ("Queen", 1100), ("Sweet Willy Rollbar", 1050),
                                     ("Revolve", 1000)]),
]


def test_get_their_hits_uses_the_hits_rules(cfg):
    discography(MELVINS, "Melvins", MELVINS_ALBUMS)
    with db.connect() as c:
        c.execute("INSERT INTO hit_charts (title, performer, peak, weeks) VALUES ('revolve', 'Melvins', 40, 3)")
        c.execute("INSERT INTO hit_artist_songs (source, artist, title, value) VALUES ('single', ?, 'night goat', 1992)",
                  (MELVINS,))
    got = artists.suggested_hits(MELVINS)
    picked = {s["title"]: s for s in got["songs"]}
    # Hooch stands out on its album, Night Goat was a single (and counts on the album, not the single),
    # Revolve charted; the rest don't pass a rule, and there's no minimum to fill.
    assert set(picked) == {"Hooch", "Night Goat", "Revolve"}                 # not the B-side or the mixes
    assert picked["Night Goat"]["album"] == "Houdini" and "Single 1992" in picked["Night Goat"]["reasons"]
    assert "Hot 100 #40" in picked["Revolve"]["reasons"] and "4.50x listeners" in picked["Hooch"]["reasons"]
    s = picked["Hooch"]
    assert s["key"] == f"mb:{rid('Melvins|Hooch')}" and s["duration_ms"] == 200000 and s["artist"] == "Melvins"
    assert s["artwork_url"].startswith("https://coverartarchive.org/release/")
    # Songs you have don't count.
    songs(cfg, "Melvins", "Houdini", ["Hooch"], mbid=MELVINS)
    got = artists.suggested_hits(MELVINS)
    assert "Hooch" not in [x["title"] for x in got["songs"]] and got["owned"] == 1
    # Nothing to go on: nothing passes.
    discography(rid("quiet"), "Quiet Band", [("Hush", "album", 2001, [("A", None), ("B", None), ("C", None)])])
    got = artists.suggested_hits(rid("quiet"))
    assert got["known"] and got["songs"] == []
    assert artists.suggested_hits(rid("never looked up")) == {"known": False, "songs": [], "owned": 0}
    # A higher cutoff picks fewer: at 5x the median, Hooch (4.5x) doesn't stand out.
    with db.connect() as c:
        c.execute("DELETE FROM library")
    strict = [x["title"] for x in artists.suggested_hits(MELVINS, rules={"max": 5, "cutoff": 5.0})["songs"]]
    assert "Hooch" not in strict and "Revolve" in strict


def test_suggestions_playlists(cfg, client, monkeypatch):
    discography(MELVINS, "Melvins", MELVINS_ALBUMS)
    discography(rid("tad"), "Tad", [("8-Way Santa", "album", 1991, [("Jinx", 5000), ("Wired God", 1000),
                                                                      ("Delinquent", 1000), ("Stumblin' Man", 1000)])])
    a = rid("a")
    songs(cfg, "Nirvana", "Nevermind", ["One", "Two"], mbid=a)
    with db.connect() as c:
        link(c, a, "similar", MELVINS, "Melvins", 0.8)
        link(c, a, "similar", rid("tad"), "Tad", 0.5)
    page = client.get("/suggestions").text
    assert "Get their hits (1 song)" in page and "Hooch" in page                 # 4.5x; the rest don't pass
    r = client.post(f"/suggestions/{MELVINS}/get", follow_redirects=False)
    assert r.status_code == 303
    one, every = "suggested:" + MELVINS, "suggested:suggestions"
    sync.wait_idle(one)
    sync.wait_idle(every)
    assert sync.playlist(one)["name"] == "Melvins Hits" and sync.playlist(every)["name"] == "Suggestions"
    assert sync.playlist(one)["folder"] == "Suggestions/Melvins Hits"
    client.post(f"/suggestions/{rid('tad')}/get")
    sync.wait_idle("suggested:" + rid("tad"))
    sync.wait_idle(every)

    def titles(pid):
        with db.connect() as c:
            return [r[0] for r in c.execute("SELECT s.title FROM entries e JOIN songs s ON s.key = e.song_key "
                                            "WHERE e.playlist_id = ? ORDER BY e.position", (pid,))]
    assert titles(one) == ["Hooch"] and titles(every) == ["Hooch", "Jinx"]
    # Each song is one download, wanted by both playlists.
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM items WHERE song_key = ? AND want = 1",
                         (f"mb:{rid('Melvins|Hooch')}",)).fetchone()[0] == 2
    # A finished download goes in both folders, the second as a hard link.
    src = cfg.data_dir / "dl" / "hooch.mp3"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"ID3 hooch")
    placed = sync.place_download(cfg, f"mb:{rid('Melvins|Hooch')}", src)
    files = sorted((cfg.music_root / "Suggestions").rglob("*.mp3"))
    assert len(files) == 2 and os.stat(files[0]).st_ino == os.stat(files[1]).st_ino and placed in files
    # Pages: the artist's playlist says where its songs came from; both can be copied to Apple Music or Spotify.
    page = client.get(f"/playlists/{one}").text
    assert "The songs Get their hits picked for" in page and "Copy to" in page
    assert "Every artist you used Get their hits on" in client.get(f"/playlists/{every}").text
    # Removing an artist's playlist takes its songs out of Suggestions.
    client.post(f"/playlists/{one}/forget")
    sync.wait_idle(every)
    assert titles(every) == ["Jinx"]
    # Someone else's suggestions playlists are their own.
    pid2 = sync.add_suggested(cfg.for_profile(2), MELVINS, "Melvins")
    assert pid2 == (f"suggested:{MELVINS}@2", "suggested:suggestions@2")


def test_get_their_hits_waits_for_the_artists_albums(cfg, fake, monkeypatch):
    songs(cfg, "Nirvana", "Nevermind", ["One"], mbid=NIRVANA)
    with db.connect() as c:
        link(c, NIRVANA, "similar", NAILS, "Nails", 0.7)
    monkeypatch.setattr(artists, "start", lambda cfg, full=True: True)       # the lookup runs below instead
    assert artists.get_hits(cfg, NAILS) is None and artists.is_pending(1, NAILS)
    assert artists._priority == [NAILS]
    artists.research(cfg, full=False)
    assert not artists.is_pending(1, NAILS) and artists.albums_known(NAILS)
    pid = "suggested:" + NAILS
    sync.wait_idle(pid)
    with db.connect() as c:
        titles = [r[0] for r in c.execute("SELECT s.title FROM entries e JOIN songs s ON s.key = e.song_key "
                                          "WHERE e.playlist_id = ?", (pid,))]
        n_tracks = c.execute("SELECT COUNT(*) FROM artist_tracks t JOIN artist_releases r ON r.rgid = t.rgid "
                             "WHERE r.rgid = '6d2da807-879c-4ce4-b9dc-797162c56456'").fetchone()[0]
    # No listener counts here, so the singles are what pass: each one's title song.
    assert sync.playlist(pid)["name"] == "Nails Hits" and {"Fear Based Life", "Imposing Will"} <= set(titles)
    assert n_tracks == 10                                    # the album's first release, not a later edition


def test_the_worker_searches_soulseek_for_suggested_songs(cfg):
    from app.worker import Worker
    discography(MELVINS, "Melvins", MELVINS_ALBUMS)
    pid, _ = sync.add_suggested(cfg, MELVINS, "Melvins")
    sync.add_suggested_songs(pid, artists.suggested_hits(MELVINS)["songs"])
    sync.reconcile(cfg, pid, apply=True, want_downloads=True)
    with db.connect() as c:
        d = dict(c.execute("SELECT * FROM downloads").fetchone())

    class Client:
        searched = []

        def start_search(self, q):
            self.searched.append(q)
            return "s1"
    w = Worker()
    w.start_search(Client(), d)
    assert Client.searched and "hooch" in Client.searched[0].lower() and "melvins" in Client.searched[0].lower()
