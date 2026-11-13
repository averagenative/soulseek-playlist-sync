## ADDED Requirements

### Requirement: Each profile connects its own services
The system SHALL keep a separate Apple Music connection and Spotify connection for each profile, and SHALL use the signed-in profile's connections for everything done from its pages. The first profile SHALL keep the connections the app had before the upgrade. The Spotify Client ID and the Apple Music developer key SHALL stay one per install.

#### Scenario: A second person connects Apple Music
- **WHEN** Ana signs in and connects Apple Music with Ana's Apple ID
- **THEN** Ana's Apple Music playlists are offered to add, and Dana's Apple Music connection is unchanged

#### Scenario: Disconnect
- **WHEN** Ana disconnects Spotify
- **THEN** only Ana's Spotify token is removed, and Dana's playlists keep syncing

#### Scenario: Connecting at the same time
- **WHEN** Dana and Ana start the Spotify sign-in at the same time in two browsers
- **THEN** both sign-ins finish, each into the right profile

### Requirement: Explain the Spotify allowlist
When Spotify refuses a person because they aren't on the developer app's allowlist, the system SHALL say that the admin needs to add them under **User Management** in the Spotify developer dashboard, and SHALL link to the dashboard.

#### Scenario: Not on the allowlist
- **WHEN** Ana connects Spotify and Spotify answers that the user isn't registered in the Developer Dashboard
- **THEN** the page tells Ana to ask the admin to add Ana's Spotify name and email under **User Management**, with a link to the dashboard

### Requirement: Background work uses the owner's connections
The system SHALL fetch, sync, refresh, and copy each playlist with its owner's connections, both when a person starts the work and when the scheduled refresh runs it.

#### Scenario: Scheduled refresh
- **WHEN** the scheduled refresh runs with playlists from Dana and Ana
- **THEN** Dana's playlists are read through Dana's accounts and Ana's through Ana's

#### Scenario: Copy to a service
- **WHEN** Ana copies Ana's playlist to Spotify
- **THEN** the copy is created in Ana's Spotify account

#### Scenario: An owner without a connection
- **WHEN** the scheduled refresh reaches a Spotify playlist whose owner has disconnected Spotify
- **THEN** that playlist shows "Spotify isn't connected for Ana" as its error, and the other playlists refresh normally

### Requirement: Favorites sync per profile
The system SHALL keep the favorites sync's options, status, and history for each profile, sync each profile's Spotify Liked Songs and Apple Music favorites with each other only, and run the scheduled sync for each profile that turned on **Run on its own** and has both services connected. The first profile SHALL keep the options and history it had before the upgrade.

#### Scenario: Two people syncing favorites
- **WHEN** Dana and Ana both turn on the favorites sync
- **THEN** Dana's Spotify likes go only to Dana's Apple Music favorites, and Ana's only to Ana's

#### Scenario: History kept
- **WHEN** Dana had removed a song on Spotify that the sync once added from Apple Music
- **THEN** after the upgrade, the sync still doesn't add it back to Dana's Spotify

### Requirement: Hits playlists use their owner's favorites
The system SHALL pick songs for a hits playlist using the favorites of the playlist's owner, and SHALL gather favorites from every profile's connected services when the hits lookup runs.

#### Scenario: Different favorites
- **WHEN** Dana and Ana each have a hits playlist from the same filter, and only Ana has "Karma Police" as a favorite
- **THEN** Ana's hits playlist marks "Karma Police" as a favorite pick, and Dana's picks it only if another rule does
