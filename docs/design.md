# Design

soulseek-playlist-sync is one Python process: a FastAPI web app plus a background worker, with its state in SQLite. It talks to Apple Music, Spotify, slskd, and optionally YouTube, and writes only inside its playlist folders. Playlists come from Apple Music, Spotify, YouTube, or your own music (the `local` source), and any of them can be copied to a streaming service.

```
browser --> web app (FastAPI, Jinja templates, SQLite) --> Apple Music API
                |                                      --> Spotify Web API
                |-- worker thread --> slskd REST API --> Soulseek
                |                 --> yt-dlp --> YouTube (optional)
                '-- MUSIC_ROOT: playlist folders, plus your library for matching
```

## Modules

| Module | Job |
|---|---|
| `app/web.py` | Pages and form actions. |
| `app/sync.py` | Fetches a playlist, matches each song to a local file, places files, renumbers, writes the `.m3u`, and builds the status rows. |
| `app/worker.py` | Soulseek searches (one at a time, spaced out), downloads (several at once), and YouTube jobs (one at a time). |
| `app/apple.py` | Apple Music client: a self-signed developer token (option B) or the web player's token (option A). |
| `app/spotify.py` | Spotify client: PKCE sign-in, playlists, Liked Songs. |
| `app/slskd.py` | slskd REST client. |
| `app/youtube.py` | YouTube playlists as a source (read with yt-dlp, no sign-in; video titles parsed into artist and song), and the YouTube fallback: YouTube Music and YouTube search, ranking, and download through yt-dlp. |
| `app/mirror.py` | Copies a playlist to a streaming service: matches songs, creates or updates the copy, and takes a random sample when a playlist is over Spotify's 10,000-song limit. |
| `app/favorites.py` | Favorites sync: reads Spotify Liked Songs and Apple Music Favorite Songs, matches each new song on the other service, and adds it there. Only adds, and remembers each pair. |
| `app/export.py` | Streams a playlist as a ZIP without building it on disk. Checks the size first (asks above 2 GB, refuses above 20 GB) and records progress for the page. |
| `app/hits.py` | Hits playlists. `research()` runs in the background and fills the `hit_*` tables from ListenBrainz, MusicBrainz, Genius, the Billboard Hot 100 history, and your favorites, fetching only what's missing or stale. `plan()` picks each album's songs from those tables without any requests, so a rebuild is quick. |
| `app/match.py` | Text normalization and Soulseek candidate ranking. Pure functions. |
| `app/library.py` | Index of the music already on disk (incremental, by size and modification time), plus the queries behind **My music** and auto-updating playlists. |
| `app/art.py` | Cover thumbnails for library files, from embedded art or a `cover.jpg` next to the file, cached under `DATA_DIR/artwork/local`. A playlist's mosaic cover takes one album each from its first artists with art (`sync.covers`), checking at most 40 local albums per call. |
| `app/folders.py` | Safe file names, hardlink or copy, two-pass renames, `.m3u` writing. |
| `app/tagger.py` | ID3v2.3 tags and cover art. |
| `app/settings.py` | Settings page values, stored in SQLite over the environment defaults. |
| `app/profiles.py` | Profiles: optional scrypt-hashed passwords, the admin rule, removal, and the signed cookie that remembers a browser's profile. |

## Data

