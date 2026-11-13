import importlib
import io
import os
import zipfile

import pytest

from app import config, db, library, mirror, settings, sync


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path / "music"))
    base = config.load()
    db.init(base.db_path)
    settings.init(base)
    return base


def add_files(cfg, *files):
    """Index fake files: (relative path, artist, title, album[, track])."""
    rows = []
    for f in files:
        rel, artist, title, album = f[:4]
        p = cfg.music_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"ID3" + rel.encode())
        rows.append({"path": str(p), "size": p.stat().st_size, "mtime": p.stat().st_mtime, "artist": artist,
                     "title": title, "album": album, "duration": 200.0, "track_number": f[4] if len(f) > 4 else None})
    library._write(rows)


def test_song_key_ignores_album_and_folder():
    a = library.song_key("Beck", "Loser", "/m/Albums/Beck/Mellow Gold/02 Loser.mp3")
    b = library.song_key("Beck feat. Someone", "Loser (Remastered)", "/m/Random/loser.mp3")
    assert a == b and a.startswith("local:")
    assert library.song_key("Beck", "Loser (Live)", "/x") != a          # a live version is a different song
    assert library.song_key(None, "Loser", "/a") != library.song_key(None, "Loser", "/b")   # untagged: by file


def test_browse_filters_by_words_folder_and_artist(cfg):
    add_files(cfg, ("Albums/Beyoncé/Lemonade/01.mp3", "Beyoncé", "Pray You Catch Me", "Lemonade", 1),
              ("Albums/Beck/Odelay/01.mp3", "Beck", "Devils Haircut", "Odelay", 1),
              ("Random/beck.m4a", "Beck", "Loser", "Mellow Gold"),
              ("AppleMusic/Road Trip/001 - Beck - Loser.mp3", "Beck", "Loser", "Mellow Gold"))
    rows, total = library.browse(cfg, {"q": "beyonce"})                  # accents and case don't matter
    assert total == 1 and rows[0]["title"] == "Pray You Catch Me"
    assert library.browse(cfg, {"folders": ["Random"]})[1] == 1
    assert library.browse(cfg, {"artist": "Beck"})[1] == 3
    assert dict(library.folders(cfg)) == {"Albums": 2, "Random": 1, "AppleMusic/Road Trip": 1}
    # "_" in a folder name is a letter, not a wildcard
    add_files(cfg, ("1_New/a.mp3", "X", "A", "Y"), ("1xNew/b.mp3", "X", "B", "Y"))
    assert [r["title"] for r in library.browse(cfg, {"folders": ["1_New"]})[0]] == ["A"]
    # One entry per song: the two copies of Loser are one song.
    assert [s["title"] for s in library.collection(cfg, {"artist": "Beck"})] == ["Loser", "Devils Haircut"]


def test_auto_playlist_follows_the_library(cfg):
    add_files(cfg, ("Albums/Beck/Odelay/01.mp3", "Beck", "Devils Haircut", "Odelay", 1),
              ("Albums/Beck/Odelay/02.mp3", "Beck", "Hotwax", "Odelay", 2),
              ("Random/x.mp3", "Other", "Elsewhere", "Misc"))
    pid = sync.add_local_playlist(cfg, "All Albums", rule={"folders": ["Albums"]})
    sync.fetch(cfg, pid)
    sync.reconcile(cfg, pid, apply=True)
    rows, counts = sync.summary(cfg, pid)
    assert [r["song"]["title"] for r in rows] == ["Devils Haircut", "Hotwax"] and counts == {"synced": 2}
    p = sync.playlist(pid)
    assert p["folder"] == "Playlists/All Albums" and p["library_mode"] == "reference"
    m3u = (cfg.music_root / "Playlists/All Albums/All Albums.m3u").read_text().splitlines()
    assert "../../Albums/Beck/Odelay/01.mp3" in m3u
    # A new album shows up, and a deleted file leaves.
    add_files(cfg, ("Albums/Beck/Sea Change/01.mp3", "Beck", "The Golden Age", "Sea Change", 1))
    os.unlink(cfg.music_root / "Albums/Beck/Odelay/02.mp3")
    sync.refresh_local(cfg, pid)
    assert [r["song"]["title"] for r in sync.summary(cfg, pid)[0]] == ["Devils Haircut", "The Golden Age"]


