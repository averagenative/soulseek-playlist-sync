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
    """YouTube Music's song search first (it finds official "Artist - Topic" uploads that general
    search buries under unrelated videos), then up to two general YouTube searches."""
    import yt_dlp
    artist, title = primary_artist(song.get("artist")), core_title(song.get("title"))
    tol = tolerance(length_tolerance)
    try:
        found = rank(_music_search(f"{artist} {title}", title), song.get("title"), song.get("artist"),
                     song.get("duration_ms"), tol)
        if found:
            return found
        opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
        for q in (f"{artist} {title}", f"{artist} {title} song"):
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch12:{q}", download=False)
            found = rank(info.get("entries") or [], song.get("title"), song.get("artist"),
                         song.get("duration_ms"), tol)
            if found:
                return found
    except Exception as e:      # yt-dlp raises many kinds
        raise YouTubeError(f"YouTube search failed: {e}") from e
    return []


def _music_search(query: str, title: str, look_at: int = 4) -> list[dict]:
    """music.youtube.com song results, with length and channel looked up for the likely ones
    (the search page itself doesn't include them)."""
    import yt_dlp
    from urllib.parse import quote_plus
    need = set(norm(title).split())
    flat_opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": 10}
    with yt_dlp.YoutubeDL(flat_opts) as ydl:
        info = ydl.extract_info(f"https://music.youtube.com/search?q={quote_plus(query)}#songs", download=False)
    likely = [e for e in (info.get("entries") or []) if e and e.get("id")
              and need <= set(norm(e.get("title")).split())][:look_at]
    out = []
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        for e in likely:
            try:
                v = ydl.extract_info(f"https://www.youtube.com/watch?v={e['id']}", download=False)
            except Exception:
                continue
            out.append({"id": v.get("id") or e["id"], "title": v.get("title") or e.get("title"),
                        "channel": v.get("channel") or v.get("uploader") or "", "duration": v.get("duration"),
                        "url": f"https://www.youtube.com/watch?v={e['id']}"})
    return out


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


def can_read_playlists() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return bool(shutil.which("ffmpeg"))


# ------------------------------------------------------------------ playlists as a source

# Bracketed parts of a video title that describe the video, not the song: "(Official Music Video) [HD]".
_VIDEO_NOISE = re.compile(r"\s*[\(\[][^\)\]]*\b(official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|mv|m/v|"
                          r"videoclip|clip officiel|closed[- ]captioned|cc)\b[^\)\]]*[\)\]]", re.I)
# The same notes without brackets at the end: 'Artist "Song" official music video'.
_END_NOISE = re.compile(r"[\s\-:]*\b(official\s+(music\s+)?video|official\s+audio|music\s+video|lyric\s+video|lyrics|"
                        r"hd|hq|4k)\s*$", re.I)
_WEB_ADDRESS = re.compile(r"\s*\b(https?://\S+|www\.\S+|\S+\.(com|net|org|tv|co\.uk)\b\S*)", re.I)
_MUSIC_VIDEO = re.compile(r"\b(official\s+(music\s+)?video|music\s+video|official\s+4k\s+video|videoclip|"
                          r"clip officiel|\bmv\b|m/v)\b", re.I)
_PIPE = re.compile(r"\s+\|\s+.*$")      # "Song | Live at ...", "Song | Official Video": the rest describes the video
_SEPARATOR = re.compile(r"\s+[-\u2013\u2014]\s+")
_QUOTED = re.compile(r'^(.+?)\s+["\u201c](.+?)["\u201d]\s*$')
_CHANNEL_SUFFIX = re.compile(r"\s*(-\s*topic|vevo|official|music|band|tv)\s*$", re.I)
PRIVATE_LISTS = {"LL": "Liked videos", "LM": "Liked Music", "WL": "Watch later"}


def playlist_id(text: str) -> str:
    """The playlist ID in a YouTube or YouTube Music link, or a bare ID."""
    t = (text or "").strip()
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", t) or re.search(r"/browse/VL([A-Za-z0-9_-]+)", t)
    pid = m.group(1) if m else (t if re.fullmatch(r"[A-Za-z0-9_-]{12,}", t) else None)
    if not pid:
        raise YouTubeError("That isn't a link to a YouTube playlist. Open the playlist, click Share, and copy the link.")
    if pid in PRIVATE_LISTS:
        raise YouTubeError(f"{PRIVATE_LISTS[pid]} is private to your account, so the app can't read it. Save the songs "
                           "to a playlist set to Public or Unlisted, and add that instead.")
    if pid.startswith("RD") and not pid.startswith("RDCLAK"):
        raise YouTubeError("That's a YouTube Mix, which changes every time it's opened. Save the songs to a playlist "
                           "first, and add that instead.")
    return pid


