# Feature ideas

Ideas for later, with enough detail to start from. Nothing here is built yet.

## Hits playlist follow-ups

- **Last.fm listener counts** as a third popularity signal, from `artist.getTopTracks` (one request per artist).
- **AcoustID** fingerprints, to identify songs whose tags are wrong or missing. It needs the `fpcalc` tool in the image and reads each whole file, so it'd be the last lookup to try.
- **ListenBrainz batch lookups** (`POST /1/metadata/lookup/`, with a user token) to identify songs without MusicBrainz IDs faster than one MusicBrainz search a second.
- **Whole albums from Apple Music.** The **Whole album** button works in this app. To do the same while listening in the Music app, add the song to an Apple Music playlist with a set name, such as "Whole albums". At its next refresh, the hits playlist reads that playlist and adds each song's album whole. A Spotify playlist could work the same way.
- **Plays and skips from an MP3 player.** Players running Rockbox can write a `.scrobbler.log` that marks each song as listened to or skipped. Reading it would let skips push filler out of the hits playlist.

## Profile follow-ups

- **Copy another person's playlist** into your own, to change it or copy it to your own Apple Music or Spotify.
- **A sign-in proxy:** a `TRUSTED_USER_HEADER` setting that maps a user from a proxy such as Authelia or Authentik to a profile, for installs that already have one.

## Artist page follow-ups

- **Weigh the songs your hits playlists picked** more than the rest of the library when ranking suggestions, if the suggestions lean too far toward artists you only have a few songs of.
- **Bios in other languages,** from the Wikipedia in the browser's language, falling back to English.
- **Missing hits for artists you have:** each library artist's charted songs and singles that aren't in your library, with the same **Get their hits** button.
- **Suggestions from what you listen to,** from your ListenBrainz or Last.fm listening history, when you connect it.
