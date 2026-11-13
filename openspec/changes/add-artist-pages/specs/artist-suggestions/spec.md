## ADDED Requirements

### Requirement: Rank artists to suggest
The system SHALL suggest artists that aren't in the library to each profile, ranked by how strongly the library points at them, using that profile's favorites and dismissals. A suggestion's score SHALL be the sum, over the library artists that point at it, of that library artist's weight times the strength of the link. A library artist's weight SHALL grow with the number of its songs in the library and more steeply with the number of its songs among your Apple Music and Spotify favorites. A similar-artist link's strength SHALL be its normalized score, and an influence link (in either direction) SHALL count as strength 0.5.

#### Scenario: Several of your artists point at one
- **WHEN** Nirvana, Pixies, and Mudhoney (all in the library) are each similar to Melvins, which isn't in the library
- **THEN** Melvins ranks above an artist that only one library artist is similar to with the same scores

#### Scenario: Favorites count more
- **WHEN** two library artists have the same number of songs, and one has five songs among your favorites
- **THEN** artists linked to the one with favorites rank higher

#### Scenario: Already in the library
- **WHEN** an artist's songs are added to the library
- **THEN** that artist is no longer suggested

### Requirement: Explain each suggestion
Each suggestion SHALL show why it's suggested: the library artists it's similar to and the influence links, strongest first, up to five, with how many more.

#### Scenario: Reasons shown
- **WHEN** Melvins is suggested
- **THEN** the suggestion reads like "Similar to Nirvana, Pixies, and Mudhoney; influenced Nirvana"

### Requirement: Suggestions page
The system SHALL provide a **Suggestions** page listing the top suggestions with each artist's image, genres, a short bio excerpt with its credit, the reasons, and, when they're known, the songs **Get their hits** would add. It SHALL also show an artist's suggestions on that artist's page as "Fans of this artist also have" and "If you like this artist" lists.

#### Scenario: Their hits listed
- **WHEN** a suggested artist's songs that pass the hits rules are known
- **THEN** the suggestion lists the best five with why each was picked (such as "Hot 100 #12" or "Single 1993"), each linked to its MusicBrainz recording page, and says how many more there are

### Requirement: Dismiss a suggestion
The user SHALL be able to mark a suggestion **Not interested**. The system SHALL stop suggesting that artist and SHALL let the user see and undo dismissals.

#### Scenario: Dismiss and undo
- **WHEN** the user clicks **Not interested** on Creed
- **THEN** Creed disappears from Suggestions, and appears in the list of dismissed artists with **Suggest again**

### Requirement: Get a suggested artist's hits
The user SHALL be able to click **Get their hits** on a suggestion. The system SHALL pick the artist's songs with the same rules a hits playlist uses, applied to the artist's albums, EPs, and singles on MusicBrainz: a song SHALL be picked when it charted on the Billboard Hot 100, came out as a single near its album, or has a listener count or Genius page-view count at least the cutoff times its album's median. Each album SHALL give at most the max songs per album set on the Suggestions page (five by default, with a cutoff of 1.25 by default), and SHALL get no minimum. A song on several releases SHALL count once, on the earliest album. The system SHALL skip songs already in the library, add the rest with title, artist, album, and length from MusicBrainz, and download them through Soulseek like the songs of any other playlist. The button SHALL show how many songs it adds when that's known.

#### Scenario: Songs that pass the rules
- **WHEN** the user clicks **Get their hits** on Melvins
- **THEN** the Melvins songs that charted, came out as singles, or stand out on their albums are added, Soulseek searches for each by artist, title, and length, and found songs download

#### Scenario: Deluxe editions
- **WHEN** an album's later deluxe edition adds bonus tracks
- **THEN** the bonus tracks aren't counted, because the album's tracklist is its earliest official release

#### Scenario: Nothing passes
- **WHEN** none of an artist's songs has chart, single, listener, or Genius data
- **THEN** the suggestion says no song passes the hits rules and doesn't offer **Get their hits**

#### Scenario: Not looked up yet
- **WHEN** the user clicks **Get their hits** on a suggestion whose albums haven't been fetched
- **THEN** the system looks that artist up ahead of the others, the suggestion shows the progress, and the songs are added when the lookup finishes

#### Scenario: A song already owned
- **WHEN** one of the artist's picked songs is already in the library
- **THEN** it isn't added

### Requirement: Suggestions playlists
The system SHALL add the songs from **Get their hits** to a playlist for that artist, named "<Artist> Hits", and to one **Suggestions** playlist that holds the songs of every artist playlist in the order they were added. Each song SHALL download once and appear in both playlists' folders. Removing an artist's playlist SHALL remove its songs from the Suggestions playlist. Both kinds SHALL be playlists like any other: shown on the playlists page, exportable, and copyable to Apple Music or Spotify.

#### Scenario: Two playlists, one download
- **WHEN** the user clicks **Get their hits** on Melvins and nine songs are picked
- **THEN** "Melvins Hits" and Suggestions each gain the nine songs, and each song downloads once and appears in both folders

#### Scenario: Remove one artist
- **WHEN** the user removes the "Melvins Hits" playlist
- **THEN** the Melvins songs leave the Suggestions playlist at its next refresh, and the other artists' songs stay

#### Scenario: Copied like any playlist
- **WHEN** the Suggestions playlist or an artist's playlist has songs
- **THEN** the user can copy it to Apple Music or Spotify from its page, the same as other playlists
