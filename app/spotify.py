"""Spotify Web API: PKCE sign-in, the user's playlists, Liked Songs.

Written against the February 2026 Development Mode rules: playlist tracks come from
/playlists/{id}/items (owned or collaborative playlists only), batch lookups are gone.
"""
import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

from . import db
from .config import Config

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
SCOPES = ("playlist-read-private playlist-read-collaborative user-library-read "
          "playlist-modify-private playlist-modify-public")
WRITE_SCOPE = "playlist-modify-private"
LIKED = "liked"


class SpotifyError(Exception):
    pass


class NotConnected(SpotifyError):
    pass


class NotFound(SpotifyError):
    pass


def configured(cfg: Config) -> bool:
    return bool(cfg.spotify_client_id)


def connected(cfg: Config) -> bool:
    return cfg.spotify_token_file.exists()


def can_write(cfg: Config) -> bool:
    """Signed in with permission to create playlists (sign-ins before that was added lack it)."""
    tok = _load_token(cfg) or {}
    return WRITE_SCOPE in (tok.get("scope") or "").split()


def disconnect(cfg: Config) -> None:
    cfg.spotify_token_file.unlink(missing_ok=True)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def auth_url(cfg: Config, redirect_uri: str) -> str:
    verifier = _b64(secrets.token_bytes(64))
    state = secrets.token_urlsafe(24)
    db.set_kv("spotify_pkce", json.dumps({"verifier": verifier, "state": state, "redirect_uri": redirect_uri}))
    return f"{ACCOUNTS}/authorize?" + urlencode({
        "client_id": cfg.spotify_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
        "scope": SCOPES,
        "state": state,
    })


def finish_auth(cfg: Config, code: str, state: str) -> None:
    pending = json.loads(db.get_kv("spotify_pkce") or "{}")
    if not pending or not secrets.compare_digest(state or "", pending.get("state", "")):
        raise SpotifyError("Sign-in state didn't match; start again")
    db.set_kv("spotify_pkce", None)
    _token_request(cfg, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "client_id": cfg.spotify_client_id,
        "code_verifier": pending["verifier"],
    })


def _token_request(cfg: Config, form: dict) -> dict:
    r = httpx.post(f"{ACCOUNTS}/api/token", data=form, timeout=20)
    if r.status_code >= 400:
        raise SpotifyError(f"Spotify token request failed: HTTP {r.status_code} {r.text[:200]}")
    tok = r.json()
    old = _load_token(cfg) or {}
    tok.setdefault("refresh_token", old.get("refresh_token"))   # refreshes may omit it
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.spotify_token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)
    return tok


def _load_token(cfg: Config) -> dict | None:
    f = cfg.spotify_token_file
    return json.loads(f.read_text()) if f.exists() else None


