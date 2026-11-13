## ADDED Requirements

### Requirement: Identify each library artist
The system SHALL identify each artist in the library by MusicBrainz artist ID. It SHALL take the ID from the files' tags when they carry one, and otherwise look the artist up on MusicBrainz by name together with the title of one of the artist's albums in the library. It SHALL record how each ID was found and when, and SHALL NOT look a name up again within 30 days of a lookup that found nothing.

#### Scenario: ID from the tags
- **WHEN** a library file by "Nirvana" carries the MusicBrainz artist ID `5b11f4ce-a62d-471e-81fc-a69a8278c7da`
- **THEN** the artist "Nirvana" is identified by that ID without any request

#### Scenario: ID found by name and album
- **WHEN** an artist's files carry no MusicBrainz ID, and a MusicBrainz release group search for one of the artist's album titles returns a release group credited to an artist with the same name
- **THEN** the system records that artist's ID with the method "search"

#### Scenario: Name that matches nothing
- **WHEN** the MusicBrainz search finds no release group by an artist with that name
- **THEN** the system records that nothing was found, and the artist's page shows only the library's albums
- **AND** the name is not searched again for 30 days

#### Scenario: Collaborations and Various Artists
- **WHEN** a song's artist is "JAY-Z & Kanye West" or the album artist is "Various Artists"
- **THEN** the system identifies the first credited artist for the song, and creates no artist page for "Various Artists"

### Requirement: Correct an artist's identity by hand
The system SHALL let the user set or replace an artist's MusicBrainz ID by pasting a MusicBrainz artist link, and SHALL keep a hand-set ID over any found by search.

#### Scenario: Paste the right artist
- **WHEN** the user pastes `https://musicbrainz.org/artist/<id>` on an artist page that has the wrong artist or none
- **THEN** the page uses that artist's data at the next lookup, and later lookups do not replace it

### Requirement: Fetch artist facts from MusicBrainz
For each identified artist, the system SHALL fetch from MusicBrainz the artist's type, area, begin area, life span, genres, aliases, band membership relations with their dates and instruments, and URL relations (Wikidata, Wikipedia, AllMusic, Discogs, Last.fm, official homepage, and others).

#### Scenario: A band's members
- **WHEN** the system fetches Nirvana
- **THEN** it stores each member with the years they were in the band and their instruments, and stores the band's links to Wikidata, AllMusic, Discogs, and Last.fm

### Requirement: Fetch facts and influences from Wikidata
For each artist with a Wikidata link, the system SHALL fetch the artist's "influenced by" statements (P737), genres (P136), inception (P571), dissolution (P576), place of formation (P740), image (P18), and its English Wikipedia article title. It SHALL find the artists each library artist influenced with batched SPARQL queries for the reverse of P737. It SHALL resolve every related Wikidata item to a name and, when the item has one, a MusicBrainz artist ID (P434), in batches of up to 50.

#### Scenario: Influences both ways
- **WHEN** the system fetches Nirvana's Wikidata item
- **THEN** it stores Pixies, Sonic Youth, and The Stooges as "influenced by" with their MusicBrainz IDs, and the artists whose items name Nirvana under P737 as "influenced"

#### Scenario: An influence between two library artists
- **WHEN** artist A's item says A was influenced by artist B, and both are in the library
- **THEN** B's page lists A under "Influenced" even when the SPARQL query didn't return A

### Requirement: Fetch bios with their license
The system SHALL fetch an English bio from each available source, in this order of preference: the Wikipedia article's introduction (as plain text), the Last.fm bio (when `LASTFM_API_KEY` is set), the Discogs profile (with its link markup turned into plain names), and the TheAudioDB bio (when it returns one). It SHALL store each bio with its source, a link to the source page, and the source's license, and SHALL NOT copy bio text from sources whose terms don't allow reuse, including AllMusic and Genius.

#### Scenario: Wikipedia first
- **WHEN** an artist has an English Wikipedia article
- **THEN** the stored bio is the article's introduction, credited to Wikipedia under CC BY-SA 4.0 with a link to the article

#### Scenario: No Wikipedia article
- **WHEN** an artist has no English Wikipedia article and `LASTFM_API_KEY` is set
- **THEN** the system stores the Last.fm bio, credited to Last.fm under CC BY-SA with a link to the artist's Last.fm page

#### Scenario: Discogs markup
- **WHEN** a Discogs profile contains `[a=Kurt Cobain]` or `[a12345]` links
- **THEN** the stored text shows the artist's name in their place

### Requirement: Fetch similar artists
The system SHALL fetch similar artists from ListenBrainz Labs and, when `LASTFM_API_KEY` is set, from Last.fm. It SHALL ask ListenBrainz Labs without a token, and only when Labs answers 401 SHALL it ask once more with `LISTENBRAINZ_TOKEN` when that's set. It SHALL store each similar artist with its name, MusicBrainz ID when known, source, and a score between 0 and 1 normalized within that source's list.

