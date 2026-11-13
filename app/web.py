"""Web UI."""
import base64
import contextvars
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import httpx

from . import apple, art, control, db, export, favorites, hits, library, mirror, profiles, settings, spotify, sync, youtube
from .config import Config, load
from .slskd import Slskd
from .worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)
HERE = Path(__file__).parent
ROW_LIMIT = 500          # songs shown on a playlist page before "Show all"
PAGE_SIZE = 100          # songs per page on My music
HITS_PAGE = 40           # albums per page of a hits playlist's picks
AUDIO_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
               ".ogg": "audio/ogg", ".opus": "audio/ogg", ".wma": "audio/x-ms-wma"}
base_cfg = load()
worker: Worker | None = None
# The profile this request is for, set by the gate middleware for everything the request runs.
_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("profile", default=None)


def C() -> Config:
    """Current settings: environment defaults plus anything saved on the Settings page, with the signed-in
    profile's Apple Music and Spotify connections."""
    cfg = settings.current()
    p = _current.get()
    return cfg.for_profile(p["id"]) if p else cfg


def _me() -> dict:
    p = _current.get()
    if not p:
        raise HTTPException(401)
    return p


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker
    db.init(base_cfg.db_path)
    settings.init(base_cfg)
    profiles.load_key(base_cfg.data_dir)
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
# A short reason for a failed download ("Age-restricted: ..."), or None to show the message as it is.
templates.env.filters["why"] = youtube.explain
templates.env.globals["SOURCES"] = sync.SOURCES
templates.env.globals["DOWNLOAD_LABELS"] = {
    "downloading": "Downloading", "queued": "Queued", "searching": "Searching", "pending": "Waiting to search",
    "found": "Ready, waiting for a slot", "found_only": "Found (not requested)", "done": "Done", "not_found": "Not found",
    "failed": "Failed",
}


# Pages anyone may load without a profile: health checks, files the browser asks for, and signing in.
OPEN_PATHS = {"/healthz", "/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/signin"}


def _same_site(request: Request) -> bool:
    """A POST must come from the app's own pages: browsers say where a form came from."""
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True                  # not a browser, or an old one
    return origin != "null" and urlsplit(origin).netloc == request.headers.get("host")


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def _set_profile_cookie(request: Request, response: Response, p: dict) -> None:
    response.set_cookie(profiles.COOKIE, profiles.sign(p), max_age=profiles.COOKIE_AGE, httponly=True,
                        samesite="lax", secure=_https(request), path="/")


def _signin_url(request: Request) -> str:
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if request.method != "GET" or request.headers.get("x-live") or here == "/":
        return "/signin"
    return "/signin?" + urlencode({"next": here})


@app.middleware("http")
async def gate(request: Request, call_next):
    """The app password (when set), then which profile this browser uses."""
    path = request.url.path
    if path == "/healthz":
        return await call_next(request)
    if base_cfg.app_password:
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
    if request.method == "POST" and not _same_site(request):
        return Response("This request came from another site, so it was refused.", status_code=403,
                        media_type="text/plain")
    if path.startswith("/static/") or path in OPEN_PATHS:
        return await call_next(request)
    p = profiles.verify(request.cookies.get(profiles.COOKIE))
    fresh = False
    if not p:
        p = profiles.auto()
        fresh = p is not None
    if not p:
        url = _signin_url(request)
        form = request.headers.get("content-type", "").startswith(("application/x-www-form-urlencoded", "multipart/"))
        if request.headers.get("x-live") or (request.method != "GET" and not form):
            # A live region or a script: tell it where to go, and poll.js takes the whole page there.
            return Response("Sign in first.", status_code=401, headers={"X-Signin": url}, media_type="text/plain")
        return RedirectResponse(url, status_code=303)
    request.state.profile = p
    token = _current.set(p)
    try:
        response = await call_next(request)
    finally:
        _current.reset(token)
    if fresh:
        _set_profile_cookie(request, response, p)
    return response


@app.exception_handler(StarletteHTTPException)
async def refused(request: Request, exc: StarletteHTTPException):
    """A refusal (such as another person's playlist) as a page, for a browser; anything else as usual."""
    if exc.status_code == 403 and "text/html" in request.headers.get("accept", "") and _current.get():
        title = "Admins only" if str(exc.detail).startswith("Only an admin") else "Not yours to change"
        return templates.TemplateResponse(request, "message.html", {
            **_common(), "title": title, "text": exc.detail}, status_code=403)
    return await http_exception_handler(request, exc)


