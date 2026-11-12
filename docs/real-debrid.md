# Real-Debrid playback

Omakase Plus can use a personal Real-Debrid account as a standalone media source.
No Plex server or NAS is required, and Plex can stay connected as an optional
source for a mixed setup.

## Connect

1. Open **Settings → Connections** in the web app.
2. Paste a Real-Debrid API token created in your Real-Debrid account.
3. Set **Playback source** to **Real-Debrid only**, **Plex only**, or **Auto**.

Credentials stay encrypted on your server. The token is never sent to the Apple
TV or another browser.

## Catalog matching

The server matches tracked anime, movie and TV titles against the torrents and
downloads already in the Real-Debrid account. A title is offered for direct
playback only when a matching video file exists. Confirmed matches are resolved
to one exact file, so playback does not guess between random copies.

Cloud files that are not matched to a title remain available under
**Settings → Connections → Browse Cloud files**. They play in the main web player
with the same progress and resume behavior as the rest of the library.

## Playback behavior

- Warm and preloaded episodes start quickly; a never-opened episode has one
  short provider transcode wait for its first segment.
- Near the end of an episode, the next episode is prepared in the background.
- Embedded text subtitles are extracted and exposed as HLS subtitle tracks with
  an always-on preference.
- Long seeks on H.264 copies use a seekable timeline built from the file's own
  keyframe index.
- HEVC copies use exact six-second H.264 seek segments generated from the direct
  file. Far seeks work instead of erroring, with about eight seconds to prepare a
  far segment and a 720p conversion for that window.
- Titles that only carry image-based PGS subtitles do not have a text subtitle
  track yet.

## Limits

Real-Debrid can block individual files at the provider level. Such titles show
as unavailable instead of retrying forever. Provider transfer limits and
account fair-use rules still apply. Use only media and accounts you are
authorized to access.
