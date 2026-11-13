import importlib

import pytest

from app import config, db, favorites, settings, spotify


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path / "music"))
    base = config.load()
    db.init(base.db_path)
    settings.init(base)
    return base


def sp(sid, title, isrc=None):
    return {"key": f"spotify:{sid}", "title": title, "artist": "A", "isrc": isrc, "duration_ms": 200000}


def am(cid, title, isrc=None):
    return {"key": f"apple:{cid}", "title": title, "artist": "A", "isrc": isrc, "duration_ms": 200000}


class FakeSpotify:
    def __init__(self, liked, catalog):
        self.liked, self.catalog, self.saved, self.searches = liked, catalog, [], 0
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def playlist_tracks(self, remote_id):
        assert remote_id == spotify.LIKED
        return list(self.liked)
    def find_track(self, song):
        self.searches += 1
        sid = self.catalog.get(song["title"])
        return f"spotify:track:{sid}" if sid else None
    def save_likes(self, uris, progress=None):
        self.saved += uris
        self.liked += [sp(u.rsplit(":", 1)[1], "?") for u in uris]


class FakeApple:
    def __init__(self, favs, catalog):
        self.favs, self.catalog, self.added, self.searches = favs, catalog, [], 0
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def favorite_songs(self):
        return list(self.favs)
    def find_song(self, song):
        self.searches += 1
        return self.catalog.get(song["title"])
    def add_favorites(self, ids, progress=None):
        self.added += ids
        self.favs += [am(c, "?") for c in ids]
        return list(ids)


def test_favorites_sync_adds_once_and_respects_removals(cfg, monkeypatch):
    fs = FakeSpotify([sp("s1", "One"), sp("s2", "Two", isrc="usx1"), sp("s3", "Three"), sp("s4", "Four"),
                      sp("s1b", "One")],                                   # a second version of One
                     catalog={"Five": "s5"})
    fa = FakeApple([am("a1", "One"), am("a2", "Deux", isrc="USX1"), am("a5", "Five"), am("a6", "Six")],
                   catalog={"Three": "c3"})
    monkeypatch.setattr(favorites, "_clients", lambda cfg: (fs, fa))
    monkeypatch.setattr(spotify, "can_save_likes", lambda cfg: True)
    favorites.save_options(1, to_apple=True, to_spotify=True, auto=False)

    # A check reports the plan and changes nothing.
    out = favorites.run(cfg, apply=False)
    assert out["to_apple"] == ["A - Three"] and out["to_spotify"] == ["A - Five"]
    assert out["missing_apple"] == ["A - Four"] and out["missing_spotify"] == ["A - Six"]
    assert out["in_sync"] == 2                      # One by name, Two/Deux by ISRC, without searching
    assert fa.searches == 2 and fs.searches == 2    # Three and Four on Apple; Five and Six on Spotify
    assert fa.added == [] and fs.saved == [] and favorites.pair_count(1) == 0

    # A sync adds only what's missing, and remembers every pair. Matches come from the cache.
    out = favorites.run(cfg, apply=True)
    assert fa.added == ["c3"] and fs.saved == ["spotify:track:s5"]
    assert out["added_apple"] == 1 and out["added_spotify"] == 1 and favorites.pair_count(1) == 5   # One pairs twice
    assert fa.searches == 2 and fs.searches == 2
    assert favorites.status(1)["state"] == "done"

    # Nothing new: nothing written.
    favorites.run(cfg, apply=True)
    assert fa.added == ["c3"] and fs.saved == ["spotify:track:s5"]

    # Unlike Three on Spotify and unfavorite Five on Apple: neither comes back.
    fs.liked = [s for s in fs.liked if s["key"] != "spotify:s3"]
    fa.favs = [s for s in fa.favs if s["key"] != "apple:a5"]
    out = favorites.run(cfg, apply=True)
    assert fa.added == ["c3"] and fs.saved == ["spotify:track:s5"] and out["to_apple"] == out["to_spotify"] == []


def test_one_direction_only_and_permission_check(cfg, monkeypatch):
    fs = FakeSpotify([sp("s3", "Three")], catalog={"Five": "s5"})
    fa = FakeApple([am("a5", "Five")], catalog={"Three": "c3"})
    monkeypatch.setattr(favorites, "_clients", lambda cfg: (fs, fa))
    monkeypatch.setattr(spotify, "can_save_likes", lambda cfg: False)
    with pytest.raises(ValueError):
        favorites.run(cfg, apply=True)              # no direction chosen
    favorites.save_options(1, to_apple=False, to_spotify=True, auto=False)
    with pytest.raises(ValueError, match="Reconnect Spotify"):
        favorites.run(cfg, apply=True)
    assert favorites.status(1)["state"] == "error" and fs.saved == []
    favorites.save_options(1, to_apple=True, to_spotify=False, auto=False)
    out = favorites.run(cfg, apply=True)            # Spotify -> Apple needs no new Spotify permission
    assert fa.added == ["c3"] and fs.saved == [] and out["to_spotify"] == []


def test_favorites_page(cfg, monkeypatch):
    monkeypatch.setenv("WORKER", "0")
    monkeypatch.setenv("SLSKD_URL", "http://127.0.0.1:9")
    from fastapi.testclient import TestClient
    import app.web as web
    importlib.reload(web)
    with TestClient(web.app) as client:
        assert 'href="/favorites"' in client.get("/").text
        r = client.get("/favorites")
        assert r.status_code == 200 and "What to sync" in r.text and "Connect both services first" in r.text
        r = client.post("/favorites/options", data={"to_apple": "1", "auto": "1"}, follow_redirects=False)
        assert r.headers["location"] == "/favorites?saved=1"
        assert favorites.options(1) == {"to_apple": True, "to_spotify": False, "auto": True}
        assert client.get("/partials/favorites").status_code == 200
        assert client.post("/favorites/nope").status_code == 404
