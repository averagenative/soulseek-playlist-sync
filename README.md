# soulseek-playlist-sync

Keep local MP3 copies of your Apple Music and Spotify playlists. Pick a playlist, and soulseek-playlist-sync gives it its own folder of tagged MP3s plus an `.m3u`, ready to copy onto an MP3 player or to play in Navidrome, Jellyfin, or Plex. Songs you already own are copied from your library; missing songs are downloaded one by one from Soulseek through [slskd](https://github.com/slskd/slskd), with an optional YouTube fallback for songs Soulseek doesn't have. It also goes the other way: browse the music you already have, make playlists from it, and copy them to Spotify or Apple Music, up to your whole collection. Self-hosted, runs in Docker, and works without an Apple Developer account.

![Playlist cards with cover art, sync progress, and a Download all button](docs/screenshots/home.png)

## Features

- **Apple Music, Spotify, and YouTube playlists**, including Apple collaborative playlists, Apple Favorite Songs, your whole Apple Music library, Spotify Liked Songs, and any public or unlisted YouTube or YouTube Music playlist.
- **One folder per playlist**, with numbered files (`001 - Artist - Title.mp3`) and a relative-path `.m3u`, so the folder works when you copy it to a phone, an SD card, or an MP3 player. File names are safe for FAT32 and exFAT.
- **Per-song sync status**: synced, in your library, downloading (with speed and time left), not found, removed from the playlist, and so on.
- **Reuses what you have.** Songs already in your library are matched by ISRC or by artist, title, and length, and hardlinked into the playlist folder instead of downloaded again.
- **Careful downloads.** MP3 only, 192 kbps or better (320 preferred), and the length has to match the streaming service's within 3 seconds, so you don't get live versions or the wrong edit.
- **Clean tags.** Every file is retagged from the streaming service: title, artist, album, album artist, track number, year, ISRC, and cover art (ID3v2.3, which every player reads).
- **YouTube fallback (optional).** For songs Soulseek doesn't have, it searches YouTube Music first for official "Artist - Topic" audio uploads, and skips covers, live takes, and sped-up edits.
- **Copy playlists between services.** Make a Spotify copy of an Apple Music playlist, or an Apple Music copy of a Spotify playlist, matched by ISRC.
- **Sync favorites.** Add the songs you like on Spotify to Apple Music Favorite Songs, the songs you favorite on Apple Music to Spotify Liked Songs, or both, on a schedule.
- **Browse your own music** on the **My music** page: search, filter by folder, play songs, and see album art from your files.
- **Make playlists from your music**, by hand or as "every song in these folders" playlists that keep up as your library changes, and copy them to Spotify, Apple Music, or both. Put your whole collection on your phone's music app and shuffle it.
- **Hits playlists.** Keep only the standout songs of each album in a big collection: the songs that charted, came out as singles, or get the most listeners, plus your favorites. The filler stays out.
- **Check songs by ear.** Play any downloaded song in the browser, and compare it with Apple's 30-second clip of the original.
- **Download a playlist as a ZIP** to the computer you're browsing from, with its `.m3u`.
- **Download all** button per playlist, live progress from slskd, a Downloads page for everything in flight, and pages that refresh themselves.
- Settings page, six color themes, works on phones, and installs to a phone's home screen.

<img src="docs/screenshots/phone-dark.png" width="260" alt="A playlist on a phone in dark mode, with the status under each song">

![A playlist page with per-song status, album art, and download progress](docs/screenshots/playlist.png)

## Prerequisites

You need the following:

| What | Needed for | Cost |
|---|---|---|
| A computer that runs Docker with Docker Compose (Linux server, NAS, Mac, or Windows with Docker Desktop) | Everything | Free |
| Disk space for the MP3s | Everything | About 8 to 10 MB per song |
| A Soulseek username and password | Downloading songs | Free. Pick any username: slskd creates the account the first time it logs in. |
| An Apple Music subscription | Apple Music playlists | Your existing subscription |
| An Apple Developer Program membership | Apple Music, **only if you want** the official sign-in | 99 USD a year. Not needed: see [Connect Apple Music](#connect-apple-music). |
| Spotify Premium and a free Spotify developer app | Spotify playlists | Your existing subscription. Spotify only lets Premium accounts use developer apps. |

You don't need both Apple Music and Spotify. Use either one, or both.

## Quick start

1. Clone the repository and create the folders the containers use:

   ```sh
   git clone https://github.com/averagenative/soulseek-playlist-sync.git
   cd soulseek-playlist-sync
   mkdir -p music data secrets slskd
   cp .env.example .env
   ```

   Create the folders before you start the containers. If Docker creates them, they belong to `root` and the app can't write to them.

1. Edit `.env`. At minimum, set these values:

   - `SOULSEEK_USERNAME` and `SOULSEEK_PASSWORD`: any username and password you want for Soulseek.
   - `SLSKD_WEB_PASSWORD`: a password for slskd's own web page.
   - `SLSKD_API_KEY`: a random string. To make one, run `openssl rand -hex 32`.
   - `MUSIC_DIR`: the folder where your music is, or leave `./music`.
   - `PUID` and `PGID`: the user and group that own that folder. To see yours, run `id`.

1. Start it:

   ```sh
   docker compose up -d
   ```

   The first start downloads the images. The app's image runs on Intel, AMD, and ARM machines, including Apple Silicon Macs and a Raspberry Pi with a 64-bit OS. To build it from this folder instead, run `docker compose up -d --build`; the first build takes a few minutes.

1. Open `http://127.0.0.1:8080` on the computer that runs it, or `http://<server IP>:8080` from another device on your network.

1. Connect Apple Music, Spotify, or both, as described in the following sections.

1. Click **+ Apple Music** or **+ Spotify**, add a playlist, and click **Download all**.

The **Soulseek** status dot in the header turns green when slskd is logged in. Soulseek is a sharing network: the included setup shares your music folder read-only, because the network bans accounts that share nothing.

## Connect Apple Music

There are two ways to connect. Both read the same playlists.

### Option A: No Apple Developer account

This is the default. The app uses the same access that Apple's music.apple.com web player uses, and you sign in once by copying a cookie from your browser.

1. Leave `APPLE_TEAM_ID` and `APPLE_KEY_ID` empty in `.env`.
1. In the app, click **Connect Apple Music**.
1. On a computer, open [music.apple.com](https://music.apple.com) and sign in.
1. Open the browser's developer tools (F12, or Cmd+Option+I on a Mac).
   - Chrome or Edge: **Application** tab, then **Cookies**, then `https://music.apple.com`.
   - Firefox or Safari: **Storage** tab, then **Cookies**.
1. Copy the value of the `media-user-token` cookie and paste it into the app. The app checks it with Apple before saving it.

The cookie lasts a few months. When it expires, the app says so; paste a fresh one.

### Option B: With an Apple Developer account

If you have an Apple Developer Program membership, you can use your own MusicKit key and sign in with a normal Apple sign-in window.

1. Create a Media ID at [Certificates, Identifiers & Profiles > Identifiers](https://developer.apple.com/account/resources/identifiers/list/media): click **+**, choose **Media IDs**, enter a description and an identifier such as `media.com.example.playlistsync`, and select **MusicKit**.
1. Create a key at [Keys](https://developer.apple.com/account/resources/authkeys/list): click **+**, select **Media Services (MusicKit, ShazamKit)**, click **Configure**, and choose the Media ID from the previous step.
1. Download the `.p8` key file (Apple lets you download it only once) and save it as `secrets/AuthKey.p8`.
1. In `.env`, set `APPLE_KEY_ID` to the key's 10-character ID and `APPLE_TEAM_ID` to your Team ID from [Membership details](https://developer.apple.com/account).
1. Run `docker compose up -d`, then click **Connect Apple Music** in the app and sign in.

Apple's sign-in window needs the app to be open at `http://127.0.0.1:8080`, `http://localhost:8080`, or an HTTPS address. Each person who runs the app needs their own key: never share your `.p8` file.

## Connect Spotify

You need Spotify Premium, because Spotify only lets Premium accounts use developer apps. Each person makes their own developer app, with their own Spotify account.

In the app, click **Connect Spotify**. That page walks you through the following steps, with the exact redirect address for your setup and a copy button for each value.

1. Go to the [Spotify developer dashboard](https://developer.spotify.com/dashboard), sign in with your Spotify account, and click **Create app**.
1. Fill in the form:
   - **App name** and **App description**: anything you like, such as `Playlist sync` and `Copies my playlists`.
   - **Website**: leave it empty.
   - **Redirect URIs**: the address shown under **Spotify** on this app's **Settings** page. Paste it and click **Add**. When the app runs on your own computer, it's `http://127.0.0.1:8080/spotify/callback`. Spotify refuses addresses that start with `http://localhost`.
   - For the APIs you plan to use, select **Web API**.
   - Accept Spotify's terms and save the app.
1. On your Spotify app's page, click **Settings** and copy the **Client ID**. You don't need the client secret.
1. On this app's **Settings** page, paste it into **Spotify Client ID** and click **Save**.
1. Click **Connect Spotify** and approve.

Spotify accepts only HTTPS redirect addresses or `127.0.0.1`. If the app runs on another machine and you don't use HTTPS, connect through an SSH tunnel once:

```sh
ssh -L 8080:127.0.0.1:8080 you@your-server
```

Then open `http://127.0.0.1:8080` on your own computer and click **Connect Spotify**. After that, use the app from any address.

Copying playlists to Spotify needs permission to create playlists. If a playlist page shows **Reconnect Spotify to allow creating playlists**, click it and approve again.

Spotify only lets apps read playlists you own or collaborate on, plus Liked Songs. Playlists you only follow are listed but can't be added. If Spotify returns a 403 error, add your account under **User Management** in your Spotify app's dashboard page.

## Add YouTube playlists

YouTube and YouTube Music playlists need no sign-in and no setup. The playlist has to be Public or Unlisted.

1. On YouTube or YouTube Music, open the playlist, click **Share**, and copy the link.
1. In the app, click **+ YouTube**, paste the link, and click **Add playlist**.

How YouTube playlists differ from the other services:

- **Artist and title come from the video's title.** "Artist - Song (Official Video)" becomes "Song" by "Artist". For YouTube Music's "Artist - Topic" uploads, the artist comes from the channel. A video whose title doesn't name the artist gets the channel's name as the artist.
- **Music videos don't set the song's length.** A music video often has an intro or an ending, so for those, a Soulseek file's length doesn't have to match. Lyric videos, audio uploads, and "Topic" uploads keep their length.
- **The YouTube fallback uses the playlist's own video.** With **Try YouTube when Soulseek doesn't have a song** on, a song Soulseek doesn't have is downloaded from the video in the playlist, instead of a search. If that video can't be downloaded, the app tries up to three other uploads of the song, such as the artist's "Topic" upload.
- **Age-restricted videos need a YouTube sign-in.** To download them, export a `cookies.txt` from a browser that's signed in to YouTube, put it where the container can write to it, and set `YOUTUBE_COOKIES_FILE` to its path. YouTube can limit an account that downloads a lot, so use one you don't mind losing.
- **Cover art** is the middle of the video's thumbnail, which for "Topic" uploads is the album cover.
- **Deleted and private videos are left out.** YouTube still lists them in the playlist, but with no title or artist.
- **Private playlists, Liked videos, Liked Music, and Mixes can't be added**, because reading them needs a YouTube sign-in. Save the songs to a Public or Unlisted playlist and add that.
- You can copy a YouTube playlist to Spotify or Apple Music from its page.

## How syncing works

When you click **Download all**, the app does the following for each song in the playlist:

1. It looks for the song in your music folder (by ISRC, or by artist, title, and length). If it finds the song, it hardlinks it into the playlist folder, or copies it if the playlist folder is on another disk.
1. If the song isn't in your library, it searches Soulseek and downloads the best MP3: 320 kbps first, from a peer with a free slot, with the right length.
1. If Soulseek has no match and the YouTube fallback is on, it tries YouTube.
1. It retags the file from the streaming service's data and puts it in the playlist folder.
1. It writes the `.m3u` in the playlist's order.

![The Downloads page in dark mode, with live speed and time left for each song](docs/screenshots/downloads-dark.png)

To see the songs with one status, click its count over the song list, such as **Failed 12**. A song that failed shows a short reason, such as a copyright block or an age restriction. Click the reason to see the full message.

Run **Download all** again whenever the playlist changes. New songs are added, reordered songs are renumbered, and songs removed from the playlist are marked **Removed upstream** and kept until you click **Prune removed**. The app only ever deletes files it created.

The result looks like this:

```
music/
  AppleMusic/
    Road Trip Mix/
      Road Trip Mix.m3u
      001 - Justin Timberlake - CAN'T STOP THE FEELING!.mp3
      002 - Jack Black - Peaches.mp3
  SpotifyMusic/
    Liked Songs/
```

Media servers that scan your music folder (Navidrome, Jellyfin, Plex) pick up the folders and the `.m3u` playlists. A song that's in your library and in a playlist folder shows up twice in them.

## Copy a playlist to the other service

On a playlist's page, the **Copy to Spotify** or **Copy to Apple Music** panel makes a playlist with the same songs on the other service. Songs are matched by ISRC, or by title, artist, and length when there's no ISRC match. The copy uses the playlist's name in the app, so to give a copy another name, rename the playlist in its **Settings** first.

Matching allows for the usual differences between services: a title Apple Music censors ("F\*\*k"), a dropped g ("Fuckin'" and "Fucking"), a "(Bonus Track)" tag, and a duo credited differently ("JAY-Z & Ye" and "JAY-Z & Kanye West").

The panel links to the songs the other service didn't find. To find one yourself:

1. Click the **songs not found** link in the panel.
1. Next to the song, click **Find**. The app searches by artist and title, and by title alone, which finds a song credited to another artist (such as a 2Pac song credited to Thug Life). Use **Preview** to compare, or search with other words.
1. Click **Use this**. An Apple Music copy gets the song right away; a Spotify copy gets it at the next **Update**. The app keeps your pick from then on.

Songs that weren't found are searched again at the first **Update** a week later.

Click **Update** to bring the copy up to date later. The two directions behave differently:

- **To Spotify:** the copy is made identical every time, with the same songs in the same order.
- **To Apple Music:** new songs are added. Apple's API doesn't let apps remove or reorder songs, so songs removed from the original stay in the Apple Music copy until you remove them in the Music app. A song you remove from the Apple Music copy isn't added back.

## Sync favorites between Spotify and Apple Music

The **Sync favorites** page keeps Spotify Liked Songs and Apple Music Favorite Songs in step. It needs both services connected.

1. On the home page, click **Sync favorites**.
1. Choose one or both directions:
   - **Add songs I like on Spotify to my Apple Music Favorite Songs.**
   - **Add songs I favorite on Apple Music to my Spotify Liked Songs.**
1. To keep them in sync, also select **Keep them in sync: run every 6 hours**. Click **Save**.
1. Click **Check**. The page lists the songs a sync would add to each service, and the songs it can't find there. Nothing changes on either service.
1. Click **Sync now**.

How it behaves:

- **It only adds.** It carries each song across once and remembers the pair, so a song you later remove on either service stays removed. It never removes a favorite.
- **Songs are matched** by ISRC, or by title, artist, and length. A song that's already a favorite on both services, even as a different version, is left alone.
- **On Apple Music**, a favorited song is also added to your library, as the Music app does when you favorite a song.
- **Spotify needs one more permission** to add Liked Songs. If you connected Spotify before this feature, the page asks you to reconnect.
- **The first run takes a while** for a big collection: it searches the other service for each song, roughly 100 songs a minute. Later runs only look at new favorites.
- The app finds Apple's Favorite Songs playlist by its English name. If your Apple Music is set to another language, syncing to Apple Music works, but songs you favorite on Apple Music aren't read.

## Make playlists from your own music

The **My music** page lists every song the app found under your music folder. Search by artist, album, or title, tick folders to narrow the list, and click an artist or album name to see only that. Click a song's cover to play it.

You can make a playlist from it in two ways:

- **Everything that matches:** with no songs selected, name the playlist in the **Make a playlist** panel and click **Make playlist of all N songs**. With **Keep up to date** ticked, the playlist is a filter rather than a fixed list: songs you add to those folders later join it, and songs you delete leave it. The app checks every 6 hours, and whenever you click **Refresh** or update a copy. A song that's in two albums or folders is listed once.
- **Songs you pick:** tick songs, choose **New playlist** or one you made before, and click **Add**. To build a playlist across several pages, add from one page, go to the next, and add again; the menu remembers the playlist you added to last.

To put your whole collection on Spotify or Apple Music, clear the filters (or tick the folders you want), make a playlist with **Keep up to date** ticked, and use **Copy to Spotify** or **Copy to Apple Music** on its page. Then shuffle it in the Spotify or Music app.

To take songs out of a playlist you made, open its page:

1. To find songs, type in **Find songs in this playlist**. It searches titles, artists, and albums.
2. Select songs and click **Leave out of the playlist** in the bar that appears, or click **Remove** on a song. After a search, **Remove all N** takes out every song it found.
3. To see the songs you took out, click the **Left out** count. Click **Put back** on a song, or **Put them all back**.

In a **Keep up to date** playlist, songs you take out stay out of its `.m3u`, its ZIP, its copies, and later refreshes. A playlist of songs you picked drops them instead. Take songs out before you copy the playlist to Apple Music: Apple only lets apps add songs, so a song taken out later stays in the Apple Music copy.

Keep the following in mind:

- **Spotify playlists hold up to 10,000 songs.** A bigger playlist's Spotify copy gets a random 10,000 of its songs, and each **Update** picks a new 10,000. Apple Music copies get every song.
- **The first copy of a big playlist takes a while.** The app searches the service for each song, spaced out so it doesn't get rate-limited: allow one to two hours per 10,000 songs. It remembers what it found, so updates only search for new songs. An Apple Music copy fills up as songs are matched, so you can open it while the copy runs. If the service asks the app to pause, the copy stops with a message, and the next **Update** carries on where it left off. When the service fails for a moment, the app retries. A song whose search still fails is skipped and looked for again on the next **Update**. If 10 searches in a row fail, the copy stops.
- **Songs are matched by artist, title, and length**, or by ISRC when your files have one. A title Apple Music censors, such as "F\*\*k", matches your file's uncensored title. Songs the service didn't find are linked from the copy panel, where you can find them by hand.
- **The playlist's `.m3u` points at your library files** by default, in a folder under `Playlists/`, so it takes no space and media servers such as Navidrome can import it. To get a folder you can copy to an MP3 player instead, set **Songs already in your library** to **Copy into the playlist folder** in the playlist's settings. These playlists keep your files as they are, including `.m4a` files.

Your Apple Music library also appears at the top of the list when you add Apple Music playlists, as **Apple Music library**. Track it like a playlist to download it, or to copy it to Spotify. Spotify's equivalent is **Liked Songs**.

## Make a hits playlist

A hits playlist takes a big collection and keeps only each album's standout songs, so shuffling it skips the filler. It's a **Keep up to date** playlist, so new albums join it as you add them.

To make one, do either of the following:

- On the **My music** page, choose what to include (or clear the filters for everything), tick **Hits only**, and click **Make playlist**.
- On the page of a **Keep up to date** playlist, click **Make a hits playlist**. The new playlist has the same songs, standouts only.

### How songs are picked

Each album gets its 2 best songs, and up to 5 when more of them stand out. A song stands out when it does any of the following:

- Charted on the Billboard Hot 100, from 1958 on.
- Came out as a single within two years of its album, according to MusicBrainz.
- Has at least 1.25 times the album's median listeners on ListenBrainz, or the album's median page views on Genius. Comparing within the album works the same for a band with a few hundred listeners as for one with millions.
- Is in your Apple Music Favorite Songs or Spotify Liked Songs. Favorites always go in.

The following songs are left out:

- **Skits, intros, and interludes,** and songs under 90 seconds, unless they charted or you favorited them.
- **Live, demo, and remix versions**, by their titles, and live albums when the artist has studio albums in the collection.
- **Second copies.** A song on both an album and a greatest-hits record counts once, on the album. Compilations and singles only add songs that stand out and aren't on an album you have.

An album with no numbers at all keeps its first 2 songs. To see what was picked from each album and why, click **See each album's picks** on the playlist page. There you can change the 2, the 5, and the 1.25 under **Change the numbers**. To drop a song you don't want, leave it out on the playlist page: the album's next best song takes its place. When a song makes you want the rest of its album, click **Whole album** on the song: every song from that album goes in, skits and interludes too, and stays in through later rebuilds. To go back to the standouts, click **Back to the picks** on the album under **See each album's picks**.

### Song lookups

The app looks songs up in the background and keeps what it finds, so later rebuilds don't wait on the network. The first run takes an hour or two for a library of about 30,000 songs, because MusicBrainz answers one request a second. The playlist updates after each step. After that, the app looks up new songs every 6 hours, and refreshes counts that are more than 90 days old.

ListenBrainz needs a user token for each artist's listener counts, which catch listens that went to another version of the same recording. Sign in at https://listenbrainz.org, copy the token from https://listenbrainz.org/settings/, and set `LISTENBRAINZ_TOKEN`. Without it, the app uses each recording's own count. If a service refuses or keeps failing, that step stops and the status box says why; the other steps still run.

Songs are found by the MusicBrainz IDs that taggers such as MusicBrainz Picard write into files. For a song without them, the app tries its ISRC, then a MusicBrainz search by artist, title, and length. Genius page views are optional: set `GENIUS_API_TOKEN` to a Genius API client access token, which you can make at https://genius.com/api-clients. Genius allows 10,000 requests a day, so a big library can take two days to finish.

The lookups send song titles, artists, and MusicBrainz IDs to ListenBrainz, MusicBrainz, and Genius, and download the Hot 100 history from the `utdata/rwd-billboard-data` project on GitHub. Nothing about you or your account is sent.

## Check songs and take them with you

- Click a downloaded song's cover to play it in the browser. The player moves on to the next downloaded song. For Apple Music playlists, **Original** plays Apple's 30-second clip of the song so you can compare it with your copy.
- **Download ZIP** sends the playlist's songs and its `.m3u` to the computer you're browsing from, as one ZIP file you can copy to a device. The app checks the size first: it asks before a ZIP bigger than 2 GB, and refuses one bigger than 20 GB, because it could fill the disk it's saved to. A line under the buttons shows how far the download has got.

## Settings

Most settings are on the app's **Settings** page and apply right away: output folder names, numbering, how songs from your library are handled, minimum bitrate, length tolerance, downloads at once, the YouTube fallback, and the Spotify Client ID. Each playlist also has its own settings, including its name and folder. Changing the folder moves the files. Renaming a playlist from a streaming service gives its folder, its `.m3u`, and new copies your name, and renaming it on the service no longer changes it; clear the name to use the service's name again.

The following environment variables configure things that can't change while the app runs:

| Variable | Default | Description |
|---|---|---|
| `MUSIC_ROOT` | `/music` | Music folder inside the container. Playlists go in `AppleMusic/`, `SpotifyMusic/`, and `Playlists/` under it. |
| `LIBRARY_DIRS` | `MUSIC_ROOT` | Comma-separated folders to look in for songs you already own. |
| `DATA_DIR` | `/data` | Database and sign-in tokens. |
| `SLSKD_URL` | `http://slskd:5030` | slskd's address. |
| `SLSKD_API_KEY` or `SLSKD_API_KEY_FILE` | none | slskd API key. |
| `SLSKD_DOWNLOADS` | `/slskd-downloads` | slskd's finished-downloads folder, as mounted in this container. |
| `APPLE_MODE` | `auto` | `developer` (MusicKit key), `web` (no developer account), or `auto` (developer if a key is set up). |
| `APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_KEY_FILE` | none | MusicKit key, for Apple option B. |
| `SPOTIFY_CLIENT_ID` | none | Spotify app Client ID. |
| `SPOTIFY_REDIRECT_URI` | worked out from the address you open | Set it if the app is behind a proxy that hides its public address. |
| `YOUTUBE_FALLBACK` | `0` | `1` to try YouTube for songs Soulseek doesn't have. |
| `APP_PASSWORD` | none | Asks for a password (HTTP basic auth) before showing the app. |
| `GENIUS_API_TOKEN` or `GENIUS_API_TOKEN_FILE` | none | Genius API client access token. Adds Genius page views to hits playlists. |
| `LISTENBRAINZ_TOKEN` or `LISTENBRAINZ_TOKEN_FILE` | none | ListenBrainz user token. Adds each artist's listener counts to hits playlists. |
| `YOUTUBE_COOKIES_FILE` | none | A `cookies.txt` from a browser signed in to YouTube, so age-restricted videos download. yt-dlp writes to it, so it can't be read-only. |
| `SLSKD_CONTROL` | `none` | `docker` or `webhook` adds a **Turn on** button for a stopped slskd. `docker` needs `/var/run/docker.sock` mounted and `SLSKD_CONTAINERS` (container names, in start order); `webhook` needs `SLSKD_CONTROL_URL`. |

## Use an slskd you already run

If you already run slskd (for example, with Lidarr and soularr), run only the `playlist-sync` service and point it at your slskd:

1. Add an API key to your slskd configuration with the `readwrite` role, limited to your Docker network:

   ```yaml
   web:
     authentication:
       api_keys:
         playlistsync:
           key: <a random string of 16 or more characters>
           role: readwrite
           cidr: 172.16.0.0/12
   ```

1. Set `SLSKD_URL` to your slskd's address and `SLSKD_API_KEY` to that key.
1. Mount slskd's finished-downloads folder at `/slskd-downloads`.

The app moves each finished download out of slskd's folder right away, so other tools that watch the same folder don't pick up its files.

## Run Soulseek through a VPN

`docker-compose.vpn.yml` routes slskd's Soulseek traffic through a VPN with [gluetun](https://github.com/qdm12/gluetun). The playlist app and its downloads from Apple, Spotify, and YouTube don't use the VPN.

1. In `.env`, set `VPN_USER` and `VPN_PASSWORD`. The defaults are for Private Internet Access (PIA) in a region with port forwarding (`CA Montreal`).
1. Start both files together:

   ```sh
   mkdir -p gluetun
   docker compose -f docker-compose.yml -f docker-compose.vpn.yml up -d
   ```

1. Run `docker compose logs gluetun` and look for `Initialization Sequence Completed`. The **Soulseek** dot in the app turns green after slskd logs in.

Use the same two `-f` options for every later `docker compose` command, such as `logs`, `pull`, and `down`.

The VPN setup does the following:

- It asks PIA for a forwarded port and gives it to slskd, so other Soulseek users can connect to you. This gets you more search results and faster downloads.
- It refreshes gluetun's list of VPN servers every day. Providers move servers, and a stale list means the VPN never connects.
- It uses UDP port 1198 with PIA's "normal" encryption preset, because the default preset can stall large transfers.

For another provider, set `VPN_SERVICE_PROVIDER` and the settings that provider needs from the [gluetun provider guides](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers), and remove the two PIA lines in `docker-compose.vpn.yml`.

## Update

```sh
git pull
docker compose pull
docker compose up -d
```

The published image has the yt-dlp that was current when the image was built. If YouTube downloads start failing, build the image yourself to get the newest yt-dlp:

```sh
docker compose build --pull --no-cache playlist-sync
docker compose up -d
```

## Troubleshooting

- **`dependency failed to start: container gluetun is unhealthy`.** The VPN didn't connect within 2 minutes. Run `docker logs gluetun`. If it shows `AUTH_FAILED`, the VPN username or password in `.env` is wrong; if the password has a `$` or `#` in it, put it in single quotes, such as `VPN_PASSWORD='pa$$word'`. Then run the same `up -d` command again.
- **The header says "Soulseek offline."** slskd is running but isn't logged in to Soulseek. Check `docker compose logs slskd`. If you use a VPN, check that it's connected.
- **Apple Music asks you to reconnect.** Your sign-in expired. With option A, paste a fresh `media-user-token`.
- **A song stays "Not found."** Soulseek has no MP3 with the right length. Turn on the YouTube fallback, or click **Skip**. Every **Download all** tries not-found songs again.
- **Permission denied writing to the music folder.** Set `PUID` and `PGID` in `.env` to the owner of your music folder, and run `docker compose up -d` again.
- **Downloads are slow.** Each Soulseek peer has its own upload speed. Raise **Downloads at once** on the Settings page to download from more peers in parallel.

## A note on use

This tool is for keeping personal copies of music you have access to. Soulseek is a peer-to-peer network, and downloading from YouTube is against YouTube's terms of service; the YouTube fallback is off unless you turn it on. Check what's legal where you live.

The Apple option A sign-in uses Apple's web player access, which Apple doesn't document for other apps and can change without notice.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -r requirements-youtube.txt
.venv/bin/python -m pytest -q
DATA_DIR=./data MUSIC_ROOT=./music SLSKD_URL=http://localhost:5030 .venv/bin/uvicorn app.web:app --reload
```

The design is described in [docs/design.md](docs/design.md).

## License

[MIT](LICENSE)