def _back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _common() -> dict:
    cfg = C()
    me = _current.get()
    return {
        "me": me,
        "is_admin": bool(me and me["admin"]),
        "profile_count": len(profiles.all_profiles()),
        "apple_configured": apple.configured(cfg),
        "apple_mode": apple.mode(cfg),
        "apple_connected": bool(apple.user_token(cfg)),
        "spotify_configured": spotify.configured(cfg),
        "spotify_connected": spotify.connected(cfg),
        "youtube_ready": youtube.can_read_playlists(),
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


# ------------------------------------------------------------------ profiles

def _safe_next(url: str | None) -> str:
    return url if url and url.startswith("/") and not url.startswith("//") else "/"


@app.get("/signin", response_class=HTMLResponse)
def signin_page(request: Request, p: int | None = None, next: str = "/", error: str | None = None):
    people = profiles.all_profiles()
    chosen = ([x for x in people if x["id"] == p] or [None])[0]
    current = profiles.verify(request.cookies.get(profiles.COOKIE))
    return templates.TemplateResponse(request, "signin.html", {
        "people": people, "chosen": chosen, "next": _safe_next(next), "error": error, "current": current})


@app.post("/signin")
def signin(request: Request, profile: int = Form(...), password: str = Form(""), next: str = Form("/")):
    p = profiles.get(profile)
    if not p:
        return _back("/signin")
    if p["password"] and not profiles.check_password(p["password"], password):
        time.sleep(1)                # slows down guessing
        return _back("/signin?" + urlencode({"p": p["id"], "next": _safe_next(next), "error": "That password isn't right."}))
    response = _back(_safe_next(next))
    _set_profile_cookie(request, response, p)
    return response


@app.post("/signout")
def signout():
    response = _back("/signin")
    response.delete_cookie(profiles.COOKIE, path="/")
    return response


def _admin_only() -> dict:
    me = _me()
    if not me["admin"]:
        raise HTTPException(403, "Only an admin can change this. Ask the person who set up the app.")
    return me


def _profiles_ctx() -> dict:
    people = profiles.all_profiles()
    counts = {}
    with db.connect() as c:
        for r in c.execute("SELECT owner, COUNT(*) FROM playlists GROUP BY owner"):
            counts[r[0]] = r[1]
    return {"people": people, "playlist_counts": counts, "admins": sum(1 for x in people if x["admin"])}


@app.get("/profiles", response_class=HTMLResponse)
def profiles_page(request: Request, saved: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "profiles.html", {**_common(), **_profiles_ctx(),
                                                                 "saved": saved, "error": error})


def _profile_error(e: Exception) -> RedirectResponse:
    return _back(f"/profiles?error={quote(str(e))}")


@app.post("/profiles/me")
def profile_me(request: Request, name: str = Form(""), folder: str = Form(""), password: str = Form(""),
               no_password: str = Form("")):
    me = _me()
    try:
        profiles.update(me["id"], name=name, folder=folder)
        if password or no_password:
            profiles.set_password(me["id"], None if no_password else password)
    except profiles.ProfileError as e:
        return _profile_error(e)
    response = _back("/profiles?saved=me")
    if password or no_password:
        # The change signs other browsers out of this profile; this one stays signed in.
        _set_profile_cookie(request, response, profiles.get(me["id"]))
    return response


@app.post("/profiles/add")
def profile_add(name: str = Form(""), password: str = Form(""), admin: str = Form("")):
    _admin_only()
    try:
        profiles.add(name, password, bool(admin))
    except profiles.ProfileError as e:
        return _profile_error(e)
    return _back("/profiles?saved=added")


@app.post("/profiles/{pid}/edit")
def profile_edit(pid: int, name: str = Form(""), folder: str = Form(""), admin: str = Form("")):
    _admin_only()
    if not profiles.get(pid):
        raise HTTPException(404)
    try:
        profiles.update(pid, name=name, folder=folder)
        profiles.set_admin(pid, bool(admin))
    except profiles.ProfileError as e:
        return _profile_error(e)
    return _back("/profiles?saved=edited")


@app.post("/profiles/{pid}/clear-password")
def profile_clear_password(pid: int):
    _admin_only()
    if not profiles.get(pid):
        raise HTTPException(404)
    profiles.set_password(pid, None)
    return _back("/profiles?saved=cleared")


@app.post("/profiles/{pid}/remove")
def profile_remove(pid: int, confirm: str = Form("")):
    me = _admin_only()
    p = profiles.get(pid)
    if not p:
        raise HTTPException(404)
    if confirm.strip().casefold() != p["name"].casefold():
        return _profile_error(f"Type {p['name']} to remove the profile")
    try:
        profiles.remove(C(), pid)
    except profiles.ProfileError as e:
        return _profile_error(e)
    return _back("/signin" if pid == me["id"] else "/profiles?saved=removed")


# ------------------------------------------------------------------ home

SHOWING_COOKIE = "showing"


def _showing(request: Request) -> str:
    """Whose playlists the home page shows: mine, all, or a profile ID."""
    v = request.query_params.get("showing") or request.cookies.get(SHOWING_COOKIE) or "mine"
    if v in ("mine", "all"):
        return v
    return v if v.isdigit() and profiles.get(v) and int(v) != _me()["id"] else "mine"


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    showing = _showing(request)
    response = templates.TemplateResponse(request, "index.html", {**_common(), **_home_ctx(showing)})
    if request.query_params.get("showing"):
        response.set_cookie(SHOWING_COOKIE, showing, max_age=profiles.COOKIE_AGE, samesite="lax", path="/")
    return response


@app.get("/partials/nav", response_class=HTMLResponse)
def partial_nav(request: Request):
    return templates.TemplateResponse(request, "_nav.html", _common())


@app.get("/partials/home", response_class=HTMLResponse)
def partial_home(request: Request):
    return templates.TemplateResponse(request, "_home.html", {**_common(), **_home_ctx(_showing(request))})


def _home_ctx(showing: str) -> dict:
    """The playlist cards, grouped by person: just yours, one other person's, or everyone's."""
    me = _me()
    people = profiles.all_profiles()
    if showing == "mine":
        shown = [me]
    elif showing == "all":
        shown = sorted(people, key=lambda x: x["id"] != me["id"])     # yours first
    else:
        shown = [x for x in people if str(x["id"]) == showing]
    groups = [{"profile": x, "mine": x["id"] == me["id"], "playlists": _home_playlists(x["id"])} for x in shown]
    mine = next((g["playlists"] for g in groups if g["mine"]), None)
    return {"groups": groups, "showing": showing, "people": people,
            "playlists": [q for g in groups for q in g["playlists"]],
            "my_playlists": mine if mine is not None else sync.tracked(me["id"])}


def _home_playlists(owner: int) -> list[dict]:
    out = []
    for p in sync.tracked(owner):
        _, counts = sync.summary(C(), p["id"])
        label, at = _last_activity(p)
        out.append({**p, "activity": label, "activity_at": at, "counts": counts, "covers": sync.covers(C(), p["id"])})
    return out


def _last_activity(p) -> tuple[str, float | None]:
    """The card's last-done line: the last download sync, or the last copy to another service if that's newer.
    A copy isn't called a sync, because "Synced" next to "0 / 1,448 synced" would read as a contradiction."""
    label, when = "Synced", p["synced_at"]
    for target in mirror.targets_for(p["source"]):
        at = (mirror.get(p["id"], target) or {}).get("updated_at")
        if at and (not when or at > when):
            label, when = f"Copied to {mirror.TARGETS[target]}", at
    return label, when


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, error: str | None = None):
    _admin_only()
    return templates.TemplateResponse(request, "settings.html", {
        **_common(), "cfg": C(), "base": base_cfg, "fields": settings.EDITABLE, "overridden": settings.overrides(),
        "saved": saved, "error": error,
    })


