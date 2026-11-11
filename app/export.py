"""Stream a playlist as a ZIP without building it on disk first."""
import zipfile
from pathlib import Path

CHUNK = 1 << 20


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


def stream_zip(folder: str, files: list[tuple[str, Path]], m3u: str):
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        z.writestr(f"{folder}/{folder}.m3u", m3u)
        for name, path in files:
            info = zipfile.ZipInfo.from_file(path, f"{folder}/{name}")
            info.compress_type = zipfile.ZIP_STORED    # MP3s don't compress
            with open(path, "rb") as f, z.open(info, "w", force_zip64=True) as w:
                while chunk := f.read(CHUNK):
                    w.write(chunk)
                    if len(sink.buf) >= CHUNK:
                        yield sink.take()
            yield sink.take()
    yield sink.take()


def size_estimate(files: list[tuple[str, Path]]) -> int:
    return sum(p.stat().st_size for _, p in files if p.exists())
