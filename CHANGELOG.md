# Changelog

All notable public changes to Omakase Plus. Public releases use the same
version in `pyproject.toml`, the package `__version__`, the Git tag and the
GitHub Release. The update-feed identity (`PRODUCT_VERSION` in
`src/omakase/plus/update_service.py`) is a separate operator-facing build
identifier; it does not replace the public release version.

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





