import uuid

import pytest

from app import config, db, hits, library, settings, sync

ART = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path / "music"))
    base = config.load()
    db.init(base.db_path)
    settings.init(base)
    return base


def rid(n) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_OID, str(n)))


def album(cfg, artist, name, year, songs, kind="album", artist_id=ART):
    """Index an album: songs are (title, listeners or None[, seconds]). Listener counts go in the cache."""
    rows, recs = [], []
    for i, s in enumerate(songs, 1):
        title, listeners = s[0], s[1]
        p = cfg.music_root / artist / name / f"{i:02d} {title.replace('/', '-')}.mp3"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"ID3")
        rec = rid(f"{artist}|{name}|{title}")
        rows.append({"path": str(p), "size": 3, "mtime": p.stat().st_mtime, "artist": artist, "album_artist": artist,
                     "title": title, "album": name, "duration": s[2] if len(s) > 2 else 200.0, "track_number": i,
                     "year": str(year), "mb_recording": rec, "mb_artist": artist_id, "release_type": kind})
        if listeners is not None:
            recs.append((rec, listeners, listeners * 5, db.now()))
    library._write(rows)
    with db.connect() as c:
        c.executemany("INSERT OR REPLACE INTO hit_recordings (mbid, listeners, listens, fetched_at) VALUES (?, ?, ?, ?)", recs)


def picks(cfg, rule=None, skip=frozenset()):
    rule = rule or {"q": "", "folders": [], "artist": "", "album": "", "hits": dict(hits.DEFAULTS)}
    return {g["album"]: [s["title"] for s in g["songs"] if s["picked"]] for g in hits.plan(cfg, rule, skip)}


NEVERMIND = [("Smells Like Teen Spirit", 322260), ("In Bloom", 238889), ("Come as You Are", 296690),
             ("Breed", 189938), ("Lithium", 259053), ("Polly", 217131), ("Territorial Pissings", 161545),
             ("Drain You", 186796), ("Lounge Act", 161738), ("Stay Away", 158827), ("On a Plain", 188027),
             ("Something in the Way", 2)]


def test_standouts_get_extra_places_and_filler_doesnt(cfg):
    album(cfg, "Nirvana", "Nevermind", 1991, NEVERMIND)
    album(cfg, "Kyuss", "Blues for the Red Sun", 1992,
          [("Thumb", 37786), ("Green Machine", 42567), ("Molten Universe", 32614), ("Freedom Run", 27908),
           ("800", 27874), ("Writhe", 27183), ("Yeah", 546, 30.0)], artist_id=rid("kyuss"))
    got = picks(cfg)
    # The four with 1.25x the median or more, best first in album order.
    assert got["Nevermind"] == ["Smells Like Teen Spirit", "In Bloom", "Come as You Are", "Lithium"]
    # Two standouts, then a flat line: the minimum.
    assert got["Blues for the Red Sun"] == ["Thumb", "Green Machine"]


def test_listens_on_another_version_of_the_recording_count(cfg):
    """A file tagged with a little-played version still gets the artist's count for that title."""
    album(cfg, "Radiohead", "OK Computer", 1997,
          [("Airbag", 213221), ("Paranoid Android", 261380), ("Exit Music (For a Film)", 206221),
           ("Let Down", 209426), ("Karma Police", 284929), ("Lucky", 10089), ("The Tourist", 170435)],
          artist_id=rid("rh"))
    with db.connect() as c:
        c.execute("INSERT INTO hit_artist_songs (source, artist, title, value) VALUES ('listenbrainz', ?, 'lucky', 300000)",
                  (rid("rh"),))
    assert picks(cfg)["OK Computer"][:2] == ["Karma Police", "Lucky"]


