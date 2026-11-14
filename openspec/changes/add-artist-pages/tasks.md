## 1. Shared lookup layer

- [x] 1.1 Create `app/lookups.py` with `StepStopped`, the step runner, status helpers, and an HTTP client that retries 429 and 5xx with `Retry-After` and stops a step on 401 or 403
- [x] 1.2 Add a process-wide `Pacer` per host (MusicBrainz 1 a second, Discogs 25 or 60 a minute, Wikidata and Wikipedia polite pacing, Last.fm 4 a second, ListenBrainz by its rate-limit headers) and use it from every client
- [x] 1.3 Move `app/hits.py` onto `app/lookups.py` with no behavior change, and confirm the hits tests pass unchanged
- [x] 1.4 Add a test that two jobs sharing the MusicBrainz pacer stay at or below one request a second
- [x] 1.5 Move the per-album picking out of `hits.plan()` into `hits.pick_album(songs, kind, opts)`, with `hits.signals()` reading each song's numbers from the cache, and confirm the hits tests pass unchanged

## 2. Source clients

- [x] 2.1 `musicbrainz.artist(mbid)`: type, area, begin area, life span, genres, aliases, memberships (years and instruments), and URL relations, mapped to link kinds
- [x] 2.2 `musicbrainz.find_artist(name, album)`: release group search that accepts only a same-named artist credit
- [x] 2.3 `wikidata.entities(ids)`: batches of 50, returning P737, P136, P571, P576, P740, P18, P434, labels, and the enwiki title
- [x] 2.4 `wikidata.influenced(qids)`: reverse P737 SPARQL in batches of 50, splitting a batch in half on timeout
- [x] 2.5 `wikipedia.intros(titles)`: plain-text introductions, 20 titles a request, following redirects
- [x] 2.6 `commons.images(files)`: thumbnail URL, license, author, and attribution flag, 50 files a request
- [x] 2.7 `lb_labs.similar(mbid)`: scored similar artists, normalized to 0 to 1. On a 401, ask once more with `LISTENBRAINZ_TOKEN` when it's set, and keep the token for the run if it works; otherwise stop the step with a reason that names `LASTFM_API_KEY` and links to https://www.last.fm/api/account/create
- [x] 2.8 `discogs.artist(id)`: profile with markup turned into plain text, members, and groups; sends `DISCOGS_TOKEN` when set
- [x] 2.9 `lastfm.info(mbid or name)` and `lastfm.similar(...)`, used only when `LASTFM_API_KEY` is set
- [x] 2.10 `audiodb.artist(mbid)`: image URL, plus the bio only with `THEAUDIODB_API_KEY`
- [x] 2.11 `musicbrainz.discography(mbid)`: album, EP, and single release groups (no secondary type but Soundtrack), and for each album and EP the earliest official release's tracklist from one `release?release-group=...&inc=recordings+media` browse
- [x] 2.12 Record one real response per source as a test fixture (Nirvana and one small artist), with no personal data, and unit-test every client against them

## 3. Data model and config

- [x] 3.1 Add `artist_names`, `artists`, `artist_bios`, `artist_links`, `suggestion_dismissed`, `artist_releases`, and `artist_tracks` to `db.SCHEMA`
- [x] 3.2 Add `LASTFM_API_KEY`, `DISCOGS_TOKEN`, `THEAUDIODB_API_KEY` (each with a `_FILE` form), and `SUGGESTIONS_DIR` to `app/config.py`, `.env.example`, and `docker-compose.yml`
- [x] 3.3 Add `library.artists(cfg, q, letter, sort)`: one row per primary artist with album and song counts and its MusicBrainz ID, skipping Various Artists

## 4. Artist lookup job

- [x] 4.1 `app/artists.py` `research()`: steps in order (name resolution, MusicBrainz facts, Wikidata, reverse influences, Wikipedia, Commons images, ListenBrainz Labs, Discogs, Last.fm, TheAudioDB), each fetching only stale or missing data per the refresh times
- [x] 4.2 Save images resized to at most 500 pixels wide under `DATA_DIR/artwork/artists`, with credit JSON; fetch again only when the Wikidata image file changes
- [x] 4.3 Store related artists with `other_key` rules (MusicBrainz ID, else Wikidata ID, else name) and compute the local inverse for influences between library artists
- [x] 4.4 Priority queue: `artists.lookup_first(mbid)` puts one artist ahead of the rest and starts the job if it's idle
- [x] 4.5 Run the artist job after the hits job in the scheduled refresh, never at the same time
- [x] 4.6 Tests with fake clients: stale versus fresh skipping, a refused source stopping only its step, name resolution (match, miss, retry after 30 days, manual wins), and the local inverse

