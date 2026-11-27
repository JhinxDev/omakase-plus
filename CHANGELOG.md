# Changelog

All notable public changes to Omakase Plus. Public releases use the same
version in `pyproject.toml`, the package `__version__`, the Git tag and the
GitHub Release. The update-feed identity (`PRODUCT_VERSION` in
`src/omakase/plus/update_service.py`) is a separate operator-facing build
identifier; it does not replace the public release version.

## Unreleased

### Changed

- Consolidated TestFlight shell helpers into `scripts/lib/testflight_common.sh`, added `scripts/asc_cleanup_ephemeral_dev_certs.py` so ephemeral CI runs automatically revoke any `Created via API` Development certificates they mint, and added the sequential `.github/workflows/apple-testflight.yml` dispatch workflow (`platform=all|ios|tvos`, optional `prune_stale_certs`).

## 0.8.0 - 2026-11-27

### Added

- Added shared Discover and native tracking endpoint `POST /plus/api/native-tv-test/v1/library/track` backed by `omakase.plus.discover_tracking`, enabling iOS and Apple TV to save Anime, Movie, and TV watch status, episode progress, `/ 10` ratings, AniList OAuth sync, and `Not for me` dismissals without queuing downloads.
- Added iPhone (`OmakasePlusPhone` 0.8.0) `PhoneRequestDetailView` (tappable from Requests with stage-by-stage timeline, timestamps, reason, fallback search/transfer details, TV episode progress, `Check status`, and `Open in Library`), `PhoneCatalogDetailView` (tappable from Discover & Search with full metadata, synopsis, Request actions, `Watch Trailer` in `SFSafariViewController`, `Track & rate` sheet, and `Not for me`), and `PhoneTrackSheet`.
- Added Apple TV (`OmakasePlusTV` 0.8.0) visual stage timelines, posters, TV episode progress, and `Open in Library` in `TVRequestsScreen`, plus rich metadata badges and Siri-Remote-friendly `Mark Watched`, `Rate / 10`, and `Not for me` actions in `TVForYouScreen` and title details.
- Added unified `Collection`, `Watching`, and `Watched` library filter views across native `/catalog?view=...` and both iPhone and Apple TV apps, preserving watched titles even after cloud copies are cleaned up.

### Changed

- Enriched `/plus/api/native-tv-test/v1/library/for-you`, `/catalog-search`, `/requests`, and `/catalog` payloads with backward-compatible optional metadata (`year`, `format`, `episodes`, `score`, `score_label`, `genres`, `airing`, validated `trailer_id`, `tmdb_id`, `anilist_id`, `watch_status`, `rating`, `library_id`, and `episodes_ready`/`episodes_expected`) and extended `QualificationCore` models and `LiveLibraryClient`.

## 0.7.0 - 2026-11-26

### Added

- Added direct For You status, episode progress, rating, and dismissal tracking (`#track-modal`, `catalog_anime.html`, and `POST /plus/api/discover/track`) with AniList OAuth writeback (`SaveMediaListEntry`) plus local Anime, Movie, and TV tracking without queuing downloads.
- Added proof-backed Real-Debrid Watched bulk cleanup (`media_cleanup_bulk.py`, `media_bulk_cleanup.html`, `/plus/movies/completed/cleanup/{preview,execute}`, and `/plus/tv/completed/cleanup/{preview,execute}`) along with single-title cloud copy removal links on `movie_detail.html` and `tv_detail.html` while keeping watch history and ratings intact.
- Added a connected AI provider strip with brand SVG logos (`DeepSeek`, `OpenAI`, `Anthropic`, `Google Gemini`, `Ollama`, and `Codex`) and connection status on AI Recommendations (`/plus/discover?mode=recommend`).

### Changed

- Unified Collection, Watching, and Watched tab headings, empty states, and clean in-card `.cleanup-select-pill` plus `Select all` cleanup controls across Anime, Movies, and TV Shows, replacing the poster-corner circle overlay.
- Upgraded every `<select>` menu across Omakase Plus to the unified dark `.app-listbox` menu (`listbox.js`) and disabled browser autofill history popups (`autocomplete="off"`) on Find Anime and non-authentication search inputs.
- Eager-loaded the top 6 For You cards per tab and added shared HTTP client pooling plus bounded missing-banner negative caching in `posters.py`.

### Fixed

- Fixed the TV Shows For You `500` failure by reading `title` and `name` from `tv_catalog.metadata_json` in `_seed_tv`.
- Fixed missing posters on Movie `Because You Liked ...` shelves by persisting fetched Seerr/TMDB metadata into `movie_catalog` and parallelizing detail lookups.
- Fixed missing wide hero banners on catalog anime detail pages (`isolation: isolate` on `.series-hero`) and eliminated stuck purple focus contours on For You cards after mouse click and Back navigation.
- Fixed the vertical whitespace gap on AI Recommendations when expanding `Your taste notes` and aligned remaining accent styling with Omakase lavender.

## 0.6.0 - 2026-11-26

### Added

