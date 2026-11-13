## ADDED Requirements

### Requirement: Artists index
The system SHALL provide an **Artists** page listing every artist in the library once, by name, with how many albums and songs of theirs the library has and whether their page has data yet. It SHALL support searching by name, filtering by first letter, and sorting by name or by number of songs.

#### Scenario: Find an artist
- **WHEN** the user types "sonic" in the Artists search
- **THEN** the page lists Sonic Youth and any other artist whose name contains "sonic", with their album and song counts

#### Scenario: One entry per artist
- **WHEN** the library has songs credited to "Nirvana" and "Nirvana feat. Someone"
- **THEN** the Artists page lists Nirvana once

### Requirement: Artist page header and facts
The artist page SHALL show the artist's name, image (with its credit), type, years active, origin, and genres. For a group, it SHALL list the members with their years and instruments. For a person, it SHALL list the groups they were in. A member or group that is in the library SHALL link to its own artist page.

#### Scenario: A band page
- **WHEN** the user opens Nirvana's page
- **THEN** it shows "Group, 1987 to 1994, from Aberdeen, United States", its genres, and its members with their years, and Dave Grohl links to his page if the library has his music

#### Scenario: Missing facts
- **WHEN** an artist has no origin or no years on any source
- **THEN** the page leaves those facts out instead of showing empty labels

### Requirement: Bio with attribution
The artist page SHALL show one bio, from the preferred source that has one, followed by its source name, a link to the source page, and its license. When other sources also have a bio, the page SHALL let the user switch to them. The page SHALL show a long bio shortened, with a control to show all of it.

#### Scenario: Credit under the bio
- **WHEN** the bio comes from Wikipedia
- **THEN** the page shows "From Wikipedia, CC BY-SA 4.0" with a link to the article under the text

#### Scenario: Another source
- **WHEN** both Wikipedia and Discogs have a bio
- **THEN** the page shows the Wikipedia bio and offers the Discogs one

### Requirement: Influences and similar artists
The artist page SHALL show **Influenced by** and **Influenced** lists and a **Similar artists** list. In each list, artists in the library SHALL come first and link to their pages, and artists not in the library SHALL be marked as such and link to their MusicBrainz page. Similar artists SHALL be ordered by score. The page SHALL label influence data as coming from Wikidata and similarity as coming from listening data, and SHALL NOT present similar artists as influences.

#### Scenario: Influences in and out of the library
- **WHEN** Nirvana was influenced by Pixies (in the library) and The Stooges (not in the library)
- **THEN** Pixies links to its artist page, and The Stooges shows "Not in your library" with a link to MusicBrainz

#### Scenario: No influence data
- **WHEN** Wikidata has no influence statements for an artist
- **THEN** the page says no influence data is known, and still shows similar artists

### Requirement: The artist's albums in the library
The artist page SHALL list the artist's albums in the library, newest first, with year, kind (album, EP, single, compilation, live), and song count. For each album, it SHALL show which songs a hits playlist picked and offer a link to browse the album on My music.

#### Scenario: Hits marked
- **WHEN** a hits playlist picked four songs from Nevermind
- **THEN** the Nevermind entry on Nirvana's page marks those four songs as picks

### Requirement: Links out
The artist page SHALL link to the artist on MusicBrainz and, when known, on Wikipedia, Wikidata, AllMusic, Discogs, Last.fm, and the official homepage. A **Sources** section SHALL list every source the page's data came from, with its license.

#### Scenario: AllMusic link only
- **WHEN** MusicBrainz has the artist's AllMusic link
- **THEN** the page links to the AllMusic page, and shows no text taken from AllMusic

### Requirement: Artist names link to their pages
Artist names SHALL link to their artist pages on My music, on playlist song rows, and on the hits picks page. A name with several credited artists SHALL link to the first one's page.

#### Scenario: From a playlist row
- **WHEN** the user clicks "2Pac" under a song on a playlist page
- **THEN** 2Pac's artist page opens

### Requirement: Pages for artists without data
When an artist has no MusicBrainz ID or no fetched data, the page SHALL still show the artist's albums in the library, say what's missing, and offer to look the artist up now or to paste a MusicBrainz artist link.

#### Scenario: Unknown artist
- **WHEN** the user opens the page of an artist whose name matched nothing on MusicBrainz
- **THEN** the page shows the artist's library albums, says no information was found, and offers a field for a MusicBrainz artist link

### Requirement: Lookup status
The Artists page SHALL show the artist lookup's progress while it runs, when it last finished, why any source stopped, and how many artists have data. It SHALL offer **Look up artists now**.

#### Scenario: Lookup in progress
- **WHEN** the artist lookup is fetching Wikidata for artist 120 of 657
- **THEN** the Artists page shows that step and count and refreshes it while the lookup runs