@app.post("/settings")
async def settings_save(request: Request):
    _admin_only()
    form = dict(await request.form())
    try:
        settings.save(settings.parse(form))
    except settings.SettingsError as e:
        return _back(f"/settings?error={e}")
    return _back("/settings?saved=1")


# ------------------------------------------------------------------ Apple Music

def _nonce_key() -> str:
    """The Apple Music connect page's one-time value, one per profile."""
    pid = _me()["id"]
    return "apple_connect_nonce" if pid == 1 else f"apple_connect_nonce:{pid}"


@app.get("/apple/connect", response_class=HTMLResponse)
def apple_connect(request: Request, error: str | None = None):
    cfg = C()
    if apple.mode(cfg) == "web":
        return templates.TemplateResponse(request, "connect_web.html", {**_common(), "error": error})
    if not apple.key_configured(cfg):
        raise HTTPException(500, "APPLE_MODE=developer needs APPLE_TEAM_ID, APPLE_KEY_ID and APPLE_KEY_FILE")
    nonce = secrets.token_urlsafe(24)
    db.set_kv(_nonce_key(), nonce)
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
    nonce = db.get_kv(_nonce_key())
    if not nonce or not secrets.compare_digest(str(body.get("nonce", "")), nonce) or not body.get("token"):
        raise HTTPException(403)
    apple.save_user_token(C(), body["token"])
    db.set_kv(_nonce_key(), None)
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
        "missing_permissions": spotify.missing_permissions(cfg) if spotify.connected(cfg) else [], "error": error,
    })


