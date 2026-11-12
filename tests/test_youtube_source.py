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
        {"id": "v3", "title": "Smells Like Teen Spirit", "duration": 301, "channel": "Nirvana - Topic"}]}
    monkeypatch.setattr(youtube.Playlists, "_info", lambda self, rid: info)
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
    assert mirror.targets_for("youtube") == ["spotify", "apple"]
