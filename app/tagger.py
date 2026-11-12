"""Write clean ID3 tags from the streaming service's metadata."""
import hashlib
import io
import logging
from pathlib import Path

import httpx
from mutagen.id3 import (APIC, ID3, TALB, TCMP, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC,
                         ID3NoHeaderError)
from mutagen.mp3 import MP3

log = logging.getLogger(__name__)


def artwork(url: str | None, cache_dir: Path) -> bytes | None:
    if not url:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".jpg")
    if f.exists():
        return f.read_bytes()
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("artwork download failed: %s", e)
        return None
    data = _square_youtube_thumbnail(r.content) if "ytimg.com" in url else r.content
    f.write_bytes(data)
    return data


def _square_youtube_thumbnail(data: bytes) -> bytes:
    """YouTube thumbnails are 4:3 with the 16:9 video letterboxed inside. The middle square of the video
    is the cover: for YouTube Music's "Topic" uploads that's the album art."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        frame = min(h, round(w * 9 / 16))           # the video's height inside the thumbnail
        top, left = (h - frame) // 2, (w - frame) // 2
        out = io.BytesIO()
        im.crop((left, top, left + frame, top + frame)).save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception as e:                           # keep the original if Pillow can't read it
        log.warning("couldn't crop a YouTube thumbnail: %s", e)
        return data


def tag_mp3(path: Path, song: dict, cover: bytes | None) -> None:
    """Replace all ID3 tags. ID3v2.3, which every hardware player reads."""
    MP3(path)  # raises if this isn't really an MP3
    try:
        ID3(path).delete(path)
    except ID3NoHeaderError:
        pass
    tags = ID3()
    tags.add(TIT2(encoding=3, text=song.get("title") or ""))
    tags.add(TPE1(encoding=3, text=song.get("artist") or ""))
    if song.get("album"):
        tags.add(TALB(encoding=3, text=song["album"]))
    album_artist = song.get("album_artist") or song.get("artist")
    if album_artist:
        tags.add(TPE2(encoding=3, text=album_artist))
    if album_artist and album_artist.lower() == "various artists":
        tags.add(TCMP(encoding=3, text="1"))
    if song.get("track_number"):
        tags.add(TRCK(encoding=3, text=str(song["track_number"])))
    if song.get("disc_number"):
        tags.add(TPOS(encoding=3, text=str(song["disc_number"])))
    if song.get("release_date"):
        tags.add(TDRC(encoding=3, text=song["release_date"][:4]))
    if song.get("genre"):
        tags.add(TCON(encoding=3, text=song["genre"]))
    if song.get("isrc"):
        tags.add(TSRC(encoding=3, text=song["isrc"]))
    if cover:
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    tags.save(path, v2_version=3, v1=0)
