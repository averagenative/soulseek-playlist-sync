"""Web UI."""
import base64
import logging
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from urllib.parse import quote, urlencode, urlsplit
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import httpx

from . import apple, art, control, db, export, library, mirror, settings, spotify, sync
from .config import Config, load
from .slskd import Slskd
from .worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)
HERE = Path(__file__).parent
ROW_LIMIT = 500          # songs shown on a playlist page before "Show all"
PAGE_SIZE = 100          # songs per page on My music
AUDIO_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
               ".ogg": "audio/ogg", ".opus": "audio/ogg", ".wma": "audio/x-ms-wma"}
base_cfg = load()
worker: Worker | None = None


def C() -> Config:
    """Current settings: environment defaults plus anything saved on the Settings page."""
    return settings.current()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker
    db.init(base_cfg.db_path)
    settings.init(base_cfg)
    worker = Worker()
    if os.environ.get("WORKER", "1") != "0":
        worker.start()
    else:   # WORKER=0: only the web pages (development, screenshots)
        worker.status = Slskd(C()).status()
    threading.Thread(target=library.scan, args=(C(),), daemon=True, name="library-scan").start()
    yield
    worker.stop.set()


app = FastAPI(lifespan=lifespan, title="Playlist Sync")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def _ago(ts) -> str:
    if not ts:
        return "never"
    secs = int(datetime.now().timestamp() - float(ts))
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= n:
            v = secs // n
            return f"{v} {unit}{'s' if v != 1 else ''} ago"
    return "just now"


templates.env.filters["ago"] = _ago
templates.env.filters["n"] = lambda v: f"{v or 0:,}"


def _length(secs) -> str:
    if not secs:
        return ""
    secs = int(round(float(secs)))
    return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}" if secs >= 3600 else f"{secs // 60}:{secs % 60:02d}"


templates.env.filters["duration"] = _length
STATUS_LABELS = {
    "synced": "Synced", "in_library": "In library", "found": "Found", "queued": "Queued",
    "downloading": "Downloading", "searching": "Searching", "waiting": "Waiting", "not_found": "Not found",
    "failed": "Failed", "removed": "Removed upstream", "skipped": "Skipped", "missing": "Not local",
}
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["SOURCES"] = sync.SOURCES
templates.env.globals["DOWNLOAD_LABELS"] = {
    "downloading": "Downloading", "queued": "Queued", "searching": "Searching", "pending": "Waiting to search",
    "found": "Ready, waiting for a slot", "found_only": "Found (not requested)", "done": "Done", "not_found": "Not found",
    "failed": "Failed",
}


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if base_cfg.app_password and request.url.path != "/healthz":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, base_cfg.app_password)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="playlist-sync"'})
    return await call_next(request)


def _back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _common() -> dict:
    cfg = C()
    return {
        "apple_configured": apple.configured(cfg),
        "apple_mode": apple.mode(cfg),
        "apple_connected": bool(apple.user_token(cfg)),
        "spotify_configured": spotify.configured(cfg),
        "spotify_connected": spotify.connected(cfg),
        "slskd": worker.status if worker else {},
        "can_turn_on": control.available(cfg),
        "library": library.stats(),
        "downloads_active": _active_downloads(),
    }


def _active_downloads() -> int:
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) FROM downloads WHERE state IN ('queued', 'downloading')").fetchone()[0]


def _spotify_redirect(request: Request) -> str:
    cfg = C()
    if cfg.spotify_redirect_uri:
        return cfg.spotify_redirect_uri
    url = request.url_for("spotify_callback")
    # Spotify refuses "localhost" but accepts the loopback address, which reaches the same app.
    if url.hostname == "localhost":
        url = url.replace(hostname="127.0.0.1")
    return str(url)


def _spotify_accepts(uri: str) -> bool:
    """Spotify takes HTTPS redirect addresses, and plain HTTP only on a loopback address."""
    u = urlsplit(uri)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "::1"))