def test_hand_picked_playlist_add_and_remove(cfg):
    add_files(cfg, ("Albums/A/1.mp3", "A", "One", "X", 1), ("Albums/A/2.mp3", "A", "Two", "X", 2),
              ("Albums/A/3.m4a", "A", "Three", "X", 3))
    keys = {s["title"]: s["key"] for s in library.collection(cfg, {})}
    pid = sync.add_local_playlist(cfg, "Mix")
    assert sync.add_songs(cfg, pid, [keys["Three"], keys["One"]]) == 2
    assert sync.add_songs(cfg, pid, [keys["One"], keys["Two"]]) == 1        # One is already there
    with db.connect() as c:
        c.execute("UPDATE playlists SET library_mode = 'copy' WHERE id = ?", (pid,))
    sync.reconcile(cfg, pid, apply=True)
    folder = cfg.music_root / "Playlists/Mix"
    assert sorted(f.name for f in folder.iterdir()) == ["001 - A - Three.m4a", "002 - A - One.mp3", "003 - A - Two.mp3",
                                                       "Mix.m3u"]
    sync.reconcile(cfg, pid, apply=True)                                   # stable: no renames the second time
    assert (folder / "001 - A - Three.m4a").exists()
    sync.remove_songs(cfg, pid, [keys["Three"]])
    assert sorted(f.name for f in folder.iterdir()) == ["001 - A - One.mp3", "002 - A - Two.mp3", "Mix.m3u"]
    assert (cfg.music_root / "Albums/A/3.m4a").exists()                    # the library file stays


def test_spotify_copy_takes_a_random_sample_when_too_big(cfg, monkeypatch):
    add_files(cfg, *[(f"Albums/A/{i}.mp3", "A", f"Song {i}", "X", i) for i in range(1, 8)])
    pid = sync.add_local_playlist(cfg, "Everything", rule={})
    sync.refresh_local(cfg, pid)

    class Fake:
        tracks = {}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def find_track(self, song): return None if song["title"] == "Song 4" else f"spotify:track:{song['title']}"
        def playlist_exists(self, pid): return pid in Fake.tracks
        def create_playlist(self, name, desc): Fake.tracks["sp"] = []; return "sp"
        def set_tracks(self, pid, uris): Fake.tracks[pid] = list(uris)

    monkeypatch.setattr(mirror, "_client", lambda cfg, target: Fake())
    monkeypatch.setattr(mirror, "SPOTIFY_MAX", 3)
    mirror.run(cfg, pid, "spotify")
    got = Fake.tracks["sp"]
    assert len(got) == 3 and "spotify:track:Song 4" not in got
    assert got == sorted(got, key=lambda u: int(u.rsplit(" ", 1)[1]))      # in playlist order
    m = mirror.get(pid, "spotify")
    assert m["capped"] and m["matched"] == 3 and m["total"] == 7 and "random 3 of the 7" in m["note"]
    with pytest.raises(ValueError):
        mirror.run(cfg, pid, "tidal")


def test_pages_render(cfg, monkeypatch):
    monkeypatch.setenv("WORKER", "0")
    monkeypatch.setenv("SLSKD_URL", "http://127.0.0.1:9")
    from fastapi.testclient import TestClient
    import app.web as web
    importlib.reload(web)
    add_files(cfg, ("Albums/Beck/Odelay/01.mp3", "Beck", "Devils Haircut", "Odelay", 1))
    with TestClient(web.app) as client:
        r = client.get("/library?q=beck")
        assert r.status_code == 200 and "Devils Haircut" in r.text and "Make playlist" in r.text
        key = library.collection(cfg, {})[0]["key"]
        r = client.post("/library/playlist", data={"what": "selected", "key": key, "new_name": "Picks", "back": "q=beck"},
                        follow_redirects=False)
        assert r.status_code == 303 and "added=1" in r.headers["location"]
        pid = next(p["id"] for p in sync.tracked() if p["name"] == "Picks")
        sync.wait_idle(pid)
        r = client.get(f"/playlists/{pid}")
        assert r.status_code == 200 and "Copy to Spotify" in r.text and "Copy to Apple Music" in r.text
        assert client.get("/").status_code == 200
        r = client.get("/settings")
        assert r.status_code == 200 and 'name="local_dir"' in r.text
        art_key = library.browse(cfg, {})[0][0]["art_key"]
        r = client.get(f"/art/{art_key}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg")   # no art in the fake file
        assert client.get("/library/files/1").status_code == 200


