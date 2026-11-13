"""Text normalization and Soulseek candidate ranking. Pure functions, no I/O."""
import math
import re
import unicodedata

# Bracketed parts that don't change which recording it is.
_NOISE_BRACKET = re.compile(
    r"\s*[\(\[][^\)\]]*\b(feat|ft|with|from|remaster(ed)?|explicit|clean|radio edit|"
    r"single version|album version|original motion picture|soundtrack)\b[^\)\]]*[\)\]]",
    re.I,
)
_BONUS = re.compile(r"\s*[\(\[]\s*bonus(?:\s+track)?\s*[\)\]]", re.I)
_ANY_BRACKET = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_FEAT = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", re.I)
_TRACK_PREFIX = re.compile(r"^\s*(\d{1,3}|[a-d]\d{1,2})\s*[-_. ]+\s*", re.I)
# Spotify-style suffixes: "Song - Remastered 2002". Dropped unless they name a different recording.
_DASH_SUFFIX = re.compile(r"\s+-\s+([^-]+)$")
_DASH_NOISE = re.compile(r"\b(remaster(ed)?|mono|stereo|single version|album version|radio edit|explicit|clean|"
                         r"bonus track)\b", re.I)
_DASH_KEEP = re.compile(r"\b(live|acoustic|remix|instrumental|demo|unplugged|session)\b", re.I)
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"})


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _fold(s: str) -> str:
    s = ascii_fold(s.translate(_DASHES)).lower().replace("&", " and ")
    return re.sub(r"[’'`]", "", s)


