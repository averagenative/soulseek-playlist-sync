"""Thin client for the slskd REST API."""
import os
import uuid
from pathlib import Path

import httpx

from .config import Config


class SlskdError(Exception):
    """status is slskd's HTTP status when it answered with an error, None when it couldn't be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Slskd:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        headers = {"X-API-Key": cfg.slskd_api_key} if cfg.slskd_api_key else {}
        self.http = httpx.Client(base_url=cfg.slskd_url + "/api/v0", timeout=20, headers=headers)

    def close(self):
        self.http.close()

    def _req(self, method: str, path: str, **kw):
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise SlskdError(f"slskd unreachable: {e}") from e
        if r.status_code >= 400:
            raise SlskdError(f"slskd {method} {path}: HTTP {r.status_code} {r.text[:200]}", r.status_code)
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else None

    # status -----------------------------------------------------------

    def status(self) -> dict:
        """{'reachable': bool, 'version': str|None, 'connected': bool|None, 'error': str|None}"""
        # /application reports the Soulseek login to any API key; /server needs the administrator role.
        try:
            app = self._req("GET", "/application") or {}
        except SlskdError as e:
            if e.status is None:
                return {"reachable": False, "version": None, "connected": None, "error": str(e)}
            app = {}            # an older slskd: fall back to the version alone
        version = (app.get("version") or {}).get("current")
        if not version:
            try:
                version = self._req("GET", "/application/version")
            except SlskdError as e:
                return {"reachable": False, "version": None, "connected": None, "error": str(e)}
        server = app.get("server")
        connected = bool(server.get("isConnected") and server.get("isLoggedIn")) if isinstance(server, dict) else None
        return {"reachable": True, "version": version, "connected": connected, "error": None}

    # searches ---------------------------------------------------------

    def start_search(self, text: str) -> str:
        sid = str(uuid.uuid4())
        try:
            self._post_search(sid, text)
        except SlskdError:
            self.delete_search(sid)   # slskd records a failed search; don't leave it behind
            raise
        return sid

    def _post_search(self, sid: str, text: str) -> None:
        self._req("POST", "/searches", json={
            "id": sid,
            "searchText": text,
            "searchTimeout": 15000,
            "responseLimit": 200,
            "fileLimit": 20000,
            "filterResponses": True,
            "minimumResponseFileCount": 1,
        })

    def search_done(self, sid: str) -> bool:
        s = self._req("GET", f"/searches/{sid}")
        return bool(s.get("isComplete"))

    def search_responses(self, sid: str) -> list[dict]:
        return self._req("GET", f"/searches/{sid}/responses") or []

    def delete_search(self, sid: str) -> None:
        try:
            self._req("DELETE", f"/searches/{sid}")
        except SlskdError:
            pass

    # transfers --------------------------------------------------------

    def enqueue(self, username: str, filename: str, size: int) -> None:
        self._req("POST", f"/transfers/downloads/{username}", json=[{"filename": filename, "size": size}])

    def transfer(self, username: str, filename: str) -> dict | None:
        """The newest transfer record for this remote file, or None."""
        try:
            data = self._req("GET", f"/transfers/downloads/{username}")
        except SlskdError as e:
            if "HTTP 404" in str(e):
                return None
            raise
        found = [f for d in (data or {}).get("directories", []) for f in d.get("files", [])
                 if f.get("filename") == filename]
        found.sort(key=lambda f: f.get("requestedAt") or "", reverse=True)
        return found[0] if found else None

    def remove_transfer(self, username: str, transfer_id: str) -> None:
        try:
            self._req("DELETE", f"/transfers/downloads/{username}/{transfer_id}", params={"remove": "true"})
        except SlskdError:
            pass

    def local_path(self, filename: str) -> Path | None:
        """Where slskd saved a finished download: <downloads>/<remote folder>/<file>."""
        parts = filename.replace("\\", "/").split("/")
        folder = self.cfg.slskd_downloads / parts[-2] if len(parts) > 1 else self.cfg.slskd_downloads
        exact = folder / parts[-1]
        if exact.exists():
            return exact
        # slskd adds a suffix when the name was taken: "song_6389....mp3"
        stem, ext = os.path.splitext(parts[-1])
        if folder.is_dir():
            matches = sorted((p for p in folder.iterdir() if p.name.startswith(stem) and p.suffix.lower() == ext.lower()),
                             key=lambda p: p.stat().st_mtime, reverse=True)
            if matches:
                return matches[0]
        return None
