## Why

More than one person in a household uses the app, but it has one set of playlists and one connection each to Apple Music and Spotify. Everyone who opens it sees the same playlists and changes them through the same accounts, and nothing asks who they are. Each person needs their own playlists and their own Apple Music and Spotify connections, while the music library, the downloads, and Soulseek stay shared, because they're the same files and the same Soulseek account.

## What Changes

- Add **profiles**. Each person has a profile with a name and an optional password. A sign-in page lists the profiles: a profile without a password opens with one click, and one with a password asks for it. The browser remembers the profile until the person signs out or switches.
- While there's one profile and it has no password, the app opens it without a sign-in page, so a one-person install works as before.
- On upgrade, the app creates the first profile as the admin and gives it every existing playlist, both service connections, the favorites sync, and the hits playlists. Nothing moves on disk.
- Make these belong to a profile: Apple Music and Spotify connections, playlists (including hits playlists), copies to the services, the favorites sync and its options, and the favorites that hits playlists use.
- Keep these shared: My music, downloads and the Soulseek queue, the hits lookup cache, artist data, and the Settings page.
- Add a **Showing** selector on the playlists page to see another profile's playlists. Another person's playlist opens read-only: you can see its songs, play them, and export them, but not change, sync, or copy it.
- Run background work for each playlist's owner, with the owner's connections: the scheduled refresh, copies to the services, and the favorites sync.
- Let the admin add, rename, and remove profiles, clear a forgotten password, and change the Settings page.
- Put the playlists of profiles other than the first in a folder named for the profile under each service folder, so two people's playlists with the same name don't mix.
- Keep `APP_PASSWORD` working as a password for the whole app, in front of the profiles.

## Capabilities

### New Capabilities

- `profiles`: Profiles, optional passwords, signing in and switching, how the browser remembers the profile, the admin, and managing profiles.
- `profile-playlists`: Which profile owns each playlist, viewing other profiles' playlists read-only, playlist IDs that let two people add the same service playlist, and per-profile folders.
- `profile-connections`: Apple Music and Spotify connections per profile, the favorites sync per profile, and background work that uses each playlist owner's connections.

### Modified Capabilities

None. The repository has no specs yet.

## Impact

- **New code:** `app/profiles.py` (profiles, password hashing, the signed cookie), `app/templates/signin.html`, and `app/templates/profiles.html`.
- **Changed code:** `app/db.py` (the `profiles` table and owner columns), `app/config.py` (per-profile token paths), `app/web.py` (the profile middleware, sign-in routes, and an owner check on every playlist route), `app/sync.py`, `app/mirror.py`, `app/favorites.py`, `app/hits.py`, and `app/worker.py` (use the owner's connections), and the templates for the nav, the playlists page, and the playlist page.
- **Data:** a `DATA_DIR/session-key` file for signing the cookie, and `DATA_DIR/profiles/<id>/` for the service tokens of profiles after the first.
- **Dependencies:** none new. Passwords use `hashlib.scrypt`, and the cookie uses `hmac`.
- **Docs:** README (profiles, adding a person to the Spotify app's allowlist, `APP_PASSWORD`), `docs/design.md`.
- **Other changes:** `add-artist-pages` builds on this one: suggestions, dismissals, and the Suggestions playlists belong to the profile that uses them.
