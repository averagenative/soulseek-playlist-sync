import importlib
import io

import pytest

from app import config, db, mirror, settings, sync, tagger, youtube


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSIC_ROOT", str(tmp_path / "music"))
    base = config.load()
    db.init(base.db_path)
    settings.init(base)
    return base


@pytest.mark.parametrize("title, channel, want", [
    ("Third Eye Blind - Semi-Charmed Life (Official Music Video) [HD]", "thirdeyeblind",
     ("Third Eye Blind", "Semi-Charmed Life", True)),
    ("Bullet With Butterfly Wings (Remastered 2012)", "Smashing Pumpkins",
     ("Smashing Pumpkins", "Bullet With Butterfly Wings (Remastered 2012)", False)),
    ("Smells Like Teen Spirit", "Nirvana - Topic", ("Nirvana", "Smells Like Teen Spirit", False)),
    ('Weezer "Buddy Holly"', "weezer", ("Weezer", "Buddy Holly", False)),
    ("Hozier - Take Me To Church (Official Live Video)", "HozierVEVO", ("Hozier", "Take Me To Church (Live)", False)),
    ("Adele - Hello (Lyrics)", "7clouds", ("Adele", "Hello", False)),
    ("Beck \u2013 Loser [Official Music Video]", "BeckVEVO", ("Beck", "Loser", True)),
    ("Mazzy Star - Fade Into You | Live at Shoreline Amphitheatre (1994)", "x", ("Mazzy Star", "Fade Into You (Live)", False)),
    ("Tonic - If You Could Only See (Closed-Captioned)", "x", ("Tonic", "If You Could Only See", False)),
    ("Fastball - Out Of My Head www.fastballtheband.com", "x", ("Fastball", "Out Of My Head", False)),
    ("Best I Ever Had (Grey Sky Morning)", "VerticalHorizonMusic", ("Vertical Horizon", "Best I Ever Had (Grey Sky Morning)", False)),
    ('Better Than Ezra "Desperately Wanting" official music video', "x", ("Better Than Ezra", "Desperately Wanting", True)),
])
def test_video_titles_become_artist_and_song(title, channel, want):
    assert youtube.parse_video_title(title, channel) == want


def test_playlist_links():
    pid = "PLkpn4UHlnIHnfh9Ye0ysC__1f29F2Bnv1"
    for link in (f"https://www.youtube.com/playlist?list={pid}", f"https://music.youtube.com/playlist?list={pid}&si=x",
                 f"https://www.youtube.com/watch?v=abc&list={pid}&index=2", f"https://music.youtube.com/browse/VL{pid}", pid):
        assert youtube.playlist_id(link) == pid
    assert youtube.playlist_id("https://music.youtube.com/playlist?list=RDCLAK5uy_kmPRjHDECIcuVwnKsx2Ng7fyNgFKWNJFs")
    for bad, why in (("https://www.youtube.com/playlist?list=LM", "Liked Music"), ("https://youtu.be/x?list=RDMM", "Mix"),
                     ("not a link", "isn't a link")):
        with pytest.raises(youtube.YouTubeError, match=why):
            youtube.playlist_id(bad)


def test_video_songs_and_their_own_download():
    mv = youtube.video_song({"id": "a1", "title": "Pearl Jam - Jeremy (Official 4K Video)", "duration": 334, "channel": "Pearl Jam"})
    assert mv["key"] == "youtube:a1" and mv["artist"] == "Pearl Jam" and mv["title"] == "Jeremy"
    assert mv["duration_ms"] is None                     # a music video's length isn't the song's
    topic = youtube.video_song({"id": "b2", "title": "Loser", "duration": 234, "channel": "Beck - Topic"})
    assert topic["duration_ms"] == 234000 and topic["artist"] == "Beck"
    assert youtube.video_song({"id": "c3", "title": "[Deleted video]", "duration": None}) is None
    assert youtube.video_song({"id": "c4", "title": None, "duration": None}) is None
    cand = youtube.own_video(topic)
    assert cand[0]["url"] == "https://www.youtube.com/watch?v=b2" and cand[0]["duration"] == 234
    assert youtube.own_video({"key": "spotify:x", "url": "https://open.spotify.com/track/x"}) == []