def test_scan_keeps_an_unmounted_folder(cfg, tmp_path, monkeypatch):
    add_files(cfg, ("Albums/A/1.mp3", "A", "One", "X"))
    import shutil
    shutil.rmtree(cfg.music_root)
    cfg.music_root.mkdir()                     # mounted but empty, like a disk that didn't come up
    library.scan(cfg)
    assert library.stats()["files"] == 1


def test_titles_and_names_from_the_services_compare_cleanly():
    from app.match import artist_matches, norm_title
    assert norm_title("Hooked On Love - Remastered 2002") == norm_title("Hooked on Love")
    assert norm_title("Soothe - Demo/Remastered") != norm_title("Soothe")              # a demo is another recording
    assert norm_title("Wait For Me - Live Version") != norm_title("Wait For Me")
    assert norm_title("Under Pressure - Remastered 2011") == "under pressure"
    assert artist_matches("Dick Dale and His Del\u2010Tones", "Dick Dale and His Del-Tones")


def test_spotify_setup_guides_you_through_connecting(cfg, monkeypatch):
    monkeypatch.setenv("WORKER", "0")
    monkeypatch.setenv("SLSKD_URL", "http://127.0.0.1:9")
    from urllib.parse import parse_qs, urlsplit
    from fastapi.testclient import TestClient
    import app.web as web
    importlib.reload(web)
    warning = "Spotify won't accept this page's address"
    with TestClient(web.app, base_url="http://localhost:8080") as client:      # the README's old address
        for page in ("/", "/settings"):
            assert 'href="/spotify/setup"' in client.get(page).text            # every way in leads to the guide
        assert "isn't connected" not in client.get("/").text                   # no nagging without playlists
        r = client.get("/spotify/setup")
        assert 'data-copy="http://127.0.0.1:8080/spotify/callback"' in r.text and "localhost" not in r.text
        assert warning not in r.text
        # A bad Client ID stays on the guide with a reason; a good one is saved and goes straight to Spotify.
        r = client.post("/spotify/setup", data={"client_id": "not-an-id"}, follow_redirects=False)
        assert r.headers["location"].startswith("/spotify/setup?error=")
        assert "32 letters and numbers" in client.get(r.headers["location"]).text
        r = client.post("/spotify/setup", data={"client_id": " 0123456789ABCDEF0123456789abcdef "}, follow_redirects=False)
        assert r.headers["location"] == "/spotify/connect"
        assert settings.current().spotify_client_id == "0123456789abcdef0123456789abcdef"
        r = client.get("/spotify/connect", follow_redirects=False)
        q = parse_qs(urlsplit(r.headers["location"]).query)
        assert r.headers["location"].startswith("https://accounts.spotify.com/authorize")
        assert q["redirect_uri"] == ["http://127.0.0.1:8080/spotify/callback"]
        assert q["client_id"] == ["0123456789abcdef0123456789abcdef"]
        # Saying no on Spotify's page comes back to the guide with an explanation.
        r = client.get("/spotify/callback?error=access_denied", follow_redirects=False)
        assert "approve" in client.get(r.headers["location"]).text
    with TestClient(web.app, base_url="http://192.168.1.20:8080") as client:  # another device on the network
        r = client.get("/spotify/setup")
        assert warning in r.text and 'href="http://127.0.0.1:8080/spotify/setup"' in r.text
    with TestClient(web.app, base_url="https://music.example.com") as client:
        r = client.get("/spotify/setup")
        assert 'data-copy="https://music.example.com/spotify/callback"' in r.text and warning not in r.text


def test_home_card_says_copied_not_synced_after_a_copy(cfg, monkeypatch):
    monkeypatch.setenv("WORKER", "0")
    monkeypatch.setenv("SLSKD_URL", "http://127.0.0.1:9")
    from fastapi.testclient import TestClient
    import app.web as web
    importlib.reload(web)
    pid = sync.add_playlist(cfg, "spotify", "liked", "Liked Songs")
    with TestClient(web.app) as client:
        assert "Synced never" in client.get("/partials/home").text
        mirror._save(pid, "apple", "p.1", matched=1, total=1, missing=[])         # copied, nothing downloaded
        assert "Copied to Apple Music just now" in client.get("/partials/home").text
        mirror._save(pid, "apple", None, error="Apple refused")                   # a failed copy keeps the old time
        assert "Copied to Apple Music just now" in client.get("/partials/home").text
        with db.connect() as c:
            c.execute("UPDATE playlists SET synced_at = ? WHERE id = ?", (db.now() + 1, pid))
        assert "Synced just now" in client.get("/partials/home").text


