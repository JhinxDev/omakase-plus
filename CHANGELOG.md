# Changelog

All notable public changes to Omakase Plus. Public releases use the same
version in `pyproject.toml`, the package `__version__`, the Git tag and the
GitHub Release. The update-feed identity (`PRODUCT_VERSION` in
`src/omakase/plus/update_service.py`) is a separate operator-facing build
identifier; it does not replace the public release version.

## 0.4.12 - 2026-11-12

### Added

- Apple TV playback now credits movie and TV completions at the same 85 percent
  threshold as anime. Movie History, TV episode History and Continue Watching
  ordering update on the server without a native rebuild.
- Migration 056 records the Apple TV source detail on movie and TV history rows
  while preserving existing Plex and manual values.
- HEVC Real-Debrid copies now get a seekable timeline of exact six-second H.264
  segments generated from the direct file. Far-forward seeks work instead of
  erroring; a far segment takes about eight seconds to prepare at 720p.

## 0.4.11 - 2026-11-12

### Fixed

- HEVC HLS master playlists now include deterministic video and audio
  `CODECS` hints even when the local ffmpeg omits them, covering HDR, SDR,
  muxed and alternate-audio variants.
- Apple TV playback now credits anime progress at the same 85 percent
  threshold as the web player. It updates the local watch entry, History and
  Continue Watching order, and queues the AniList progress outbox.
- Ruff formatting and the native TV environment reference are now clean in CI.

### Changed

- The README setup visual is rebuilt with the current Plex-optional,
  Real-Debrid and Apple TV source choices, using the projects' real logos.

## 0.4.10 - 2026-11-12

### Added

- Native Apple TV library with one-time device pairing, Anime, Movies and TV
  shelves, title details, resume sync, a next-episode control and automatic
  refresh after playback.
- Standalone Real-Debrid playback on the web and Apple TV. Matched anime,
  movie and TV entries resolve to one exact Real-Debrid file, with Plex-only,
  Real-Debrid-only and Auto source modes. Plex and a NAS stay optional.
- Embedded text subtitle extraction from Real-Debrid files, exposed as HLS
  subtitle tracks with a player picker, English auto-selection and an
  always-on subtitle setting.
- Quiet next-episode preloading near the end of an episode, plus catalog and
  series-page warming so the likely next item is ready.
- Seekable H.264 playback built from the Real-Debrid file's own keyframe index,
  with on-demand ffmpeg segments and look-ahead caching. A 33-minute seek
  returned a playable H.264/E-AC3 segment in about 9.6 seconds.
- A looping loading animation after roughly ten seconds of waiting, an
  icon-only spinning Refresh control, and Back returning to the series page
  before the shelf.
- Apple TV playback now credits anime progress at the same 85 percent
  completion threshold as the web player, updates History and Continue
  Watching order, and queues the AniList progress sync.

### Changed

- Play replies return before the cold first segment is ready and warm it in the
  background. Measured warm or preloaded starts dropped from about 12.5 seconds
  to 1.2 to 2.0 seconds, with cached first segments in milliseconds.
- The Cloud tab is removed from the Apple TV app; source information lives in
  Settings, with Cloud files still available from the web app.
- The public README and new Apple TV and Real-Debrid pages describe the current
  source modes, subtitles, fast starts, seeking and remaining limits.

### Known limitations

- HEVC titles currently keep the provider transcode path while HEVC fragment
  timestamps are corrected, so long forward seeks on those titles remain
  limited.
- Movies whose only subtitles are image-based PGS tracks have no text subtitle
  track until an OCR or burn-in fallback is added.

## 0.4.9 - 2026-11-09

### Added

- Home shows Play or Resume on the artwork with the episode number, and episode
  lists use a consistent play control.
- For You allocates unique anime across personalized, genre, airing and film
  shelves. Already watched titles offer an optional score before saving.
- Settings opens on a directory, with picture quality and fallback choices
  first and advanced download rules separate.

### Changed

- The player shows the full episode length while the stream prepares, and the
  Windows Chromium player composites styled subtitles with the picture.
- After marking a movie watched, Omakase offers a read-only review of its exact
  Plex copy while keeping watch history and rating.

## 0.4.8 - 2026-11-09

### Added

- For You poster shelves for continuing titles, recommendations, favourite
  genres, airing anime and films, plus a catalogue-backed movie and TV shelf.
- Ask opens a menu for the separate AI recommendation flow.

### Fixed

- The optional HLS player retries a transient startup timeout once and
  distinguishes it from an unsupported codec.
- FFmpeg publishes complete HLS segments, and styled subtitles prepare
  independently so video can play while subtitles load or are unavailable.
- A transient publication conflict retries safely while account and media
  access checks still apply.

## 0.4.7 - 2026-11-06


### Fixed

- Seeking beyond the currently buffered HLS segments now briefly reloads the same media at the requested timeline position through Shaka. A seek to 10 minutes lands at 10 minutes even when the MSE buffer only reached 4 minutes, instead of clamping to the buffer edge. The progress session and styled subtitle overlay remain attached.
## 0.4.6 - 2026-11-06

### Fixed

- The player timeline now uses the larger of the media element duration and Shaka's reported seek range, and seeks through the native media element path used by Shaka's own controls. It no longer calls a nonexistent player.seek API.
- Keyboard Home and End use the same server timeline duration.
## 0.4.5 - 2026-11-06

### Fixed

- The Plus Content-Security-Policy now includes `'wasm-unsafe-eval'` in `script-src`, which the styled ASS subtitle renderer needs to compile its WebAssembly worker. Without it, attached subtitles failed with "Styled subtitles cannot display in this playback mode".
## 0.4.4 - 2026-11-06

### Fixed

- The streaming subtitle endpoint now returns 404 until the background ASS extraction has written non-empty output. Previously the empty placeholder could be attached by the player and produce a styled-subtitle error at playback start.
- The Plus Content-Security-Policy now allows same-origin `blob:` URLs for media and workers, which Shaka MediaSource requires. Without it, playback stalled before requesting the HLS playlist.
## 0.4.3 - 2026-11-06

### Added

- Direct-stream HLS playback for MKV sources in supported browsers. Playback starts
  after the first few segments instead of preparing a complete copy, so large anime
  episodes and movies are no longer limited by the prepared-remux size cap. Seeking
  works as segments become available and becomes fully available when the stream
  completes.
- Background ASS subtitle extraction for streaming sessions, attached through the
  existing styled-subtitle renderer when ready.
- `OMAKASE_PLAYBACK_STREAMING`, `OMAKASE_PLAYBACK_STREAM_CACHE_ROOT` and
  `OMAKASE_PLAYBACK_STREAM_CACHE_MAX_BYTES` configuration for the stream cache.

### Changed

- Prepared copy-remux remains available for browsers or sources where streaming is
  unavailable. Plex remains the explicit fallback for unsupported codecs.

## 0.4.2 - 2026-11-06

### Added

- A branded browser 404 page inside the Plus product. Machine clients still
  receive the JSON `{"detail": "Not Found"}` response.
- A signed-out notice on the sign-in page.

### Changed

- Logout now returns to `/plus/login` instead of the legacy unauthenticated
  root.
- The package `__version__` and `pyproject.toml` version are synchronized at
  0.4.2.

### Security

- Signup applies the same minimum password policy as password changes
  (8 to 1024 characters).
- Sessions now have an absolute lifetime in addition to the sliding idle
  lifetime. `OMAKASE_SESSION_ABSOLUTE_DAYS` defaults to 365 and is bounded to
  7 through 730 days.
- Login throttling prunes stale entries and caps tracked source addresses at
  4096 to bound memory use.

## 0.4.1 - 2026-11-06

### Added

- Settings, Security page with password change and sign out other sessions.
- Password changes revoke every other session.
- Operator recovery commands: `python -m omakase.plus.admin set-password` and
  `revoke-sessions`.

### Documentation

- Public changelog and security guide updated with account security and
  recovery limits.

## 0.4.0 - 2026-11-06

### Added

- Optional authenticated in-browser anime web player.
- Documentation site refresh, public trust pages and release identity.
- Docker build CI gate, hardened repository defaults and dependency security
  settings.

### Fixed

- Public CI reliability, packaging hygiene and the documented release
  surface.