@app.post("/spotify/setup")
def spotify_setup_save(client_id: str = Form("")):
    _admin_only()
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
    return _back(back if back in ("/spotify/setup", "/profiles", "/settings") else "/profiles")


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
    tracked = {p["remote_id"]: p["id"] for p in sync.tracked(_me()["id"]) if p["source"] == source}
    return templates.TemplateResponse(request, "pick_playlists.html",
                                      {**_common(), "source": source, "remote": remote, "tracked": tracked})


# ------------------------------------------------------------------ YouTube

@app.get("/youtube/add", response_class=HTMLResponse)
def youtube_add_page(request: Request, error: str | None = None, link: str = ""):
    return templates.TemplateResponse(request, "youtube_add.html", {
        **_common(), "error": error, "link": link, "youtube_fallback_label": settings.EDITABLE["youtube_fallback"][1]})


@app.post("/youtube/add")
def youtube_add(link: str = Form("")):
    try:
        remote_id = youtube.playlist_id(link)
        existing = next((p["id"] for p in sync.tracked(_me()["id"])
                         if p["source"] == "youtube" and p["remote_id"] == remote_id), None)
        if existing:
            return _back(f"/playlists/{existing}")
        with youtube.Playlists(C()) as yt:
            name = yt.playlist_name(remote_id)
    except youtube.YouTubeError as e:
        return _back(f"/youtube/add?error={quote(str(e))}&link={quote(link.strip())}")
    pid = sync.add_playlist(C(), "youtube", remote_id, name)
    sync.run_job(pid, "Checking", sync.preview, C(), pid)
    return _back(f"/playlists/{pid}")


# ------------------------------------------------------------------ favorites

def _favorites_ctx() -> dict:
    cfg = C()
    o = favorites.options(cfg.profile)
    sp_ok, am_ok = spotify.connected(cfg), bool(apple.user_token(cfg))
    return {
        "opts": o, "fav": favorites.status(cfg.profile), "fav_running": favorites.running(cfg.profile),
        "pairs": favorites.pair_count(cfg.profile),
        "needs_likes_scope": sp_ok and o["to_spotify"] and not spotify.can_save_likes(cfg),
        "fav_ready": sp_ok and am_ok and (o["to_apple"] or o["to_spotify"]),
    }


@app.get("/favorites", response_class=HTMLResponse)
def favorites_page(request: Request, saved: int = 0, error: str | None = None):
    return templates.TemplateResponse(request, "favorites.html",
                                      {**_common(), **_favorites_ctx(), "saved": saved, "error": error})


@app.get("/partials/favorites", response_class=HTMLResponse)
def favorites_partial(request: Request):
    return templates.TemplateResponse(request, "_favorites.html", {**_common(), **_favorites_ctx()})


@app.post("/favorites/options")
def favorites_options(to_apple: str = Form(""), to_spotify: str = Form(""), auto: str = Form("")):
    favorites.save_options(_me()["id"], bool(to_apple), bool(to_spotify), bool(auto))
    return _back("/favorites?saved=1")


@app.post("/favorites/{action}")
def favorites_action(action: str):
    if action not in ("check", "sync"):
        raise HTTPException(404)
    try:
        favorites.start(C(), apply=action == "sync")
    except favorites.Busy as e:
        return _back(f"/favorites?error={quote(str(e))}")
    return _back("/favorites")


# ------------------------------------------------------------------ playlists

def _playlist(pid: str, write: bool = False):
    """A playlist, or 404. Anyone can look at a playlist; only its owner can change it."""
    p = sync.playlist(pid)
    if not p:
        raise HTTPException(404)
    if write and p["owner"] != _me()["id"]:
        raise HTTPException(403, f"This is {profiles.name_of(p['owner'])}'s playlist.")
    return p


@app.post("/playlists")
def add_playlist(source: str = Form(...), remote_id: str = Form(...), name: str = Form(...)):
    if source not in ("apple", "spotify"):
        raise HTTPException(400)
    pid = sync.add_playlist(C(), source, remote_id, name)
    sync.run_job(pid, "Checking", sync.preview, C(), pid)
    return _back(f"/playlists/{pid}")


@app.get("/playlists/{pid}", response_class=HTMLResponse)
def playlist_page(request: Request, pid: str, error: str | None = None, all: int = 0, q: str = "", show: str = ""):
    _playlist(pid)
    return templates.TemplateResponse(request, "playlist.html", {
        **_common(), **_playlist_ctx(pid, bool(all), q, show), **_copies_ctx(pid), "form_error": error})