def test_all_songs_playlist_page_and_zip_size_guard(cfg, monkeypatch):
    monkeypatch.setenv("WORKER", "0")
    monkeypatch.setenv("SLSKD_URL", "http://127.0.0.1:9")
    from fastapi.testclient import TestClient
    from app import export
    import app.web as web
    importlib.reload(web)
    add_files(cfg, ("Albums/Beck/Odelay/01.mp3", "Beck", "Devils Haircut", "Odelay", 1),
              ("Random/x.mp3", None, "Elsewhere", None))         # no artist or album tag
    with TestClient(web.app) as client:
        page = client.get("/library").text
        assert "With no songs selected, the playlist gets <b>all 2 songs</b> in your library" in page
        assert "Make playlist of all 2 songs" in page and 'id="pick-note"' in page
        client.post("/library/playlist", data={"what": "all", "live": "1", "name": "Everything"}, follow_redirects=False)
        pid = next(p["id"] for p in sync.tracked() if p["name"] == "Everything")
        sync.wait_idle(pid)
        # The copy panels refresh with the songs, so they follow a running job; the top buttons follow data-busy.
        rows = client.get(f"/playlists/{pid}/rows").text
        assert 'data-busy="0"' in rows and "None" not in rows
        assert "Copy to Apple Music" in client.get(f"/playlists/{pid}/copies").text
        page = client.get(f"/playlists/{pid}").text
        assert "data-busy-lock" in page and "data-zip-check" in page and page.count("Copy to Apple Music") == 1
        # Download ZIP: small ones just go, big ones ask first, too-big ones are refused even by a direct link.
        c = client.get(f"/playlists/{pid}/zip/check").json()
        assert c["ok"] and c["ask"] is None and c["songs"] == 2
        r = client.get(f"/playlists/{pid}/zip?t=abcdefgh12")
        assert r.status_code == 200
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert names[0] == "Everything/Everything.m3u" and len(names) == 3
        # The page follows the download by its token.
        p = client.get("/zip/progress?t=abcdefgh12").json()
        assert p["state"] == "done" and "2 songs" in p["text"]
        assert client.get("/zip/progress?t=notstarted").json()["state"] == "waiting"
        prog = {"total": 100, "songs": 2}
        stream = export.stream_zip("Everything", sync.export_files(cfg, pid)[1], "", prog)
        next(stream)
        assert export.progress_text(prog)["state"] == "running"
        stream.close()                                  # the browser cancelled
        assert export.progress_text(prog)["state"] == "stopped"
        monkeypatch.setattr(export, "ZIP_ASK", 10)
        assert "(2 songs)" in client.get(f"/playlists/{pid}/zip/check").json()["ask"]
        monkeypatch.setattr(export, "ZIP_MAX", 20)
        c = client.get(f"/playlists/{pid}/zip/check").json()
        assert not c["ok"] and "limit for one download" in c["error"]
        assert client.get(f"/playlists/{pid}/zip").status_code == 413


