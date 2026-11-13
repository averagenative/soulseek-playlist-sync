## Why

A big library is a list of names: the app can play an artist's songs, but it can't say who they are, who shaped them, or what to listen to next. Open sources (MusicBrainz, Wikidata, Wikipedia, ListenBrainz, Discogs, Last.fm) hold that information, and the files already carry MusicBrainz artist IDs that tie each artist to it. The hits playlist work added the background lookup and cache pattern this needs, so artist pages can reuse it instead of starting over.

## What Changes

- Add an **Artists** page that lists every artist in the library, with search, and an **artist page** for each one, in the style of AllMusic:
  - A bio, with its source, a link to it, and its license.
  - Facts: type (person or group), years active, origin, genres, and members with their years and instruments. A member who is in another library band links to it.
  - **Influenced by** and **Influenced**: both directions, with artists you own linked to their pages.
  - **Similar artists**, split into the ones in your library and the ones that aren't.
  - **Your albums** by the artist, with each album's hits picks marked and a link to play or browse them.
  - Links out to the artist on MusicBrainz, Wikipedia, AllMusic, Discogs, Last.fm, and the official site, when known.
  - An artist image, with credit when the license asks for it.
- Add a background lookup, like the hits lookup, that fills an artist cache from the sources above. It fetches only what's missing or stale, keeps going when one source refuses, and says why on the page.
- Add **Suggestions**: artists you don't have that many of your artists are similar to or were influenced by, each with the reasons and its hits. **Get their hits** picks the artist's songs with the hits playlist rules (charted, singles, and standouts on each album) and adds them to a playlist for that artist and to one **Suggestions** playlist for all of them, which download through Soulseek like any other playlist. **Not interested** hides a suggestion.
- Link artist names to their pages across the app: My music, playlist song rows, and the hits picks page.
- Add optional keys: `LASTFM_API_KEY` (bios, tags, similar artists), `DISCOGS_TOKEN` (a higher Discogs rate limit), and `THEAUDIODB_API_KEY` (artist images and bios beyond the free tier). Every feature works without them, with fewer sources.

## Capabilities

### New Capabilities

- `artist-data`: Identifying the artists in the library, fetching their facts, bios, relationships, similar artists, and images from open sources, caching the results with refresh times, respecting each source's rate limits and terms, and recording attribution.
- `artist-pages`: The Artists index and the artist page: what each shows, how sources are combined and credited, and how artist names link to it from the rest of the app.
- `artist-suggestions`: Ranking artists that aren't in the library from similarity and influence data, explaining why each is suggested, hiding dismissed ones, and turning a suggestion's best-known songs into Soulseek downloads through a Suggestions playlist.

### Modified Capabilities

None. The repository has no specs yet. The hits lookup is reused at the code level (its MusicBrainz artist IDs, listener counts, singles, and per-album picking rules), without changing its behavior.

## Impact

- **New code:** `app/artists.py` (lookups, cache, and the page model), `app/templates/artists.html`, `app/templates/artist.html`, and `app/templates/suggestions.html`.
- **Changed code:** `app/db.py` (artist cache tables), `app/web.py` (routes), `app/sync.py` and `app/worker.py` (a playlist source for songs that aren't on a streaming service yet), `app/library.py` (artist lists), `app/config.py` (optional keys), and the templates that show artist names.
- **Network:** new requests to Wikidata (including its SPARQL endpoint), Wikipedia, Wikimedia Commons, ListenBrainz Labs, Discogs, and optionally Last.fm and TheAudioDB. Each is paced to its published limits and identified by the app's User-Agent. Requests carry artist IDs and names only.
- **Storage:** a few megabytes of text per thousand artists, plus artist images cached under `DATA_DIR/artwork/artists`.
- **Dependencies:** none new. `httpx` covers every source.
- **Docs:** README (artist pages, suggestions, keys), `docs/design.md`, and `.env.example`.
- **Other changes:** builds on `add-profiles`. Artist pages are shared, and suggestions, dismissals, and the Suggestions playlists belong to each profile.
