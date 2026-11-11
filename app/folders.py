"""Playlist folder layout: safe file names, placing files, writing the .m3u."""
import os
import re
import shutil
import uuid
from pathlib import Path

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_name(name: str, max_len: int = 120) -> str:
    """A file or folder name that works on FAT32/exFAT/NTFS as well as Linux."""
    s = _BAD.sub("_", name or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = s[:max_len].rstrip(" .")
    if not s or s.lower() in _RESERVED:
        s = f"_{s}"
    return s


def short_artist(artist: str, max_len: int = 45) -> str:
    """Long credit lists are cut at a comma ('A, B, C & D' -> 'A, B'), else at a word break."""
    if len(artist) <= max_len:
        return artist
    names = [n.strip() for n in re.split(r",|\s&\s", artist) if n.strip()]
    out = names[0]
    for n in names[1:]:
        if len(out) + 2 + len(n) > max_len:
            break
        out = f"{out}, {n}"
    if len(out) > max_len:
        out = out[:max_len].rsplit(" ", 1)[0]
    return out


def song_file_name(song: dict, position: int | None, width: int) -> str:
    artist = safe_name(short_artist(song.get("artist") or "Unknown"), 60)
    title = safe_name(song.get("title") or "Untitled", 80)
    base = f"{artist} - {title}"
    if position is not None:
        base = f"{position:0{width}d} - {base}"
    return safe_name(base, 150) + ".mp3"


def place(src: Path, dst: Path, move: bool = False) -> None:
    """Hardlink (same filesystem) or copy src to dst; move instead when asked."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{uuid.uuid4().hex}.part")
    try:
        if move:
            shutil.move(src, tmp)
        else:
            try:
                os.link(src, tmp)
            except OSError:
                shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def rename_all(folder: Path, renames: dict[str, str]) -> None:
    """Rename files in two passes so swaps (001 <-> 002) don't collide."""
    temp = {}
    for old, new in renames.items():
        if old == new or not (folder / old).exists():
            continue
        t = f".{uuid.uuid4().hex}.renaming"
        os.rename(folder / old, folder / t)
        temp[t] = new
    for t, new in temp.items():
        os.rename(folder / t, folder / new)


def write_m3u(path: Path, lines: list[tuple[int, str, str]]) -> None:
    """lines: (seconds, 'Artist - Title', path relative to the .m3u)."""
    out = ["#EXTM3U"]
    for secs, label, rel in lines:
        out.append(f"#EXTINF:{secs},{label}")
        out.append(rel)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)