# ------------------------------------------------------------------ pages

@app.get("/healthz")
def healthz():
    return {"ok": True}


# Browsers and iOS ask for these at the site root, whatever the page's <link> tags say.
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(HERE / "static" / "favicon.ico", media_type="image/x-icon")


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
def apple_touch_icon():
    return FileResponse(HERE / "static" / "icons" / "apple-touch-icon.png", media_type="image/png")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {**_common(), "playlists": _home_playlists()})


@app.get("/partials/nav", response_class=HTMLResponse)
def partial_nav(request: Request):
    return templates.TemplateResponse(request, "_nav.html", _common())


@app.get("/partials/home", response_class=HTMLResponse)
def partial_home(request: Request):
    return templates.TemplateResponse(request, "_home.html", {**_common(), "playlists": _home_playlists()})


def _home_playlists() -> list[dict]:
    out = []
    for p in sync.tracked():
        _, counts = sync.summary(C(), p["id"])
        out.append({**p, "synced_at": _last_synced(p), "counts": counts, "covers": sync.covers(p["id"])})
    return out


def _last_synced(p: dict) -> float | None:
    """Copying to another service counts as a sync too, so use whichever happened last."""
    times = [p["synced_at"]] + [(mirror.get(p["id"], t) or {}).get("updated_at") for t in mirror.targets_for(p["source"])]
    return max((t for t in times if t), default=None)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, error: str | None = None):
    return templates.TemplateResponse(request, "settings.html", {
        **_common(), "cfg": C(), "base": base_cfg, "fields": settings.EDITABLE, "overridden": settings.overrides(),
        "saved": saved, "error": error,
    })


@app.post("/settings")
async def settings_save(request: Request):
    form = dict(await request.form())
    try:
        settings.save(settings.parse(form))
    except settings.SettingsError as e:
        return _back(f"/settings?error={e}")
    return _back("/settings?saved=1")


# ------------------------------------------------------------------ Apple Music

@app.get("/apple/connect", response_class=HTMLResponse)
def apple_connect(request: Request, error: str | None = None):
    cfg = C()
    if apple.mode(cfg) == "web":
        return templates.TemplateResponse(request, "connect_web.html", {**_common(), "error": error})
    if not apple.key_configured(cfg):
        raise HTTPException(500, "APPLE_MODE=developer needs APPLE_TEAM_ID, APPLE_KEY_ID and APPLE_KEY_FILE")
    nonce = secrets.token_urlsafe(24)
    db.set_kv("apple_connect_nonce", nonce)
    return templates.TemplateResponse(request, "connect.html",
                                      {"developer_token": apple.developer_token(cfg), "nonce": nonce})


@app.post("/apple/paste")
def apple_paste(token: str = Form(...)):
    cfg = C()
    value = apple.clean_pasted_token(token)
    if len(value) < 40:
        return _back("/apple/connect?error=That doesn't look like a media-user-token value. Copy the whole value.")
    try:
        apple.check_user_token(cfg, value)
    except apple.NotConnected:
        return _back("/apple/connect?error=Apple didn't accept that token. Sign in at music.apple.com again and copy a fresh one.")
    except (apple.AppleError, httpx.HTTPError) as e:
        return _back(f"/apple/connect?error=Couldn't check the token with Apple: {e}")
    apple.save_user_token(cfg, value)
    return _back("/apple/playlists")


@app.post("/apple/token")
async def apple_token(request: Request):
    body = await request.json()
    nonce = db.get_kv("apple_connect_nonce")
    if not nonce or not secrets.compare_digest(str(body.get("nonce", "")), nonce) or not body.get("token"):
        raise HTTPException(403)
    apple.save_user_token(C(), body["token"])
    db.set_kv("apple_connect_nonce", None)
    return JSONResponse({"ok": True})


@app.get("/apple/playlists", response_class=HTMLResponse)
def apple_playlists(request: Request):
    try:
        with apple.Client(C()) as api:
            remote = [{**p, "readable": True, "note": p.get("note") or ("" if p["can_edit"] else "read-only")}
                      for p in api.playlists()]
    except apple.NotConnected:
        return _back("/apple/connect")
    return _pick(request, "apple", remote)


# ------------------------------------------------------------------ Spotify

def _setup_error(message: str) -> RedirectResponse:
    return _back(f"/spotify/setup?error={quote(message)}")


@app.get("/spotify/setup", response_class=HTMLResponse)
def spotify_setup(request: Request, error: str | None = None):
    cfg = C()
    redirect = _spotify_redirect(request)
    return templates.TemplateResponse(request, "spotify_setup.html", {
        **_common(), "client_id": cfg.spotify_client_id or "", "redirect": redirect,
        "redirect_ok": _spotify_accepts(redirect),
        "loopback": str(request.url.replace(hostname="127.0.0.1", query="")),
        "can_write": spotify.connected(cfg) and spotify.can_write(cfg), "error": error,
    })


@app.post("/spotify/setup")
def spotify_setup_save(client_id: str = Form("")):
    try:
        settings.set_value("spotify_client_id", settings.clean_spotify_client_id(client_id))
    except settings.SettingsError as e:
        return _setup_error(str(e))
    return _back("/spotify/connect")


@app.get("/spotify/connect")
def spotify_connect(request: Request):
    if not spotify.configured(C()):
        return _back("/spotify/setup")
    return RedirectResponse(spotify.auth_url(C(), _spotify_redirect(request)), status_code=303)


@app.get("/spotify/callback", name="spotify_callback")
def spotify_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error == "access_denied":
        return _setup_error("You didn't approve on Spotify's page, so Spotify isn't connected. Click Save and connect to try again.")
    if error or not code:
        return _setup_error(f"Spotify sign-in failed: {error or 'Spotify sent no sign-in code'}")
    try:
        spotify.finish_auth(C(), code, state)
    except spotify.SpotifyError as e:
        return _setup_error(str(e))
    return _back("/spotify/playlists")


@app.post("/spotify/disconnect")
def spotify_disconnect(back: str | None = None):
    spotify.disconnect(C())
    return _back("/spotify/setup" if back == "/spotify/setup" else "/settings")


@app.get("/spotify/playlists", response_class=HTMLResponse)
def spotify_playlists(request: Request):
    try:
        with spotify.Client(C()) as api:
            remote = api.playlists()
    except spotify.NotConnected:
        return _back("/spotify/setup")
    except spotify.SpotifyError as e:
        return _setup_error(str(e))
    return _pick(request, "spotify", remote)


def _pick(request: Request, source: str, remote: list[dict]):
    tracked = {p["remote_id"]: p["id"] for p in sync.tracked() if p["source"] == source}
    return templates.TemplateResponse(request, "pick_playlists.html",
                                      {**_common(), "source": source, "remote": remote, "tracked": tracked})


# ------------------------------------------------------------------ playlists

@app.post("/playlists")
def add_playlist(source: str = Form(...), remote_id: str = Form(...), name: str = Form(...)):
    if source not in ("apple", "spotify"):
        raise HTTPException(400)
    pid = sync.add_playlist(C(), source, remote_id, name)
    sync.run_job(pid, "Checking", sync.preview, C(), pid)
    return _back(f"/playlists/{pid}")


@app.get("/playlists/{pid}", response_class=HTMLResponse)
def playlist_page(request: Request, pid: str, error: str | None = None, all: int = 0):
    if not sync.playlist(pid):
        raise HTTPException(404)
    return templates.TemplateResponse(request, "playlist.html",
                                      {**_common(), **_playlist_ctx(pid, bool(all)), "form_error": error})