def test_take_songs_out_of_an_auto_playlist(cfg, monkeypatch):
    monkeypatch.setenv("WORKER", "0")
    monkeypatch.setenv("SLSKD_URL", "http://127.0.0.1:9")
    from fastapi.testclient import TestClient
    import app.web as web
    importlib.reload(web)
    add_files(cfg, *[(f"Albums/A/{i}.mp3", "A", f"Song {i}", "X", i) for i in range(1, 6)],
              ("Random/t13.mp3", "B", "Track 13", "Y"), ("Random/t14.mp3", "B", "Track 14", "Y"))
    pid = sync.add_local_playlist(cfg, "Everything", rule={})
    sync.refresh_local(cfg, pid)
    m3u = cfg.music_root / "Playlists/Everything/Everything.m3u"
    key = {r["song"]["title"]: r["key"] for r in sync.summary(cfg, pid)[0]}

    with TestClient(web.app) as client:
        # One song, then every song a search finds.
        r = client.post(f"/playlists/{pid}/songs/{key['Song 2']}/remove?back=q%3Dsong", follow_redirects=False)
        assert r.headers["location"] == f"/playlists/{pid}?q=song"
        page = client.get(f"/playlists/{pid}?q=track").text
        assert "2 of 7 songs match" in page and "Remove all 2" in page and 'id="pl-pick"' in page
        client.post(f"/playlists/{pid}/songs/remove", data={"matching": "1", "back": "q=track"})
        rows, counts = sync.summary(cfg, pid)
        assert counts == {"synced": 4, "skipped": 3}
        assert "Song 2" not in m3u.read_text() and "Track 13" not in m3u.read_text()

        # They stay out when the playlist refreshes, and out of its copies.
        sync.refresh_local(cfg, pid)
        assert sync.summary(cfg, pid)[1] == {"synced": 4, "skipped": 3}

        class Fake:
            tracks = {}
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def find_track(self, song): return f"spotify:track:{song['title']}"
            def playlist_exists(self, pid): return pid in Fake.tracks
            def create_playlist(self, name, desc): Fake.tracks["sp"] = []; return "sp"
            def set_tracks(self, pid, uris): Fake.tracks[pid] = list(uris)

        monkeypatch.setattr(mirror, "_client", lambda cfg, target: Fake())
        mirror.run(cfg, pid, "spotify")
        assert Fake.tracks["sp"] == [f"spotify:track:Song {i}" for i in (1, 3, 4, 5)]
        assert mirror.get(pid, "spotify")["total"] == 4

        # The songs you took out are listed, and can go back one at a time or all together.
        page = client.get(f"/playlists/{pid}?show=left_out").text
        assert "3 songs you took out" in page and "Put them all back" in page and "Left out" in page
        client.post(f"/playlists/{pid}/songs/{key['Song 2']}/putback")
        assert "Song 2" in m3u.read_text()
        client.post(f"/playlists/{pid}/songs/putback", data={"matching": "1", "back": "show=left_out"})
        assert sync.summary(cfg, pid)[1] == {"synced": 7}

        # A hand-picked playlist drops the songs instead.
        hand = sync.add_local_playlist(cfg, "Picks")
        sync.add_songs(cfg, hand, [key["Song 1"], key["Song 3"]])
        client.post(f"/playlists/{hand}/songs/remove", data={"key": [key["Song 1"]]})
        assert [r["song"]["title"] for r in sync.summary(cfg, hand)[0]] == ["Song 3"]
        assert client.post(f"/playlists/{pid}/songs/nope").status_code == 404


