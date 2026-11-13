# Feature ideas

Ideas for later, with enough detail to start from. Nothing here is built yet.

## Artist pages: bios, influences, and suggestions

A local, AllMusic-style page for each artist in your library, built from open sources and cached like the hits playlist data, so it works offline and loads fast.

Each artist page would show the following:

- **Bio**, with where it came from and a link to the source.
- **Facts:** years active, origin, members (with links between bands that share members), and genres.
- **Influenced by, and influenced:** both directions, with artists you own linked to their pages.
- **Similar artists**, split into the ones in your library and the ones that aren't.
- **Your albums** by the artist, with each album's hits picks marked.

Suggestions would come from the same data: artists similar to the ones you play most, or that influenced them, that aren't in your library yet. Each suggestion could offer the artist's hits (from the singles and chart data) to download through Soulseek, like a playlist.

### Sources

| Source | Key | Gives |
|---|---|---|
| MusicBrainz | none | Members, band relationships, and links to the artist's Wikidata, Wikipedia, Discogs, and AllMusic entries. The hub that ties the other sources to one artist ID. |
| Wikidata | none | Influenced by (property P737), genres, origin, and formation and breakup dates. |
| Wikipedia | none | The bio: the article's summary from the REST API. |
| Last.fm | API key | A short bio, tags, and similar artists with a match score. |
| ListenBrainz | none | Similar artists from listening data. |
| Discogs | token | Profile, members, and aliases, strongest for underground and independent artists. |
| Genius | token | Artist descriptions, strongest for hip-hop. |
| TheAudioDB | free key | Bios, artist images, and styles. |

AllMusic has no public API, and its terms don't allow scraping, so AllMusic links would open the site rather than copy its text.

### Notes

- **Attribution.** Wikipedia and Last.fm bios are licensed CC BY-SA, so each needs its source, a link, and the license shown next to it.
- **Matching.** Use the artist's MusicBrainz ID from the file tags (or the hits lookups), then follow MusicBrainz's links to the other sources. Searching by name confuses artists who share one.
- **Caching.** Keep an `artist_info` table keyed by MusicBrainz artist ID with a fetched time. Refresh bios rarely (every six months) and similar artists more often. Store images under `DATA_DIR/artwork/artists`.
- **Influence graph.** Wikidata's influence data is uneven: rich for well-known artists, thin for small ones. Similar-artist data from Last.fm and ListenBrainz fills the gaps, and should be labeled as "similar", not "influenced by".

## Hits playlist follow-ups

- **Last.fm listener counts** as a third popularity signal, from `artist.getTopTracks` (one request per artist).
- **AcoustID** fingerprints, to identify songs whose tags are wrong or missing. It needs the `fpcalc` tool in the image and reads each whole file, so it'd be the last lookup to try.
- **ListenBrainz batch lookups** (`POST /1/metadata/lookup/`, with a user token) to identify songs without MusicBrainz IDs faster than one MusicBrainz search a second.
- **Missing hits:** list each artist's charted songs and singles that aren't in your library, and offer to download them through Soulseek.
- **Whole albums from Apple Music.** The **Whole album** button works in this app. To do the same while listening in the Music app, add the song to an Apple Music playlist with a set name, such as "Whole albums". At its next refresh, the hits playlist reads that playlist and adds each song's album whole. A Spotify playlist could work the same way.
- **Plays and skips from an MP3 player.** Players running Rockbox can write a `.scrobbler.log` that marks each song as listened to or skipped. Reading it would let skips push filler out of the hits playlist.