def test_youtube_thumbnail_becomes_a_square_cover():
    from PIL import Image
    im = Image.new("RGB", (480, 360), "black")
    im.paste(Image.new("RGB", (480, 270), "red"), (0, 45))          # the letterboxed 16:9 frame
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    out = Image.open(io.BytesIO(tagger._square_youtube_thumbnail(buf.getvalue())))
    assert out.size == (270, 270)
    r, g, b = out.getpixel((5, 5))
    assert r > 200 and g < 60 and b < 60                            # no black bar left at the top


def test_add_a_youtube_playlist(cfg, monkeypatch):
    monkeypatch.setenv("WORKER", "0")
    monkeypatch.setenv("SLSKD_URL", "http://127.0.0.1:9")
    info = {"title": "90s Alt", "entries": [
        {"id": "v1", "title": "Eve 6 - Inside Out", "duration": 223, "channel": "EVE6"},
        {"id": "v2", "title": "[Private video]", "duration": None},
        {"id": "v4", "title": None, "duration": None, "channel": None},        # unavailable: no title at all
        {"id": "v3", "title": "Smells Like Teen Spirit", "duration": 301, "channel": "Nirvana - Topic"}]}
    monkeypatch.setattr(youtube.Playlists, "_info", lambda self, rid: info)
    monkeypatch.setattr(youtube, "can_read_playlists", lambda: True)     # passes without yt-dlp installed
    from fastapi.testclient import TestClient
    import app.web as web
    importlib.reload(web)
    with TestClient(web.app) as client:
        assert 'href="/youtube/add"' in client.get("/").text
        r = client.post("/youtube/add", data={"link": "https://www.youtube.com/playlist?list=LM"}, follow_redirects=False)
        assert r.headers["location"].startswith("/youtube/add?error=")
        assert "Liked Music is private" in client.get(r.headers["location"]).text
        link = "https://music.youtube.com/playlist?list=PL0123456789abcdef"
        r = client.post("/youtube/add", data={"link": link}, follow_redirects=False)
        pid = "youtube:PL0123456789abcdef"
        assert r.headers["location"] == f"/playlists/{pid}"
        sync.wait_idle(pid)
        p = sync.playlist(pid)
        assert p["name"] == "90s Alt" and p["folder"] == "YouTube/90s Alt"
        rows, _ = sync.summary(cfg, pid)
        assert [(x["song"]["artist"], x["song"]["title"]) for x in rows] == [("Eve 6", "Inside Out"),
                                                                             ("Nirvana", "Smells Like Teen Spirit")]
        page = client.get(f"/playlists/{pid}").text
        assert "Inside Out" in page and "Copy to Spotify" in page and "Copy to Apple Music" in page
        # Adding the same playlist again opens it instead of adding a second copy.
        r = client.post("/youtube/add", data={"link": link}, follow_redirects=False)
        assert r.headers["location"] == f"/playlists/{pid}" and len(sync.tracked()) == 1

        # Your own name: it renames the folder, and stays when the playlist is renamed on YouTube.
        settings_form = {"folder": "YouTube/90s Alt", "numbering": "1", "library_mode": "copy"}
        assert 'placeholder="The name on YouTube"' in page
        client.post(f"/playlists/{pid}/settings", data={**settings_form, "name": "Road Trip"})
        sync.wait_idle(pid)
        info["title"] = "90s Alternative"
        info["entries"] = info["entries"][:1]           # Nirvana left the playlist before it was downloaded
        client.post(f"/playlists/{pid}/preview")
        sync.wait_idle(pid)
        p = sync.playlist(pid)
        assert p["name"] == "Road Trip" and p["folder"] == "YouTube/Road Trip" and p["own_name"] == 1
        rows, counts = sync.summary(cfg, pid)
        assert [x["song"]["title"] for x in rows] == ["Inside Out"] and "removed" not in counts
        # Clearing it goes back to YouTube's name.
        client.post(f"/playlists/{pid}/settings", data={**settings_form, "folder": "YouTube/Road Trip", "name": ""})
        sync.wait_idle(pid)
        assert sync.playlist(pid)["name"] == "90s Alternative"
    assert mirror.targets_for("youtube") == ["spotify", "apple"]