## 5. Artist pages

- [x] 5.1 `artists.page(mbid)`: the page model from the cache only (facts, chosen bio and alternatives, members, influences, similar, library albums with hits picks, links, sources)
- [x] 5.2 `GET /artists` with search, letter, sort, and a live lookup-status region with **Look up artists now**
- [x] 5.3 `GET /artists/{mbid}` with the bio and its credit, other bios, facts, members, influences, similar artists, albums, links out, and a Sources section
- [x] 5.4 `GET /artists/by-name?n=`, the library-only page, and `POST /artists/identify` (the library name and a pasted MusicBrainz link)
- [x] 5.5 Link artist names on My music, playlist song rows, and the hits picks page; add an **Artists** pill to the nav
- [x] 5.6 Styles for the artist page that work at phone width in all six themes
- [x] 5.7 Page tests: a full artist, an artist with no data, an artist with no influences, the credit under each bio, and in-library linking

## 6. Suggestions

- [x] 6.1 `artists.suggestions(profile)`: the scoring from the design with that profile's favorites, excluding library artists and the profile's dismissed ones, top 100 with the top five reasons
- [x] 6.2 Add a "Finding suggested artists' hits" step to the artist job: for the top 25 suggestions with no fresh discography, fetch it (2.12), then their listener counts, singles, and Genius views into the hits tables
- [x] 6.3 `artists.suggested_hits(mbid, rules)`: each song once on its earliest album, `hits.pick_album()` with min 0 and the Suggestions rules (`kv` key `suggestions_rules`: max per album 5, cutoff 1.25), minus songs in the library
- [x] 6.4 `GET /suggestions` with images, genres, bio excerpts with credit, reasons, the picked songs with why each was picked, and **Get their hits (N songs)**; the rules form (`POST /suggestions/rules`); plus the two suggestion lists on artist pages
- [x] 6.5 **Not interested** and **Suggest again** (`suggestion_dismissed`), with a list of dismissed artists
- [x] 6.6 Tests: ranking (several artists pointing at one, favorites weighting, already-owned artists dropped), reasons text, dismissals, and the hits rules for a suggested artist (charted, single, standout, deluxe bonus tracks ignored, nothing passes)
- [x] 6.7 Print the top 20 suggestions for the real library with their reasons and picked songs, review them with the user, and decide whether the hits playlist's picks need extra weight. Decided: no extra weight; lean toward lesser-known artists by ListenBrainz listeners; at most 20 songs an artist; Genius views need listeners too

## 7. Suggestions playlist

- [x] 7.1 Add the `suggested` source to `sync.SOURCES`, and have `sync.fetch()` skip the service call for it: an artist playlist keeps its entries, and `suggested:suggestions` rebuilds its entries from every `suggested:<mbid>` playlist in the order they were added
- [x] 7.2 Create `suggested:<mbid>` ("<Artist> Hits") and `suggested:suggestions` ("Suggestions") on first use, owned by the signed-in profile with its ID suffix and folder, under `SUGGESTIONS_DIR`
- [x] 7.3 Save `mb:<recording id>` songs with title, artist, album, length, and Cover Art Archive art, and skip songs already in the playlist or the library
- [x] 7.4 `POST /suggestions/{mbid}/get` adds the picks to both playlists and starts **Download all** for both; for an artist not looked up yet, it queues the artist first and adds the songs when the lookup finishes
- [x] 7.5 Allow copying both kinds of playlist to Apple Music and Spotify (`mirror.targets_for`)
- [x] 7.6 Tests: adding hits to both playlists, one download placed in both folders, removing an artist playlist drops its songs from Suggestions, skipping owned songs, the worker's Soulseek search for `mb:` songs, and the playlist pages

## 8. Docs and release

- [x] 8.1 README: Artists and artist pages, Suggestions and **Get their hits**, the new keys, what each source sends and its license
- [x] 8.2 `docs/design.md`: modules, tables, the lookup layer, and the Suggestions source; move the artist pages entry out of `docs/ideas.md`
- [x] 8.3 Check the pages in a running instance with screenshots at desktop and phone widths, then commit, push, and publish the image
