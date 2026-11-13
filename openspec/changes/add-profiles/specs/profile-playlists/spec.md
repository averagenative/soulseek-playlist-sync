## ADDED Requirements

### Requirement: Every playlist belongs to a profile
The system SHALL record which profile owns each playlist: the profile that added it, or for existing playlists after the upgrade, the first profile. The playlists page SHALL show the signed-in profile's playlists by default.

#### Scenario: Existing playlists after the upgrade
- **WHEN** an install with 12 playlists starts the new version
- **THEN** all 12 belong to the first profile, keep their IDs and folders, and show on its playlists page

#### Scenario: A new person's playlists page
- **WHEN** Ana signs in for the first time
- **THEN** Ana's playlists page shows no playlists, with the buttons to add them

### Requirement: Two people can add the same service playlist
The system SHALL let two profiles each add the same Apple Music or Spotify playlist, including their own Liked Songs or Apple Music library, as separate playlists with their own songs, folders, and copies.

#### Scenario: Both add Liked Songs
- **WHEN** Dana and Ana each add their Spotify Liked Songs
- **THEN** each gets their own Liked Songs playlist, filled from their own Spotify account

### Requirement: See other people's playlists
The playlists page SHALL offer a **Showing** selector with **Mine**, each other profile, and **Everyone's**, and SHALL remember the choice in the browser. **Everyone's** SHALL group the playlists by person.

#### Scenario: Look at another person's playlists
- **WHEN** Ana picks "Dana" in **Showing**
- **THEN** the page lists Dana's playlists, marked as Dana's, and keeps showing them on Ana's next visit until Ana picks **Mine**

### Requirement: Other people's playlists are read-only
On a playlist owned by another profile, the system SHALL allow viewing its songs and copies, playing its songs, and exporting it as a zip, and SHALL refuse every change: syncing, downloading, refreshing, renaming, moving, removing, changing songs or settings, and copying to a service. The page SHALL say whose playlist it is and SHALL hide the controls that change it.

#### Scenario: Open another person's playlist
- **WHEN** Ana opens Dana's playlist "Road Trip"
- **THEN** the page says "Dana's playlist", lists its songs with play buttons and the zip export, and shows no sync, settings, or song controls

#### Scenario: A change sent anyway
- **WHEN** a request from Ana's browser posts **Download all** to Dana's playlist
- **THEN** the app answers 403 with "This is Dana's playlist." and changes nothing

### Requirement: Add songs only to your own playlists
On My music, **Add to playlist** SHALL list only the signed-in profile's playlists, and the system SHALL refuse to add songs to another profile's playlist.

#### Scenario: The playlist list on My music
- **WHEN** Ana selects songs on My music and opens **Add to playlist**
- **THEN** only Ana's My music playlists are offered

### Requirement: Folders per profile
The system SHALL put a new playlist's folder under its service folder and then the owner's profile folder. The first profile's folder SHALL be empty, so its playlists stay where they are, and each other profile's folder SHALL default to the profile's name.

#### Scenario: Ana's playlist folder
- **WHEN** Ana adds the Apple Music playlist "Road Trip" and the Apple Music folder is `AppleMusic`
- **THEN** its songs go in `AppleMusic/Ana/Road Trip`, apart from any "Road Trip" of Dana's in `AppleMusic/Road Trip`

#### Scenario: One download, two people
- **WHEN** Dana's and Ana's playlists both have a song that isn't in the library
- **THEN** the song downloads once, and the file appears in both playlists' folders