def test_charts_singles_favorites_and_compilations(cfg):
    pac = rid("2pac")
    album(cfg, "2Pac", "Me Against the World", 1995,
          [("Intro", 14088, 100.0), ("If I Die 2Nite", 14254), ("Me Against the World", 13000), ("So Many Tears", 28607),
           ("Temptations", 27058), ("Young Niggaz", 11986), ("Lord Knows", 11627), ("Dear Mama", 14656),
           ("It Ain't Easy", 12530), ("Old School", 14682), ("Death Around the Corner", 27412), ("Outlaw", 12000)],
          artist_id=pac)
    album(cfg, "2Pac", "Greatest Hits", 1998, [("Dear Mama", 14656), ("Changes", 50000), ("Keep Ya Head Up", 40000)],
          kind="album; compilation", artist_id=pac)
    with db.connect() as c:
        c.execute("INSERT INTO hit_charts (title, performer, peak, weeks) VALUES ('dear mama', '2Pac', 9, 21)")
        c.execute("INSERT INTO hit_charts (title, performer, peak, weeks) VALUES ('changes', '2Pac Featuring Talent', 32, 10)")
        c.execute("INSERT INTO hit_artist_songs (source, artist, title, value) VALUES ('single', ?, 'old school', 1995)", (pac,))
        c.execute("INSERT INTO hit_artist_songs (source, artist, title, value) VALUES ('single', ?, 'outlaw', 2010)", (pac,))
        c.execute("INSERT INTO hit_favorites (song_key) VALUES (?)", (library.song_key("2Pac", "Lord Knows", ""),))
    got = picks(cfg)
    mama = got["Me Against the World"]
    assert "Dear Mama" in mama and "Lord Knows" in mama                  # charted; your favorite
    assert "Intro" not in mama                                           # a skit, whatever its numbers
    assert "Outlaw" not in mama                                          # its single came out 15 years later
    # The favorite takes one of the five places; Old School (a single) stands out but the album is full.
    assert len(mama) == 5 and "Old School" not in mama
    # A compilation only adds what the albums don't have, and only songs that stand out.
    assert got["Greatest Hits"] == ["Changes"]
    plan = {g["album"]: g for g in hits.plan(cfg, {"hits": {}})}
    dear = next(s for s in plan["Greatest Hits"]["songs"] if s["title"] == "Dear Mama")
    assert dear["out"] == "On Me Against the World" and not dear["picked"]
    changes = next(s for s in plan["Greatest Hits"]["songs"] if s["title"] == "Changes")
    assert "Hot 100 #32" in changes["reasons"]


def test_an_album_with_no_numbers_keeps_its_openers(cfg):
    album(cfg, "Nails", "Every Bridge Burning", 2019,
          [("Imposing Will", None), ("Punishment Map", None), ("Every Bridge Burning", None), ("Trapped", None)],
          artist_id=rid("nails"))
    g = hits.plan(cfg, {"hits": {}})[0]
    assert not g["has_data"]
    assert [s["title"] for s in g["songs"] if s["picked"]] == ["Imposing Will", "Punishment Map"]


def test_left_out_songs_are_replaced_by_the_next_best(cfg):
    album(cfg, "Nirvana", "Nevermind", 1991, NEVERMIND)
    pid = sync.add_local_playlist(cfg, "Hits", rule={"q": "", "folders": [], "artist": "", "album": "",
                                                     "hits": {"min": 2, "max": 2, "cutoff": 1.25}})
    sync.refresh_local(cfg, pid, scan=False)

    def listed():
        with db.connect() as c:
            return [r[0] for r in c.execute(
                "SELECT s.title FROM entries e JOIN songs s ON s.key = e.song_key LEFT JOIN items i "
                "ON i.playlist_id = e.playlist_id AND i.song_key = e.song_key "
                "WHERE e.playlist_id = ? AND COALESCE(i.skip, 0) = 0 ORDER BY e.position", (pid,))]
    assert listed() == ["Smells Like Teen Spirit", "Come as You Are"]
    sync.leave_out(cfg, pid, [library.song_key("Nirvana", "Come as You Are", "")])
    sync.wait_idle(pid)
    assert listed() == ["Smells Like Teen Spirit", "Lithium"]
    assert sync.song_count(pid, skipped=True) == 3                       # the left-out song is still listed


def test_rule_with_options_is_clamped():
    assert hits.options({"q": ""}) is None
    assert hits.options({"hits": {"min": 9, "max": 3, "cutoff": 0.2}}) == {"min": 9, "max": 9, "cutoff": 1.0, "whole": []}