def test_service_hiccups_are_retried_and_one_bad_search_skips_a_song(cfg, monkeypatch):
    import httpx
    from app import apple, spotify
    monkeypatch.setattr(apple.time, "sleep", lambda s: None)
    monkeypatch.setattr(spotify.time, "sleep", lambda s: None)
    calls = []

    def flaky(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if len(calls) == 2:
            return httpx.Response(504, text="Gateway Time-out")
        return httpx.Response(200, json={"ok": 1})

    a = object.__new__(apple.Client)
    a.mode, a.http = "api", httpx.Client(transport=httpx.MockTransport(flaky), base_url="https://api.example")
    assert a.get("/v1/x") == {"ok": 1} and len(calls) == 3
    s = object.__new__(spotify.Client)
    s.tok = {"access_token": "t", "expires_at": 9e12}
    s.http = httpx.Client(transport=httpx.MockTransport(flaky), base_url="https://api.example")
    calls.clear()
    assert s.get("/v1/x") == {"ok": 1} and len(calls) == 3

    # A search that still fails skips that song; the rest of the copy carries on.
    add_files(cfg, *[(f"Albums/A/{i}.mp3", "A", f"Song {i}", "X", i) for i in range(1, 6)])
    pid = sync.add_local_playlist(cfg, "Everything", rule={})
    sync.refresh_local(cfg, pid)

    class Fake:
        tracks, down = {}, False
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def find_track(self, song):
            if Fake.down or song["title"] == "Song 2":
                raise httpx.HTTPStatusError("504", request=httpx.Request("GET", "https://x"), response=httpx.Response(504))
            return f"spotify:track:{song['title']}"
        def playlist_exists(self, pid): return pid in Fake.tracks
        def create_playlist(self, name, desc): Fake.tracks["sp"] = []; return "sp"
        def set_tracks(self, pid, uris): Fake.tracks[pid] = list(uris)

    monkeypatch.setattr(mirror, "_client", lambda cfg, target: Fake())
    mirror.run(cfg, pid, "spotify")
    m = mirror.get(pid, "spotify")
    assert Fake.tracks["sp"] == [f"spotify:track:Song {i}" for i in (1, 3, 4, 5)]
    assert "1 song couldn't be looked up" in m["note"] and m["missing"] == []
    with db.connect() as c:          # not remembered as missing, so the next update looks again
        assert c.execute("SELECT COUNT(*) FROM song_links WHERE target = 'spotify'").fetchone()[0] == 4
    # When the service is down, the copy stops instead of skipping everything.
    with db.connect() as c:
        c.execute("DELETE FROM song_links")
    Fake.down = True
    monkeypatch.setattr(mirror, "GIVE_UP", 3)
    with pytest.raises(httpx.HTTPStatusError):
        mirror.run(cfg, pid, "spotify")


def test_apple_copy_adds_songs_as_it_matches_and_a_restart_carries_on(cfg, monkeypatch):
    from app import apple
    add_files(cfg, *[(f"Albums/A/{i}.mp3", "A", f"Song {i}", "X", i) for i in range(1, 8)])
    pid = sync.add_local_playlist(cfg, "Everything", rule={})
    sync.refresh_local(cfg, pid)

    class Fake:
        lists, calls, down = {}, [], None
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def find_song(self, song):
            if song["title"] == Fake.down:
                raise apple.AppleError("Apple Music kept failing")
            return None if song["title"] == "Song 4" else f"c{song['title'][-1]}"
        def playlist_exists(self, rid): return rid in Fake.lists
        def playlist_song_ids(self, rid): return set(Fake.lists[rid])
        def create_playlist(self, name, desc, ids):
            Fake.lists["am"] = list(ids); Fake.calls.append(("create", list(ids))); return "am"
        def add_tracks(self, rid, ids):
            Fake.lists[rid] += ids; Fake.calls.append(("add", list(ids)))

    monkeypatch.setattr(mirror, "_client", lambda cfg, target: Fake())
    monkeypatch.setattr(mirror, "ADD_BATCH", 2)
    monkeypatch.setattr(mirror, "GIVE_UP", 1)
    # Apple stops answering at Song 6: what was matched before is already in the playlist.
    Fake.down = "Song 6"
    with pytest.raises(apple.AppleError):
        mirror.run(cfg, pid, "apple")
    assert Fake.calls == [("create", ["c1", "c2"]), ("add", ["c3", "c5"])]
    m = mirror.get(pid, "apple")
    assert m["remote_id"] == "am" and m["matched"] == 4 and "kept failing" in m["error"]
    # Run it again: same playlist, nothing added twice, and the rest goes in.
    Fake.down = None
    mirror.run(cfg, pid, "apple")
    assert Fake.calls[2:] == [("add", ["c6", "c7"])]
    assert Fake.lists == {"am": ["c1", "c2", "c3", "c5", "c6", "c7"]}
    m = mirror.get(pid, "apple")
    assert m["matched"] == 6 and m["total"] == 7 and m["missing"] == ["A - Song 4"] and not m["error"]

    # Apple lists a song you already had in another version under that version's ID, and you can delete
    # songs from the copy in the Music app. An update adds neither back, only the new song.
    Fake.lists["am"] = ["c1", "c2", "x3", "c6", "c7"]          # c3 shows as x3; c5 deleted in the Music app
    add_files(cfg, ("Albums/A/8.mp3", "A", "Song 8", "X", 8))
    mirror.run(cfg, pid, "apple")
    assert Fake.calls[3:] == [("add", ["c8"])]


def test_cover_shows_found_art_from_different_artists(cfg, monkeypatch):
    from pathlib import Path
    from app import art
    add_files(cfg, ("Albums/A/X1/1.mp3", "A", "a1", "X1"), ("Albums/A/X2/1.mp3", "A", "a2", "X2"),
              ("Albums/B/Y/1.mp3", "B", "b1", "Y"), ("Albums/C/Z/1.mp3", "C", "c1", "Z"),
              ("Albums/D/W/1.mp3", "D", "d1", "W"))
    # Album Y has no art: its URL would show the placeholder.
    monkeypatch.setattr(art, "thumbnail", lambda cfg, key, path: None if "/B/" in path else Path(path))
    pid = sync.add_local_playlist(cfg, "Everything", rule={})
    sync.refresh_local(cfg, pid)
    with db.connect() as c:
        url = {r["album"]: f"/art/{r['art_key']}" for r in c.execute("SELECT album, art_key FROM library")}
    # One album each from A, C and D, then A's other album fills the fourth tile.
    assert sync.covers(cfg, pid) == [url["X1"], url["Z"], url["W"], url["X2"]]


def test_find_a_song_by_hand_for_an_apple_copy(cfg, monkeypatch):
    monkeypatch.setenv("WORKER", "0")
    monkeypatch.setenv("SLSKD_URL", "http://127.0.0.1:9")
    from fastapi.testclient import TestClient
    import app.web as web
    from app import apple
    importlib.reload(web)
    monkeypatch.setattr(apple, "user_token", lambda cfg: "token")
    add_files(cfg, ("Albums/2Pac/1.mp3", "2Pac", "Bury Me a G", "Thug Life"),
              ("Albums/2Pac/2.mp3", "2Pac", "Dear Mama", "Me Against the World"))
    pid = sync.add_local_playlist(cfg, "Everything", rule={})
    sync.refresh_local(cfg, pid)

    class Fake:
        lists, searches = {}, []
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def find_song(self, song): return None if song["title"] == "Bury Me a G" else "c-mama"
        def playlist_exists(self, rid): return rid in Fake.lists
        def playlist_song_ids(self, rid): return set(Fake.lists[rid])
        def create_playlist(self, name, desc, ids): Fake.lists["am"] = list(ids); return "am"
        def add_tracks(self, rid, ids): Fake.lists[rid] += ids
        def search_songs(self, term):
            Fake.searches.append(term)
            return [{"id": "c-other", "title": "Bury Me", "artist": "Someone", "album": "", "duration_ms": 100_000,
                     "explicit": False, "art": None, "preview": None, "url": None},
                    {"id": "c-thug", "title": "Bury Me a G", "artist": "Thug Life", "album": "Thug Life, Vol. 1",
                     "duration_ms": 205_000, "explicit": True, "art": None, "preview": "https://x.test/p.m4a",
                     "url": "https://music.apple.com/x"}]

    monkeypatch.setattr(mirror, "_client", lambda cfg, target: Fake())
    mirror.run(cfg, pid, "apple")
    assert Fake.lists == {"am": ["c-mama"]}
    key = mirror.not_found(pid, "apple")[0]["key"]
    assert mirror.get(pid, "apple")["not_found"] == 1

    with TestClient(web.app) as client:
        r = client.get(f"/playlists/{pid}/copies")
        assert "1 song not found on Apple Music" in r.text
        r = client.get(f"/playlists/{pid}/copies/apple/missing")
        assert r.status_code == 200 and "Bury Me a G" in r.text and f"/copies/apple/find/" in r.text
        # By artist and title, then by title alone; the same title comes first, though Thug Life isn't 2Pac.
        r = client.get(f"/playlists/{pid}/copies/apple/find/{key}")
        assert Fake.searches == ["2pac Bury Me a G", "Bury Me a G"]
        assert r.text.index("Thug Life, Vol. 1") < r.text.index("Someone") and "+5 s" in r.text
        r = client.post(f"/playlists/{pid}/copies/apple/find/{key}", data={"remote_id": "c-thug"},
                        follow_redirects=False)
        assert r.status_code == 303 and "Added+to+the+Apple+Music+copy" in r.headers["location"]
        assert client.get(f"/playlists/{pid}/copies/spotify/find/{key}").status_code == 200    # local: both targets
    assert Fake.lists == {"am": ["c-mama", "c-thug"]}
    assert mirror.not_found(pid, "apple") == [] and mirror.get(pid, "apple")["matched"] == 2
    # An update keeps your pick and doesn't add it again.
    mirror.run(cfg, pid, "apple")
    assert Fake.lists == {"am": ["c-mama", "c-thug"]}
