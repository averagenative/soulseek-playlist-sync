"""ListenBrainz Labs: artists listened to in the same sessions. Data CC0.

Labs is ListenBrainz's experimental host and takes no token. If it starts asking for one (401), the client
asks once more with LISTENBRAINZ_TOKEN, which would most likely be the same user token, and keeps sending
it for the rest of the run if Labs takes it. The token isn't sent before Labs asks."""
from ..lookups import Http, StepStopped

URL = "https://labs.api.listenbrainz.org/similar-artists/json"
ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
CREATE_LASTFM = "https://www.last.fm/api/account/create"


class Labs:
    def __init__(self, http: Http, token: str | None, lastfm: bool):
        self.http, self.token, self.lastfm = http, token, lastfm
        self.use_token = False

    def similar(self, mbid: str) -> list[dict]:
        params = {"artist_mbids": mbid, "algorithm": ALGORITHM}
        r = self.http.send("GET", URL, params=params, headers=self._headers())
        if r.status_code == 401 and self.token and not self.use_token:
            self.use_token = True
            r = self.http.send("GET", URL, params=params, headers=self._headers())
        if r.status_code in (401, 403):
            then = ("Similar artists come only from Last.fm for now." if self.lastfm else
                    f"Similar artists need a Last.fm API key then: set LASTFM_API_KEY (make one at {CREATE_LASTFM}). "
                    "Until then, suggestions use influences only.")
            raise StepStopped(f"ListenBrainz refused the similar-artists request ({r.status_code}). {then}")
        if r.status_code in (400, 404):
            return []
        r.raise_for_status()
        return parse_similar(r.json(), mbid)

    def _headers(self) -> dict:
        return {"Authorization": f"Token {self.token}"} if self.use_token else {}


def parse_similar(d, mbid: str) -> list[dict]:
    rows = d if isinstance(d, list) else (d or {}).get("data") or []
    rows = [r for r in rows if isinstance(r, dict) and r.get("artist_mbid") and r.get("artist_mbid") != mbid]
    top = max((r.get("score") or 0 for r in rows), default=0) or 1
    return [{"mbid": r["artist_mbid"], "name": r.get("name"), "score": round((r.get("score") or 0) / top, 4)}
            for r in rows]