@app.get("/playlists/{pid}/rows", response_class=HTMLResponse)
def playlist_rows(request: Request, pid: str, all: int = 0):
    if not sync.playlist(pid):
        raise HTTPException(404)
    return templates.TemplateResponse(request, "_songs.html", {**_common(), **_playlist_ctx(pid, bool(all))})


def _copy_ready(cfg: Config, target: str) -> tuple[bool, str | None]:
    if target == "spotify":
        if not spotify.connected(cfg):
            return False, "Connect Spotify first"
        if not spotify.can_write(cfg):
            return False, "Reconnect Spotify to allow creating playlists"
        return True, None
    return (True, None) if apple.user_token(cfg) else (False, "Connect Apple Music first")


def _playlist_ctx(pid: str, show_all: bool = False) -> dict:
    cfg = C()
    p = sync.playlist(pid)
    rows, counts = sync.summary(cfg, pid)
    active = bool(p["busy"]) or any(counts.get(s) for s in ("waiting", "searching", "found", "queued", "downloading"))
    copies = []
    for target in mirror.targets_for(p["source"]):
        ok, why = _copy_ready(cfg, target)
        copies.append({"target": target, "label": mirror.TARGETS[target], "mirror": mirror.get(pid, target),
                       "can_copy": ok, "blocker": why})
    rule = sync.rule_of(p)
    return {"p": {**p, "synced_at": _last_synced(p)}, "rows": rows if show_all else rows[:ROW_LIMIT], "total_rows": len(rows), "show_all": show_all,
            "counts": counts, "active": active, "music_root": str(cfg.music_root), "covers": sync.covers(pid),
            "copies": copies, "size": sum(n for s, n in counts.items() if s != "removed"),
            "spotify_max": mirror.SPOTIFY_MAX, "local": p["source"] == "local", "rule": rule,
            "rule_text": _rule_text(rule) if rule is not None else None,
            "rule_qs": _filter_qs(rule) if rule is not None else None,
            "todo": sum(counts.get(s, 0) for s in ("missing", "in_library", "found", "not_found", "failed", "waiting")),
            "folder": str(sync.folder_of(cfg, p)), "m3u": sync.m3u_path(cfg, p).name}


@app.post("/playlists/{pid}/settings")
def playlist_settings(pid: str, folder: str = Form(...), numbering: str | None = Form(None),
                      library_mode: str = Form("copy"), name: str | None = Form(None)):
    p = sync.playlist(pid)
    if not p:
        raise HTTPException(404)
    if library_mode not in ("copy", "reference"):
        raise HTTPException(400)
    try:
        folder = settings.clean_folder(folder)
        if folder != p["folder"]:
            if p["busy"]:
                raise ValueError("Wait for the current job to finish before moving the folder")
            sync.move_playlist_folder(C(), pid, folder)
        name = (name or "").strip()
        if p["source"] == "local" and name and name != p["name"]:
            sync.rename_playlist(C(), pid, name)     # its own name, since there's no service to take it from
    except ValueError as e:
        return _back(f"/playlists/{pid}?error={e}")
    with db.connect() as c:
        c.execute("UPDATE playlists SET numbering = ?, library_mode = ? WHERE id = ?",
                  (1 if numbering else 0, library_mode, pid))
    sync.run_job(pid, "Syncing", sync.reconcile, C(), pid, True)
    return _back(f"/playlists/{pid}")


@app.post("/playlists/{pid}/{action}")
def playlist_action(pid: str, action: str, back: str | None = None, target: str | None = None):
    p = sync.playlist(pid)
    if not p:
        raise HTTPException(404)
    jobs = {"preview": ("Checking", sync.preview), "sync": ("Downloading all", sync.sync),
            "prune": ("Pruning", sync.prune)}
    if p["source"] == "local" and action in ("preview", "sync", "refresh"):
        sync.run_job(pid, "Refreshing", sync.refresh_local, C(), pid)
    elif action == "mirror":
        target = target or mirror.target_for(p["source"])
        if target not in mirror.targets_for(p["source"]):
            raise HTTPException(400)
        sync.run_job(pid, f"Copying to {mirror.TARGETS[target]}", mirror.run, C(), pid, target)
    elif action in jobs:
        label, fn = jobs[action]
        sync.run_job(pid, label, fn, C(), pid)
    elif action == "forget":
        sync.remove_playlist(pid)
        return _back("/")
    else:
        raise HTTPException(404)
    return _back("/" if back == "/" else f"/playlists/{pid}")


