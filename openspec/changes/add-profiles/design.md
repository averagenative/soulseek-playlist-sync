## Context

The app is one FastAPI process with a SQLite database and background threads (the download worker, playlist jobs, the scheduled refresh, the favorites sync, and the hits lookup). Today it has no idea who is using it:

- The only sign-in is `APP_PASSWORD`, one HTTP basic-auth password for the whole app, and it's optional. A typical install sits on a home network with it unset.
- The Apple Music user token is one file, `DATA_DIR/apple-music-user-token`, and the Spotify token is one file, `DATA_DIR/spotify-token.json`. Every service call reads them through `Config`.
- A playlist's ID is `<source>:<remote id>`, such as `spotify:liked` or `apple:library-songs`, so two people couldn't both add their Liked Songs. Service calls use the `remote_id` column; no code parses the ID back apart.
- The favorites sync keeps its options and status in `kv` and its history in `favorite_pairs`. The hits playlists use favorites from `hit_favorites`. Both assume one Apple Music account and one Spotify account.
- The web app has 54 routes. 17 of them act on one playlist (`/playlists/{pid}/...`), and two more create a playlist or add songs to one (`POST /playlists` and `POST /library/playlist`).

The case this is built for is two people in one home, each with their own Apple Music and Spotify accounts. Each can look at the other's playlists, but by choice, not by default.

Two facts about the services shape the design:

- **Spotify:** a developer app in Development Mode allows up to five authorized users, and each must be added by name and email under **User Management** in the app's dashboard (Spotify developer update, 2026-02-06). The Client ID stays one per install; each person signs in with their own Spotify account.
- **Apple Music:** the developer key is one per install, and any Apple Music subscriber can authorize it with their own Apple ID. No allowlist.

## Goals / Non-Goals

**Goals:**

- Each person sees and changes only their own playlists and service accounts, unless they choose to look at someone else's playlists.
- One-person installs look and work the same as before, with no sign-in page.
- No file moves on disk, and existing playlists, connections, and history stay with the first profile.
- No new dependencies.

**Non-Goals:**

- Internet-facing account security: email, password reset by email, lockouts, or two-factor sign-in. The app is for a home network or a VPN, and the README says so. `APP_PASSWORD` or a proxy in front stays the answer for anything more.
- A separate music library per person. My music is the files on disk, and they're shared.
- Per-person Soulseek, YouTube, or download settings.
- Editing another person's playlist, or copying it into your own. Copying is noted in `docs/ideas.md` for later.

## Decisions

### 1. Profiles table and passwords

`profiles(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, password TEXT, admin INTEGER NOT NULL DEFAULT 0, folder TEXT NOT NULL DEFAULT '', session_version INTEGER NOT NULL DEFAULT 1, created_at REAL)`.

A password is stored as `scrypt$16384$8$1$<salt hex>$<hash hex>` from `hashlib.scrypt` with a 16-byte random salt. A profile with no password has `password` NULL. Comparisons use `hmac.compare_digest`. A wrong password waits one second before the page answers, which is enough on a home network.

- *Alternative:* PBKDF2. Either is fine; scrypt is in the standard library and harder to brute-force on a GPU.

### 2. The browser remembers the profile in a signed cookie

The cookie `profile` holds `<profile id>.<session version>.<issued time>.<signature>`, where the signature is HMAC-SHA256 over the rest with a 32-byte key in `DATA_DIR/session-key` (created on first start, mode 600). It's `HttpOnly` and `SameSite=Lax`, `Secure` when the request came over HTTPS (Traefik sets `X-Forwarded-Proto`), and lasts a year. Changing a profile's password, clearing it, or removing the profile raises `session_version`, which signs every browser out of that profile.

- *Alternative:* Starlette's `SessionMiddleware`. Rejected, because it needs `itsdangerous`, a new dependency, for the same thing.
- *Alternative:* sessions in a database table. Rejected for now: it would allow listing and ending single sessions, which a household doesn't need, and costs a query on every request.
- *Alternative:* a sign-in proxy in front (Authelia, Authentik) passing a user header. Not chosen as the default because it's a second service to run. A `TRUSTED_USER_HEADER` setting could map a proxy's user to a profile later.

### 3. Every request resolves a profile

A middleware runs after the `APP_PASSWORD` check and sets `request.state.profile`:

1. If the cookie is valid and its session version matches, use that profile.
2. Otherwise, if there's exactly one profile and it has no password, use it, and set the cookie.
3. Otherwise, redirect to `/signin`. An htmx request gets an `HX-Redirect` header instead, so a partial doesn't render the sign-in page inside a page.

`/healthz`, `/static`, the icons, and `/signin` itself skip the middleware. POST requests also must carry an `Origin` header that matches the request's host when they carry one at all; with `SameSite=Lax`, this blocks forms posted from another site.

The sign-in page lists the profiles by name. Clicking a profile without a password signs in. Clicking one with a password shows its password field. The nav shows the current profile's name, with **Switch profile** and **Sign out**.

### 4. The admin

The first profile is the admin. Only an admin can open the Settings page, set the Spotify Client ID, and manage profiles: add one (name, optional password, admin or not), rename one, clear its password, make it an admin or not, and remove it. The app always keeps at least one admin. Everyone can change their own name and password, connect their own services, rescan the library, start the hits lookup, and turn on slskd, because those affect only shared things that are safe to refresh.

Removing a profile removes its playlists from the app, like **Remove** on each of them (files stay on disk), its service tokens, and its favorites history. The admin confirms with the profile's name.

### 5. Every playlist has an owner