@app.get("/playlists/{pid}/rows", response_class=HTMLResponse)
def playlist_rows(request: Request, pid: str, all: int = 0, q: str = "", show: str = ""):
    _playlist(pid)
    return templates.TemplateResponse(request, "_songs.html", {**_common(), **_playlist_ctx(pid, bool(all), q, show)})


@app.get("/playlists/{pid}/copies", response_class=HTMLResponse)
def playlist_copies(request: Request, pid: str):
    _playlist(pid)
    return templates.TemplateResponse(request, "_copies.html", {**_common(), **_copies_ctx(pid)})


MISSING_LIMIT = 300      # songs listed at once on the not-found page; a search narrows it


def _copy_target(pid: str, target: str, write: bool = False) -> dict:
    p = _playlist(pid, write)
    if target not in mirror.targets_for(p["source"]):
        raise HTTPException(404)
    return p


def _song(key: str) -> dict:
    with db.connect() as c:
        r = c.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone()
    if not r:
        raise HTTPException(404)
    return dict(r)


@app.get("/playlists/{pid}/copies/{target}/missing", response_class=HTMLResponse)
def copy_missing(request: Request, pid: str, target: str, q: str = "", done: str = ""):
    """The songs a copy didn't find, each with a way to find it by hand."""
    p = _copy_target(pid, target)
    songs = mirror.not_found(pid, target)
    words = q.casefold().split()
    shown = [s for s in songs if all(w in f"{s['title'] or ''} {s['artist'] or ''} {s['album'] or ''}".casefold()
                                     for w in words)] if words else songs
    return templates.TemplateResponse(request, "missing.html", {
        **_common(), "p": p, "target": target, "label": mirror.TARGETS[target], "total": len(songs),
        "songs": shown[:MISSING_LIMIT], "matching": len(shown), "q": q.strip(), "done": done,
        "mirror": mirror.get(pid, target), "can_copy": _copy_ready(sync.owner_cfg(C(), p), target)[0],
        **_ownership(p)})


@app.get("/playlists/{pid}/copies/{target}/find/{key}", response_class=HTMLResponse)
def copy_find(request: Request, pid: str, target: str, key: str, q: str = "", back: str = ""):
    """Search the service for one song and pick the right one."""
    p = _copy_target(pid, target, write=True)
    song = _song(key)
    ok, why = _copy_ready(C(), target)
    results, error = [], why
    if ok:
        try:
            results = mirror.candidates(C(), target, song, q)
        except (apple.AppleError, spotify.SpotifyError, httpx.HTTPError) as e:
            error = f"{mirror.TARGETS[target]} search failed: {e}"
    return templates.TemplateResponse(request, "find_song.html", {
        **_common(), "p": p, "target": target, "label": mirror.TARGETS[target], "song": song, "q": q.strip(),
        "back": back, "results": results, "error": error, "has_file": bool(sync.song_path(C(), pid, key))})


@app.post("/playlists/{pid}/copies/{target}/find/{key}")
def copy_link(pid: str, target: str, key: str, remote_id: str = Form(...), back: str = Form("")):
    _copy_target(pid, target, write=True)
    song = _song(key)
    msg = mirror.link(C(), pid, target, key, remote_id)
    qs = urlencode([(k, v) for k, v in (("q", back), ("done", f"{song['title']}: {msg}")) if v])
    return _back(f"/playlists/{pid}/copies/{target}/missing?{qs}")


def _copy_ready(cfg: Config, target: str) -> tuple[bool, str | None]:
    if target == "spotify":
        if not spotify.connected(cfg):
            return False, "Connect Spotify first"
        if not spotify.can_write(cfg):
            return False, "Reconnect Spotify to allow creating playlists"
        return True, None
    return (True, None) if apple.user_token(cfg) else (False, "Connect Apple Music first")


def _copies_ctx(pid: str) -> dict:
    """The Copy to Spotify and Apple Music panels."""
    p = sync.playlist(pid)
    cfg = sync.owner_cfg(C(), p)          # copies go into the owner's accounts
    copies = []
    for target in mirror.targets_for(p["source"]):
        ok, why = _copy_ready(cfg, target)
        copies.append({"target": target, "label": mirror.TARGETS[target], "mirror": mirror.get(pid, target),
                       "can_copy": ok, "blocker": why})
    return {"p": p, "copies": copies, "size": sync.song_count(pid, skipped=p["source"] != "local"),
            "spotify_max": mirror.SPOTIFY_MAX, **_ownership(p)}


def _ownership(p) -> dict:
    """Whether the signed-in profile owns the playlist, for pages that hide controls from everyone else."""
    mine = p["owner"] == _me()["id"]
    return {"mine": mine, "owner_name": None if mine else profiles.name_of(p["owner"])}


