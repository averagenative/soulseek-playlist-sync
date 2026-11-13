## 1. Profiles and the database

- [ ] 1.1 Add `profiles` to `db.SCHEMA`, `owner` to `playlists`, and `profile` to `hit_favorites`, and rebuild `favorite_pairs` with `profile` in its primary key, copying the old rows to profile 1 in one transaction
- [ ] 1.2 Create profile 1 ("Me", admin, no password, empty folder) in `db.init()` when `profiles` is empty
- [ ] 1.3 `app/profiles.py`: list, get, add, rename, set and clear a password (scrypt, `compare_digest`), set admin with the at-least-one-admin rule, and remove (playlists, tokens, favorites history)
- [ ] 1.4 Test the migration on a copy of a real database: row counts kept, every playlist owned by profile 1, favorites history unchanged, and a second `db.init()` changes nothing

## 2. Signing in

- [ ] 2.1 Create `DATA_DIR/session-key` on first start (32 random bytes, mode 600), and sign and check the `profile` cookie (profile ID, session version, issued time, HMAC-SHA256)
- [ ] 2.2 Middleware after the `APP_PASSWORD` check: resolve the profile (cookie, else the only profile without a password, else `/signin`), `HX-Redirect` for htmx requests, and skip `/healthz`, `/static`, the icons, and `/signin`
- [ ] 2.3 Refuse a POST whose `Origin` names another host; set the cookie `HttpOnly`, `SameSite=Lax`, and `Secure` behind HTTPS
- [ ] 2.4 `GET /signin` and `POST /signin` (one click without a password, a password field with one, a one-second wait on a wrong password), `POST /signout`, and the profile name with **Switch profile** and **Sign out** in the nav
- [ ] 2.5 Profiles page: your own name and password for everyone; add, rename, clear a password, admin or not, and remove (confirm by typing the name) for admins; a note on profiles without a password
- [ ] 2.6 Admin-only: the Settings page and the Spotify Client ID setup
- [ ] 2.7 Tests: one profile with no password opens with no sign-in page; two profiles redirect; wrong password; a changed password signs other browsers out; the last admin can't be removed; a cross-site POST is refused

## 3. Playlist ownership

- [ ] 3.1 Set `owner` when a playlist is added (services, YouTube, My music, hits), and give playlists of profiles after the first the ID `<source>:<remote id>@<profile id>`
- [ ] 3.2 `_playlist(request, pid, write)`: the owner gets everything, another profile gets only the read routes, and a write answers 403 "This is <name>'s playlist."; use it in all 17 `/playlists/{pid}` routes, and check ownership in `POST /library/playlist`
- [ ] 3.3 A test that walks every route with a playlist owned by another profile and fails on any POST that doesn't answer 403
- [ ] 3.4 The **Showing** selector (Mine, each other profile, Everyone's grouped by person) remembered in a cookie, and the home page and nav counts for the chosen view
- [ ] 3.5 The read-only playlist page: "<Name>'s playlist", songs with play and zip export, and no controls that change it
- [ ] 3.6 **Add to playlist** on My music lists only your playlists
- [ ] 3.7 The profile `folder` in `sync._unique_folder()`: empty for profile 1, the profile name by default for others
- [ ] 3.8 Tests: two profiles add Liked Songs; folders apart; one download placed in both profiles' folders; the read-only page hides its controls

## 4. Service connections

- [ ] 4.1 `Config.profile`, `cfg.for_profile(id)`, and per-profile token paths (profile 1 keeps the old files, others use `DATA_DIR/profiles/<id>/`)
- [ ] 4.2 Use the signed-in profile's config in the Apple Music and Spotify connect, paste, disconnect, and playlist-picker routes, and move the Spotify PKCE state and Apple Music nonce to per-profile `kv` keys
- [ ] 4.3 Use the owner's config in `sync.fetch()`, the playlist jobs, `mirror`, and `refresh_auto_playlists()`; a playlist whose owner isn't connected gets "<Service> isn't connected for <name>" as its error
- [ ] 4.4 Explain Spotify's "User not registered in the Developer Dashboard" refusal on the connect page, with the dashboard link
- [ ] 4.5 Tests: two profiles' tokens stay apart; the scheduled refresh uses each owner's client; a copy goes to the owner's account; a missing connection fails only that playlist

## 5. Favorites and hits

- [ ] 5.1 Favorites options and status `kv` keys per profile (profile 1 takes the old keys), `favorite_pairs` by profile, and the favorites page for the signed-in profile
- [ ] 5.2 `favorites.scheduled()` runs each profile with **Run on its own** and both services connected, one after another
- [ ] 5.3 The hits favorites step reads every profile's favorites into `hit_favorites` with the profile, and `hits.plan()` uses the playlist owner's
- [ ] 5.4 Tests: two profiles' favorites sync apart, history kept for profile 1, and hits picks follow the owner's favorites

## 6. Docs and release

- [ ] 6.1 README: profiles, signing in, the admin, what's shared and what isn't, adding a person to the Spotify app's **User Management** list (five people at most), and `APP_PASSWORD` in front; add copying another person's playlist to `docs/ideas.md`
- [ ] 6.2 `docs/design.md`: the profiles table, the cookie, ownership, per-profile tokens, and the rollback steps
- [ ] 6.3 Check the sign-in page, the Profiles page, the **Showing** selector, and a read-only playlist in a running instance with screenshots at desktop and phone widths, in two browsers signed in as different profiles
- [ ] 6.4 Commit, push, publish the image, and deploy; then the admin renames "Me", sets a password, adds the second profile, and adds that person's Spotify account under **User Management**
