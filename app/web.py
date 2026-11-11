"""Web UI."""
import base64
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import httpx

from . import apple, control, db, library, settings, spotify, sync
from .config import Config, load
from .slskd import Slskd
from .worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)
HERE = Path(__file__).parent
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
    return cfg.spotify_redirect_uri or str(request.url_for("spotify_callback"))


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
    playlists = []
    for p in sync.tracked():
        _, counts = sync.summary(C(), p["id"])
        playlists.append({**p, "counts": counts, "covers": sync.covers(p["id"])})
    return templates.TemplateResponse(request, "index.html", {**_common(), "playlists": playlists})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, error: str | None = None):
    return templates.TemplateResponse(request, "settings.html", {
        **_common(), "cfg": C(), "base": base_cfg, "fields": settings.EDITABLE, "overridden": settings.overrides(),
        "spotify_redirect": _spotify_redirect(request), "saved": saved, "error": error,
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
            remote = [{**p, "readable": True, "note": "" if p["can_edit"] else "read-only"} for p in api.playlists()]
    except apple.NotConnected:
        return _back("/apple/connect")
    return _pick(request, "apple", remote)


# ------------------------------------------------------------------ Spotify

@app.get("/spotify/connect")
def spotify_connect(request: Request):
    if not spotify.configured(C()):
        return _back("/settings?error=Enter your Spotify Client ID first")
    return RedirectResponse(spotify.auth_url(C(), _spotify_redirect(request)), status_code=303)


@app.get("/spotify/callback", name="spotify_callback")
def spotify_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code:
        return _back(f"/settings?error=Spotify sign-in failed: {error or 'no code'}")
    try:
        spotify.finish_auth(C(), code, state)
    except spotify.SpotifyError as e:
        return _back(f"/settings?error={e}")
    return _back("/spotify/playlists")


@app.post("/spotify/disconnect")
def spotify_disconnect():
    spotify.disconnect(C())
    return _back("/settings")


@app.get("/spotify/playlists", response_class=HTMLResponse)
def spotify_playlists(request: Request):
    try:
        with spotify.Client(C()) as api:
            remote = api.playlists()
    except spotify.NotConnected:
        return _back("/spotify/connect")
    except spotify.SpotifyError as e:
        return _back(f"/settings?error={e}")
    return _pick(request, "spotify", remote)


def _pick(request: Request, source: str, remote: list[dict]):
    tracked = {p["remote_id"]: p["id"] for p in sync.tracked() if p["source"] == source}
    return templates.TemplateResponse(request, "pick_playlists.html",
                                      {**_common(), "source": source, "remote": remote, "tracked": tracked})


# ------------------------------------------------------------------ playlists

@app.post("/playlists")
def add_playlist(source: str = Form(...), remote_id: str = Form(...), name: str = Form(...)):
    if source not in sync.SOURCES:
        raise HTTPException(400)
    pid = sync.add_playlist(C(), source, remote_id, name)
    sync.run_job(pid, "Checking", sync.preview, C(), pid)
    return _back(f"/playlists/{pid}")


@app.get("/playlists/{pid}", response_class=HTMLResponse)
def playlist_page(request: Request, pid: str, error: str | None = None):
    if not sync.playlist(pid):
        raise HTTPException(404)
    return templates.TemplateResponse(request, "playlist.html",
                                      {**_common(), **_playlist_ctx(pid), "form_error": error})


@app.get("/playlists/{pid}/rows", response_class=HTMLResponse)
def playlist_rows(request: Request, pid: str):
    if not sync.playlist(pid):
        raise HTTPException(404)
    return templates.TemplateResponse(request, "_songs.html", {**_common(), **_playlist_ctx(pid)})


def _playlist_ctx(pid: str) -> dict:
    cfg = C()
    p = sync.playlist(pid)
    rows, counts = sync.summary(cfg, pid)
    active = bool(p["busy"]) or any(r["status"] in ("waiting", "searching", "queued", "downloading") for r in rows)
    return {"p": p, "rows": rows, "counts": counts, "active": active, "music_root": str(cfg.music_root),
            "covers": sync.covers(pid),
            "folder": str(sync.folder_of(cfg, p)), "m3u": sync.m3u_path(cfg, p).name}


@app.post("/playlists/{pid}/settings")
def playlist_settings(pid: str, folder: str = Form(...), numbering: str | None = Form(None),
                      library_mode: str = Form("copy")):
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
    except ValueError as e:
        return _back(f"/playlists/{pid}?error={e}")
    with db.connect() as c:
        c.execute("UPDATE playlists SET numbering = ?, library_mode = ? WHERE id = ?",
                  (1 if numbering else 0, library_mode, pid))
    sync.run_job(pid, "Syncing", sync.reconcile, C(), pid, True)
    return _back(f"/playlists/{pid}")


@app.post("/playlists/{pid}/{action}")
def playlist_action(pid: str, action: str, back: str | None = None):
    if not sync.playlist(pid):
        raise HTTPException(404)
    jobs = {"preview": ("Checking", sync.preview), "sync": ("Downloading all", sync.sync),
            "prune": ("Pruning", sync.prune)}
    if action in jobs:
        label, fn = jobs[action]
        sync.run_job(pid, label, fn, C(), pid)
    elif action == "forget":
        sync.remove_playlist(pid)
        return _back("/")
    else:
        raise HTTPException(404)
    return _back("/" if back == "/" else f"/playlists/{pid}")


@app.post("/playlists/{pid}/songs/{key}/{action}")
def song_action(pid: str, key: str, action: str):
    if action == "skip":
        sync.set_song(pid, key, skip=1, want=0)
    elif action == "unskip":
        sync.set_song(pid, key, skip=0)
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


# ------------------------------------------------------------------ system

@app.post("/library/scan")
def library_scan():
    threading.Thread(target=library.scan, args=(C(),), daemon=True, name="library-scan").start()
    return _back("/")


@app.post("/slskd/on")
def slskd_on():
    try:
        msg = control.turn_on(C())
    except control.ControlError as e:
        raise HTTPException(502, str(e))
    log.info("slskd control: %s", msg)
    return _back("/")
