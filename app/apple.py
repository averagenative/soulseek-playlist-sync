"""Apple Music API: developer tokens, the user's library playlists and their tracks."""
import base64
import json
import re
import time

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .config import Config

API = "https://api.music.apple.com"
WEB_API = "https://amp-api.music.apple.com"
ARTWORK_SIZE = 600
THUMB_SIZE = 120
LIBRARY_SONGS = "library-songs"      # every song in your library, offered like a playlist
LIBRARY_NAME = "Apple Music library"
# Apple's automatic playlist of your favorites. Its API entry has no type, so it's found by name:
# a library playlist you can't edit, not from the catalog, with this name.
FAVORITES_NAMES = ("favorite songs", "favourite songs")


class AppleError(Exception):
    pass


class NotConnected(AppleError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def developer_token(cfg: Config, lifetime: int = 12 * 3600) -> str:
    """Sign a short-lived ES256 developer token with the MusicKit key."""
    if not (cfg.apple_team_id and cfg.apple_key_id and cfg.apple_key_file):
        raise AppleError("APPLE_TEAM_ID, APPLE_KEY_ID and APPLE_KEY_FILE must be set")
    key = load_pem_private_key(cfg.apple_key_file.read_bytes(), None)
    now = int(time.time())
    header = {"alg": "ES256", "kid": cfg.apple_key_id}
    claims = {"iss": cfg.apple_team_id, "iat": now, "exp": now + lifetime}
    signing_input = ".".join(_b64(json.dumps(p, separators=(",", ":")).encode()) for p in (header, claims))
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    return f"{signing_input}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def key_configured(cfg: Config) -> bool:
    """A MusicKit key from an Apple Developer account is set up."""
    return bool(cfg.apple_team_id and cfg.apple_key_id and cfg.apple_key_file and cfg.apple_key_file.exists())


def mode(cfg: Config) -> str:
    """'developer' with your own MusicKit key, else 'web': the music.apple.com web player's token."""
    if cfg.apple_mode in ("developer", "web"):
        return cfg.apple_mode
    return "developer" if key_configured(cfg) else "web"


def configured(cfg: Config) -> bool:
    return mode(cfg) == "web" or key_configured(cfg)


# ------------------------------------------------------------------ web player token

WEB_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130.0 Safari/537.36")


def _claims(jwt: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
    except Exception:
        return {}


def web_token(force: bool = False) -> str:
    """The token music.apple.com's own player uses, read from its JavaScript and cached until it expires."""
    from . import db
    cached = db.get_kv("apple_web_token")
    if cached and not force and _claims(cached).get("exp", 0) > time.time() + 86400:
        return cached
    try:
        with httpx.Client(timeout=30, headers={"User-Agent": WEB_UA}, follow_redirects=True) as h:
            html = h.get("https://music.apple.com/us/browse").text
            scripts = re.findall(r'<script[^>]+src="(/assets/[^"]+\.js)"', html)
            for src in scripts:
                js = h.get("https://music.apple.com" + src).text
                for m in re.finditer(r"eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+", js):
                    if _claims(m.group(0)).get("iss") == "AMPWebPlay":
                        db.set_kv("apple_web_token", m.group(0))
                        return m.group(0)
    except httpx.HTTPError as e:
        raise AppleError(f"Couldn't reach music.apple.com for its web token: {e}") from e
    raise AppleError("Couldn't find the web player's token on music.apple.com (Apple may have changed the page)")


# ------------------------------------------------------------------ the signed-in user

def user_token(cfg: Config) -> str | None:
    f = cfg.apple_user_token_file
    return f.read_text().strip() if f.exists() else None


def save_user_token(cfg: Config, token: str) -> None:
    import os
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.apple_user_token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token.strip())


def clean_pasted_token(raw: str) -> str:
    """Accept the bare cookie value, 'media-user-token=...', or a whole Cookie header."""
    raw = (raw or "").strip().strip('"').strip("'")
    m = re.search(r"media-user-token=([^;\s]+)", raw)
    return (m.group(1) if m else raw).strip()


def check_user_token(cfg: Config, token: str) -> str:
    """Returns the account's storefront (e.g. 'us') if Apple accepts the token, else raises."""
    with Client(cfg, token=token) as api:
        return api.storefront()


class Client:
    def __init__(self, cfg: Config, token: str | None = None):
        token = token or user_token(cfg)
        if not token:
            raise NotConnected("Apple Music is not connected")
        self.mode = mode(cfg)
        self.cfg = cfg
        self._user = token
        self.http = httpx.Client(base_url=API if self.mode == "developer" else WEB_API, timeout=30)
        self._auth()
        self._storefront = None

    def _auth(self, refresh: bool = False):
        if self.mode == "developer":
            self.http.headers.update({"Authorization": f"Bearer {developer_token(self.cfg)}",
                                      "Music-User-Token": self._user})
        else:
            self.http.headers.update({"Authorization": f"Bearer {web_token(force=refresh)}",
                                      "Media-User-Token": self._user, "Origin": "https://music.apple.com",
                                      "Referer": "https://music.apple.com/", "User-Agent": WEB_UA})

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _send(self, method: str, path: str, **kw) -> httpx.Response:
        """One request, with a retry after a rotated web token and backing off when rate-limited
        or when Apple answers with a temporary server error or doesn't answer in time."""
        delay = 2.0
        for attempt in range(6):
            try:
                r = self.http.request(method, path, **kw)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt == 5:
                    raise AppleError(f"{method} {path} failed after {attempt + 1} attempts: "
                                     f"{type(e).__name__}: {e}") from e
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code in (500, 502, 503, 504):
                if attempt == 5:
                    raise AppleError(f"{method} {r.request.url} failed after {attempt + 1} attempts: "
                                     f"HTTP {r.status_code} {r.reason_phrase}: {r.text[:500]}")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 401 and self.mode == "web" and attempt == 0:
                self._auth(refresh=True)       # the web player's token may have been rotated
                continue
            if r.status_code == 429:
                wait = r.headers.get("Retry-After")
                time.sleep(min(float(wait) if wait and wait.isdigit() else delay, 60))
                delay *= 2
                continue
            return r
        raise AppleError("Apple Music kept rate-limiting; try again in a few minutes")

    def get(self, path: str, params: dict | None = None) -> dict:
        r = self._send("GET", path, params=params)
        if r.status_code in (401, 403):
            raise NotConnected(f"Apple returned {r.status_code}; reconnect Apple Music")
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict) -> dict:
        r = self._send("POST", path, json=body)
        if r.status_code in (401, 403):
            raise NotConnected(f"Apple returned {r.status_code}; reconnect Apple Music")
        if r.status_code >= 400:
            raise AppleError(f"Apple refused the change: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}

    def _change(self, method: str, path: str, **kw) -> None:
        r = self._send(method, path, **kw)
        if r.status_code in (401, 403):
            raise NotConnected(f"Apple returned {r.status_code}; reconnect Apple Music")
        if r.status_code >= 400:
            raise AppleError(f"Apple refused the change: HTTP {r.status_code} {r.text[:200]}")

    # ---------------------------------------------------------- favorites

    def favorites_playlist(self) -> str | None:
        """The library ID of Favorite Songs, or None before you favorite anything."""
        for p in self.get_all("/v1/me/library/playlists", {"limit": 100}):
            a = p.get("attributes", {})
            if (not a.get("canEdit", True) and not a.get("playParams", {}).get("globalId")
                    and (a.get("name") or "").strip().casefold() in FAVORITES_NAMES):
                return p["id"]
        return None

    def favorite_songs(self) -> list[dict]:
        """Your Favorite Songs, as song dicts like a playlist's."""
        pid = self.favorites_playlist()
        return self.playlist_tracks(pid) if pid else []

    def _library_ids(self, storefront: str, catalog_ids: list[str]) -> dict[str, str]:
        """Catalog ID -> library ID, for the songs that are in your library."""
        if not catalog_ids:
            return {}
        r = self.get(f"/v1/catalog/{storefront}/songs", {"ids": ",".join(catalog_ids), "relate": "library"})
        out = {}
        for d in r.get("data", []):
            lib = d.get("relationships", {}).get("library", {}).get("data") or []
            if lib:
                out[d["id"]] = lib[0]["id"]
        return out

    def add_favorites(self, catalog_ids: list[str], progress=None) -> list[str]:
        """Favorite these catalog songs: add the ones not in your library, then rate each library copy.
        Returns the catalog IDs that are now favorites."""
        sf, done = self.storefront(), []
        for i in range(0, len(catalog_ids), 50):
            batch = catalog_ids[i:i + 50]
            lib = self._library_ids(sf, batch)
            new = [c for c in batch if c not in lib]
            if new:
                self._change("POST", "/v1/me/library", params={"ids[songs]": ",".join(new)})
                for wait in (2, 3, 5, 8, 12):          # Apple takes a few seconds to list new library songs
                    time.sleep(wait)
                    lib.update(self._library_ids(sf, [c for c in new if c not in lib]))
                    if all(c in lib for c in new):
                        break
            for c in batch:
                if c in lib:
                    self._change("PUT", f"/v1/me/ratings/library-songs/{lib[c]}",
                                 json={"type": "rating", "attributes": {"value": 1}})
                    done.append(c)
                    time.sleep(0.1)
            if progress:
                progress(len(done))
        return done

    # ---------------------------------------------------------- writing

    def find_song(self, song: dict) -> str | None:
        """Apple catalog ID for a song from another service: by ISRC, else by title, artist and length."""
        from .match import artist_matches, core_title, search_artist, title_matches
        want_len = (song.get("duration_ms") or 0) / 1000
        sf = self.storefront()
        found = []
        if song.get("isrc"):
            found = self.get(f"/v1/catalog/{sf}/songs", {"filter[isrc]": song["isrc"]}).get("data", [])
        if not found:
            term = f"{search_artist(song.get('artist'))} {core_title(song.get('title'))}"
            res = self.get(f"/v1/catalog/{sf}/search", {"term": term, "types": "songs", "limit": 15})
            found = [s for s in res.get("results", {}).get("songs", {}).get("data", [])
                     if title_matches(song.get("title"), s["attributes"].get("name"))
                     and artist_matches(song.get("artist"), s["attributes"].get("artistName"))]
        if want_len:
            off = {s["id"]: abs((s["attributes"].get("durationInMillis") or 0) / 1000 - want_len) for s in found}
            close = [s for s in found if off[s["id"]] <= 5]
            if not close and (song.get("key") or "").startswith("local:"):
                # Your own files are often older rips: a remaster can be several seconds longer.
                close = [s for s in found if off[s["id"]] <= max(10, want_len * 0.03)]
            found = close
        if not found:
            return None
        want_explicit = song.get("content_rating") in ("explicit", "unknown")
        found.sort(key=lambda s: ((s["attributes"].get("contentRating") == "explicit") != want_explicit,
                                  abs((s["attributes"].get("durationInMillis") or 0) / 1000 - want_len)))
        return found[0]["id"]

    def search_songs(self, term: str, limit: int = 15) -> list[dict]:
        """Catalog songs for a search, for picking one by hand."""
        res = self.get(f"/v1/catalog/{self.storefront()}/search", {"term": term, "types": "songs", "limit": limit})
        out = []
        for s in res.get("results", {}).get("songs", {}).get("data", []):
            a = s.get("attributes", {})
            art = (a.get("artwork") or {}).get("url") or ""
            out.append({"id": s["id"], "title": a.get("name"), "artist": a.get("artistName"), "album": a.get("albumName"),
                        "duration_ms": a.get("durationInMillis"), "explicit": a.get("contentRating") == "explicit",
                        "art": art.replace("{w}", "120").replace("{h}", "120") or None,
                        "preview": ((a.get("previews") or [{}])[0]).get("url"), "url": a.get("url")})
        return out

    def playlist_exists(self, playlist_id: str) -> bool:
        r = self.http.get(f"/v1/me/library/playlists/{playlist_id}")
        return r.status_code == 200

    def create_playlist(self, name: str, description: str, song_ids: list[str]) -> str:
        body = {"attributes": {"name": name, "description": description},
                "relationships": {"tracks": {"data": [{"id": i, "type": "songs"} for i in song_ids[:100]]}}}
        pid = self.post("/v1/me/library/playlists", body)["data"][0]["id"]
        self.add_tracks(pid, song_ids[100:])
        return pid

    def add_tracks(self, playlist_id: str, song_ids: list[str]) -> None:
        for i in range(0, len(song_ids), 100):
            self.post(f"/v1/me/library/playlists/{playlist_id}/tracks",
                      {"data": [{"id": s, "type": "songs"} for s in song_ids[i:i + 100]]})

    def playlist_song_ids(self, playlist_id: str) -> set[str]:
        """Catalog IDs already in a library playlist."""
        tracks = self.get_all(f"/v1/me/library/playlists/{playlist_id}/tracks", {"limit": 100})
        return {t.get("attributes", {}).get("playParams", {}).get("catalogId") for t in tracks} - {None}

    def get_all(self, path: str, params: dict | None = None) -> list:
        items = []
        limit = (params or {}).get("limit")
        while path:
            page = self.get(path, params)
            items += page.get("data", [])
            path, params = page.get("next"), None
            # Keep the page size: Apple's next links don't carry it. Add it to the link itself, because
            # httpx replaces a URL's query string when given params.
            if path and limit and "limit=" not in path:
                path += f"{'&' if '?' in path else '?'}limit={limit}"
        return items

    def storefront(self) -> str:
        if not self._storefront:
            self._storefront = self.get("/v1/me/storefront")["data"][0]["id"]
        return self._storefront

    def playlists(self) -> list[dict]:
        out = []
        for p in self.get_all("/v1/me/library/playlists", {"limit": 100}):
            a = p.get("attributes", {})
            out.append({
                "remote_id": p["id"],
                "name": a.get("name", "?"),
                "can_edit": bool(a.get("canEdit")),
                "modified": a.get("lastModifiedDate") or a.get("dateAdded"),
                "description": (a.get("description") or {}).get("standard"),
                "image": ((a.get("artwork") or {}).get("url") or "").replace("{w}", "120").replace("{h}", "120") or None,
            })
        library = {"remote_id": LIBRARY_SONGS, "name": LIBRARY_NAME, "can_edit": True, "modified": None,
                   "description": None, "image": None, "note": "Every song in your Apple Music library"}
        return [library] + sorted(out, key=lambda p: p["name"].casefold())

    def playlist_name(self, remote_id: str) -> str:
        if remote_id == LIBRARY_SONGS:
            return LIBRARY_NAME
        data = self.get(f"/v1/me/library/playlists/{remote_id}")["data"]
        return data[0]["attributes"].get("name", remote_id)

    def playlist_tracks(self, remote_id: str) -> list[dict]:
        """The playlist in order, as song dicts with a stable `key`."""
        path = "/v1/me/library/songs" if remote_id == LIBRARY_SONGS else f"/v1/me/library/playlists/{remote_id}/tracks"
        lib = self.get_all(path, {"limit": 100})
        cids = [t.get("attributes", {}).get("playParams", {}).get("catalogId") for t in lib]
        catalog, albums = {}, {}
        wanted = [c for c in dict.fromkeys(cids) if c]
        for i in range(0, len(wanted), 300):
            page = self.get(f"/v1/catalog/{self.storefront()}/songs",
                            {"ids": ",".join(wanted[i:i + 300]), "include": "albums"})
            for song in page.get("data", []):
                catalog[song["id"]] = song["attributes"]
                rel = song.get("relationships", {}).get("albums", {}).get("data", [])
                if rel and rel[0].get("attributes"):
                    albums[song["id"]] = rel[0]["attributes"]

        out = []
        for t, cid in zip(lib, cids):
            a = t.get("attributes", {})
            c = catalog.get(cid, {})
            alb = albums.get(cid, {})
            art = (c.get("artwork") or a.get("artwork") or {}).get("url")
            genres = c.get("genreNames") or a.get("genreNames") or []
            out.append({
                "key": f"apple:{cid}" if cid else f"apple-lib:{t['id']}",
                "isrc": c.get("isrc"),
                "title": a.get("name") or c.get("name"),
                "artist": a.get("artistName") or c.get("artistName"),
                "album": a.get("albumName") or c.get("albumName"),
                "album_artist": alb.get("artistName"),
                "track_number": c.get("trackNumber") or a.get("trackNumber"),
                "disc_number": c.get("discNumber") or a.get("discNumber"),
                "duration_ms": c.get("durationInMillis") or a.get("durationInMillis"),
                "content_rating": c.get("contentRating") or a.get("contentRating"),
                "release_date": c.get("releaseDate") or a.get("releaseDate"),
                "genre": next((g for g in genres if g != "Music"), genres[0] if genres else None),
                "artwork_url": art.replace("{w}", str(ARTWORK_SIZE)).replace("{h}", str(ARTWORK_SIZE)) if art else None,
                "thumb_url": art.replace("{w}", str(THUMB_SIZE)).replace("{h}", str(THUMB_SIZE)) if art else None,
                "url": c.get("url"),
                "preview_url": ((c.get("previews") or [{}])[0] or {}).get("url"),
            })
        return out
