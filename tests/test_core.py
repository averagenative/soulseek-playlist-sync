import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.id3 import ID3

from app.folders import place, rename_all, safe_name, song_file_name, write_m3u
from app.match import (artist_matches, core_title, norm, norm_title, primary_artist, rank_candidates,
                       search_queries)
from app.tagger import tag_mp3


# ---------------------------------------------------------------- match

def test_norm_and_titles():
    assert norm("Cowboys Don’t Cry") == "cowboys dont cry"
    assert norm_title('Better Place (From TROLLS Band Together)') == "better place"
    assert norm_title("So What'Cha Want (Remastered 2009)") == "so whatcha want"
    assert norm_title("Lies (feat. Lana Del Rey)") == "lies"
    # Version info that changes the recording is kept.
    assert norm_title("Steve's Lava Chicken (Extended Version)") == "steves lava chicken extended version"
    assert core_title("The Claw (Pachelbel's Canon) [feat. Marly Luske & Joff Bush]") == "The Claw"


def test_primary_artist():
    assert primary_artist("Justin Timberlake, Eric Andre, Daveed Diggs & Troye Sivan") == "Justin Timberlake"
    assert primary_artist("Bob Marley & The Wailers") == "Bob Marley & The Wailers"
    assert artist_matches("Zach Bryan & Lana Del Rey", "Zach Bryan")
    assert not artist_matches("Neil Young", "Neil Diamond")


def test_search_queries():
    q = search_queries("Perfect", "Justin Timberlake, Eric Andre", "TROLLS Band Together (Original Motion Picture Soundtrack)")
    assert q[0] == "justin timberlake perfect"
    assert q[1] == "trolls band together perfect"


def _resp(user, files, free=True, speed=500000, queue=0):
    return {"username": user, "hasFreeUploadSlot": free, "uploadSpeed": speed, "queueLength": queue, "files": files}


def test_rank_candidates_filters_and_orders():
    want_ms = 240_000
    responses = [
        _resp("flac_guy", [{"filename": r"Music\Artist\01 - Perfect.flac", "size": 30_000_000, "length": 240}]),
        _resp("wrong_len", [{"filename": r"Music\Artist\01 - Perfect.mp3", "size": 9_600_000, "length": 300, "bitRate": 320}]),
        _resp("low_br", [{"filename": r"Music\Artist\01 - Perfect.mp3", "size": 3_000_000, "length": 240, "bitRate": 128}]),
        _resp("wrong_song", [{"filename": r"Music\Perfect Album\03 - Something Else.mp3", "size": 9_600_000, "length": 240, "bitRate": 320}]),
        _resp("v0", [{"filename": r"Music\Justin Timberlake\02 - Perfect.mp3", "size": 7_500_000, "length": 241, "bitRate": 250}]),
        _resp("best", [{"filename": r"Music\Justin Timberlake\Trolls\05 - Perfect.mp3", "size": 9_600_000, "length": 240, "bitRate": 320}]),
        _resp("busy", [{"filename": r"Music\Justin Timberlake\05 Perfect.mp3", "size": 9_600_000, "length": 240, "bitRate": 320}],
              free=False, queue=40),
    ]
    picked = rank_candidates(responses, "Perfect", "Justin Timberlake, Eric Andre", want_ms)
    users = [c["username"] for c in picked]
    assert users[0] == "best"
    assert set(users) == {"best", "busy", "v0"}
    assert users.index("v0") < users.index("busy") or picked[users.index("busy")]["score"] < picked[0]["score"]


# ---------------------------------------------------------------- folders

def test_safe_names():
    assert safe_name('AC/DC: "Live"?') == "AC_DC_ _Live__"
    assert safe_name("con") == "_con"
    assert safe_name("name. ") == "name"
    song = {"artist": "*NSYNC & Justin Timberlake", "title": 'Better Place (From "TROLLS")'}
    assert song_file_name(song, 5, 3) == "005 - _NSYNC & Justin Timberlake - Better Place (From _TROLLS_).mp3"
    assert song_file_name(song, None, 3) == "_NSYNC & Justin Timberlake - Better Place (From _TROLLS_).mp3"


def test_rename_all_swaps(tmp_path):
    (tmp_path / "001 - A.mp3").write_text("a")
    (tmp_path / "002 - B.mp3").write_text("b")
    rename_all(tmp_path, {"001 - A.mp3": "002 - A.mp3", "002 - B.mp3": "001 - B.mp3"})
    assert (tmp_path / "002 - A.mp3").read_text() == "a"
    assert (tmp_path / "001 - B.mp3").read_text() == "b"


def test_place_links_and_moves(tmp_path):
    src = tmp_path / "src.mp3"
    src.write_text("x")
    place(src, tmp_path / "out" / "a.mp3")
    assert (tmp_path / "out" / "a.mp3").stat().st_ino == src.stat().st_ino   # hardlink
    place(src, tmp_path / "out" / "b.mp3", move=True)
    assert not src.exists() and (tmp_path / "out" / "b.mp3").read_text() == "x"


def test_m3u(tmp_path):
    write_m3u(tmp_path / "List.m3u", [(240, "A - B", "001 - A - B.mp3"), (10, "C - D", "../x/C.mp3")])
    assert (tmp_path / "List.m3u").read_text().splitlines() == [
        "#EXTM3U", "#EXTINF:240,A - B", "001 - A - B.mp3", "#EXTINF:10,C - D", "../x/C.mp3"]


# ---------------------------------------------------------------- tagging

@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_tag_mp3_replaces_tags(tmp_path):
    f = tmp_path / "t.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "1",
                    "-metadata", "title=junk title", "-metadata", "comment=ripped by someone", "-b:a", "320k", str(f)],
                   check=True)
    song = {"title": "Peaches", "artist": "Jack Black", "album": "The Super Mario Bros. Movie",
            "album_artist": "Various Artists", "track_number": 9, "disc_number": 1, "release_date": "2023-04-07",
            "genre": "Soundtrack", "isrc": "USUM72302888"}
    tag_mp3(f, song, b"\xff\xd8\xff\xe0fakejpeg")
    tags = ID3(f)
    assert tags.version[:2] == (2, 3)
    assert str(tags["TIT2"]) == "Peaches" and str(tags["TPE1"]) == "Jack Black"
    assert str(tags["TPE2"]) == "Various Artists" and str(tags["TCMP"]) == "1"
    assert str(tags["TSRC"]) == "USUM72302888" and str(tags["TRCK"]) == "9"
    assert "COMM::eng" not in tags and not any(k.startswith("COMM") for k in tags.keys())
    assert tags.getall("APIC")[0].data.startswith(b"\xff\xd8")


def test_short_artist():
    from app.folders import short_artist
    assert short_artist("Justin Timberlake, Eric Andre, Daveed Diggs, Kid Cudi & Troye Sivan") == \
        "Justin Timberlake, Eric Andre, Daveed Diggs"
    assert short_artist("Neil Young") == "Neil Young"
    assert len(short_artist("A" * 80)) <= 45
