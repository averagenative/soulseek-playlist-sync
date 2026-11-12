"""Small cover images for songs in your library, from the file's embedded art or a cover file next to it."""
import logging
import os
import time
import uuid
from io import BytesIO
from pathlib import Path

import mutagen
from PIL import Image

from .config import Config

log = logging.getLogger(__name__)
SIZE = 300
COVER_FILES = ("cover", "folder", "front", "albumart", "album")
RETRY_NONE = 86400       # look again for art a day after finding none
PLACEHOLDER = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">'
               b'<text x="20" y="27" font-size="18" text-anchor="middle" fill="#8b879b">&#9835;</text></svg>')


def embedded(path: str) -> bytes | None:
    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None:
        return None
    tags = f.tags
    if tags is not None and hasattr(tags, "getall"):              # MP3 (ID3)
        pics = tags.getall("APIC")
        if pics:
            return next((p for p in pics if p.type == 3), pics[0]).data
    if getattr(f, "pictures", None):                              # FLAC
        return f.pictures[0].data
    if tags is not None and "covr" in tags:                       # M4A
        return bytes(tags["covr"][0])
    return None


def folder_cover(path: str) -> bytes | None:
    folder = Path(path).parent
    try:
        names = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
    except OSError:
        return None
    for stem in COVER_FILES:
        for ext in (".jpg", ".jpeg", ".png"):
            if stem + ext in names:
                return names[stem + ext].read_bytes()
    return None


def thumbnail(cfg: Config, key: str, path: str) -> Path | None:
    """A cached JPEG for an art key, made on first use. None when the album has no art."""
    d = cfg.artwork_dir / "local"
    out, none = d / f"{key}.jpg", d / f"{key}.none"
    if out.exists():
        return out
    if none.exists() and time.time() - none.stat().st_mtime < RETRY_NONE:
        return None
    d.mkdir(parents=True, exist_ok=True)
    data = embedded(path) or folder_cover(path)
    if not data:
        none.touch()
        return None
    tmp = d / f".{uuid.uuid4().hex}.jpg"
    try:
        im = Image.open(BytesIO(data))
        im.thumbnail((SIZE, SIZE))
        im.convert("RGB").save(tmp, "JPEG", quality=82)
        os.replace(tmp, out)
    except Exception as e:
        log.info("no usable art in %s: %s", path, e)
        none.touch()
        return None
    finally:
        tmp.unlink(missing_ok=True)
    return out