def parse_video_title(title: str, channel: str | None) -> tuple[str, str, bool]:
    """(artist, song title, is a music video) from a video's title and channel.

    "Artist - Song (Official Video)" and 'Artist "Song"' give the artist from the title. YouTube Music's
    "Artist - Topic" channels give it from the channel. Otherwise the channel name is the best guess.
    """
    raw = (title or "").strip()
    music_video = bool(_MUSIC_VIDEO.search(raw)) and not re.search(r"\blyrics?\b", raw, re.I)
    live = bool(re.search(r"([\(\[][^\)\]]*|\|.*)\blive\b", raw, re.I))
    t = _WEB_ADDRESS.sub("", _PIPE.sub("", _VIDEO_NOISE.sub("", raw)))
    while (shorter := _END_NOISE.sub("", t)) != t:
        t = shorter
    t = t.strip()
    ch = (channel or "").strip()
    if ch.lower().endswith("- topic"):
        artist, song = _CHANNEL_SUFFIX.sub("", ch), t
    elif (parts := _SEPARATOR.split(t, maxsplit=1)) and len(parts) == 2:
        artist, song = parts
    elif m := _QUOTED.match(t):
        artist, song = m.group(1), m.group(2)
    else:
        artist, song = _channel_artist(ch) or "Unknown artist", t
    song = song.strip().strip('"\u201c\u201d').strip()
    if live and not re.search(r"\blive\b", song, re.I):
        song += " (Live)"
    return artist.strip(), song or raw, music_video


def _channel_artist(channel: str) -> str:
    """A best guess at the artist from a channel name: "VerticalHorizonMusic" -> "Vertical Horizon"."""
    ch = channel.strip()
    if " " not in ch and re.search(r"[a-z][A-Z]", ch):
        ch = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", ch)
    prev = None
    while prev != ch:
        prev, ch = ch, _CHANNEL_SUFFIX.sub("", ch).strip()
    return ch or channel.strip()


def video_song(entry: dict) -> dict | None:
    """A song dict, like Apple Music's and Spotify's, from a playlist entry. None for deleted or private videos."""
    vid, title = entry.get("id"), entry.get("title") or ""
    if not vid or title in ("[Deleted video]", "[Private video]"):
        return None
    artist, song, music_video = parse_video_title(title, entry.get("channel") or entry.get("uploader"))
    duration = entry.get("duration")
    return {
        "key": f"youtube:{vid}", "title": song, "artist": artist, "album": None, "album_artist": None,
        "isrc": None, "content_rating": None,
        # A music video often runs longer than the song (an intro, an ending), so its length can't be used to
        # pick the right file. Lyric videos, audio uploads and "Topic" uploads keep theirs.
        "duration_ms": int(duration * 1000) if duration and not music_video else None,
        "artwork_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "url": f"https://www.youtube.com/watch?v={vid}",
    }


class Playlists:
    """A playlist source like apple.Client and spotify.Client: public and unlisted YouTube and
    YouTube Music playlists, read with yt-dlp. No sign-in."""

    def __init__(self, cfg=None):
        self._cache: dict[str, dict] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def _info(self, remote_id: str) -> dict:
        if remote_id not in self._cache:
            import yt_dlp
            opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    self._cache[remote_id] = ydl.extract_info(
                        f"https://www.youtube.com/playlist?list={remote_id}", download=False)
            except Exception as e:      # yt-dlp raises many kinds
                msg = str(e)
                if "private" in msg.lower() or "does not exist" in msg.lower():
                    raise YouTubeError("YouTube says this playlist is private or doesn't exist. The app can read "
                                       "playlists set to Public or Unlisted.") from e
                raise YouTubeError(f"Couldn't read the playlist from YouTube: {msg[:200]}") from e
        return self._cache[remote_id]

    def playlist_name(self, remote_id: str) -> str:
        return self._info(remote_id).get("title") or remote_id

    def playlist_tracks(self, remote_id: str) -> list[dict]:
        songs = [video_song(e) for e in self._info(remote_id).get("entries") or [] if e]
        return [s for s in songs if s]


def own_video(song: dict) -> list[dict]:
    """For a song from a YouTube playlist, its own video is the download candidate: no search needed."""
    if not (song.get("key") or "").startswith("youtube:") or not song.get("url"):
        return []
    return [{"id": song["key"].split(":", 1)[1], "title": f"{song.get('artist')} - {song.get('title')}",
             "channel": "", "duration": (song.get("duration_ms") or 0) / 1000 or None, "url": song["url"]}]