- Upgraded **For You** with multi-shelf personalized rails across Anime, Movies, and TV Shows (`Top Picks for You`, `Because You Liked ...` collaborative recommendations, `Hidden Gems` / `Critically Acclaimed`, `Airing This Season` / `New Releases`, and `AI Tasting Menu`) plus multi-signal affinity scoring across genres, studios, non-spoiler tags, ratings, and collaborative picks.
- Added per-category **Edit For You** shelf customization (`viewer_discover_layout`, `POST /plus/discover/layout`) mirroring **Edit Home**, allowing every profile to reorder or hide shelves independently on Anime, Movies, and TV Shows.
- Added optional automatic AI recommendation refresh scheduling (`discover_refresh_settings`, `POST /plus/discover/refresh-settings`) supporting `Manual only`, `When new watches or ratings arrive`, and `Weekly background check`, with signal-change tracking across all media domains.
- Integrated official YouTube trailers (`trailers.py`) across Anime (AniList), Movies, and TV Shows (Seerr/TMDB) with an inline dark `youtube-nocookie.com` **Watch trailer** modal (`#trailer-modal`), strict YouTube ID validation, `frame-src` CSP scoping, and direct **Open on YouTube** fallback.
- Database migration `069-for-you-layout-and-refresh.sql`.

### Changed

- Unified the **For You** page header actions (`Edit For You` and context-aware `AI Picks`), removed the Anime-only `Find anime` header override and Movies-only inline filter link, and standardized card metadata (`format`, `year`, `episodes`, `score%`), card **Options** (`Watch trailer`, `Already watched`, `Not for me`), and live card removal across all three media tabs.
- Rebuilt `catalog_anime.html` onto the shared compact backdrop title detail hero (`_title_detail.html` and `series-detail.css`) and wired `?from=discover` back navigation across Anime, Movies, and TV detail views.

### Fixed

- Fixed `/plus/api/feedback` so marking a For You movie or TV card as `Already watched` records cleanly locally without triggering AniList/MAL tracker queue errors.
- Guarded `ops/release/build_image.py` to preserve an extra tag (`omakase-plus:prior-<id>`) on any running container's image before rebuilding over an identical revision tag.

## 0.5.0 - 2026-11-26

### Added

- Native iPhone app (`ios-source/`, iOS V1) sharing `QualificationCore` with Apple TV, supporting one-time pairing codes, Keychain credential persistence, household profile switching, Home shelves, synced Edit Home, library browsing, Requests posters, native `AVPlayerViewController`, and GitHub Actions TestFlight Cloud Signing (`.github/workflows/ios-testflight.yml`).
- Public GHCR container image publishing (`ghcr.io/jhinxdev/omakase-plus` via `.github/workflows/docker-publish.yml`), interactive Docker quick installer (`install.sh`), and automated tag-triggered GitHub Releases (`.github/workflows/release.yml` and `ops/release/github_release_notes.py`).
- Live authenticated benchmark suite (`ops/benchmark_live.py`) and requests hit-rate overhaul: tiered release fallback, multi-attempt movie fallback with a 60-second cooldown, TV season-pack acquisition, international and original title search, automatic stale Riven escalation, and Prowlarr search metadata.
- Apple TV (`OmakasePlusTV`) native `AVPlayer` playback (dropping `TVVLCKit`), verified Skip Intro during opening markers, 10-second Up Next auto-play countdown, Audio and Subtitle language pickers in Settings, TV season selector, Media Info inspector, and Internal TestFlight Cloud Signing (`.github/workflows/tvos-testflight.yml`).
- Database migrations `057` through `068` covering native watch-credit intent, For You requests, synced Home layout, request candidates, TV collection additions, movie and TV fallback transfers, title reviews, Real-Debrid completion cleanup, season packs, and multi-attempt movie fallback.

### Changed

- Unified Anime, Movies, and TV Shows browse search/filter pop-ups and compact backdrop title detail heroes (`_title_detail.html` and `series-detail.css`), removing Anime's legacy `Overview | Episodes` bar and raw browse-card quality badges while listing exact 720p fallback episode numbers on detail pages when present.
- Redesigned Requests with plain-English status pills, TV episode progress bars, submitter and activity records, verified Real-Debrid episode visibility during Riven outages, and collapsible diagnostics; separated History into viewing, library activity, and collection changes.
- Tuned SQLite WAL and cache PRAGMAs, batched library queries, added a shared loop-scoped HTTP/2 connection pool and 20-second Real-Debrid downloads cache, pre-warmed playback startup, raised PGS caption extraction timeout to 20 seconds, and retired legacy HLS/dashboard paths (`prepared_hls.py`, `debrid_seek.py`, `debrid-player.js`, `/cloud/player`, and `dashboard.html`).
- Refreshed the public README, `v6` redacted showcase captures, Bring Your Own Media documentation, Compose chain collapse (`ops/release/collapse.py` and `promote.py --collapse-chain`), CI pip/Buildx caching, and the embedded YouTube walkthrough.

### Fixed

- Fixed the `Dim artwork` toggle in the user account menu so clicking persists and updates the page immediately instead of navigating away.
- Fixed Real-Debrid completion review cleanup so removing media queues provider cleanup reliably, dismisses the completed notification immediately, and replaces the ambiguous `Cancel` button with a clear `Back` action.
- Fixed web player unbuffered VOD HLS seeking without reloading `index.m3u8`, parallel cloud subtitle state synchronization, Shaka `playbackRate == 0` telemetry filtering, `data:` URI `media-src` CSP support in `omakase.plus.web_security`, and Settings validation and reset alignment.
- Fixed Apple TV focus rendering, Top Shelf artwork framing, duplicate Up Next overlays, and `UIRequiredDeviceCapabilities` (`arm64`) plus `ITSAppUsesNonExemptEncryption = false` for App Store Connect TestFlight uploads.
- Fixed CI reliability by skipping `ffmpeg`-dependent media tests when `ffmpeg` is absent, guarding cancelled warm tasks, and running performance budgets sequentially after parallel `pytest-xdist`.

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

### Fixed

- Large HEVC TV files can use the native Apple TV HLS path and avoid the Plex
  CPU transcode path.

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