def norm(s: str | None) -> str:
    """Lowercase ASCII words separated by single spaces."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", _fold(s)).strip()


def clean_title(title: str | None) -> str:
    """Title without featuring credits, soundtrack notes and remaster tags."""
    t = _BONUS.sub("", _NOISE_BRACKET.sub("", title or ""))
    m = _DASH_SUFFIX.search(t)
    if m and _DASH_NOISE.search(m.group(1)) and not _DASH_KEEP.search(m.group(1)):
        t = t[:m.start()]
    return _FEAT.sub("", t).strip()


def core_title(title: str | None) -> str:
    """Title with every bracketed part removed: the words a file name must contain."""
    return _ANY_BRACKET.sub("", clean_title(title)).strip() or clean_title(title)


def norm_title(title: str | None) -> str:
    return norm(clean_title(title))


# Apple Music censors words in titles: "F**k", "N***a", "S*%t", "F*@#". Each mark stands for one letter.
_MARKS = "*%#@"
_WORD = re.compile(r"[a-z0-9]+[*%#@][a-z0-9*%#@]*|[a-z0-9]+")


def _same_letters(x: str, y: str) -> bool:
    return len(x) == len(y) and all(cx == cy or cx in _MARKS or cy in _MARKS for cx, cy in zip(x, y))


def _same_word(x: str, y: str) -> bool:
    """Same word, allowing for censoring and a dropped g: "fuckin'" is "fucking" (or "f*****g")."""
    return (_same_letters(x, y) or (len(x) >= 5 and x.endswith("in") and _same_letters(x + "g", y))
            or (len(y) >= 5 and y.endswith("in") and _same_letters(x, y + "g")))


def title_matches(a: str | None, b: str | None) -> bool:
    """Same title, allowing for a censored one: "I Don't Give a F**k" matches "I Don't Give a Fuck"."""
    if norm_title(a) == norm_title(b):
        return True
    wa, wb = (_WORD.findall(_fold(clean_title(t))) for t in (a, b))
    return len(wa) == len(wb) and all(_same_word(x, y) for x, y in zip(wa, wb))


def primary_artist(artist: str | None) -> str:
    """First credited artist: 'A, B & C' -> 'A'. Keeps 'Simon & Garfunkel' style names whole."""
    a = _FEAT.sub("", artist or "").strip()
    if "," in a:
        return a.split(",")[0].strip()
    return a


_CREDIT_SPLIT = re.compile(r"\s*,\s*|\s+(?:&|x|and|with|vs\.?|feat\.?|ft\.?|featuring)\s+", re.I)


def credited_names(artist: str | None) -> set[str]:
    """Each name in a credit: 'JAY-Z & Kanye West' -> {'jay z', 'kanye west'}."""
    return {n for n in (norm(x) for x in _CREDIT_SPLIT.split(artist or "")) if len(n) >= 2}


def artist_matches(want: str | None, have: str | None) -> bool:
    w, h = norm(primary_artist(want)), norm(have)
    if not w or not h:
        return False
    # A shared name is enough for a duo credited differently: "JAY-Z & Ye" is "JAY-Z & Kanye West".
    return w in h or h in norm(want) or bool(credited_names(want) & credited_names(have))


def search_artist(artist: str | None) -> str:
    """The artist words to search with: the first name in 'A & B', 'A x B', 'A, B'."""
    a = re.split(r"\s+(?:&|x|and|with|vs\.?)\s+", primary_artist(artist), maxsplit=1, flags=re.I)[0]
    return norm(a)


def search_queries(title: str, artist: str, album: str | None) -> list[str]:
    """Soulseek searches to try, most specific first."""
    core = norm(core_title(title))
    queries = [f"{search_artist(artist)} {core}"]
    album_core = norm(core_title(album)) if album else ""
    if album_core and album_core != norm(primary_artist(artist)):
        # Soundtracks are often filed by movie name rather than by performer.
        queries.append(f"{' '.join(album_core.split()[:3])} {core}")
    if len(core.split()) >= 2:
        queries.append(core)
    seen, out = set(), []
    for q in queries:
        q = q.strip()
        # Soulseek refuses a search with no word of two letters or more: "F.E.B.N." alone is "f e b n".
        if q and q not in seen and any(len(w) > 1 for w in q.split()):
            seen.add(q)
            out.append(q)
    return out


def _bitrate(f: dict) -> float | None:
    if f.get("bitRate"):
        return float(f["bitRate"])
    if f.get("length") and f.get("size"):
        return f["size"] * 8 / f["length"] / 1000
    return None


def rank_candidates(responses: list[dict], title: str, artist: str, duration_ms: int | None,
                    min_bitrate: int = 192, tolerance: float = 3.0, limit: int = 5) -> list[dict]:
    """Pick acceptable MP3s from slskd search responses, best first."""
    want_len = (duration_ms or 0) / 1000
    need = set(norm(core_title(title)).split())
    artist_words = set(norm(primary_artist(artist)).split())
    out = []
    for r in responses:
        for f in r.get("files", []):
            remote = f.get("filename", "")
            base = remote.replace("\\", "/").rsplit("/", 1)[-1]
            if not base.lower().endswith(".mp3"):
                continue
            length = f.get("length")
            if want_len and (not length or abs(length - want_len) > tolerance):
                continue
            br = _bitrate(f)
            if br is not None and br < min_bitrate - 5:
                continue
            name_words = set(norm(_TRACK_PREFIX.sub("", base[:-4])).split())
            if need and not need <= name_words:
                continue
            path_words = set(norm(remote).split())
            score = 0.0
            if br is None:
                score += 20
            elif br >= 315:
                score += 100
            elif br >= 240:
                score += 80
            else:
                score += 50
            if artist_words and artist_words <= path_words:
                score += 15
            extra = len(name_words - need - artist_words)
            score -= min(extra, 10) * 2          # "Perfect (Live) [Remix]" loses to "Perfect"
            if r.get("hasFreeUploadSlot"):
                score += 20
            score -= min(r.get("queueLength", 0), 50) * 0.5
            score += min(math.log10(max(r.get("uploadSpeed", 0), 1)), 8) * 2
            if want_len and length:
                score -= abs(length - want_len) * 3
            out.append({
                "username": r.get("username"),
                "filename": remote,
                "size": f.get("size"),
                "bitrate": round(br) if br else None,
                "length": length,
                "free_slot": bool(r.get("hasFreeUploadSlot")),
                "score": round(score, 1),
            })
    out.sort(key=lambda c: c["score"], reverse=True)
    # One file per peer, so a failing peer doesn't use up every fallback.
    seen, picked = set(), []
    for c in out:
        if c["username"] in seen:
            continue
        seen.add(c["username"])
        picked.append(c)
        if len(picked) >= limit:
            break
    return picked
