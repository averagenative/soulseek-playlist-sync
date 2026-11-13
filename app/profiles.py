"""The people who use the app.

Each profile has its own playlists and its own Apple Music and Spotify connections; the music library,
downloads, and Soulseek are shared. A password is optional: a profile without one opens with a click.
The browser remembers its profile in a signed cookie. While the only profile has no password, the app
opens it without asking, so a one-person install works as it did before profiles.
"""
import hashlib
import hmac
import os
import secrets
import shutil
import time
from pathlib import Path

from . import db
from .config import Config
from .folders import safe_name

COOKIE = "profile"
COOKIE_AGE = 365 * 86400
SCRYPT_N, SCRYPT_R, SCRYPT_P = 16384, 8, 1
NAME_MAX = 40
_key: bytes | None = None


class ProfileError(ValueError):
    pass


# ------------------------------------------------------------------ passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${h.hex()}"


def check_password(stored: str | None, password: str) -> bool:
    if not stored:
        return True
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), want)


# ------------------------------------------------------------------ profiles

def all_profiles() -> list[dict]:
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM profiles ORDER BY id")]


def get(pid) -> dict | None:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    with db.connect() as c:
        r = c.execute("SELECT * FROM profiles WHERE id = ?", (pid,)).fetchone()
    return dict(r) if r else None


def name_of(pid) -> str:
    p = get(pid)
    return p["name"] if p else "someone else"


def auto() -> dict | None:
    """The profile to open without asking: the only one, when it has no password."""
    ps = all_profiles()
    return ps[0] if len(ps) == 1 and not ps[0]["password"] else None


def clean_name(name: str, exclude: int | None = None) -> str:
    n = " ".join((name or "").split())
    if not n:
        raise ProfileError("Give the profile a name")
    if len(n) > NAME_MAX:
        raise ProfileError(f"Keep the name to {NAME_MAX} characters")
    with db.connect() as c:
        r = c.execute("SELECT id FROM profiles WHERE name = ? COLLATE NOCASE", (n,)).fetchone()
    if r and r["id"] != exclude:
        raise ProfileError(f"There's already a profile called {n}")
    return n


def clean_folder(folder: str) -> str:
    """A profile's folder: one plain folder name, or empty for none."""
    f = (folder or "").strip().strip("/")
    if not f:
        return ""
    if "/" in f or f in (".", ".."):
        raise ProfileError("The folder is one folder name, without slashes")
    return safe_name(f)


def add(name: str, password: str = "", admin: bool = False) -> int:
    name = clean_name(name)
    with db.connect() as c:
        cur = c.execute("INSERT INTO profiles (name, password, admin, folder, created_at) VALUES (?, ?, ?, ?, ?)",
                        (name, hash_password(password) if password else None, 1 if admin else 0, safe_name(name),
                         time.time()))
        return cur.lastrowid


def update(pid: int, name: str | None = None, folder: str | None = None) -> None:
    with db.connect() as c:
        if name is not None:
            c.execute("UPDATE profiles SET name = ? WHERE id = ?", (clean_name(name, exclude=pid), pid))
        if folder is not None:
            c.execute("UPDATE profiles SET folder = ? WHERE id = ?", (clean_folder(folder), pid))


def set_password(pid: int, password: str | None) -> None:
    """Set or clear (None or empty) a password. Every browser signed in to the profile is signed out."""
    with db.connect() as c:
        c.execute("UPDATE profiles SET password = ?, session_version = session_version + 1 WHERE id = ?",
                  (hash_password(password) if password else None, pid))


def _admins(c) -> set[int]:
    return {r[0] for r in c.execute("SELECT id FROM profiles WHERE admin = 1")}


def set_admin(pid: int, on: bool) -> None:
    with db.connect() as c:
        if not on and _admins(c) == {pid}:
            raise ProfileError("The app needs at least one admin")
        c.execute("UPDATE profiles SET admin = ? WHERE id = ?", (1 if on else 0, pid))


def remove(cfg: Config, pid: int) -> None:
    """Remove a profile: its playlists leave the app (their files stay on disk), and its service tokens and
    favorites history are deleted."""
    from . import favorites, sync        # they import this module
    with db.connect() as c:
        if pid in _admins(c) and _admins(c) == {pid}:
            raise ProfileError("The app needs at least one admin")
        if c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] <= 1:
            raise ProfileError("The app needs at least one profile")
        owned = [r[0] for r in c.execute("SELECT id FROM playlists WHERE owner = ?", (pid,))]
    for playlist_id in owned:
        sync.remove_playlist(playlist_id)
    pcfg = cfg.for_profile(pid)
    for f in (pcfg.spotify_token_file, pcfg.apple_user_token_file):
        f.unlink(missing_ok=True)
    if pcfg.profile_dir != cfg.data_dir:
        shutil.rmtree(pcfg.profile_dir, ignore_errors=True)
    with db.connect() as c:
        c.execute("DELETE FROM favorite_pairs WHERE profile = ?", (pid,))
        c.execute("DELETE FROM hit_favorites WHERE profile = ?", (pid,))
        for key in favorites.kv_keys(pid) + [f"spotify_pkce:{pid}", f"apple_connect_nonce:{pid}"]:
            c.execute("DELETE FROM kv WHERE key = ?", (key,))
        c.execute("DELETE FROM profiles WHERE id = ?", (pid,))


# ------------------------------------------------------------------ the cookie

def load_key(data_dir: Path) -> bytes:
    """The key that signs profile cookies, made on first start."""
    global _key
    path = data_dir / "session-key"
    if not path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))
        except FileExistsError:
            pass                         # another process made it first
    _key = path.read_bytes()
    return _key


def _sig(body: str) -> str:
    if _key is None:
        raise RuntimeError("profiles.load_key() first")
    return hmac.new(_key, body.encode(), hashlib.sha256).hexdigest()


def sign(profile: dict) -> str:
    body = f"{profile['id']}.{profile['session_version']}.{int(time.time())}"
    return f"{body}.{_sig(body)}"


def verify(value: str | None) -> dict | None:
    """The profile a cookie names, if its signature is right and the profile hasn't signed out since."""
    if not value or value.count(".") != 3:
        return None
    body, _, sig = value.rpartition(".")
    if not hmac.compare_digest(_sig(body), sig):
        return None
    pid, version, issued = body.split(".")
    if not issued.isdigit() or time.time() - int(issued) > COOKIE_AGE:
        return None
    p = get(pid)
    if not p or str(p["session_version"]) != version:
        return None
    return p
