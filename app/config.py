"""Settings, all from environment variables so nothing site-specific lives in the code."""
import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _secret(name: str) -> str | None:
    """Read NAME, or the file named by NAME_FILE (for Docker secrets)."""
    path = _env(f"{name}_FILE")
    if path:
        return Path(path).read_text().strip()
    return _env(name)


@dataclass(frozen=True)
class Config:
    data_dir: Path
    music_root: Path
    apple_dir: str
    library_dirs: tuple[Path, ...]

    spotify_dir: str
    local_dir: str
    youtube_dir: str
    default_numbering: bool
    default_library_mode: str

    apple_team_id: str | None
    apple_key_id: str | None
    apple_key_file: Path | None
    apple_mode: str                     # auto | developer | web

    spotify_client_id: str | None
    spotify_redirect_uri: str | None    # only needed when the app can't work it out from the request

    slskd_url: str
    slskd_api_key: str | None
    slskd_downloads: Path
    slskd_control: str          # none | docker | webhook
    slskd_control_url: str | None
    slskd_containers: tuple[str, ...]

    max_downloads: int
    youtube_fallback: bool
    search_delay: float
    min_bitrate: int
    length_tolerance: float
    queue_timeout: int

    app_password: str | None
    genius_token: str | None = None   # optional: Genius page views for hits playlists
    listenbrainz_token: str | None = None   # optional: ListenBrainz per-artist counts for hits playlists
    youtube_cookies: Path | None = None   # optional: cookies.txt for age-restricted YouTube videos
    profile: int = 1                      # whose Apple Music and Spotify connections to use

    def for_profile(self, profile: int) -> "Config":
        return dataclasses.replace(self, profile=int(profile))

    @property
    def profile_dir(self) -> Path:
        """Where a profile's service tokens live. The first profile keeps the files from before profiles,
        so an older version of the app still finds them."""
        return self.data_dir if self.profile == 1 else self.data_dir / "profiles" / str(self.profile)

    @property
    def apple_root(self) -> Path:
        return self.music_root / self.apple_dir

    @property
    def spotify_root(self) -> Path:
        return self.music_root / self.spotify_dir

    def source_dir(self, source: str) -> str:
        return {"apple": self.apple_dir, "spotify": self.spotify_dir, "local": self.local_dir,
                "youtube": self.youtube_dir}[source]

    @property
    def spotify_token_file(self) -> Path:
        return self.profile_dir / "spotify-token.json"

    @property
    def apple_user_token_file(self) -> Path:
        return self.profile_dir / "apple-music-user-token"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "playlist-sync.db"

    @property
    def artwork_dir(self) -> Path:
        return self.data_dir / "artwork"


def load() -> Config:
    music_root = Path(_env("MUSIC_ROOT", "/music"))
    library = _env("LIBRARY_DIRS")
    key_file = _env("APPLE_KEY_FILE")
    return Config(
        data_dir=Path(_env("DATA_DIR", "/data")),
        music_root=music_root,
        apple_dir=_env("APPLE_DIR", "AppleMusic"),
        spotify_dir=_env("SPOTIFY_DIR", "SpotifyMusic"),
        local_dir=_env("LOCAL_DIR", "Playlists"),
        youtube_dir=_env("YOUTUBE_DIR", "YouTube"),
        default_numbering=_env("DEFAULT_NUMBERING", "1") not in ("0", "false", "no"),
        default_library_mode=_env("DEFAULT_LIBRARY_MODE", "copy"),
        library_dirs=tuple(Path(p) for p in library.split(",")) if library else (music_root,),
        apple_team_id=_env("APPLE_TEAM_ID"),
        apple_key_id=_env("APPLE_KEY_ID"),
        apple_key_file=Path(key_file) if key_file else None,
        apple_mode=_env("APPLE_MODE", "auto"),
        spotify_client_id=_env("SPOTIFY_CLIENT_ID"),
        spotify_redirect_uri=_env("SPOTIFY_REDIRECT_URI"),
        slskd_url=_env("SLSKD_URL", "http://slskd:5030").rstrip("/"),
        slskd_api_key=_secret("SLSKD_API_KEY"),
        slskd_downloads=Path(_env("SLSKD_DOWNLOADS", "/slskd-downloads")),
        slskd_control=_env("SLSKD_CONTROL", "none"),
        slskd_control_url=_env("SLSKD_CONTROL_URL"),
        slskd_containers=tuple(c for c in (_env("SLSKD_CONTAINERS", "") or "").split(",") if c),
        max_downloads=int(_env("MAX_DOWNLOADS", "3")),
        youtube_fallback=_env("YOUTUBE_FALLBACK", "0") not in ("0", "false", "no"),
        search_delay=float(_env("SEARCH_DELAY", "5")),
        min_bitrate=int(_env("MIN_BITRATE", "192")),
        length_tolerance=float(_env("LENGTH_TOLERANCE", "3")),
        queue_timeout=int(_env("QUEUE_TIMEOUT", "900")),
        app_password=_secret("APP_PASSWORD"),
        genius_token=_secret("GENIUS_API_TOKEN"),
        listenbrainz_token=_secret("LISTENBRAINZ_TOKEN"),
        youtube_cookies=Path(_env("YOUTUBE_COOKIES_FILE")) if _env("YOUTUBE_COOKIES_FILE") else None,
    )