- `profiles`: one row per person: name, optional password hash, whether they're an admin, the folder their new playlists go in, and a session version that's raised to sign every browser out.
- `playlists`: one row per tracked playlist, with its owner, folder, and options. A `local` playlist with a `rule` (a JSON filter of words, folders, artist, and album) is rebuilt from the library on every refresh; one without a rule is a fixed list.
- `songs`: one row per song, keyed by source and ID (`apple:<catalog id>`, `spotify:<track id>`, `youtube:<video id>`, `local:<hash>`). A local song's key is a hash of its normalized artist and title, so the same song in two albums or folders is one song.
- `entries`: the playlist's order, one row per position. A song can appear more than once.
- `items`: what exists locally for each song in each playlist (file name, origin, skipped, wanted). In a My music playlist that keeps itself up to date, a skipped song is one you took out: it stays out of the `.m3u`, the ZIP, copies, and refreshes.
- `downloads`: one row per song, shared by every playlist that has it: the Soulseek or YouTube search, candidates, transfer progress, and the finished file.
- `library`: the index of files under the library folders, with their tags, song key, cover key (folder plus album), and a normalized search column. It also keeps the MusicBrainz recording, artist, and release group IDs and the album type that taggers such as Picard write.
- `mirrors`: a playlist's copy on the other service, with how many songs matched and which didn't.
- `song_links`: each song's match on the other service, so later updates don't search again. Songs that weren't found are looked for again after a week.
- `favorite_pairs`: each Spotify track and Apple Music song a profile's favorites sync has seen as the same song. A paired song is never added again, so a favorite you remove stays removed.
- `hit_ids`, `hit_recordings`, `hit_artist_songs`, `hit_artists`, `hit_charts`, `hit_favorites`: what hits playlists know about your songs. These hold MusicBrainz IDs looked up for files without them, ListenBrainz listeners per recording, per-artist lists (listeners per title, singles with their year, Genius page views), when each artist was last fetched, the Hot 100 history reduced to each song's peak and weeks, and the song keys of each profile's favorites. A hits playlist's `rule` is a filter plus a `hits` object with `min`, `max`, and `cutoff`.

A song's status on the playlist page comes from its item and its download row: synced, in library, waiting, searching, found, queued, downloading, not found, failed, removed upstream, or skipped.

## Profiles

Every request goes through one middleware, `gate`. It checks `APP_PASSWORD` when that's set, refuses a POST that a browser says came from another site (`Sec-Fetch-Site: cross-site`, or an `Origin` that isn't this host), and then finds the profile. The profile comes from the `profile` cookie, which holds the profile ID, its session version, and when it was issued, signed with HMAC-SHA256 under a key in `DATA_DIR/session-key`. Without a valid cookie, the only profile is used if it has no password; otherwise a page goes to `/signin`, and a live region gets a 401 with an `X-Signin` header so `poll.js` moves the whole page there. The profile is kept in a context variable for the request, so `C()` returns settings with that profile's connections.

- **Connections:** `Config.profile` picks the token files. Profile 1 keeps `DATA_DIR/spotify-token.json` and `DATA_DIR/apple-music-user-token`; any other profile uses `DATA_DIR/profiles/<id>/`. The Spotify sign-in state, the Apple Music connect nonce, and the favorites options and status are stored per profile too.
- **Ownership:** every route under `/playlists/{pid}` loads the playlist through `_playlist(pid, write)`. Anyone can read a playlist; a write by anyone but the owner answers 403. A test walks every POST route under `/playlists/{pid}` to keep it that way.
- **Owner's connections for background work:** `sync.fetch()`, copies (`mirror.run()`), and hits picks use `cfg.for_profile(owner)`, whoever started the job.
- **IDs:** profile 1 keeps `<source>:<remote id>`. Other profiles' playlists are `<source>:<remote id>@<profile id>`, so two people can add the same service playlist.
- **Rolling back** to a version from before profiles: remove the playlists of profiles after the first, or turn off their copies, first. An older version ignores `owner`, so it would read and write those playlists through the first profile's accounts.

## Matching rules