`playlists.owner INTEGER NOT NULL DEFAULT 1`. Every route under `/playlists/{pid}` loads the playlist through one helper, `_playlist(request, pid, write)`, and `POST /library/playlist` accepts only the signed-in profile's playlists:

- The owner gets everything.
- Another profile gets only the read routes: the playlist page and its rows, playing a song, the copies list, and the zip export. Every POST answers 403 with "This is <name>'s playlist."

A test walks every route in the app with a playlist owned by another profile, and fails if any POST route doesn't answer 403. That catches a route added later without the check.

### 6. Playlist IDs that two people can share

The first profile keeps `<source>:<remote id>`. A playlist added by any other profile gets `<source>:<remote id>@<profile id>`, such as `spotify:liked@2`. So two people can each add their Liked Songs, their Apple Music library, or the same public playlist, and each gets their own folder, entries, and copies. My music playlists already have random IDs and don't change. `@` is safe in a URL path and never appears in a service's playlist ID.

### 7. Service connections per profile

`Config` gains a `profile` field, and the token paths come from it:

- Profile 1 keeps `DATA_DIR/spotify-token.json` and `DATA_DIR/apple-music-user-token`, so an older image still finds them after a rollback.
- Profile N uses `DATA_DIR/profiles/<N>/spotify-token.json` and `DATA_DIR/profiles/<N>/apple-music-user-token`.

`cfg.for_profile(id)` returns a copy for that profile. Each caller that builds a service client passes the right one:

- A web request uses the signed-in profile.
- Work on a playlist (fetch, sync, copies to the services) uses the playlist's owner.
- The favorites sync and the hits favorites step loop over the profiles.

The Spotify sign-in's PKCE state and the Apple Music connect nonce move from single `kv` keys to per-profile keys, so two people connecting at once don't overwrite each other.

When Spotify refuses a person because they aren't on the app's allowlist (`403` with "User not registered in the Developer Dashboard"), the connect page says to add them under **User Management** in the Spotify dashboard, with the link.

### 8. Favorites per profile

`favorite_pairs` gains `profile INTEGER NOT NULL DEFAULT 1` and moves its primary key to `(profile, spotify_id, apple_id)`. SQLite can't change a primary key in place, so the migration copies the table into a new one in one transaction. The favorites options and status `kv` keys get a `:<profile id>` suffix; profile 1 takes over the old keys. The scheduled favorites sync runs for each profile with **Run on its own** on and both services connected, one after another.

`hit_favorites` gains `profile` the same way, and a hits playlist uses its owner's favorites. The hits lookup's favorites step reads every profile's favorites.

### 9. What stays shared

My music (`library`), `songs`, `downloads`, `song_links`, the hits lookup caches, artist data, the worker and the Soulseek queue, and the Settings page. The Downloads page shows the whole queue, because everyone's songs share it. A song two people want downloads once and goes into both playlists' folders, as it does today for two playlists.

### 10. Seeing other people's playlists

The playlists page gets a **Showing** selector with **Mine**, one entry per other profile, and **Everyone's**. The choice is remembered in a cookie. **Everyone's** groups the playlists by person. Another person's playlist page shows "<Name>'s playlist" at the top, and hides the buttons and settings that change it. On My music, **Add to playlist** lists only your own playlists.

### 11. Folders per profile

A profile's `folder` is where its new playlists go under each service folder: `<service folder>/<profile folder>/<playlist>`. It's empty for the first profile, so its folders stay where they are, and defaults to the profile's name for every other profile, such as `AppleMusic/Ana/Road Trip`. A playlist's own folder setting can still move it.

## Risks / Trade-offs

- **A profile without a password protects nothing.** Mitigation: The Profiles page says so next to each profile without one. That's the household's choice, and it's what was asked for.
- **Profile names show on the sign-in page.** Mitigation: The app is for a home network, and `APP_PASSWORD` can hide the whole app from anyone without it.
- **A playlist route added later without the owner check.** Mitigation: The route-walking test in decision 5.
- **Spotify allows five people per developer app.** Mitigation: The README says so, and the connect page explains the allowlist error. A bigger household would need a second Client ID, which is out of scope.
- **A rollback to an older image uses the first profile's tokens for everyone's playlists.** An older image ignores `owner`, so it would read other profiles' Spotify and Apple Music playlists through the first profile's accounts, and its copies to the services would write into the first profile's accounts. Mitigation: The rollback steps say to remove other profiles' playlists first, or to turn off their copies.
- **The `favorite_pairs` table copy.** Mitigation: One transaction, tested on a copy of a real database before release.

## Migration Plan

1. `db.init()` creates `profiles` and adds `owner` to `playlists`, `profile` to `hit_favorites`, and the new `favorite_pairs` table, copying the old rows to profile 1.
2. If `profiles` is empty, it creates profile 1, named "Me", as an admin with no password and an empty folder. It's the only profile and has no password, so the app opens it without a sign-in page, exactly as before.
3. The Profiles page suggests renaming "Me" and setting a password. Adding a second profile turns on the sign-in page.
4. The new person signs in, connects Apple Music, and connects Spotify after the admin adds them to the Spotify app's **User Management** list.
5. Rollback: remove the playlists of profiles other than the first (or turn off their copies to the services), then deploy the previous image. The new tables and columns are ignored, and profile 1's tokens are where the older image looks.

## Open Questions

- What should the first profile be called on upgrade? "Me" works for a one-person install, and the Profiles page invites a rename.
- Should **Everyone's** be the default view for some households? The default here is **Mine**, as asked.
