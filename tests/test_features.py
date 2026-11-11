import pytest

from app import config, db, settings, spotify, sync
from app.match import search_artist, search_queries


def test_search_artist_takes_first_name():
    assert search_artist("*NSYNC & Justin Timberlake") == "nsync"
    assert search_artist("Justin Timberlake, Eric Andre") == "justin timberlake"
    assert search_artist("Zach Bryan & Lana Del Rey") == "zach bryan"
    assert search_queries("Better Place (From TROLLS Band Together)", "*NSYNC & Justin Timberlake", None)[0] == "nsync better place"


def test_spotify_song_mapping():
    t = {"id": "abc", "name": "Peaches", "type": "track", "explicit": False, "duration_ms": 184000,
         "track_number": 9, "disc_number": 1, "external_ids": {"isrc": "USUM72302888"},
         "artists": [{"name": "Jack Black"}], "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
         "album": {"name": "The Super Mario Bros. Movie", "release_date": "2023-04-07",
                   "artists": [{"name": "Various Artists"}], "images": [{"url": "https://i.scdn.co/big"}]}}
    s = spotify.song(t)
    assert s["key"] == "spotify:abc" and s["isrc"] == "USUM72302888"
    assert s["artist"] == "Jack Black" and s["album_artist"] == "Various Artists"
    assert s["artwork_url"] == "https://i.scdn.co/big" and s["content_rating"] is None
    # Fields Spotify may drop in Development Mode must not break the mapping.
    s2 = spotify.song({"id": "x", "name": "Y", "artists": [], "album": {}})
    assert s2["isrc"] is None and s2["artwork_url"] is None


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path / "music"))
    base = config.load()
    db.init(base.db_path)
    settings.init(base)
    return base


def test_settings_parse_and_apply(cfg):
    out = settings.parse({"apple_dir": "Playlists/Apple", "spotify_dir": "SpotifyMusic", "min_bitrate": "256",
                          "default_numbering": "1", "default_library_mode": "copy"})
    assert out == {"apple_dir": "Playlists/Apple", "min_bitrate": 256}     # only what differs from defaults
    settings.save(out)
    assert settings.current().apple_dir == "Playlists/Apple"
    assert settings.current().min_bitrate == 256
    for bad in ({"apple_dir": "../etc"}, {"apple_dir": "/abs"}, {"min_bitrate": "999"}, {"max_downloads": "x"}):
        with pytest.raises(settings.SettingsError):
            settings.parse(bad)


def test_new_playlists_use_source_folder_and_moves(cfg, tmp_path):
    settings.save({"spotify_dir": "Spot"})
    c = settings.current()
    pid = sync.add_playlist(c, "spotify", "liked", "Liked Songs")
    p = sync.playlist(pid)
    assert p["folder"] == "Spot/Liked Songs" and p["numbering"] == 1
    folder = c.music_root / p["folder"]
    folder.mkdir(parents=True)
    (folder / "001 - A - B.mp3").write_text("x")
    with db.connect() as conn:
        conn.execute("INSERT INTO library (path) VALUES (?)", (str(folder / "001 - A - B.mp3"),))
    sync.move_playlist_folder(c, pid, "Kids/Liked")
    assert (c.music_root / "Kids/Liked/001 - A - B.mp3").exists() and not folder.exists()
    assert sync.playlist(pid)["folder"] == "Kids/Liked"
    with db.connect() as conn:
        assert conn.execute("SELECT path FROM library").fetchone()[0] == str(c.music_root / "Kids/Liked/001 - A - B.mp3")
    (c.music_root / "Busy").mkdir()
    (c.music_root / "Busy" / "x").write_text("x")
    with pytest.raises(ValueError):
        sync.move_playlist_folder(c, pid, "Busy")


def test_progress_text():
    d = {"percent": 42.4, "speed": 1_048_576, "bytes_done": 3_145_728, "size": 7_340_032, "username": "peer"}
    assert sync.progress_text(d) == "42% · 1.0 MB/s · 3.0 MB of 7.0 MB · 0:04 left from peer"


def test_youtube_rank_prefers_topic_and_rejects_wrong_versions():
    from app.youtube import rank
    entries = [
        {"id": "a", "title": "Peaches (Official Music Video)", "channel": "Illumination", "duration": 101},
        {"id": "b", "title": "Peaches", "channel": "Jack Black - Topic", "duration": 95},
        {"id": "c", "title": "Peaches (Sped Up)", "channel": "nightcore guy", "duration": 70},
        {"id": "d", "title": "Peaches - 1 Hour", "channel": "loops", "duration": 3600},
        {"id": "e", "title": "Peaches Live at the Oscars", "channel": "ABC", "duration": 96},
    ]
    picked = rank(entries, "Peaches", "Jack Black", 95_000, tol=6)
    assert [c["id"] for c in picked] == ["b", "a"]
    assert not picked[0].get("relaxed")


def test_youtube_relaxed_pass_needs_the_artist():
    from app.youtube import rank
    entries = [
        {"id": "x", "title": "Halloween Song | Music Video", "channel": "Random Kids TV", "duration": 124},
        {"id": "y", "title": "PJ Masks Halloween | Music Video | Disney Junior", "channel": "Disney Jr.", "duration": 124},
        {"id": "z", "title": "PJ Masks Halloween Full Episode", "channel": "PJ Masks", "duration": 1400},
    ]
    picked = rank(entries, "PJ Masks Halloween", "PJ Masks", 105_000, tol=6)
    assert [c["id"] for c in picked] == ["y"]
    assert picked[0]["relaxed"] and picked[0]["length_diff"] == 19