@app.get("/playlists/{pid}/songs/{key}/audio")
def song_audio(pid: str, key: str):
    path = sync.song_path(C(), pid, key)
    if not path:
        raise HTTPException(404, "No local file for this song")
    return FileResponse(path, media_type=AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"))


@app.get("/playlists/{pid}/zip")
def playlist_zip(pid: str):
    if not sync.playlist(pid):
        raise HTTPException(404)
    folder, files, m3u = sync.export_files(C(), pid)
    if not files:
        raise HTTPException(404, "Nothing downloaded in this playlist yet")
    name = quote(f"{folder}.zip")
    return StreamingResponse(export.stream_zip(folder, files, m3u), media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


@app.post("/playlists/{pid}/songs/{key}/{action}")
def song_action(pid: str, key: str, action: str):
    if action == "skip":
        sync.set_song(pid, key, skip=1, want=0)
    elif action == "unskip":
        sync.set_song(pid, key, skip=0)
    elif action == "remove":
        p = sync.playlist(pid)
        if not p or p["source"] != "local" or p["rule"]:
            raise HTTPException(400, "Only songs in a playlist you made by hand can be removed here")
        sync.remove_songs(C(), pid, [key])
        return _back(f"/playlists/{pid}")
    else:
        raise HTTPException(404)
    return _back(f"/playlists/{pid}#song-{key}")


@app.get("/downloads", response_class=HTMLResponse)
def downloads_page(request: Request):
    return templates.TemplateResponse(request, "downloads.html", {**_common(), **_downloads_ctx()})


@app.get("/downloads/rows", response_class=HTMLResponse)
def downloads_rows(request: Request):
    return templates.TemplateResponse(request, "_downloads.html", {**_common(), **_downloads_ctx()})


def _downloads_ctx() -> dict:
    rows, counts = sync.downloads_overview()
    active = any(r["state"] in ("pending", "searching", "found", "queued", "downloading") for r in rows)
    return {"rows": rows, "counts": counts, "active": active}


# ------------------------------------------------------------------ My music

def _filter(q: str = "", folders: list[str] | None = None, artist: str = "", album: str = "") -> dict:
    return {"q": (q or "").strip(), "folders": [f for f in folders or [] if f], "artist": artist or "",
            "album": album or ""}


def _filter_qs(f: dict, **extra) -> str:
    pairs = [("q", f.get("q"))] + [("folder", x) for x in f.get("folders") or []] + \
            [("artist", f.get("artist")), ("album", f.get("album"))] + list(extra.items())
    return urlencode([(k, v) for k, v in pairs if v])


def _rule_text(f: dict) -> str:
    parts = []
    if f.get("folders"):
        parts.append("in " + ", ".join(f["folders"]))
    if f.get("artist"):
        parts.append(f"by {f['artist']}")
    if f.get("album"):
        parts.append(f"from {f['album']}")
    if f.get("q"):
        parts.append(f"matching \"{f['q']}\"")
    return "Every song in your library" + (" " + ", ".join(parts) if parts else "")


def _suggest_name(f: dict) -> str:
    if f.get("album"):
        return f["album"]
    if f.get("artist"):
        return f["artist"]
    if f.get("q"):
        return f["q"]
    if f.get("folders"):
        return ", ".join(Path(x).name for x in f["folders"])
    return "All my music"


@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request, q: str = "", folder: list[str] = Query([]), artist: str = "", album: str = "",
                 sort: str = "artist", page: int = 1, added: int | None = None, to: str | None = None,
                 error: str | None = None):
    cfg = C()
    f = _filter(q, folder, artist, album)
    sort = sort if sort in library.SORTS else "artist"
    page = max(page, 1)
    rows, total = library.browse(cfg, f, sort, (page - 1) * PAGE_SIZE, PAGE_SIZE)
    fixed = [p for p in sync.tracked() if p["source"] == "local" and not p["rule"]]
    return templates.TemplateResponse(request, "library.html", {
        **_common(), "f": f, "sort": sort, "page": page, "rows": rows, "total": total,
        "pages": max(1, -(-total // PAGE_SIZE)), "per_page": PAGE_SIZE, "folders": library.folders(cfg),
        "fixed": fixed, "to": to, "added": added, "added_to": sync.playlist(to) if to else None, "error": error,
        "qs": _filter_qs(f, sort=sort if sort != "artist" else None), "suggest": _suggest_name(f),
        "sorts": {"artist": "Sort by artist", "album": "Sort by album", "title": "Sort by title",
                  "added": "Newest files first"},
        "songs": library.count_songs(cfg, f) if total else 0,
        "filtered": any(f.values()),
        "without": {k: _filter_qs({**f, k: ""}, sort=sort if sort != "artist" else None) for k in ("artist", "album")},
    })


@app.post("/library/playlist")
async def library_playlist(request: Request):
    form = await request.form()
    cfg = C()
    f = _filter(form.get("q", ""), form.getlist("folder"), form.get("artist", ""), form.get("album", ""))
    back = "/library?" + (form.get("back") or "")
    name = (form.get("new_name" if form.get("what") == "selected" else "name") or "").strip() or _suggest_name(f)
    if form.get("what") == "all" and form.get("live"):
        pid = sync.add_local_playlist(cfg, name, rule=f)
        sync.run_job(pid, "Building the playlist", sync.refresh_local, cfg, pid)
        return _back(f"/playlists/{pid}")
    keys = form.getlist("key") if form.get("what") == "selected" else [s["key"] for s in library.collection(cfg, f)]
    if not keys:
        return _back(back + "&error=Tick some songs first")
    pid = form.get("playlist") or ""
    if pid:
        p = sync.playlist(pid)
        if not p or p["source"] != "local" or p["rule"]:
            raise HTTPException(400, "Songs can only be added to a playlist you made by hand")
    else:
        pid = sync.add_local_playlist(cfg, name)
    n = sync.add_songs(cfg, pid, keys)
    sync.run_job(pid, "Updating the .m3u", sync.reconcile, cfg, pid, True)
    if form.get("what") == "all":
        return _back(f"/playlists/{pid}")
    return _back(f"{back}&added={n}&to={quote(pid)}")


@app.get("/library/files/{file_id}")
def library_file(file_id: int):
    r = library.file_by_id(file_id)
    if not r or not os.path.isfile(r["path"]):
        raise HTTPException(404)
    path = Path(r["path"])
    return FileResponse(path, media_type=AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"))


@app.get("/art/{key}")
def art_image(key: str):
    if not re.fullmatch(r"[0-9a-f]{16}", key):
        raise HTTPException(404)
    r = library.file_for_art(key)
    path = art.thumbnail(C(), key, r["path"]) if r else None
    if not path:
        return Response(art.PLACEHOLDER, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


# ------------------------------------------------------------------ system

@app.post("/library/scan")
def library_scan(back: str | None = None):
    threading.Thread(target=library.scan, args=(C(),), daemon=True, name="library-scan").start()
    return _back("/library" if back == "/library" else "/")


@app.post("/slskd/on")
def slskd_on():
    try:
        msg = control.turn_on(C())
    except control.ControlError as e:
        raise HTTPException(502, str(e))
    log.info("slskd control: %s", msg)
    return _back("/")
