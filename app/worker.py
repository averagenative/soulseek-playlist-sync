"""Background loop: Soulseek searches (one at a time, throttled) and downloads (a few at once)."""
import json
import logging
import shutil
import threading
import time
from pathlib import Path

from . import db, library, settings, sync, youtube
from .config import Config
from .match import rank_candidates, search_queries
from .slskd import Slskd, SlskdError
from .tagger import artwork, tag_mp3

log = logging.getLogger(__name__)
TICK = 2.0
SEARCH_STALE = 120     # a search running this long is abandoned


class Worker:
    def __init__(self):
        self.stop = threading.Event()
        self.last_search = 0.0
        self.status = {"reachable": None}
        self.thread = threading.Thread(target=self.run, daemon=True, name="worker")

    @property
    def cfg(self) -> Config:
        return settings.current()    # picks up changes made on the Settings page

    def start(self):
        self.thread.start()

    def run(self):
        client = Slskd(self.cfg)
        last_status = 0.0
        paused_until = 0.0
        while not self.stop.is_set():
            try:
                if time.time() >= paused_until and time.time() - last_status > 15:
                    self.status = client.status()
                    last_status = time.time()
                self.tick_youtube()
                if self.status.get("reachable") and time.time() >= paused_until:
                    self.tick(client)
            except SlskdError as e:
                if "must be connected" in str(e):
                    # slskd is up but not logged in to Soulseek (often the VPN). Check again in a minute.
                    self.status = {"reachable": True, "connected": False, "version": self.status.get("version"),
                                   "error": "slskd is running but not connected to the Soulseek network"}
                    paused_until = time.time() + 60
                else:
                    self.status = {"reachable": False, "error": str(e)}
                last_status = time.time()
            except Exception:
                log.exception("worker tick failed")
            self.stop.wait(TICK)
        client.close()

    # ------------------------------------------------------------- one pass

    def tick(self, client: Slskd):
        with db.connect() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM downloads WHERE source = 'soulseek' AND state IN "
                                               "('pending', 'searching', 'found', 'queued', 'downloading') "
                                               "ORDER BY created_at")]
            wanted = {r["song_key"] for r in c.execute("SELECT DISTINCT song_key FROM items WHERE want = 1 AND skip = 0")}
        for d in rows:
            if d["state"] in ("queued", "downloading"):
                self.poll_transfer(client, d)
        active = sum(1 for d in rows if d["state"] in ("queued", "downloading"))
        for d in rows:
            if active >= self.cfg.max_downloads:
                break
            if d["state"] == "found" and d["song_key"] in wanted:
                if self.enqueue(client, d):
                    active += 1
        searching = [d for d in rows if d["state"] == "searching"]
        if searching:
            self.poll_search(client, searching[0])
        elif time.time() - self.last_search >= self.cfg.search_delay:
            pending = [d for d in rows if d["state"] == "pending"]
            # Songs someone asked to download go before dry-run searches.
            pending.sort(key=lambda d: d["song_key"] not in wanted)
            if pending:
                self.start_search(client, pending[0])

    # ------------------------------------------------------------- searching

    def _song(self, key: str) -> dict:
        with db.connect() as c:
            return dict(c.execute("SELECT * FROM songs WHERE key = ?", (key,)).fetchone())

    def _update(self, key: str, **fields):
        fields["updated_at"] = db.now()
        with db.connect() as c:
            c.execute(f"UPDATE downloads SET {', '.join(f'{k} = ?' for k in fields)} WHERE song_key = ?",
                      (*fields.values(), key))

    def start_search(self, client: Slskd, d: dict):
        s = self._song(d["song_key"])
        queries = search_queries(s["title"], s["artist"], s["album"])
        q = queries[min(d["query_index"], len(queries) - 1)]
        sid = client.start_search(q)
        self.last_search = time.time()
        self._update(d["song_key"], state="searching", search_id=sid, detail=f'Searching "{q}"')

    def poll_search(self, client: Slskd, d: dict):
        sid = d["search_id"]
        stale = time.time() - (d["updated_at"] or 0) > SEARCH_STALE
        try:
            done = client.search_done(sid)
        except SlskdError:
            done = True
        if not done and not stale:
            return
        try:
            responses = client.search_responses(sid)
        except SlskdError:
            responses = []
        client.delete_search(sid)
        s = self._song(d["song_key"])
        cands = rank_candidates(responses, s["title"], s["artist"], s["duration_ms"],
                                self.cfg.min_bitrate, self.cfg.length_tolerance)
        if cands:
            self._update(d["song_key"], state="found", candidates=json.dumps(cands), attempt=0, search_id=None,
                         detail=None)
            return
        queries = search_queries(s["title"], s["artist"], s["album"])
        if d["query_index"] + 1 < len(queries):
            self._update(d["song_key"], state="pending", query_index=d["query_index"] + 1, search_id=None)
        else:
            files = sum(len(r.get("files", [])) for r in responses)
            self._give_up(d, "not_found", f"No MP3 with the right length on Soulseek ({files} files looked at)",
                          search_id=None)

    # ------------------------------------------------------------- downloading

    def enqueue(self, client: Slskd, d: dict) -> bool:
        cands = json.loads(d["candidates"] or "[]")
        if d["attempt"] >= len(cands):
            self._give_up(d, "failed", "Every Soulseek candidate failed")
            return False
        c = cands[d["attempt"]]
        try:
            client.enqueue(c["username"], c["filename"], c["size"])
        except SlskdError as e:
            self._next_candidate(d, f"Could not queue: {e}")
            return False
        self._update(d["song_key"], state="queued", username=c["username"], filename=c["filename"],
                     size=c["size"], percent=0, queued_at=time.time(), detail=None)
        return True

    def _next_candidate(self, d: dict, why: str):
        cands = json.loads(d["candidates"] or "[]")
        if d["attempt"] + 1 < len(cands):
            self._update(d["song_key"], state="found", attempt=d["attempt"] + 1, detail=why)
        else:
            self._give_up(d, "failed", why)

    def _give_up(self, d: dict, state: str, why: str, **extra):
        """Soulseek is out of options: hand the song to YouTube if that's on, else record why."""
        if self.cfg.youtube_fallback and youtube.available():
            self._update(d["song_key"], state="pending", source="youtube", detail=f"{why}. Trying YouTube next.",
                         percent=0, speed=0, bytes_done=0, **extra)
        else:
            self._update(d["song_key"], state=state, detail=why, **extra)

    # ------------------------------------------------------------- YouTube

    def tick_youtube(self):
        """One YouTube job at a time, in its own thread (yt-dlp blocks)."""
        if getattr(self, "_yt", None) and self._yt.is_alive():
            return
        with db.connect() as c:
            row = c.execute("SELECT d.* FROM downloads d WHERE d.source = 'youtube' AND d.state = 'pending' "
                            "AND EXISTS (SELECT 1 FROM items i WHERE i.song_key = d.song_key AND i.want = 1 "
                            "AND i.skip = 0) ORDER BY d.updated_at LIMIT 1").fetchone()
        if row:
            self._yt = threading.Thread(target=self.youtube_job, args=(dict(row),), daemon=True, name="youtube")
            self._yt.start()

    def youtube_job(self, d: dict):
        key, cfg = d["song_key"], self.cfg
        song = self._song(key)
        self._update(key, state="searching", detail="Searching YouTube")
        try:
            cands = youtube.search(song, cfg.length_tolerance)
        except youtube.YouTubeError as e:
            self._update(key, state="failed", detail=str(e)[:300])
            return
        if not cands:
            self._update(key, state="not_found", detail="Not on Soulseek or YouTube (nothing with the right length)")
            return
        work = cfg.data_dir / "youtube-tmp"
        last_error = None
        for cand in cands:
            who = f"YouTube · {cand['channel'] or 'unknown channel'}"
            self._update(key, state="downloading", username=who, filename=cand["url"], size=None, percent=0,
                         speed=0, bytes_done=0, detail=cand["title"])
            last = [0.0]

            def progress(pct, speed, done, total):
                if time.time() - last[0] >= 1:
                    last[0] = time.time()
                    self._update(key, percent=pct, speed=speed, bytes_done=done, size=total or None)
            try:
                mp3 = youtube.download(cand, work, progress)
            except youtube.YouTubeError as e:
                last_error = str(e)
                continue
            try:
                tag_mp3(mp3, song, artwork(song.get("artwork_url"), cfg.artwork_dir))
                placed = sync.place_download(cfg, key, mp3)
            finally:
                shutil.rmtree(mp3.parent, ignore_errors=True)
            if placed:
                library.add_file(placed, song)
            note = f"From YouTube: {cand['channel']}"
            if cand.get("relaxed") and cand.get("length_diff"):
                diff = cand["length_diff"]
                note += f" ({abs(diff)} s {'longer' if diff > 0 else 'shorter'} than the original)"
            self._update(key, state="done", path=str(placed) if placed else None, percent=100, detail=note)
            log.info("downloaded from YouTube %s - %s -> %s", song["artist"], song["title"], placed)
            return
        self._update(key, state="failed", detail=f"YouTube downloads failed: {(last_error or '')[:250]}")

    def poll_transfer(self, client: Slskd, d: dict):
        t = client.transfer(d["username"], d["filename"])
        if t is None:
            if time.time() - (d["queued_at"] or 0) > 60:
                self._next_candidate(d, "Transfer disappeared from slskd")
            return
        state = t.get("state", "")
        if state.startswith("Completed"):
            if "Succeeded" in state:
                self.finish(client, d, t)
            else:
                client.remove_transfer(d["username"], t["id"])
                self._next_candidate(d, f"{d['username']}: {state.split(',')[-1].strip()}")
            return
        done_bytes = t.get("bytesTransferred") or 0
        speed = t.get("averageSpeed") or 0
        if not speed and d.get("bytes_done") and done_bytes > d["bytes_done"] and d.get("updated_at"):
            # slskd doesn't always report a speed; work it out from the bytes since the last check.
            speed = (done_bytes - d["bytes_done"]) / max(time.time() - d["updated_at"], 0.5)
        progress = {"percent": t.get("percentComplete") or 0, "speed": speed,
                    "bytes_done": done_bytes, "place_in_queue": t.get("placeInQueue")}
        if "InProgress" in state or "Initializing" in state:
            self._update(d["song_key"], state="downloading", **progress)
            return
        self._update(d["song_key"], **progress)
        # Still queued at the peer.
        if time.time() - (d["queued_at"] or 0) > self.cfg.queue_timeout:
            try:
                client._req("DELETE", f"/transfers/downloads/{d['username']}/{t['id']}")
            except SlskdError:
                pass
            client.remove_transfer(d["username"], t["id"])
            self._next_candidate(d, f"Queued at {d['username']} too long")

    def finish(self, client: Slskd, d: dict, t: dict):
        path = client.local_path(d["filename"])
        if not path:
            self._next_candidate(d, "Finished, but the file is not in slskd's download folder")
            return
        song = self._song(d["song_key"])
        try:
            tag_mp3(path, song, artwork(song.get("artwork_url"), self.cfg.artwork_dir))
        except Exception as e:
            path.unlink(missing_ok=True)
            client.remove_transfer(d["username"], t["id"])
            self._next_candidate(d, f"Not a usable MP3: {e}")
            return
        placed = sync.place_download(self.cfg, d["song_key"], path)
        final = placed or path
        if placed:
            library.add_file(placed, song)
            _remove_empty_dir(path.parent, self.cfg.slskd_downloads)
        client.remove_transfer(d["username"], t["id"])
        self._update(d["song_key"], state="done", path=str(final), percent=100, detail=None)
        log.info("downloaded %s - %s -> %s", song["artist"], song["title"], final)


def _remove_empty_dir(folder: Path, root: Path):
    try:
        if folder != root and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass
