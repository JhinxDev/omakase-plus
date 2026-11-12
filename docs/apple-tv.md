# Native Apple TV app

The native tvOS app is the recommended living-room client for Omakase Plus. It
talks to your own authenticated server and can play from Plex or directly from a
connected Real-Debrid account.

## What the app does

- Browse Anime, Movies and TV shelves, with Continue Watching and title details.
- Pair a device with a one-time code from the web app; the device stays linked
  until you remove it in **Settings → Devices**.
- Play episodes and films with native AVPlayer controls, resume sync and a
  next-episode control.
- Remember playback progress on the server, so a title resumes on the web, the
  Apple TV or another linked device.
- Finishing an episode credits the anime progress, History and Continue Watching
  order at the same 85 percent threshold as the web player, and queues the
  AniList sync through the durable tracker outbox.
- Show embedded text subtitles as player subtitle tracks, with English selected
  automatically and an always-on subtitle preference in Settings.
- Preload the next episode when the current one is near its end so Next starts
  quickly.
- Refresh the current shelf with the circular-arrow button. Back returns from an
  episode to the series page before the shelf.
- After about ten seconds of waiting, the loading screen switches to a subtle
  looping animation instead of a plain spinner.

## Playback sources

The app follows the account's playback source setting:

- **Real-Debrid only** uses the connected Real-Debrid account and never calls
  Plex. Matched anime, movie and TV entries play from the Real-Debrid copy.
- **Plex only** uses the exact Plex copy and Plex playback.
- **Auto** prefers the Real-Debrid copy and falls back to Plex when needed.

No Plex server or NAS is required for the Real-Debrid path.

## Subtitles

The server probes each Real-Debrid video for embedded text subtitle tracks
(ASS, SRT, WebVTT and similar), converts them in the background and exposes them
as HLS subtitle tracks. The Apple TV shows its standard subtitle menu, and the
**Subtitles: Always on** setting selects English automatically when a title has
text subtitles.

Image-based PGS subtitles cannot be converted to text without OCR, so titles
that only ship PGS currently have no subtitle track.

## Seeking

Real-Debrid playback gives instant warm starts and a short seek within the
provider's ready window. For H.264 copies, Omakase can also build a seekable HLS
timeline from the file's keyframe index, which supports long jumps. HEVC titles
currently remain on the provider transcode path while HEVC fragment timestamps
are being corrected.

## Building and installing

The app is installed on the Apple TV through the project's own signing flow. See
the repository release checklist for the current build procedure and required
Apple developer signing identity.
