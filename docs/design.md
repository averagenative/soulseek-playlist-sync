# Design

soulseek-playlist-sync is one Python process: a FastAPI web app plus a background worker, with its state in SQLite. It talks to Apple Music, Spotify, slskd, and optionally YouTube, and writes only inside its playlist folders.

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
| `app/youtube.py` | YouTube Music and YouTube search, ranking, and download through yt-dlp. |
| `app/mirror.py` | Copies a playlist to the other service: matches songs, creates or updates the copy. |
| `app/export.py` | Streams a playlist as a ZIP without building it on disk. |
| `app/match.py` | Text normalization and Soulseek candidate ranking. Pure functions. |
| `app/library.py` | Index of the music already on disk (incremental, by size and modification time). |
| `app/folders.py` | Safe file names, hardlink or copy, two-pass renames, `.m3u` writing. |
| `app/tagger.py` | ID3v2.3 tags and cover art. |
| `app/settings.py` | Settings page values, stored in SQLite over the environment defaults. |

## Data

- `playlists`: one row per tracked playlist, with its folder and options.
- `songs`: one row per song, keyed by source and ID (`apple:<catalog id>`, `spotify:<track id>`).
- `entries`: the playlist's order, one row per position. A song can appear more than once.
- `items`: what exists locally for each song in each playlist (file name, origin, skipped, wanted).
- `downloads`: one row per song, shared by every playlist that has it: the Soulseek or YouTube search, candidates, transfer progress, and the finished file.
- `library`: the index of files under the library folders.
- `mirrors`: a playlist's copy on the other service, with how many songs matched and which didn't.
- `song_links`: each song's match on the other service, so later updates don't search again. Songs that weren't found are looked for again after a week.

A song's status on the playlist page comes from its item and its download row: synced, in library, waiting, searching, found, queued, downloading, not found, failed, removed upstream, or skipped.

## Matching rules

- **Local files**: same ISRC, or same normalized title, a matching artist, and a length within the tolerance. Only MP3s are used for playlist folders.
- **Soulseek**: MP3 only, at least the minimum bitrate, length within the tolerance, and every word of the core title (without "feat." and bracketed notes) in the file name. Ranked by bitrate, free upload slot, queue length, speed, and length difference. One candidate per peer, so a failing peer doesn't use up every fallback.
- **YouTube**: YouTube Music's song search first, then general YouTube search. Length within 6 seconds, every word of the core title in the video title, and no words such as live, cover, remix, sped up, or clip unless the wanted title has them. "Artist - Topic" channels rank first. If nothing passes, a second pass allows up to about 20 seconds of difference, but only when the artist is named in the title or channel; those files are labeled with the difference.

## Files on disk

- Playlist folders live at `MUSIC_ROOT/<AppleMusic or SpotifyMusic>/<playlist name>/`, named for FAT32 and exFAT compatibility.
- Files are numbered by their first position in the playlist. Renumbering uses a two-pass rename, so swaps don't collide.
- A song used by several playlists is hardlinked into each folder when they're on the same filesystem.
- Finished downloads are moved out of slskd's download folder right away.
- **Prune removed** deletes only files the app created, for songs that left the playlist.
