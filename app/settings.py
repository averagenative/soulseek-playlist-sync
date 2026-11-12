"""Settings you can change in the web UI. Environment variables give the defaults; saved values win."""
import dataclasses
import json
import re
from pathlib import PurePosixPath

from . import db
from .config import Config

# name -> (type, label, help)
EDITABLE = {
    "apple_dir": (str, "Apple Music folder", "Under the music root. New Apple Music playlists get a folder in here."),
    "spotify_dir": (str, "Spotify folder", "Under the music root. New Spotify playlists get a folder in here."),
    "local_dir": (str, "My music folder", "Under the music root. Playlists you make on the My music page get a folder in here."),
    "default_numbering": (bool, "Number files in playlist order", "Default for new playlists."),
    "default_library_mode": (str, "Songs already in your library", "Default for new playlists."),
    "min_bitrate": (int, "Lowest MP3 bitrate (kbps)", "Downloads below this are ignored."),
    "length_tolerance": (float, "Length tolerance (seconds)", "How far a file's length may differ from the service's."),
    "max_downloads": (int, "Downloads at once", "How many songs slskd fetches at the same time."),
    "search_delay": (float, "Seconds between searches", "Soulseek bans clients that search too fast."),
    "queue_timeout": (int, "Give up on a queued download after (seconds)", "Then the next candidate is tried."),
    "youtube_fallback": (bool, "Try YouTube when Soulseek doesn't have a song",
                         "Prefers official audio uploads; the length must match. Files are tagged YouTube."),
    "spotify_client_id": (str, "Spotify Client ID", "From your app at developer.spotify.com/dashboard."),
}
LIMITS = {"min_bitrate": (64, 320), "length_tolerance": (0, 30), "max_downloads": (1, 10),
          "search_delay": (1, 120), "queue_timeout": (60, 86400)}

_base: Config | None = None


class SettingsError(ValueError):
    pass


def init(base: Config) -> None:
    global _base
    _base = base


def overrides() -> dict:
    raw = db.get_kv("settings")
    return json.loads(raw) if raw else {}


def current() -> Config:
    values = {k: v for k, v in overrides().items() if k in EDITABLE}
    return dataclasses.replace(_base, **values)


def clean_folder(value: str) -> str:
    """A relative folder under the music root: no absolute paths, no '..'."""
    v = (value or "").strip()
    absolute = v.startswith("/")
    v = v.rstrip("/")
    p = PurePosixPath(v)
    if not v or absolute or any(part in ("..", ".", "") for part in p.parts):
        raise SettingsError(f"{value!r} must be a folder name or path inside the music root, without '..'")
    return str(p)


def clean_spotify_client_id(value: str) -> str:
    """A Spotify Client ID: 32 hexadecimal characters."""
    v = (value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", v):
        raise SettingsError("That isn't a Spotify Client ID. It's 32 letters and numbers, shown under Client ID "
                            "on your Spotify app's Settings page.")
    return v


def set_value(name: str, value) -> None:
    """Save one setting, leaving the others as they are."""
    values = overrides()
    if value == getattr(_base, name):
        values.pop(name, None)
    else:
        values[name] = value
    save(values)


def parse(form: dict) -> dict:
    """Validate submitted values and return the ones that differ from the environment defaults."""
    out = {}
    for name, (kind, label, _) in EDITABLE.items():
        if kind is bool:
            value = form.get(name) in ("1", "on", "true")
        else:
            raw = (form.get(name) or "").strip()
            if raw == "":
                continue
            try:
                value = kind(raw)
            except ValueError:
                raise SettingsError(f"{label}: {raw!r} isn't a valid number")
            if name in LIMITS:
                lo, hi = LIMITS[name]
                if not lo <= value <= hi:
                    raise SettingsError(f"{label} must be between {lo} and {hi}")
            if name.endswith("_dir"):
                value = clean_folder(value)
            if name == "spotify_client_id":
                value = clean_spotify_client_id(value)
            if name == "default_library_mode" and value not in ("copy", "reference"):
                raise SettingsError("Library mode must be copy or reference")
        if value != getattr(_base, name):
            out[name] = value
    return out


def save(values: dict) -> None:
    db.set_kv("settings", json.dumps(values))
