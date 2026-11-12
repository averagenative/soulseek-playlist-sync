import importlib
import os

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
