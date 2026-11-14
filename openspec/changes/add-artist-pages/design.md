## Context

The app indexes about 28,800 files from about 1,500 artists. After the hits work, the `library` table carries each file's MusicBrainz recording, artist, and release group IDs from Picard tags: 657 of the 1,505 artist names have an artist ID, and of the 610 artists with five or more songs only 47 don't. The hits lookup (`app/hits.py`) established the pattern this change reuses: a background job, one at a time, that fills cache tables from public APIs, fetches only what's missing or stale, paces each service, and stops one step without stopping the rest when a service refuses. Pages and playlist builds read only the cache.

Each source was checked against a real artist (Nirvana) before this design:

| Source | Key | What it returned | Limits and terms |
|---|---|---|---|
| MusicBrainz `/ws/2/artist/{id}?inc=url-rels+artist-rels+genres+tags+aliases` | none | Type, life span, area, begin area, genres, members with years and instruments, and links to Wikidata, Wikipedia, AllMusic, Discogs, Last.fm, and the official site | 1 request a second, User-Agent required. Data CC0. |
| Wikidata `wbgetentities` (50 IDs a request) and `Special:EntityData` | none | P737 influenced by (8), P136 genres, P571 inception, P740 formed in, P527 members, P18 image, P434 MusicBrainz ID, P1728 AllMusic ID, the English Wikipedia title | Polite use, User-Agent required. Data CC0. |
| Wikidata SPARQL (`query.wikidata.org`) | none | Reverse P737: 33 artists influenced by Nirvana | 60 seconds of query time a minute per client. CC0. |
| Wikipedia `action=query&prop=extracts&exintro&explaintext` | none | A 2,530-character, 4-paragraph introduction (the REST summary is only 466) | Text CC BY-SA 4.0: credit and link required. |
| Wikimedia Commons `imageinfo` with `extmetadata` | none | Thumbnail URL, license (CC BY-SA 2.0), author, "attribution required" | Per-file license. |
| ListenBrainz Labs `similar-artists/json` | none (for now) | Scored similar artists (Red Hot Chili Peppers, Radiohead, and others) | Experimental host. The main ListenBrainz API started requiring a token for per-artist data on 2026-11-12. |
| Discogs `/artists/{id}` | optional token | A 2,682-character profile with `[a=...]` markup, and 10 members | 25 requests a minute without a token, 60 with one. Attribution and a link back required. |
| TheAudioDB `/api/v1/json/123/artist-mb.php` | free key `123` | Artist image, style, mood, and formed year. No bio on the free key. | Free key is rate limited, so treat it as images only unless `THEAUDIODB_API_KEY` is set. |
| Last.fm `artist.getInfo` and `artist.getSimilar` | `LASTFM_API_KEY` | A bio and tags, and up to 100 similar artists with a 0 to 1 match | Bios CC BY-SA. Not tested: no key yet. |

AllMusic has no API and doesn't allow copying its text. Genius artist descriptions belong to Genius and its contributors. Neither is used for text, and AllMusic is linked.

## Goals / Non-Goals

**Goals:**

- Artist pages that read from a local cache, so they load fast and work when the sources are down.
- Every copied text and image shown with its source, link, and license.
- Influences in both directions, kept separate from similarity.
- Suggestions that come with reasons, and whose songs download through the existing Soulseek pipeline.
- One lookup and pacing layer shared with the hits lookup, so MusicBrainz sees one client at one request a second.

**Non-Goals:**

