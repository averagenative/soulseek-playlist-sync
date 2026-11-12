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


def _seed_playlist(cfg, pid="apple:p.test", n=3, files=True):
    folder = cfg.music_root / "AppleMusic" / "Mix"
    folder.mkdir(parents=True, exist_ok=True)
    with db.connect() as c:
        c.execute("INSERT INTO playlists (id, source, remote_id, name, folder) VALUES (?, 'apple', 'p.test', 'Mix', 'AppleMusic/Mix')", (pid,))
        for i in range(1, n + 1):
            key = f"apple:{i}"
            c.execute("INSERT INTO songs (key, isrc, title, artist, duration_ms) VALUES (?, ?, ?, ?, ?)",
                      (key, f"ISRC{i}", f"Song {i}", "Artist", 180000))
            c.execute("INSERT INTO entries (playlist_id, position, song_key) VALUES (?, ?, ?)", (pid, i, key))
            name = f"{i:03d} - Artist - Song {i}.mp3"
            c.execute("INSERT INTO items (playlist_id, song_key, file, origin, owned) VALUES (?, ?, ?, 'download', 1)",
                      (pid, key, name if files else None))
            if files:
                (folder / name).write_bytes(b"ID3" + bytes([i]) * 1000)
        # the first song appears twice in the playlist
        c.execute("INSERT INTO entries (playlist_id, position, song_key) VALUES (?, ?, 'apple:1')", (pid, n + 1))
    return pid


def test_zip_export_is_a_valid_self_contained_zip(cfg):
    import io, zipfile
    from app import export
    pid = _seed_playlist(cfg)
    folder, files, m3u = sync.export_files(cfg, pid)
    data = b"".join(export.stream_zip(folder, files, m3u))
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None
    names = z.namelist()
    assert names[0] == "Mix/Mix.m3u" and len(names) == 4          # one file per song, not per position
    lines = z.read("Mix/Mix.m3u").decode().splitlines()
    assert lines[0] == "#EXTM3U" and lines.count("001 - Artist - Song 1.mp3") == 2
    assert z.read("Mix/002 - Artist - Song 2.mp3") == b"ID3" + bytes([2]) * 1000
    assert sync.song_path(cfg, pid, "apple:2").name == "002 - Artist - Song 2.mp3"
    assert sync.song_path(cfg, pid, "apple:missing") is None


def test_mirror_to_spotify_keeps_order_and_reports_missing(cfg, monkeypatch):
    from app import mirror
    pid = _seed_playlist(cfg, files=False)

    class Fake:
        made, tracks, searches = [], {}, []
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def find_track(self, song):
            Fake.searches.append(song["key"])
            return None if song["isrc"] == "ISRC2" else f"spotify:track:{song['isrc']}"
        def playlist_exists(self, pid): return pid in Fake.tracks
        def create_playlist(self, name, desc):
            Fake.made.append(name); Fake.tracks["sp1"] = []; return "sp1"
        def set_tracks(self, pid, uris): Fake.tracks[pid] = list(uris)

    monkeypatch.setattr(mirror, "_client", lambda cfg, target: Fake())
    mirror.run(cfg, pid)
    assert Fake.made == ["Mix"]
    assert Fake.tracks["sp1"] == ["spotify:track:ISRC1", "spotify:track:ISRC3", "spotify:track:ISRC1"]
    m = mirror.get(pid, "spotify")
    assert (m["matched"], m["total"], m["missing"]) == (2, 3, ["Artist - Song 2"])
    assert m["url"] == "https://open.spotify.com/playlist/sp1"
    # A second run updates the same playlist and doesn't search again for songs it already matched.
    Fake.searches.clear()
    mirror.run(cfg, pid)
    assert Fake.made == ["Mix"] and Fake.searches == []


def test_apple_paging_keeps_offset_and_page_size(monkeypatch):
    from app import apple
    seen = []

    class Fake(apple.Client):
        def __init__(self): pass
        def get(self, path, params=None):
            seen.append((path, params))
            if len(seen) == 1:
                return {"data": [1], "next": "/v1/me/library/songs?offset=100"}
            return {"data": [2]}

    assert Fake().get_all("/v1/me/library/songs", {"limit": 100}) == [1, 2]
    assert seen[1] == ("/v1/me/library/songs?offset=100&limit=100", None)
