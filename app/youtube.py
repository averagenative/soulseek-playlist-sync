"""YouTube fallback for songs Soulseek doesn't have, through yt-dlp.

Prefers YouTube Music's "Artist - Topic" uploads (the label's audio-only release), requires the
length to match the streaming service's within a few seconds, and skips covers, live takes,
sped-up edits and so on unless the wanted title has those words too.
"""
import logging
import re
import shutil
import uuid
from pathlib import Path

from .match import core_title, norm, primary_artist, search_artist

log = logging.getLogger(__name__)

BAD_WORDS = ("live", "cover", "karaoke", "instrumental", "remix", "sped up", "speed up", "slowed", "nightcore",
             "8d", "reverb", "bass boosted", "1 hour", "10 hours", "loop", "reaction", "tutorial", "lesson",
             "piano", "acoustic", "mashup", "clean version", "lyrics", "lyric", "clip", "scene", "trailer",
             "teaser", "episode", "compilation", "full movie")


class YouTubeError(Exception):
    pass


def tolerance(length_tolerance: float) -> float:
    """YouTube uploads carry a little more silence than store releases."""
    return max(length_tolerance * 2, 6)


def rank(entries: list[dict], title: str, artist: str, duration_ms: int | None, tol: float, limit: int = 3) -> list[dict]:
    """Best uploads first. If none is within `tol` seconds, a second pass allows a looser length
    (music videos often add an intro) but then insists the artist is named in the title or channel."""
    strict = _rank(entries, title, artist, duration_ms, tol, need_artist=False)
    if strict:
        return strict[:limit]
    want_len = (duration_ms or 0) / 1000
    loose = _rank(entries, title, artist, duration_ms, max(20.0, want_len * 0.15), need_artist=True)
    for c in loose:
        c["relaxed"] = True
    return loose[:limit]


def _rank(entries, title, artist, duration_ms, tol, need_artist):
    want_len = (duration_ms or 0) / 1000
    need = set(norm(core_title(title)).split())
    artist_words = set(norm(search_artist(artist)).split())
    wanted_text = norm(f"{title} {artist}")
    out = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        vtitle, channel = e.get("title") or "", e.get("channel") or e.get("uploader") or ""
        length = e.get("duration")
        if want_len and (not length or abs(length - want_len) > tol):
            continue
        words = set(norm(vtitle).split())
        if need and not need <= words:
            continue
        text = norm(vtitle)
        if any(f" {w} " in f" {text} " and f" {w} " not in f" {wanted_text} " for w in BAD_WORDS):
            continue
        named = bool(artist_words) and (artist_words <= set(norm(channel).split()) or artist_words <= words)
        if need_artist and not named:
            continue
        score = 0.0
        topic = channel.endswith(" - Topic")
        if topic:
            score += 60
        if named:
            score += 20
        if "official audio" in text:
            score += 15
        if "official video" in text or "music video" in text:
            score -= 5          # intros and skits change the audio
        score -= min(len(words - need - artist_words), 12)
        if want_len and length:
            score -= abs(length - want_len) * 2
        out.append({"id": e["id"], "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
                    "title": vtitle, "channel": channel, "length": length, "topic": topic, "score": round(score, 1),
                    "length_diff": round(length - want_len) if want_len and length else 0})
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def search(song: dict, length_tolerance: float) -> list[dict]:
    """Up to two YouTube searches; the second adds "song" because kids' and TV titles otherwise
    bring back whole episodes."""
    import yt_dlp
    artist, title = primary_artist(song.get("artist")), core_title(song.get("title"))
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
    for q in (f"{artist} {title}", f"{artist} {title} song"):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch12:{q}", download=False)
        except Exception as e:      # yt-dlp raises many kinds
            raise YouTubeError(f"YouTube search failed: {e}") from e
        found = rank(info.get("entries") or [], song.get("title"), song.get("artist"), song.get("duration_ms"),
                     tolerance(length_tolerance))
        if found:
            return found
    return []


def download(candidate: dict, work_dir: Path, progress=None) -> Path:
    """Download the audio and convert it to MP3 (VBR ~245 kbps). Returns the MP3's path."""
    import yt_dlp
    if not shutil.which("ffmpeg"):
        raise YouTubeError("ffmpeg is not installed")
    out = work_dir / uuid.uuid4().hex
    out.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            progress(100 * done / total if total else 0, d.get("speed") or 0, done, total)

    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True,
        "format": "bestaudio/best",
        "outtmpl": str(out / "audio.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}],
        "progress_hooks": [hook],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([candidate["url"]])
    except Exception as e:
        shutil.rmtree(out, ignore_errors=True)
        raise YouTubeError(f"YouTube download failed: {e}") from e
    mp3 = next(out.glob("*.mp3"), None)
    if not mp3:
        shutil.rmtree(out, ignore_errors=True)
        raise YouTubeError("YouTube download produced no MP3")
    return mp3


def available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return bool(shutil.which("ffmpeg"))