# ?show= filters on the playlist page, by status. Songs you took out are "left_out".
SHOW = {"left_out": "skipped", **{s: s for s in STATUS_LABELS if s != "skipped"}}


def _find_rows(rows: list[dict], q: str, show: str) -> list[dict]:
    if show in SHOW:
        rows = [r for r in rows if r["status"] == SHOW[show]]
    words = q.casefold().split()
    if words:
        def text(r):
            s = r["song"]
            return f"{s.get('title') or ''} {s.get('artist') or ''} {s.get('album') or ''}".casefold()
        rows = [r for r in rows if all(w in text(r) for w in words)]
    return rows


def _playlist_ctx(pid: str, show_all: bool = False, q: str = "", show: str = "") -> dict:
    cfg = C()
    p = sync.playlist(pid)
    rows, counts = sync.summary(cfg, pid)
    active = bool(p["busy"]) or any(counts.get(s) for s in ("waiting", "searching", "found", "queued", "downloading"))
    q, show = (q or "").strip(), show if show in SHOW else ""
    found = _find_rows(rows, q, show) if q or show else rows
    local = p["source"] == "local"
    rule = sync.rule_of(p)
    view = [("q", q), ("show", show)]
    return {**_ownership(p), "p": p, "rows": found if show_all else found[:ROW_LIMIT], "total_rows": len(found), "show_all": show_all,
            "song_rows": len(rows), "q": q, "show": show,
            "removable": [r["key"] for r in found if r["status"] not in ("removed", "skipped")] if local and (q or show) else [],
            "view_qs": urlencode([(k, v) for k, v in view if v]),
            "rows_qs": urlencode([(k, v) for k, v in view + [("all", 1 if show_all else "")] if v]),
            "all_qs": urlencode([(k, v) for k, v in view if v] + [("all", 1)]),
            "counts": counts, "active": active, "music_root": str(cfg.music_root), "covers": sync.covers(cfg, pid),
            "local": local, "rule": rule, "hits": hits.options(rule),
            "STATUS_LABELS": {**STATUS_LABELS, "skipped": "Left out"} if local else STATUS_LABELS,
            "rule_text": _rule_text(rule) if rule is not None else None,
            "rule_qs": _filter_qs(rule) if rule is not None else None,
            "todo": sum(counts.get(s, 0) for s in ("missing", "in_library", "found", "not_found", "failed", "waiting")),
            "folder": str(sync.folder_of(cfg, p)), "m3u": sync.m3u_path(cfg, p).name}


@app.post("/playlists/{pid}/settings")
def playlist_settings(pid: str, folder: str = Form(...), numbering: str | None = Form(None),
                      library_mode: str = Form("copy"), name: str | None = Form(None)):
    p = _playlist(pid, write=True)
    if library_mode not in ("copy", "reference"):
        raise HTTPException(400)
    refetch = False
    try:
        folder = settings.clean_folder(folder)
        if folder != p["folder"]:
            if p["busy"]:
                raise ValueError("Wait for the current job to finish before moving the folder")
            sync.move_playlist_folder(C(), pid, folder)
        name = (name or "").strip()
        if name and name != p["name"]:
            sync.rename_playlist(C(), pid, name)
        elif not name and p["source"] != "local" and p["own_name"]:
            sync.use_service_name(pid)          # the service's name comes back with the fetch below
            refetch = True
    except ValueError as e:
        return _back(f"/playlists/{pid}?error={e}")
    with db.connect() as c:
        c.execute("UPDATE playlists SET numbering = ?, library_mode = ? WHERE id = ?",
                  (1 if numbering else 0, library_mode, pid))

    def apply(cfg: Config, pid: str) -> None:
        if refetch:
            sync.fetch(cfg, pid)
        sync.reconcile(cfg, pid, True)
    sync.run_job(pid, "Syncing", apply, C(), pid)
    return _back(f"/playlists/{pid}")