class FakeHttp:
    """Canned answers for research, keyed by path."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []
        self.genius_left = None
        self.lb_token = "token"

    def _get(self, path, kw):
        self.calls.append((path, kw))
        a = self.answers.get(path)
        return a(kw) if callable(a) else a

    def lb(self, method, path, **kw):
        return self._get(path, kw)

    def mb(self, path, params):
        return self._get(path, params)

    def genius(self, path, params):
        return self._get(path, params)

    def send(self, method, url, **kw):
        class R:
            status_code, headers = 200, {"ETag": "x"}
            text = ("chart_week,current_week,title,performer,last_week,peak_pos,wks_on_chart,wks_at_no1\n"
                    "1995-03-04,12,Dear Mama/Old School,2Pac,15,9,20,NA\n"
                    "1995-03-11,9,Dear Mama/Old School,2Pac,12,9,21,NA\n"
                    "2003-01-01,40,24/7,Somebody,40,40,1,NA\n")

            def raise_for_status(self):
                pass
        return R()

    def close(self):
        pass


def test_research_fills_the_cache(cfg, monkeypatch):
    pac = rid("2pac")
    album(cfg, "2Pac", "Me Against the World", 1995, [("Dear Mama", None), ("So Many Tears", None)], artist_id=pac)
    # A downloaded song: an ISRC, no MusicBrainz IDs.
    p = cfg.music_root / "dl" / "changes.mp3"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"ID3")
    library._write([{"path": str(p), "size": 3, "mtime": 1.0, "artist": "2Pac", "title": "Changes", "album": "Greatest Hits",
                     "duration": 269.0, "isrc": "USIR19800001"}])
    changes_rec = rid("changes")
    fake = FakeHttp({
        "/popularity/recording": lambda kw: [{"recording_mbid": m, "total_user_count": 1000, "total_listen_count": 5000}
                                             for m in kw["json"]["recording_mbids"]],
        f"/popularity/top-recordings-for-artist/{pac}": [
            {"recording_name": "Dear Mama", "total_user_count": 9000},
            {"recording_name": "Dear Mama (Remastered)", "total_user_count": 12000}],
        "/release-group": {"release-group-count": 2, "release-groups": [
            {"title": "Dear Mama / Old School", "first-release-date": "1995-02-21", "secondary-types": []},
            {"title": "Dear Mama (live)", "first-release-date": "2001", "secondary-types": ["Live"]}]},
        "/isrc/USIR19800001": {"recordings": [{"id": changes_rec, "title": "Changes", "length": 268000,
                                               "artist-credit": [{"name": "2Pac", "artist": {"id": pac}}]}]},
    })
    monkeypatch.setattr(hits, "_Http", lambda cfg: fake)
    hits.research(cfg)
    assert hits.status()["state"] == "done"
    with db.connect() as c:
        charts = {(r[0], r[1]): (r[2], r[3]) for r in c.execute("SELECT title, performer, peak, weeks FROM hit_charts")}
        lists = {(r[0], r[1]): r[2] for r in c.execute("SELECT source, title, value FROM hit_artist_songs")}
        ids = {r[0]: (r[1], r[2]) for r in c.execute("SELECT song_key, recording, how FROM hit_ids")}
    assert charts[("dear mama", "2Pac")] == (9, 2) and charts[("old school", "2Pac")] == (9, 2)
    assert ("24 7", "Somebody") in charts                               # "24/7" isn't two songs
    assert lists[("listenbrainz", "dear mama")] == 12000                 # the most for any version
    assert lists[("single", "dear mama")] == 1995 and lists[("single", "old school")] == 1995
    assert ids[library.song_key("2Pac", "Changes", "")] == (changes_rec, "isrc")
    # Found by ISRC, so its listener count was fetched too.
    with db.connect() as c:
        assert c.execute("SELECT listeners FROM hit_recordings WHERE mbid = ?", (changes_rec,)).fetchone()[0] == 1000
    # A second run has nothing left to fetch.
    fake.calls.clear()
    hits.research(cfg)
    assert [c for c in fake.calls if c[0] != "/isrc/USIR19800001"] == []


def test_hits_pages(cfg, monkeypatch):
    import importlib

    from fastapi.testclient import TestClient
    monkeypatch.setenv("WORKER", "0")
    monkeypatch.setenv("SLSKD_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(hits, "start", lambda cfg: True)       # no lookups from tests
    album(cfg, "Nirvana", "Nevermind", 1991, NEVERMIND)
    import app.web as web
    importlib.reload(web)
    with TestClient(web.app) as client:
        r = client.post("/library/playlist", data={"what": "all", "name": "All my music", "hits": "1", "live": "1"},
                        follow_redirects=False)
        hits_url = r.headers["location"]
        pid = hits_url.split("/")[2]
        sync.wait_idle(pid)
        assert sync.playlist(pid)["name"] == "All my music Hits"
        page = client.get(hits_url).text
        assert "Nevermind" in page and "1.71x listeners" in page and "4 of 12 picked" in page
        assert "HITS" in client.get(f"/playlists/{pid}").text
        r = client.post(f"/playlists/{pid}/hits/settings", data={"min_songs": "1", "max_songs": "2", "cutoff": "1.5"},
                        follow_redirects=False)
        assert r.status_code == 303
        sync.wait_idle(pid)
        assert hits.options(sync.rule_of(sync.playlist(pid))) == {"min": 1, "max": 2, "cutoff": 1.5, "whole": []}
        assert sync.song_count(pid) == 2
        # You like a song: the whole album goes in, from the song's row, and comes back out.
        page = client.get(f"/playlists/{pid}").text
        assert "Whole album" in page
        teen = library.song_key("Nirvana", "Smells Like Teen Spirit", "")
        client.post(f"/playlists/{pid}/hits/whole", data={"key": teen}, follow_redirects=False)
        sync.wait_idle(pid)
        assert hits.options(sync.rule_of(sync.playlist(pid)))["whole"] == ["nirvana|nevermind"]
        assert sync.song_count(pid) == 12
        assert "Back to the picks" in client.get(f"/playlists/{pid}/hits").text
        # Changing the numbers keeps it whole.
        client.post(f"/playlists/{pid}/hits/settings", data={"min_songs": "2", "max_songs": "3", "cutoff": "1.5"})
        sync.wait_idle(pid)
        assert sync.song_count(pid) == 12
        r = client.post(f"/playlists/{pid}/hits/whole", data={"album": "nirvana|nevermind", "on": "0"}, follow_redirects=False)
        sync.wait_idle(pid)
        assert r.status_code == 303 and hits.options(sync.rule_of(sync.playlist(pid)))["whole"] == []
        assert sync.song_count(pid) == 2                     # at 1.5, only two songs stand out
        assert client.get("/partials/hits").status_code == 200
        # A hits playlist from an auto playlist, and none from a hits playlist.
        auto = sync.add_local_playlist(cfg, "Everything", rule={"q": "", "folders": [], "artist": "", "album": ""})
        assert "Make a hits playlist" in client.get(f"/playlists/{auto}").text
        r = client.post(f"/playlists/{auto}/hits/new", follow_redirects=False)
        assert sync.playlist(r.headers["location"].split("/")[2])["name"] == "Everything Hits"
        assert client.post(f"/playlists/{pid}/hits/new").status_code == 400


def test_a_step_that_is_refused_stops_alone(cfg, monkeypatch):
    album(cfg, "2Pac", "Me Against the World", 1995, [("Dear Mama", None)], artist_id=rid("2pac"))

    def refused(kw):
        raise hits.StepStopped("ListenBrainz asks for a user token for this.")
    fake = FakeHttp({"/popularity/recording": lambda kw: [],
                     f"/popularity/top-recordings-for-artist/{rid('2pac')}": refused,
                     "/release-group": {"release-group-count": 1, "release-groups": [
                         {"title": "Dear Mama", "first-release-date": "1995", "secondary-types": []}]}})
    monkeypatch.setattr(hits, "_Http", lambda cfg: fake)
    hits.research(cfg)
    st = hits.status()
    assert st["state"] == "done" and st["notes"] == ["ListenBrainz asks for a user token for this."]
    with db.connect() as c:                  # the singles step still ran
        assert c.execute("SELECT value FROM hit_artist_songs WHERE source = 'single'").fetchone()[0] == 1995
    # Without a token, the per-artist step doesn't try at all.
    fake.lb_token = None
    with db.connect() as c:
        c.execute("DELETE FROM hit_artists")
    hits.research(cfg)
    assert "Set LISTENBRAINZ_TOKEN" in hits.status()["notes"][0]


def test_a_whole_album_brings_every_song_once(cfg):
    album(cfg, "2Pac", "Me Against the World", 1995, [("Intro", 100, 60.0), ("Dear Mama", 1000), ("Lord Knows", 900),
                                                      ("Outlaw", 300)], artist_id=rid("2pac"))
    album(cfg, "2Pac", "Greatest Hits", 1998, [("Dear Mama", 1000), ("Changes", 5000)], kind="album; compilation",
          artist_id=rid("2pac"))
    rule = {"hits": {"min": 1, "max": 1, "whole": ["2pac|me against the world", "2pac|greatest hits"]}}
    got = picks(cfg, rule)
    assert got["Me Against the World"] == ["Intro", "Dear Mama", "Lord Knows", "Outlaw"]   # the skit too
    assert got["Greatest Hits"] == ["Dear Mama", "Changes"]
    titles = [s["title"] for s in hits.collection(cfg, rule)]
    assert titles.count("Dear Mama") == 1 and len(titles) == 5