- **Local files**: same ISRC, or same normalized title, a matching artist, and a length within the tolerance. Only MP3s are used for playlists from the streaming services; playlists made from your own music use whatever format the file is.
- **Copies**: by ISRC, else by title, artist, and a length within 5 seconds, preferring the same explicit or clean version. In a censored title (`F**k`, `S*%t`), each mark stands for one letter, so it matches the uncensored title. A word ending in `in` matches the same word ending in `ing`, a `(Bonus Track)` tag is ignored, and two credits that share a name (`JAY-Z & Ye`, `JAY-Z & Kanye West`) count as the same artist. Songs that still aren't found can be linked by hand from the not-found page: the pick goes into `song_links` like a match, so later updates keep it, and an Apple Music copy gets it right away. Searches are spaced out for big playlists, and a long rate-limit pause stops the copy with what it has matched saved.
- **Soulseek**: MP3 only, at least the minimum bitrate, length within the tolerance, and every word of the core title (without "feat." and bracketed notes) in the file name. Ranked by bitrate, free upload slot, queue length, speed, and length difference. One candidate per peer, so a failing peer doesn't use up every fallback.
- **Favorites**: first against the other service's favorites, by ISRC or normalized title and primary artist, so a song already favorited on both sides (even as another version) isn't added again; then like a copy, through `song_links`. On Apple Music, a song is favorited by adding it to the library and setting its library rating to 1 (`PUT /v1/me/ratings/library-songs/{id}`); the catalog rating is a separate, older "love". On Spotify, songs are saved with `PUT /me/library`.
- **YouTube**: YouTube Music's song search first, then general YouTube search. Length within 6 seconds, every word of the core title in the video title, and no words such as live, cover, remix, sped up, or clip unless the wanted title has them. "Artist - Topic" channels rank first. If nothing passes, a second pass allows up to about 20 seconds of difference, but only when the artist is named in the title or channel; those files are labeled with the difference.

## Hits playlists

`hits.plan()` groups the filter's files by album (album artist plus album), and gives each song one home album: an album before an EP, soundtrack, single, compilation, or live album, then the earliest. Skits, interludes, songs under 90 seconds, and live, demo, and remix versions are out unless they charted or are favorites. A live album counts as an album only when it's all the artist has in the selection.

For each album, listeners and page views are divided by the album's median among the songs that are in. An album needs counts for at least half its songs and a median of at least 30 listeners (300 page views) to be compared. A song's ListenBrainz count is the highest of its recordings' counts and the artist's count for that title, because listens collect on one version of a recording and a file can be tagged with another. A song's score is its higher ratio, plus 0.5 to 1.0 for a Hot 100 entry (more for the top 40 and top 10), and 0.15 for a single. A single counts when it came out within two years of the album. Live, remix, and DJ-mix singles don't count, and of a single with more than two songs only the first does.

Favorites go in first. Albums, EPs, and soundtracks then fill up to `min` by score. Every kind then adds songs that stand out (charted, a single, a favorite, or a ratio of at least `cutoff`) until `max`. A song you leave out is skipped when picking, so the next best takes its place, and it stays listed as left out. Requests go out at one a second to MusicBrainz, within ListenBrainz's rate-limit headers, and at four a second to Genius, stopping 50 short of its daily allowance.

## Files on disk

- Playlist folders live at `MUSIC_ROOT/<AppleMusic, SpotifyMusic, YouTube, or Playlists>/<profile folder>/<playlist name>/`, named for FAT32 and exFAT compatibility. The first profile's folder is empty, so its playlists sit straight in the service folder.
- If a library folder is missing or empty during a scan (for example, a disk that didn't mount), the index keeps what it had there instead of treating every song as deleted. A copy is never replaced with an empty playlist.
- Files are numbered by their first position in the playlist. Renumbering uses a two-pass rename, so swaps don't collide.
- A song used by several playlists is hardlinked into each folder when they're on the same filesystem.
- Finished downloads are moved out of slskd's download folder right away.
- **Prune removed** deletes only files the app created, for songs that left the playlist.

## Releases

The image is published to the GitHub container registry from a maintainer's machine, not from hosted CI. `scripts/publish-image.sh` builds the current commit, after it's on GitHub, for amd64 and arm64 (under emulation, or natively on an ARM machine over SSH) and pushes both under `latest` and the commit's short hash. `docker-compose.yml` keeps `build: .`, so `docker compose up -d --build` builds the same image from a checkout.