@pytest.mark.parametrize("error, want", [
    ("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
     "Age-restricted: YouTube only plays it for a signed-in account"),
    ("ERROR: [youtube] abc: Video unavailable. It was blocked due to the claimed content by Beggars Group Digital.",
     "Blocked on YouTube by a copyright claim from Beggars Group Digital"),
    ("ERROR: [youtube] abc: Video unavailable. This video is not available", "The video was deleted or isn't available"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden",
     "YouTube refused the download (HTTP 403), which is often temporary"),
    ("Not on Soulseek (no MP3 with the right length)", None),
])
def test_youtube_errors_get_a_short_reason(error, want):
    assert youtube.explain(error) == want


def _youtube_song(cfg, monkeypatch, download):
    from app import worker
    song = {"key": "youtube:own1", "title": "Break Up Your Band", "artist": "Chavez", "duration_ms": 200000,
            "url": "https://www.youtube.com/watch?v=own1"}
    with db.connect() as c:
        sync._save_songs(c, [song])
        c.execute("INSERT INTO downloads (song_key, state, source) VALUES (?, 'pending', 'youtube')", (song["key"],))
    searched = []
    monkeypatch.setattr(youtube, "search", lambda s, tol: searched.append(s["title"]) or [
        {"id": "own1", "url": song["url"], "title": "Chavez - Break Up Your Band", "channel": "fan"},
        {"id": "topic1", "url": "https://www.youtube.com/watch?v=topic1", "title": "Break Up Your Band",
         "channel": "Chavez - Topic"}])
    monkeypatch.setattr(youtube, "download", download)
    monkeypatch.setattr(worker, "tag_mp3", lambda *a: None)
    monkeypatch.setattr(worker, "artwork", lambda *a: None)
    monkeypatch.setattr(sync, "place_download", lambda cfg, key, mp3: cfg.music_root / "placed.mp3")
    monkeypatch.setattr(worker.library, "add_file", lambda *a: None)
    w = worker.Worker()
    with db.connect() as c:
        w.youtube_job(dict(c.execute("SELECT * FROM downloads WHERE song_key = 'youtube:own1'").fetchone()))
    with db.connect() as c:
        return dict(c.execute("SELECT state, detail FROM downloads WHERE song_key = 'youtube:own1'").fetchone()), searched


def test_a_playlist_video_that_fails_falls_back_to_another_upload(cfg, monkeypatch, tmp_path):
    def download(cand, work, progress=None, cookies=None):
        if "own1" in cand["url"]:
            raise youtube.YouTubeError("YouTube download failed: ERROR: [youtube] own1: Video unavailable. "
                                       "It was blocked due to the claimed content by Beggars Group Digital.")
        mp3 = tmp_path / "x" / "audio.mp3"
        mp3.parent.mkdir(exist_ok=True)
        mp3.write_bytes(b"ID3")
        return mp3
    d, searched = _youtube_song(cfg, monkeypatch, download)
    assert searched == ["Break Up Your Band"]                  # searched only after its own video failed
    assert d == {"state": "done", "detail": "From YouTube: Chavez - Topic"}


def test_a_failed_youtube_song_says_why(cfg, monkeypatch):
    def download(cand, work, progress=None, cookies=None):
        raise youtube.YouTubeError("YouTube download failed: ERROR: [youtube] x: Sign in to confirm your age.")
    d, _ = _youtube_song(cfg, monkeypatch, download)
    assert d["state"] == "failed" and d["detail"].endswith("Tried 1 other upload too.")
    assert youtube.explain(d["detail"]) == "Age-restricted: YouTube only plays it for a signed-in account"