class Client:
    def __init__(self, cfg: Config):
        if not configured(cfg):
            raise NotConnected("Set the Spotify Client ID in Settings")
        self.cfg = cfg
        self.tok = _load_token(cfg)
        if not self.tok:
            raise NotConnected("Spotify is not connected")
        self.http = httpx.Client(base_url=API, timeout=30)
        self._me = None

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _access(self) -> str:
        if time.time() >= self.tok.get("expires_at", 0):
            if not self.tok.get("refresh_token"):
                raise NotConnected("Spotify sign-in expired; reconnect")
            self.tok = _token_request(self.cfg, {"grant_type": "refresh_token", "client_id": self.cfg.spotify_client_id,
                                                 "refresh_token": self.tok["refresh_token"]})
        return self.tok["access_token"]

    def get(self, url: str, params: dict | None = None) -> dict:
        return self.request("GET", url, params=params)

    def request(self, method: str, url: str, **kw) -> dict:
        for attempt in range(5):
            r = self.http.request(method, url, headers={"Authorization": f"Bearer {self._access()}"}, **kw)
            if r.status_code == 401 and attempt == 0:
                self.tok["expires_at"] = 0       # force a refresh, then retry once
                continue
            if r.status_code == 429:
                time.sleep(min(int(r.headers.get("Retry-After", "5")), 60))
                continue
            if r.status_code == 401:
                raise NotConnected("Spotify rejected the sign-in; reconnect")
            if r.status_code == 403:
                raise SpotifyError(f"Spotify refused access (403): {r.text[:200]}")
            if r.status_code == 404:
                raise NotFound(f"Spotify: not found ({url})")
            r.raise_for_status()
            return r.json() if r.content else {}
        raise SpotifyError("Spotify kept rate-limiting; try again later")

    # ---------------------------------------------------------- writing

    def find_track(self, song: dict) -> str | None:
        """Spotify track URI for a song from another service: by ISRC, else by title, artist and length."""
        from .match import artist_matches, core_title, norm, norm_title, search_artist
        want_len = (song.get("duration_ms") or 0) / 1000
        candidates = []
        if song.get("isrc"):
            candidates = self.get("/search", {"q": f"isrc:{song['isrc']}", "type": "track", "limit": 10}
                                  ).get("tracks", {}).get("items", [])
        if not candidates:
            q = f'track:"{core_title(song.get("title"))}" artist:"{search_artist(song.get("artist"))}"'
            found = self.get("/search", {"q": q, "type": "track", "limit": 10}).get("tracks", {}).get("items", [])
            candidates = [t for t in found if t and norm_title(t.get("name")) == norm_title(song.get("title"))
                          and artist_matches(song.get("artist"), ", ".join(a["name"] for a in t.get("artists", [])))]
        candidates = [t for t in candidates if t and t.get("uri")]
        if want_len:
            candidates = [t for t in candidates if abs((t.get("duration_ms") or 0) / 1000 - want_len) <= 5]
        if not candidates:
            return None
        # Same explicitness as the source when there's a choice.
        want_explicit = song.get("content_rating") == "explicit"
        candidates.sort(key=lambda t: (bool(t.get("explicit")) != want_explicit,
                                       abs((t.get("duration_ms") or 0) / 1000 - want_len)))
        return candidates[0]["uri"]

    def playlist_exists(self, playlist_id: str) -> bool:
        try:
            self.get(f"/playlists/{playlist_id}", {"fields": "id"})
            return True
        except NotFound:
            return False

    def create_playlist(self, name: str, description: str) -> str:
        p = self.request("POST", "/me/playlists", json={"name": name, "description": description, "public": False})
        return p["id"]

    def set_tracks(self, playlist_id: str, uris: list[str]) -> None:
        """Make the playlist exactly this list, in this order."""
        self.request("PUT", f"/playlists/{playlist_id}/items", json={"uris": uris[:100]})
        for i in range(100, len(uris), 100):
            self.request("POST", f"/playlists/{playlist_id}/items", json={"uris": uris[i:i + 100]})

    def get_all(self, url: str, params: dict | None = None) -> list:
        items = []
        while url:
            page = self.get(url, params)
            items += page.get("items", [])
            url, params = page.get("next"), None
        return items

    def me(self) -> dict:
        if not self._me:
            self._me = self.get("/me")
        return self._me

    def playlists(self) -> list[dict]:
        my_id = self.me()["id"]
        out = [{"remote_id": LIKED, "name": "Liked Songs", "readable": True, "note": "Your saved songs"}]
        for p in self.get_all("/me/playlists", {"limit": 50}):
            if not p:
                continue
            owner = (p.get("owner") or {}).get("id")
            readable = owner == my_id or bool(p.get("collaborative"))
            total = (p.get("items") or p.get("tracks") or {}).get("total")
            note = f"{total} songs" if total is not None else ""
            if not readable:
                note = f"By {(p.get('owner') or {}).get('display_name') or owner}. Spotify only lets apps read playlists you own or collaborate on."
            images = p.get("images") or []
            image = next((i["url"] for i in reversed(images) if (i.get("width") or 0) >= 100),
                         images[0]["url"] if images else None)
            out.append({"remote_id": p["id"], "name": p.get("name") or "?", "readable": readable, "note": note,
                        "image": image})
        return [out[0]] + sorted(out[1:], key=lambda p: p["name"].casefold())

    def playlist_name(self, remote_id: str) -> str:
        if remote_id == LIKED:
            return "Liked Songs"
        return self.get(f"/playlists/{remote_id}", {"fields": "name"}).get("name") or remote_id

    def playlist_tracks(self, remote_id: str) -> list[dict]:
        if remote_id == LIKED:
            raw = [i.get("track") for i in self.get_all("/me/tracks", {"limit": 50})]
        else:
            raw = [i.get("item") or i.get("track") for i in self.get_all(f"/playlists/{remote_id}/items", {"limit": 50})]
        return [song(t) for t in raw if t and t.get("type", "track") == "track" and t.get("id") and not t.get("is_local")]


def song(t: dict) -> dict:
    album = t.get("album") or {}
    images = album.get("images") or []
    artists = [a.get("name") for a in t.get("artists") or [] if a.get("name")]
    album_artists = [a.get("name") for a in album.get("artists") or [] if a.get("name")]
    return {
        "key": f"spotify:{t['id']}",
        "isrc": (t.get("external_ids") or {}).get("isrc"),
        "title": t.get("name"),
        "artist": ", ".join(artists),
        "album": album.get("name"),
        "album_artist": ", ".join(album_artists) or None,
        "track_number": t.get("track_number"),
        "disc_number": t.get("disc_number"),
        "duration_ms": t.get("duration_ms"),
        "content_rating": "explicit" if t.get("explicit") else None,
        "release_date": album.get("release_date"),
        "genre": None,
        "artwork_url": images[0]["url"] if images else None,
        # images are widest first (640, 300, 64): the smallest that still looks sharp at 2x
        "thumb_url": next((i["url"] for i in reversed(images) if (i.get("width") or 0) >= 100),
                          images[0]["url"] if images else None),
        "url": (t.get("external_urls") or {}).get("spotify"),
    }
