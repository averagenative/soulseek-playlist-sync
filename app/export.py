"""Stream a playlist as a ZIP without building it on disk first."""
import zipfile
from pathlib import Path

from . import db

CHUNK = 1 << 20
ZIP_ASK = 2 * 10**9      # ask before a ZIP bigger than this
ZIP_MAX = 20 * 10**9     # refuse a ZIP bigger than this: it could fill the disk it's saved to


class _Sink:
    """A write-only, unseekable file: zipfile then writes data descriptors and never seeks back."""
    def __init__(self):
        self.buf = bytearray()
        self.pos = 0

    def write(self, b):
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self):
        return self.pos

    def flush(self):
        pass

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def stream_zip(folder: str, files: list[tuple[str, Path]], m3u: str, progress: dict | None = None):
    """Yield the ZIP in pieces. `progress` gets the bytes sent, the songs done, and how it ended."""
    prog = progress if progress is not None else {}
    prog.update(state="running", sent=0, done=0)
    sink = _Sink()
    finished = False

    def take() -> bytes:
        out = sink.take()
        prog["sent"] += len(out)
        return out

    try:
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            z.writestr(f"{folder}/{folder}.m3u", m3u)
            for name, path in files:
                info = zipfile.ZipInfo.from_file(path, f"{folder}/{name}")
                info.compress_type = zipfile.ZIP_STORED    # MP3s don't compress
                with open(path, "rb") as f, z.open(info, "w", force_zip64=True) as w:
                    while chunk := f.read(CHUNK):
                        w.write(chunk)
                        if len(sink.buf) >= CHUNK:
                            yield take()
                yield take()
                prog["done"] += 1
        yield take()
        finished = True
    finally:
        prog["state"] = "done" if finished else "stopped"


def progress_text(prog: dict | None) -> dict:
    """What the playlist page shows while a ZIP downloads: {"state", "text", "percent"}."""
    if not prog or prog.get("state") is None:
        return {"state": "waiting", "text": "Preparing the ZIP...", "percent": 0}
    sent, total, done, n = prog["sent"], prog["total"], prog["done"], prog["songs"]
    songs = f"{n:,} {'song' if n == 1 else 'songs'}"
    if prog["state"] == "done":
        return {"state": "done", "percent": 100,
                "text": f"The ZIP is downloaded: {human(sent)}, {songs}. It's with your browser's other downloads."}
    if prog["state"] == "stopped":
        return {"state": "stopped", "percent": 0,
                "text": f"The download stopped after {human(sent)} ({done:,} of {songs}). "
                        "To try again, click Download ZIP."}
    pct = min(99, int(sent * 100 / total)) if total else 0
    return {"state": "running", "percent": pct,
            "text": f"Zipping and downloading: {human(sent)} of about {human(total)} ({pct}%), {done:,} of {songs}."}


def size_estimate(files: list[tuple[str, Path]]) -> int:
    """The ZIP's size, near enough: sizes from the library index, and from disk for the rest."""
    with db.connect() as c:
        known = {r["path"]: r["size"] for r in c.execute("SELECT path, size FROM library")}
    total = 0
    for _, p in files:
        size = known.get(str(p))
        if size is None:
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
        total += size
    return total


def human(n: int) -> str:
    if n >= 10**9:
        return f"{n / 10**9:.1f}".removesuffix(".0") + " GB"
    return f"{max(1, round(n / 10**6))} MB"


def check(files: list[tuple[str, Path]]) -> dict:
    """Whether a ZIP of these files may be downloaded: {"ok", "ask", "error"}."""
    size, n = size_estimate(files), len(files)
    what = f"{human(size)} ({n:,} {'song' if n == 1 else 'songs'})"
    if size > ZIP_MAX:
        return {"ok": False, "ask": None, "size": size, "songs": n,
                "error": f"This ZIP would be {what}, more than the {human(ZIP_MAX)} limit for one download. "
                         "Copy the songs straight from the music folder on the server instead, or make a smaller playlist."}
    ask = (f"This ZIP is {what}. The computer you save it to needs that much free space. Download it?"
           if size > ZIP_ASK else None)
    return {"ok": True, "ask": ask, "error": None, "size": size, "songs": n}