@app.post("/playlists/{pid}/{action}")
def playlist_action(pid: str, action: str, back: str | None = None, target: str | None = None):
    p = _playlist(pid, write=True)
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
    _playlist(pid)
    path = sync.song_path(C(), pid, key)
    if not path:
        raise HTTPException(404, "No local file for this song")
    return FileResponse(path, media_type=AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"))


_ZIPS: dict[str, dict] = {}     # ZIP downloads in progress, by the page's token


@app.get("/playlists/{pid}/zip/check")
def playlist_zip_check(pid: str):
    _playlist(pid)
    _, files, _ = sync.export_files(C(), pid)
    if not files:
        return {"ok": False, "ask": None, "error": "Nothing downloaded in this playlist yet."}
    return export.check(files)


@app.get("/playlists/{pid}/zip")
def playlist_zip(pid: str, t: str = ""):
    _playlist(pid)
    folder, files, m3u = sync.export_files(C(), pid)
    if not files:
        raise HTTPException(404, "Nothing downloaded in this playlist yet")
    ok = export.check(files)
    if not ok["ok"]:
        raise HTTPException(413, ok["error"])
    prog = None
    if re.fullmatch(r"[a-z0-9]{8,32}", t):
        now = time.time()
        for k in [k for k, v in _ZIPS.items() if now - v["started"] > 86400]:
            del _ZIPS[k]
        prog = _ZIPS[t] = {"started": now, "total": ok["size"], "songs": ok["songs"], "state": None}
    name = quote(f"{folder}.zip")
    return StreamingResponse(export.stream_zip(folder, files, m3u, prog), media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


@app.get("/zip/progress")
def zip_progress(t: str = ""):
    return export.progress_text(_ZIPS.get(t))


def _view_qs(back: str | None) -> str:
    """The search and filter to go back to, from a form's back field."""
    pairs = parse_qsl(back or "")
    return urlencode([(k, v) for k, v in pairs if k in ("q", "show", "all")])


def _playlist_url(pid: str, back: str | None) -> str:
    qs = _view_qs(back)
    return f"/playlists/{pid}" + (f"?{qs}" if qs else "")


@app.post("/playlists/{pid}/songs/{do}")
async def change_songs(request: Request, pid: str, do: str):
    """Take the selected songs out of a My music playlist, or put them back. With matching=1, every song
    the page's search and filter show."""
    if do not in ("remove", "putback"):
        raise HTTPException(404)
    _playlist(pid, write=True)
    form = await request.form()
    back = form.get("back") or ""
    if form.get("matching"):
        view = dict(parse_qsl(back))
        rows, _ = sync.summary(C(), pid)
        keys = [r["key"] for r in _find_rows(rows, view.get("q", ""), view.get("show", ""))
                if (r["status"] == "skipped") == (do == "putback") and r["status"] != "removed"]
    else:
        keys = form.getlist("key")
    if keys:
        try:
            sync.leave_out(C(), pid, keys, out=do == "remove")
        except ValueError as e:
            raise HTTPException(400, str(e))
    return _back(_playlist_url(pid, back))


@app.post("/playlists/{pid}/songs/{key}/{action}")
def song_action(pid: str, key: str, action: str, back: str | None = None):
    _playlist(pid, write=True)
    if action == "skip":
        sync.set_song(pid, key, skip=1, want=0)
    elif action == "unskip":
        sync.set_song(pid, key, skip=0)
    elif action in ("remove", "putback"):
        try:
            sync.leave_out(C(), pid, [key], out=action == "remove")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return _back(_playlist_url(pid, back))
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

# ------------------------------------------------------------------ hits playlists

def _new_hits(cfg: Config, name: str, f: dict) -> str:
    pid = sync.add_local_playlist(cfg, name, rule={**_filter(f.get("q", ""), f.get("folders"), f.get("artist", ""),
                                                             f.get("album", "")), "hits": dict(hits.DEFAULTS)})
    sync.run_job(pid, "Picking songs", sync.refresh_local, cfg, pid)
    hits.start(cfg)
    return pid


def _hits_playlist(pid: str, write: bool = False):
    p = _playlist(pid, write)
    if hits.options(sync.rule_of(p)) is None:
        raise HTTPException(404)
    return p


def _hits_status_ctx() -> dict:
    st = hits.status()
    return {"hs": st, "hs_running": hits.running(), "hs_counts": hits.cache_counts(),
            "hs_genius": bool(C().genius_token)}


@app.post("/playlists/{pid}/hits/new")
def hits_new(pid: str):
    """A hits playlist made from an auto-updating playlist: the same songs, standouts only."""
    p = _playlist(pid, write=True)
    rule = sync.rule_of(p)
    if rule is None or hits.options(rule) is not None:
        raise HTTPException(400, "Make a hits playlist from an auto-updating My music playlist")
    new = _new_hits(C(), p["name"] + " Hits", rule)
    return _back(f"/playlists/{new}/hits")


@app.get("/playlists/{pid}/hits", response_class=HTMLResponse)
def hits_page(request: Request, pid: str, q: str = "", show: str = "", page: int = 1, saved: int = 0):
    p = _hits_playlist(pid)
    cfg = sync.owner_cfg(C(), p)          # picks use the owner's favorites
    rule = sync.rule_of(p)
    with db.connect() as c:
        skip = {r[0] for r in c.execute("SELECT song_key FROM items WHERE playlist_id = ? AND skip = 1", (pid,))}
    groups = hits.plan(cfg, rule, skip)
    albums = [g for g in groups if g["songs"]]
    picked = sum(g["picked"] for g in albums)
    no_data = sum(1 for g in albums if not g["has_data"] and g["kind"] in hits.BASE_KINDS)
    words = (q or "").casefold().split()
    if words:
        albums = [g for g in albums if all(w in f"{g['artist'] or ''} {g['album'] or ''}".casefold() for w in words)]
    if show == "nodata":
        albums = [g for g in albums if not g["has_data"] and g["kind"] in hits.BASE_KINDS]
    elif show == "picked":
        albums = [g for g in albums if g["picked"]]
    page = max(page, 1)
    pages = max(1, -(-len(albums) // HITS_PAGE))
    view = [("q", q), ("show", show)]
    return templates.TemplateResponse(request, "hits.html", {
        **_common(), **_hits_status_ctx(), "p": p, "opts": hits.options(rule), "albums": albums[(page - 1) * HITS_PAGE:page * HITS_PAGE],
        "matching": len(albums), "total_albums": sum(1 for g in groups if g["songs"]), "picked": picked, "no_data": no_data,
        "q": q, "show": show, "page": page, "pages": pages, "saved": saved, **_ownership(p),
        "view_qs": urlencode([(k, v) for k, v in view if v]), "rule_text": _rule_text(rule)})


@app.get("/partials/hits", response_class=HTMLResponse)
def hits_partial(request: Request, back: str = "/"):
    return templates.TemplateResponse(request, "_hits_status.html", {**_common(), **_hits_status_ctx(),
                                                                     "back": back if back.startswith("/") else "/"})


@app.post("/playlists/{pid}/hits/settings")
def hits_settings(pid: str, min_songs: int = Form(2), max_songs: int = Form(5), cutoff: float = Form(1.25)):
    p = _hits_playlist(pid, write=True)
    rule = sync.rule_of(p)
    rule["hits"] = hits.options({"hits": {"min": min_songs, "max": max(min_songs, max_songs), "cutoff": cutoff,
                                          "whole": hits.options(rule)["whole"]}})
    with db.connect() as c:
        c.execute("UPDATE playlists SET rule = ? WHERE id = ?", (json.dumps(rule), pid))
    sync.run_job(pid, "Picking songs", sync.refresh_local, C(), pid, False)
    return _back(f"/playlists/{pid}/hits?saved=1")


@app.post("/playlists/{pid}/hits/whole")
def hits_whole(pid: str, album: str = Form(""), key: str = Form(""), on: str = Form("1"), back: str = Form("")):
    """Put all of an album in a hits playlist (from a song on it, or from the picks page), or go back to its picks."""
    p = _hits_playlist(pid, write=True)
    rule = sync.rule_of(p)
    album = album or (hits.album_of(key) if key else "")
    if not album:
        raise HTTPException(400, "Which album?")
    whole = set(hits.options(rule)["whole"])
    whole = whole - {album} if on == "0" else whole | {album}
    rule["hits"] = {**rule.get("hits", {}), "whole": sorted(whole)}
    with db.connect() as c:
        c.execute("UPDATE playlists SET rule = ? WHERE id = ?", (json.dumps(rule), pid))
    sync.run_job(pid, "Picking songs", sync.refresh_local, C(), pid, False)
    return _back(back if back.startswith("/") else f"/playlists/{pid}")


@app.post("/hits/research")
def hits_research(back: str = Form("/")):
    hits.start(C())
    return _back(back if back.startswith("/") else "/")


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
    lead = "The standout songs of each album" if hits.options(f) is not None else "Every song"
    return lead + " in your library" + (" " + ", ".join(parts) if parts else "")


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
    fixed = [p for p in sync.tracked(_me()["id"]) if p["source"] == "local" and not p["rule"]]
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
    if form.get("what") == "all" and form.get("hits"):
        return _back(f"/playlists/{_new_hits(cfg, name if name != _suggest_name(f) else f'{name} Hits', f)}/hits")
    if form.get("what") == "all" and form.get("live"):
        pid = sync.add_local_playlist(cfg, name, rule=f)
        sync.run_job(pid, "Building the playlist", sync.refresh_local, cfg, pid)
        return _back(f"/playlists/{pid}")
    keys = form.getlist("key") if form.get("what") == "selected" else [s["key"] for s in library.collection(cfg, f)]
    if not keys:
        return _back(back + "&error=Tick some songs first")
    pid = form.get("playlist") or ""
    if pid:
        p = _playlist(pid, write=True)
        if p["source"] != "local" or p["rule"]:
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
