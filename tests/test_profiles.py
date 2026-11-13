import importlib
import re
import sqlite3

import pytest

from app import apple, config, db, favorites, hits, library, profiles, settings, spotify, sync


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path / "music"))
    monkeypatch.setenv("WORKER", "0")
    monkeypatch.setenv("SLSKD_URL", "http://127.0.0.1:9")
    base = config.load()
    db.init(base.db_path)
    settings.init(base)
    profiles.load_key(base.data_dir)
    return base


@pytest.fixture
def web(cfg, monkeypatch):
    import app.web as web
    importlib.reload(web)
    monkeypatch.setattr(web.time, "sleep", lambda s: None)      # the wrong-password pause
    monkeypatch.setattr(library, "scan", lambda cfg: None)      # no background scan left running after a test
    return web


def client(web):
    from fastapi.testclient import TestClient
    return TestClient(web.app)


def signin(c, pid, password=""):
    r = c.post("/signin", data={"profile": pid, "password": password, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/", r.headers.get("location")
    return c


# ------------------------------------------------------------------ the database

def test_upgrade_gives_everything_to_the_first_profile(cfg):
    pid = sync.add_playlist(cfg, "spotify", "liked", "Liked Songs")
    # Put the database back the way it was before profiles, with some favorites history.
    with sqlite3.connect(cfg.db_path) as c:
        c.executescript("""
            DROP TABLE profiles; DROP TABLE favorite_pairs; DROP TABLE hit_favorites;
            ALTER TABLE playlists DROP COLUMN owner;
            CREATE TABLE favorite_pairs (spotify_id TEXT NOT NULL, apple_id TEXT NOT NULL, added_to TEXT, at REAL,
                                         PRIMARY KEY (spotify_id, apple_id));
            CREATE INDEX favorite_pairs_apple ON favorite_pairs(apple_id);
            CREATE TABLE hit_favorites (song_key TEXT PRIMARY KEY);
            INSERT INTO favorite_pairs VALUES ('s1', 'a1', 'apple', 1.0), ('s2', 'a2', NULL, 2.0);
            INSERT INTO hit_favorites VALUES ('local:one'), ('local:two');
        """)
    db.init(cfg.db_path)
    db.init(cfg.db_path)                       # a second start changes nothing
    ps = profiles.all_profiles()
    assert [(p["id"], p["name"], p["admin"], p["password"], p["folder"]) for p in ps] == [(1, "Me", 1, None, "")]
    assert sync.playlist(pid)["owner"] == 1 and pid == "spotify:liked"
    with db.connect() as c:
        assert [tuple(r) for r in c.execute("SELECT profile, spotify_id, apple_id, added_to FROM favorite_pairs "
                                            "ORDER BY spotify_id")] == [(1, "s1", "a1", "apple"), (1, "s2", "a2", None)]
        assert {tuple(r) for r in c.execute("SELECT profile, song_key FROM hit_favorites")} == {(1, "local:one"), (1, "local:two")}
    assert favorites.pair_count(1) == 2 and favorites.pair_count(2) == 0


def test_passwords_and_the_cookie(cfg):
    h = profiles.hash_password("hunter2")
    assert h.startswith("scrypt$") and "hunter2" not in h
    assert profiles.check_password(h, "hunter2") and not profiles.check_password(h, "Hunter2")
    ana = profiles.add("Ana", "pw")
    p = profiles.get(ana)
    cookie = profiles.sign(p)
    assert profiles.verify(cookie)["id"] == ana
    assert profiles.verify(cookie[:-1] + ("0" if cookie[-1] != "0" else "1")) is None      # tampered
    assert profiles.verify(cookie.replace(f"{ana}.", "1.", 1)) is None                      # another profile
    profiles.set_password(ana, "new")
    assert profiles.verify(cookie) is None                       # signed out by the change
    assert profiles.verify(profiles.sign(profiles.get(ana)))["id"] == ana


def test_profile_rules(cfg):
    with pytest.raises(profiles.ProfileError):
        profiles.add("me")                     # names are unique, whatever the case
    with pytest.raises(profiles.ProfileError):
        profiles.set_admin(1, False)           # the only admin
    ana = profiles.add("Ana")
    assert profiles.get(ana)["folder"] == "Ana" and not profiles.get(ana)["admin"]
    with pytest.raises(profiles.ProfileError):
        profiles.remove(cfg, 1)                # still the only admin
    profiles.set_admin(ana, True)
    profiles.set_admin(1, False)
    assert profiles.auto() is None             # two profiles: the app asks


def test_remove_a_profile(cfg):
    ana = profiles.add("Ana")
    acfg = cfg.for_profile(ana)
    apple.save_user_token(acfg, "x" * 50)
    pid = sync.add_playlist(acfg, "spotify", "liked", "Liked Songs")
    with db.connect() as c:
        c.execute("INSERT INTO favorite_pairs (profile, spotify_id, apple_id) VALUES (?, 's', 'a')", (ana,))
    favorites.save_options(ana, True, False, True)
    profiles.remove(cfg, ana)
    assert profiles.get(ana) is None and sync.playlist(pid) is None
    assert not acfg.apple_user_token_file.exists() and not acfg.profile_dir.exists()
    assert favorites.pair_count(ana) == 0 and favorites.options(ana)["to_apple"] is False


# ------------------------------------------------------------------ playlists and connections

def test_two_people_add_their_liked_songs(cfg):
    ana = profiles.add("Ana")
    mine = sync.add_playlist(cfg, "spotify", "liked", "Liked Songs")
    hers = sync.add_playlist(cfg.for_profile(ana), "spotify", "liked", "Liked Songs")
    assert (mine, hers) == ("spotify:liked", f"spotify:liked@{ana}")
    assert sync.playlist(mine)["folder"] == "SpotifyMusic/Liked Songs"
    assert sync.playlist(hers)["folder"] == "SpotifyMusic/Ana/Liked Songs"
    assert [p["id"] for p in sync.tracked(ana)] == [hers] and len(sync.tracked()) == 2
    local = sync.add_local_playlist(cfg.for_profile(ana), "Road Trip")
    assert sync.playlist(local)["owner"] == ana and sync.playlist(local)["folder"] == "Playlists/Ana/Road Trip"


def test_tokens_stay_apart(cfg):
    ana = profiles.add("Ana")
    acfg = cfg.for_profile(ana)
    assert cfg.spotify_token_file == cfg.data_dir / "spotify-token.json"         # where it was before profiles
    assert acfg.spotify_token_file == cfg.data_dir / "profiles" / str(ana) / "spotify-token.json"
    apple.save_user_token(acfg, "a" * 50)
    assert apple.user_token(acfg) == "a" * 50 and apple.user_token(cfg) is None
    assert not spotify.connected(acfg) and not spotify.connected(cfg)


def test_work_on_a_playlist_uses_its_owners_connections(cfg, monkeypatch):
    ana = profiles.add("Ana")
    pid = sync.add_playlist(cfg.for_profile(ana), "spotify", "x1", "Road Trip")
    used = []

    class Fake:
        def __init__(self, c, source):
            used.append(c.profile)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def playlist_name(self, rid):
            return "Road Trip"

        def playlist_tracks(self, rid):
            return []
    monkeypatch.setattr(sync, "_client", Fake)
    sync.fetch(cfg, pid)                       # started with the first profile's settings
    assert used == [ana]
    monkeypatch.undo()
    with pytest.raises(sync.NotConnected, match="Spotify isn't connected for Ana"):
        sync.fetch(cfg, pid)


def test_favorites_and_hits_per_profile(cfg):
    ana = profiles.add("Ana")
    favorites.save_options(ana, True, True, True)
    assert favorites.options(1) == {"to_apple": False, "to_spotify": False, "auto": False}
    assert db.get_kv("favorites_options") is None and db.get_kv(f"favorites_options:{ana}")
    key = library.song_key("Radiohead", "Karma Police", "")
    with db.connect() as c:
        c.execute("INSERT INTO hit_favorites (profile, song_key) VALUES (?, ?)", (ana, key))
    assert key in hits._load_stats(ana)["favorites"] and key not in hits._load_stats(1)["favorites"]


def test_favorites_scheduled_runs_each_profile(cfg, monkeypatch):
    ana = profiles.add("Ana")
    favorites.save_options(1, True, False, True)
    favorites.save_options(ana, False, True, True)
    ran = []
    monkeypatch.setattr(favorites, "run", lambda c, apply: ran.append(c.profile))
    monkeypatch.setattr(spotify, "connected", lambda c: True)
    monkeypatch.setattr(apple, "user_token", lambda c: "t")
    favorites.scheduled(cfg)
    assert ran == [1, ana]


# ------------------------------------------------------------------ signing in

def test_one_profile_without_a_password_needs_no_signin(web):
    with client(web) as c:
        r = c.get("/")
        assert r.status_code == 200 and profiles.COOKIE in r.cookies
        assert "Me" in r.text and "Showing" not in r.text


def test_signin_with_two_profiles(web):
    ana = profiles.add("Ana", "secret")
    with client(web) as c:
        r = c.get("/library?q=x", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/signin?next=%2Flibrary%3Fq%3Dx"
        r = c.get("/signin")
        assert "Me" in r.text and "Ana" in r.text
        r = c.get("/partials/home", headers={"X-Live": "1"})
        assert r.status_code == 401 and r.headers["X-Signin"] == "/signin"
        r = c.post("/signin", data={"profile": ana, "password": "wrong"}, follow_redirects=False)
        assert r.status_code == 303 and "error=" in r.headers["location"] and profiles.COOKIE not in r.cookies
        signin(c, ana, "secret")
        r = c.get("/")
        assert r.status_code == 200 and "Ana" in r.text and 'href="/settings"' not in r.text      # not an admin
        assert c.get("/settings").status_code == 403
        r = c.get("/settings", headers={"Accept": "text/html"})
        assert r.status_code == 403 and "Only an admin" in r.text
        c.post("/signout")
        assert c.get("/", follow_redirects=False).status_code == 303


def test_a_password_change_signs_out_other_browsers(web):
    profiles.set_password(1, "one")
    profiles.add("Ana")
    with client(web) as laptop, client(web) as phone:
        signin(laptop, 1, "one")
        signin(phone, 1, "one")
        r = laptop.post("/profiles/me", data={"name": "Dana", "folder": "", "password": "two"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/profiles?saved=me"
        assert laptop.get("/").status_code == 200
        assert phone.get("/", follow_redirects=False).status_code == 303
        assert profiles.get(1)["name"] == "Dana"


def test_cross_site_posts_are_refused(web):
    with client(web) as c:
        c.get("/")
        assert c.post("/library/scan", headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.post("/library/scan", headers={"Origin": "null"}).status_code == 403
        assert c.post("/library/scan", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        r = c.post("/library/scan", headers={"Origin": "http://testserver"}, follow_redirects=False)
        assert r.status_code == 303


def test_admin_manages_profiles(web, cfg):
    with client(web) as c:
        c.get("/")
        r = c.post("/profiles/add", data={"name": "Ana", "password": ""}, follow_redirects=False)
        assert r.headers["location"] == "/profiles?saved=added"
        ana = next(p["id"] for p in profiles.all_profiles() if p["name"] == "Ana")
        signin(c, 1)                            # two profiles now: pick one
        r = c.post(f"/profiles/{ana}/remove", data={"confirm": "nope"}, follow_redirects=False)
        assert "error=" in r.headers["location"] and profiles.get(ana)
        c.post(f"/profiles/{ana}/remove", data={"confirm": "ana"})
        assert profiles.get(ana) is None


# ------------------------------------------------------------------ other people's playlists

def test_other_peoples_playlists_are_read_only(web, cfg):
    ana = profiles.add("Ana")
    acfg = cfg.for_profile(ana)
    library._write([{"path": str(cfg.music_root / "a.mp3"), "size": 1, "mtime": 1.0, "artist": "Beck",
                     "title": "Loser", "album": "Mellow Gold", "duration": 200.0}])
    hers = sync.add_local_playlist(acfg, "Road Trip")
    key = library.collection(cfg, {})[0]["key"]
    sync.add_songs(acfg, hers, [key])
    auto = sync.add_local_playlist(acfg, "Auto", rule={"q": "", "folders": [], "artist": "", "album": ""})
    hits_pid = sync.add_local_playlist(acfg, "Hits", rule={"q": "", "folders": [], "artist": "", "album": "",
                                                           "hits": dict(hits.DEFAULTS)})
    with client(web) as c:
        signin(c, 1)
        # Every POST route on a playlist refuses someone else's.
        fill = {"target": "spotify", "key": key, "action": "sync", "do": "remove"}
        form = {"remote_id": "x", "folder": "x", "album": "x", "key": key, "confirm": "x"}
        tried = []
        for route in web.app.routes:
            path = getattr(route, "path", "")
            if "POST" not in getattr(route, "methods", ()) or not path.startswith("/playlists/{pid}"):
                continue
            pid = hits_pid if "/hits" in path else hers
            url = re.sub(r"\{(\w+)\}", lambda m: pid if m[1] == "pid" else fill[m[1]], path)
            r = c.post(url, data=form, follow_redirects=False)
            assert r.status_code == 403, (path, r.status_code, r.text[:200])
            tried.append(path)
        assert len(tried) >= 8, tried          # every one of them, so a new route can't skip the check
        # The finder searches the service with the owner's account, so it's the owner's too.
        assert c.get(f"/playlists/{hers}/copies/spotify/find/{key}").status_code == 403
        r = c.post("/library/playlist", data={"what": "selected", "key": key, "playlist": hers}, follow_redirects=False)
        assert r.status_code == 403
        # Looking is fine.
        r = c.get(f"/playlists/{hers}")
        assert r.status_code == 200 and "Ana's playlist" in r.text and "/songs/remove" not in r.text
        assert "Settings</summary>" not in r.text and "Stop tracking" not in r.text
        assert c.get(f"/playlists/{hits_pid}/hits").status_code == 200
        assert c.get(f"/playlists/{hers}/copies").status_code == 200
        # The home page shows yours unless you pick someone else's.
        assert "Road Trip" not in c.get("/").text
        r = c.get(f"/?showing={ana}")
        assert "Road Trip" in r.text and "Ana&#39;s playlists" in r.text
        assert "Road Trip" in c.get("/partials/home").text                 # remembered
        r = c.get("/?showing=all")
        assert "Your playlists" in r.text and "Ana&#39;s playlists" in r.text
        # My music only offers your own playlists to add songs to.
        assert "Road Trip" not in c.get("/library").text
    assert sync.playlist(auto)["owner"] == ana


def test_a_copy_goes_into_the_owners_account(cfg, monkeypatch):
    from app import mirror
    ana = profiles.add("Ana")
    acfg = cfg.for_profile(ana)
    library._write([{"path": str(cfg.music_root / "a.mp3"), "size": 1, "mtime": 1.0, "artist": "Beck",
                     "title": "Loser", "album": "Mellow Gold", "duration": 200.0}])
    pid = sync.add_local_playlist(acfg, "Road Trip")
    sync.add_songs(acfg, pid, [library.collection(cfg, {})[0]["key"]])
    used = []

    def fake_client(c, target):
        used.append((c.profile, target))
        raise RuntimeError("stop here")
    monkeypatch.setattr(mirror, "_client", fake_client)
    with pytest.raises(RuntimeError, match="stop here"):
        mirror.run(cfg, pid, "spotify")       # started with the first profile's settings
    assert used == [(ana, "spotify")]


def test_one_download_goes_into_both_peoples_folders(cfg):
    ana = profiles.add("Ana")
    song = {"key": "spotify:t1", "title": "Loser", "artist": "Beck", "album": "Mellow Gold", "duration_ms": 234000}
    mine = sync.add_playlist(cfg, "spotify", "p1", "Mine")
    hers = sync.add_playlist(cfg.for_profile(ana), "spotify", "p1", "Hers")
    for pid in (mine, hers):
        sync._replace_entries(pid, [song])
    src = cfg.data_dir / "dl.mp3"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"ID3 song")
    sync.place_download(cfg, "spotify:t1", src)
    a = cfg.music_root / sync.playlist(mine)["folder"]
    b = cfg.music_root / sync.playlist(hers)["folder"]
    assert a != b and str(b).endswith("SpotifyMusic/Ana/Hers")
    files = [list(d.glob("*.mp3")) for d in (a, b)]
    assert all(len(f) == 1 for f in files) and files[0][0].stat().st_ino == files[1][0].stat().st_ino   # one file, linked