- Reviews, ratings, or "moods and themes" (AllMusic's own editorial work).
- Bios in languages other than English. The source order makes adding a language a later change.
- Editing artist data, beyond setting an artist's MusicBrainz ID by hand.
- Fetching on page load. Pages never wait on the network.
- Automatic downloads of suggestions. Songs download only after **Get their hits**.
- Pages for "Various Artists" or for every featured artist on a song.

## Decisions

### 1. The MusicBrainz artist ID is the key for everything

Every table keys on the MusicBrainz artist ID, and MusicBrainz's URL relations lead to the Wikidata, Discogs, and Last.fm entries. Wikidata's P434 maps related artists (influences) back to MusicBrainz IDs, so they can be matched to the library.

- *Alternative:* keying on normalized names. Rejected, because names collide (several bands are called "Nirvana") and the related-artist data from Wikidata and ListenBrainz comes with IDs.

### 2. Artists without an ID are found through one of their albums

For a name with no ID in its tags, search release groups: `releasegroup:"<album>" AND artist:"<name>"`, and accept the first result whose artist credit has the same normalized name. The album title tells same-named artists apart, which an artist-name search can't. Results go in `artist_names` (name, ID, method, time). A miss is retried after 30 days, and a pasted MusicBrainz link (method "manual") is never replaced.

- *Alternative:* artist-name search with the top score. Rejected, because it's wrong for common names, and a wrong ID shows a stranger's bio.

### 3. One lookup layer for hits and artists

Move `_Http`, `StepStopped`, the step runner, and the status helpers out of `app/hits.py` into `app/lookups.py`. Pacing becomes process-wide and per host: one `Pacer` per host, shared by every job, so the hits and artist lookups can't double the MusicBrainz rate together. Each source gets a small client function (`musicbrainz.artist()`, `wikidata.entities()`, `wikidata.influenced()`, `wikipedia.intros()`, `commons.images()`, `lb_labs.similar()`, `discogs.artist()`, `lastfm.info()`, `audiodb.artist()`) that returns plain dicts and raises `StepStopped` on 401 or 403.

- *Alternative:* copy the hits code into `app/artists.py`. Rejected, because two pacers for one host would exceed MusicBrainz's limit, and two copies drift.

### 4. Batch wherever the source allows

- Wikidata entities: 50 IDs a request, for artists and for their related items (labels plus P434).
- Influenced (reverse P737): one SPARQL query per 50 library artists, using `VALUES ?target {...}`, with a 60-second timeout. A batch that times out is split in half and retried.
- Wikipedia intros: 20 titles a request (the limit for `exintro`).
- Commons image info: 50 files a request.
- MusicBrainz artists, Discogs, Last.fm, and ListenBrainz Labs: one artist a request, paced.

With batching, a first run for 657 artists takes about 11 minutes for MusicBrainz (657 at one a second), well under a minute for Wikidata, Wikipedia, and Commons, about 26 minutes for Discogs without a token (11 with one), and a few minutes each for ListenBrainz Labs and Last.fm.

### 5. Data model

New tables, added in `db.SCHEMA`:

- `artist_names(name, mbid, how, checked_at)`: name resolution. The name is `norm(primary_artist(...))`, and `how` is tag, search, or manual.
- `artists(mbid PRIMARY KEY, name, sort_name, type, area, begin_area, begin, end, ended, disambiguation, genres JSON, wikidata, enwiki, links JSON, image_file, image_credit JSON, fetched JSON)`. `links` maps a kind (wikipedia, allmusic, discogs, lastfm, official, and so on) to a URL. `fetched` maps each source to the time it was last fetched, for refresh decisions.
- `artist_bios(mbid, source, text, url, license, fetched_at, PRIMARY KEY (mbid, source))`.
- `artist_links(mbid, kind, other_mbid, other_wikidata, other_name, source, score, detail JSON, PRIMARY KEY (mbid, kind, other_key, source))`. `kind` is member, member_of, influenced_by, influenced, or similar. `other_key` is the other artist's MusicBrainz ID, or else its Wikidata ID, or else its normalized name. `detail` holds years and instruments for memberships.
- `suggestion_dismissed(profile, mbid, at, PRIMARY KEY (profile, mbid))`.
- `artist_releases(rgid PRIMARY KEY, mbid, title, kind, year, release_id, fetched_at)` and `artist_tracks(rgid, disc, track, recording, title, length, PRIMARY KEY (rgid, disc, track))`: a suggested artist's albums, EPs, and singles, and one tracklist for each, for **Get their hits**. Listener counts, singles, and Genius views go in the hits tables (`hit_recordings`, `hit_artist_songs`, `hit_artists`), which are keyed by recording and artist IDs and so already fit artists outside the library.

Images are saved as `DATA_DIR/artwork/artists/<mbid>.jpg`, at most 500 pixels wide, using Pillow (already a dependency).

- *Alternative:* one JSON blob per artist. Rejected, because suggestions and in-library linking need queries across artists ("who points at X").

### 6. Refresh times

Facts, influences, bios, and suggested artists' albums refresh after 180 days, and similar artists after 60, because listening data moves faster. Images are fetched once and again only when the Wikidata P18 file name changes. Like the hits lookup, the job fetches only stale or missing data, so after the first run it costs a few requests for new artists.

### 7. Bio source order and licenses

Wikipedia intro (CC BY-SA 4.0), then Last.fm (CC BY-SA), then Discogs (shown as "Data provided by Discogs" with a link), then TheAudioDB (only with a paid key). The page shows the first and offers the others. Discogs markup (`[a=Name]`, `[a12345]`, `[l=Label]`, `[url=...]text[/url]`, `[b]`, `[i]`) becomes plain text. Numeric `[a12345]` references become names through the profile's `members` and `groups` arrays when they're there, and are dropped otherwise.

### 8. Influence and similarity stay separate

Influences come only from Wikidata P737, both directions, plus a local inverse: if library artist A lists B, then B's page shows A. Similar artists come from ListenBrainz Labs and Last.fm. Scores are normalized per source (ListenBrainz `score / max score` in the list, Last.fm `match`), and the page shows the highest. The page labels each list with where it came from.

ListenBrainz Labs takes no key today. If it answers 401, the client asks once more with `LISTENBRAINZ_TOKEN` when it's set, because a Labs token would most likely be the same ListenBrainz user token. The token isn't sent until Labs asks for one. If Labs still refuses, similar artists come only from Last.fm, which needs `LASTFM_API_KEY`. Without that key, the lookup status says similar artists need it and links to the page that creates one, and suggestions use influence links only.

### 9. Suggestions scoring

For each artist X that isn't in the library and isn't dismissed:

```
score(X) = sum over library artists A linked to X of weight(A) * strength(A, X)
weight(A) = 1 + ln(1 + songs by A) + 2 * ln(1 + favorites by A)
strength  = normalized similarity score, or 0.5 for an influence link in either direction
```

Favorites come from the signed-in profile's rows in `hit_favorites` (see the `add-profiles` change), mapped to artists through `library.song_key`, so each person gets their own suggestions. The top 100 are kept for each profile. The reasons are the top five links by `weight * strength`.

- *Alternative:* collaborative filtering over listening data. Rejected for now: the app has no listening history to train on, and ListenBrainz's similarity already encodes it.
- *Alternative:* weighing the songs a hits playlist picked more than the rest of the library. Left out of the first version: hits picks are chosen by charts and listener counts, so weighing them would favor artists that are popular in general rather than ones you like, and favorites already carry what you like at twice the weight. After the first build, the top 20 suggestions for the real library are reviewed with their reasons (task 6.6), and this weight is added then if the list needs it.

### 10. Get their hits uses the hits rules

**Get their hits** picks a suggested artist's songs with the same rules a hits playlist applies to an album you own. The per-album picking moves out of `hits.plan()` into `hits.pick_album(songs, kind, opts)`, and reading a song's numbers into `hits.signals()`, with no change in behavior for hits playlists, so both callers share one rule set.

For a suggested artist, the albums come from MusicBrainz:

1. Browse the artist's release groups of type album, EP, and single. Skip any with a secondary type other than Soundtrack (compilations, live albums, remixes, DJ mixes, demos), the same release types the hits playlists treat as lesser.
2. For each album and EP, browse its official releases with `inc=recordings+media` (one request returns up to 36 releases with their tracklists; Nevermind's 96 releases fill the first page with 36) and use the earliest one, so deluxe-edition bonus tracks don't count. A single counts as its own release with its title song.
3. Fetch listener counts for every recording with the ListenBrainz batch request the hits lookup uses (500 recordings a request), the artist's MusicBrainz singles (`_mb_singles`), and its Genius page views when `GENIUS_API_TOKEN` is set. Billboard chart data is already local.
4. Each song counts once, on the earliest album it's on, as in a hits playlist. Run `pick_album()` on each album and single with the Suggestions rules.

The Suggestions rules are a max songs per album (default 5) and a cutoff (default 1.25), set on the Suggestions page and stored in `kv` as `suggestions_rules:<profile id>`. There's no minimum per album: these are albums you don't own, and filling every album to two songs would add 60 songs for an artist with 30 albums. So a song is picked only when it passes a rule: it charted on the Hot 100, it came out as a single near its album, or its listener count or Genius views are at least the cutoff times its album's median. When no song passes (an artist with no chart, single, or listener data), the suggestion says so and doesn't offer the button.

A live run on 2026-11-13 showed that the album's median alone isn't enough for artists you don't own: an EP of remixes and B-sides has a tiny median, so its one known song looked like a 100x standout, and prolific artists came to 60 to 114 songs. So the median a suggested artist's song is compared with is never lower than the median of the artist's album songs, and titles that mark another version (mix, edit, version, take, and the like) are left out. Hits playlists are unchanged. With both, the top 25 suggestions for a test library came to 13 to 68 songs each, 28 in the middle, led by the artists' best-known songs.

A discography costs one request per album or EP, plus one or two for the release groups: about 20 to 35 seconds for most artists at MusicBrainz's pace. The scheduled artist job fetches the top 25 suggestions ahead of time, so their cards list the songs and the button shows the count. For any other suggestion, **Get their hits** puts the artist at the front of the job's queue, the card shows the progress, and the songs are added when the lookup finishes.

- *Alternative:* a fixed five most-listened songs from ListenBrainz's per-artist top recordings. Rejected: that endpoint needs a token, its top five are often one album's songs, and it misses charted singles that a hits playlist would pick.
- *Alternative:* the hits rules including the minimum per album. Rejected for the size reason above. The max per album can be raised on the Suggestions page for a deeper sample.

### 11. Suggestions playlists: one per artist, and one for all

Add the source `suggested` ("Suggestions"). The playlists belong to the profile that clicked, and follow the `add-profiles` ID rule (`@<profile id>` after the first profile), so two people each get their own. **Get their hits** adds the artist's picks to two playlists:

- `suggested:<artist mbid>`, named "<Artist> Hits": that artist's songs, created on the first **Get their hits** for the artist. Copying or removing one artist is copying or removing its playlist.
- `suggested:suggestions`, named "Suggestions": every artist's songs, in the order they were added. Its entries are built from the artist playlists at each refresh, so removing an artist's playlist removes its songs here too.

Songs are keyed `mb:<recording id>` and saved in `songs` with title, artist, album, length, and Cover Art Archive art from the release. `sync.fetch()` doesn't call a service for this source: an artist playlist's entries are what the user added, and the Suggestions playlist's are the union of the artist playlists. Everything after that is the existing streaming-playlist path: match to the library, search Soulseek by artist, title, and length, download, tag, write the `.m3u`, and copy to Apple Music or Spotify (`mirror.targets_for("suggested") = ["spotify", "apple"]`). A download is keyed by song, so each song downloads once, and `sync.place_download()` hardlinks it into both folders, so the second copy takes no disk space. The folders go under `SUGGESTIONS_DIR` (default `Suggestions/`), a setting like `APPLE_DIR`.

- *Alternative:* a My music playlist. Rejected, because My music playlists hold only songs already in the library and never download.
- *Alternative:* only one of the two kinds. Rejected: one playlist for all is easiest to play and copy, and one per artist is easiest to drop when an artist doesn't stick.

### 12. Routes and pages

- `GET /artists`: index (search, letter, sort) and the lookup status (a live region like the hits status).
- `GET /artists/{mbid}`: the artist page.
- `GET /artists/by-name?n=`: resolves a name to an ID and redirects, or shows the library-only page.
- `POST /artists/identify` (a library name and a pasted link, which also works for a name with no ID yet), and `POST /artists/lookup` (all, or one artist first by `mbid` or `name`).
- `GET /suggestions`, `POST /suggestions/rules`, `POST /suggestions/{mbid}/get`, `POST /suggestions/{mbid}/dismiss`, and `POST /suggestions/{mbid}/undismiss`.
- A **Artists** pill in the nav next to **My music**.

`artists.page(mbid)` builds the whole page model from the cache in one pass. Pages stay under 100 milliseconds, because nothing waits on the network.

### 13. On-demand lookups

Opening an artist with no data puts that artist at the front of the job's queue (a `priority` set in memory) and starts the job if it isn't running. The page shows a live "Looking up..." region that refreshes until the data arrives.

## Risks / Trade-offs

- **ListenBrainz Labs is experimental and may start requiring a token or change shape.** Mitigation: The client retries a 401 once with `LISTENBRAINZ_TOKEN`. A refusal stops only that step with a note that names `LASTFM_API_KEY`, and Last.fm fills in when its key is set.
- **Wikidata influence data is rich for famous artists and thin for small ones.** Mitigation: The page says when there's none, and similar artists still show. Influence counts aren't used to rank artists on their own pages.
- **SPARQL queries can time out under load.** Mitigation: Batches of 50 with a 60-second timeout, split and retried. Cached reverse influences stay until the next successful refresh.
- **A wrong name match shows the wrong artist.** Mitigation: Matching requires an album title, the method is recorded, and a pasted link overrides.
- **Bio license compliance.** Mitigation: Only CC0 and CC BY-SA text is copied, and every bio and image is shown with its credit. AllMusic and Genius are link-only.
- **Shared MusicBrainz budget with the hits lookup.** Mitigation: One process-wide pacer per host. The artist job runs after the hits job in the scheduled refresh, not at the same time.
- **Suggestion downloads.** Mitigation: Only after a click, only songs that pass the hits rules, at most five an album by default, and the button shows the count first. The existing quality rules apply (MP3, bitrate, length).
- **Discography lookups compete with the other MusicBrainz work.** Mitigation: Only the top 25 suggestions are fetched ahead of time, through the shared pacer, and refreshed every 180 days. That's about 15 minutes on the first run and little after.
- **Disk use for images.** Mitigation: One JPEG of at most 500 pixels per artist: about 50 KB each, about 35 MB for 700 artists.

## Migration Plan

1. New tables are created by `db.init()` (`CREATE TABLE IF NOT EXISTS`). No existing table changes.
2. `app/lookups.py` takes over the shared code from `app/hits.py`, which then imports it. The hits tests must pass unchanged before any artist code lands.
3. The first artist lookup runs after the next scheduled hits lookup, or when the user opens **Artists** and clicks **Look up artists now**.
4. Rollback: deploy the previous image. The new tables are ignored, and the Suggestions playlist folder stays on disk like any other playlist folder.

## Open Questions

None left.

Settled on 2026-11-13, after reviewing the top 20 suggestions for the real library (task 6.7) with the user:

- **Lean toward lesser-known artists.** The top of the list was the most famous bands of the library's genres (R.E.M., The Strokes, Coldplay), each a match for 200 to 370 of its artists. Dampening by how many library artists point at a band barely changed it, because those bands really are the strongest matches. So the ranking divides each match by `(max(listeners, 5000) / 5000) ** 0.75`, from ListenBrainz's artist listener counts, fetched for the best 500 matches per profile. The new top of the list is artists such as KEN mode, Conan, Sumac, and Full of Hell.
- **At most 20 songs an artist** by default, the strongest of those that pass, editable on the Suggestions page. Before, the top suggestions added 14 to 64 songs each.
- **Genius views need listeners too** for a suggested artist's song: they count only when the song's listener ratio is at least 0.5. Songs such as one that Genius readers favor but few people play no longer pass on views alone.
- **No extra weight for the songs hits playlists picked.** The ranking was driven by breadth, and favorites already count double, so extra weight would only push it further toward the same famous bands.

Settled on 2026-11-12:

- **Get their hits** adds the songs that pass the hits rules, with no minimum per album (decision 10).
- There's a playlist for each suggested artist and one Suggestions playlist with all of them (decision 11).
- If ListenBrainz Labs refuses even with the ListenBrainz token, similar artists need `LASTFM_API_KEY` (decision 8).