#### Scenario: Two sources
- **WHEN** ListenBrainz and Last.fm both list Radiohead as similar to Nirvana
- **THEN** the system stores both entries, and the page uses the higher of the two scores

#### Scenario: ListenBrainz Labs asks for a token
- **WHEN** ListenBrainz Labs answers 401 and `LISTENBRAINZ_TOKEN` is set
- **THEN** the system asks again once with the token, and keeps sending it for the rest of the run if Labs accepts it

#### Scenario: No similarity source left
- **WHEN** ListenBrainz Labs refuses with and without the token, and `LASTFM_API_KEY` isn't set
- **THEN** the lookup status says similar artists need a Last.fm API key and links to the page that creates one, and suggestions use influence links only

### Requirement: Fetch a suggested artist's albums
For a suggested artist, the system SHALL fetch from MusicBrainz the artist's album, EP, and single release groups, skipping compilations, live albums, remixes, DJ mixes, and demos. For each album and EP, it SHALL store the tracklist of its earliest official release, with each track's recording ID, title, and length. It SHALL fetch the listener counts of those recordings from ListenBrainz, the artist's singles from MusicBrainz, and the artist's Genius page views when `GENIUS_API_TOKEN` is set, into the caches the hits lookup uses. It SHALL do this in the scheduled lookup for the top 25 suggestions, and for any other suggestion when the user clicks **Get their hits**, and SHALL refresh it after 180 days.

#### Scenario: Top suggestions fetched ahead
- **WHEN** the scheduled artist lookup finishes and Melvins is among the top 25 suggestions
- **THEN** Melvins's albums, tracklists, and listener counts are cached, and the Melvins suggestion lists the songs that pass the hits rules without another lookup

### Requirement: Fetch an artist image with credit
The system SHALL fetch one image per artist: the Wikidata image from Wikimedia Commons when there is one, with its license and author, and otherwise the TheAudioDB artist image. It SHALL save the image resized to at most 500 pixels wide under `DATA_DIR/artwork/artists`.

#### Scenario: A Commons image
- **WHEN** the artist's Wikidata image is a Commons file licensed CC BY-SA 2.0 by "P.B. Rage"
- **THEN** the system saves the image and stores its credit as "P.B. Rage, CC BY-SA 2.0" with a link to the file's Commons page

### Requirement: Cache with refresh times
The system SHALL keep everything it fetches in the database and SHALL serve artist pages from that cache without making requests while the page loads. It SHALL refresh each kind of data when it's older than its refresh time: 180 days for facts, influences, and bios, 60 days for similar artists, and never for images unless the source image changes.

#### Scenario: A page for a cached artist
- **WHEN** the user opens an artist page whose data is 20 days old
- **THEN** the page loads from the cache and the system makes no request

#### Scenario: Stale data
- **WHEN** the scheduled lookup runs and an artist's similar artists are 61 days old
- **THEN** the system fetches that artist's similar artists again, and leaves its 61-day-old bio alone

### Requirement: Look up artists in the background
The system SHALL run artist lookups in a background job, one at a time, after the hits lookup in the scheduled refresh and when the user clicks **Look up artists now**. When the user opens the page of an artist with no data yet, the system SHALL look that artist up first. The job SHALL report its step and progress, and when a source refuses or fails 10 times in a row, SHALL stop using that source for the run, say why, and go on with the other sources.

#### Scenario: Opening an artist with no data
- **WHEN** the user opens the page of an artist the system hasn't looked up
- **THEN** the page says the artist is being looked up, the system looks that artist up ahead of the others, and the page fills in when the lookup finishes

#### Scenario: A source refuses
- **WHEN** ListenBrainz Labs answers 401 for similar artists, with no `LISTENBRAINZ_TOKEN` set or with the token refused too
- **THEN** the job stops asking ListenBrainz Labs for the rest of the run, shows "ListenBrainz refused the similar-artists request (401)" in the lookup status, and still fetches bios and influences

### Requirement: Respect each source's limits and terms
The system SHALL pace requests to each source within its published limits: one request a second to MusicBrainz shared with the hits lookup, Discogs at 25 requests a minute without a token and 60 with `DISCOGS_TOKEN`, and the others no faster than their published guidance. It SHALL identify itself with the app's User-Agent, follow `Retry-After` and rate-limit headers, and send only artist IDs and names.

#### Scenario: Shared MusicBrainz pace
- **WHEN** the hits lookup and the artist lookup both need MusicBrainz
- **THEN** their requests together stay at or below one a second
