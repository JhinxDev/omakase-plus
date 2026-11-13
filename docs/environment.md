# Environment variables

This page lists the environment variables read by the application source. Values in the example files are
starting points; credentials belong in `.env.local.secrets` or deployment secrets, never in Git.

Defaults below are the source fallbacks. A deployment compose file, image build or operator environment may set a
different value; check the exact deployment when diagnosing behavior. `OMAKASE_PLUS_PRIVATE=true` is what turns a
plain BYOK/Lite deployment into the authenticated Plus product.

## Core runtime

| Variable | Default / requirement | Purpose |
| --- | --- | --- |
| `OMAKASE_ENV` | unset | Set to `production` to enable production outbound URL guards. |
| `OMAKASE_DATA_DIR` | `data` | Data directory for the SQLite database, protected secrets and caches. |
| `OMAKASE_REVISION` | `dev` / `unknown` | Build revision reported by health and about surfaces; set by the image build. |
| `OMAKASE_PORT` | `8765` | Port for the `omakase web` command. |
| `OMAKASE_PLUS_URL` | `http://localhost:8765` | Public origin used for OAuth redirects and secure-cookie decisions. |
| `OMAKASE_ALLOWED_HOSTS` | empty | Comma-separated trusted Host header values; empty disables host validation. |
| `OMAKASE_POSTER_CACHE` | empty | Poster cache directory; empty uses the application data area. |
| `OMAKASE_UPDATE_CHECKS` | `true` | Set `false` to skip the update feed check. |
| `OMAKASE_SESSION_COOKIE_SECURE` | derived | Force the session cookie's Secure flag. When empty, it is derived from an `https://` `OMAKASE_PLUS_URL`. |
| `OMAKASE_CSRF_KEY` | master key | HMAC key for session-bound CSRF tokens; empty falls back to `OMAKASE_PLUS_MASTER_KEY`. |
| `OMAKASE_PLUS_MASTER_KEY` | empty | Master key that encrypts saved service credentials. Required before saving provider keys; keep it out of source. |

## Plus product, sessions and features

| Variable | Default / requirement | Purpose |
| --- | --- | --- |
| `OMAKASE_PLUS_PRIVATE` | `false` | `true` enables the authenticated Plus product, its private surfaces and the shipped Plus compose behavior. |
| `OMAKASE_PLUS_MOVIE_CLEANUP_ENABLED` | `false` | Enables the optional cleanup setting and worker for newly acquired, owner-bound movies. Leave it off until the connected Riven backend supports guarded withdrawal and Plex and Real-Debrid readbacks have been qualified. |
| `OMAKASE_PLUS_INVITE` | empty | Non-empty opens signup for invitation-driven registration. Empty keeps signup closed except the first-account bootstrap in a new private instance. |
| `OMAKASE_SESSION_DAYS` | `90` | Sliding idle session lifetime in days, bounded to 7 through 365. |
| `OMAKASE_SESSION_ABSOLUTE_DAYS` | `365` | Absolute session lifetime in days, bounded to 7 through 730. Older sessions are rejected and deleted. |
| `OMAKASE_PLUS_REVIEW_MODE` | `false` | `true` keeps risky provider and tracker writes in review/outbox mode. |
| `OMAKASE_PLUS_LANGUAGE_PUBLICATION_QUALIFIED` | `false` | Operator qualification flag for optional language publication. |
| `OMAKASE_PLUS_QUALITY_PUBLICATION_QUALIFIED` | `false` | Operator qualification flag for optional quality publication. |
| `OMAKASE_TRACKER_BRIDGE_WRITERS_READY` | `false` | Declares that the legacy tracker writers are ready for bridge operation. |
| `OMAKASE_PLUS_APPLE_TV_WATCH_ENABLED` | `false` | Enables the optional Apple TV watch bridge. |
| `OMAKASE_NATIVE_TV_TEST` | empty | `1` enables the opt-in native Apple TV playback test surface. It also requires a valid `OMAKASE_NATIVE_TV_ORIGIN`. |
| `OMAKASE_NATIVE_TV_ORIGIN` | empty | Exact HTTPS origin for the native Apple TV client. Credentials, a path and a non-HTTPS scheme are rejected. |
| `OMAKASE_NATIVE_TV_LIBRARY` | empty | `1` enables the linked native Apple TV library and device pairing API. |
| `OMAKASE_NATIVE_TV_HLS` | empty | `1` enables native HLS streaming for qualified single-part Plex MKV sources. |
| `OMAKASE_NATIVE_TV_PLEX_HLS` | empty | `1` enables the native Plex-compatible HLS path for qualified sources that need audio or container conversion. |
| `OMAKASE_NATIVE_TV_INDEXED_CAPTIONS` | empty | `1` enables indexed SRT and subrip captions in the native compatible playback path. |
| `OMAKASE_PLUS_PLEX_MANAGED_ROOT` | empty | Managed symlink root used by supported web playback; empty disables the managed source. |
| `OMAKASE_PLAYBACK_CACHE_ROOT` | `/run/omakase-playback` | Dedicated bounded playback cache directory. Do not point it at originals, the database or general storage. |
| `OMAKASE_PLAYBACK_STREAMING` | `false` | Enables MKV streaming and the Plex audio compatibility path for qualified films and TV episodes. |
| `OMAKASE_PLAYBACK_STREAM_CACHE_ROOT` | `/home/omakase/data/playback-streams` | Disk-backed cache directory for direct-stream HLS segments. |
| `OMAKASE_PLAYBACK_STREAM_CACHE_MAX_BYTES` | `6442450944` (6 GiB) | Maximum stream-cache size; oldest inactive sessions are pruned above it. |
| `OMAKASE_LIBRARY_MANIFEST_TOKEN` | empty | Bearer token for the curator manifest endpoints. |
| `OMAKASE_LIBRARY_ROOT` | `/media/library` | Media library root used by onboarding and language checks. |
| `OMAKASE_IMPORT_ROOT` | `/media/import` | Local import root used by onboarding and storage checks. |

## Connections and acquisition

| Variable | Default / requirement | Purpose |
| --- | --- | --- |
| `OMAKASE_PLEX_URL` | empty | Plex base URL for CLI doctor and media commands. |
| `OMAKASE_PLEX_TOKEN` | empty | Plex token for CLI doctor and media commands. |
| `OMAKASE_QBITTORRENT_URL` | empty | qBittorrent Web UI base URL. |
| `OMAKASE_QBITTORRENT_USERNAME` | empty | qBittorrent username. |
| `OMAKASE_QBITTORRENT_PASSWORD` | empty | qBittorrent password. |
| `OMAKASE_QBITTORRENT_CATEGORY` | `omakase` | qBittorrent category used for Omakase downloads. |
| `OMAKASE_QBITTORRENT_SAVE_ROOT` | `/downloads/omakase` | Expected qBittorrent save root for download checks. |
| `OMAKASE_VPN_MODE` | `none` | Doctor acquisition network mode: `none`, `gluetun` or `external`. |
| `OMAKASE_EXTERNAL_VPN_INTERFACE` | empty | Host interface name when `OMAKASE_VPN_MODE=external`. |
| `OMAKASE_ACQUISITION_MODE` | `import-only` | Doctor acquisition mode: `import-only`, `real-debrid` or `qbittorrent`. |
| `OMAKASE_LLM_URL` | `http://localhost:11434` | LLM base URL for CLI commands. |
| `OMAKASE_MODEL` | `qwen2.5:7b` | Model name for CLI commands. |
| `OMAKASE_ANIMETOSHO_ENABLED` | `false` | Enables the AnimeTosho catalog source. |
| `OMAKASE_ASR_MODEL` | `tiny` | faster-whisper model used by the ASR subtitle fallback for sources with no text or PGS subtitle tracks. |
| `OMAKASE_ALLOW_LOOPBACK_MEDIA_URLS` | `false` | Allows loopback media URLs for local testing. |
| `OMAKASE_ALLOW_PRIVATE_CATALOG_URLS` | `false` | Allows private catalog and Prowlarr URLs. In production this must be an explicit decision. |
| `OMAKASE_ALLOW_PRIVATE_LLM_URLS` | `true` unless production | Allows private LLM endpoints. Defaults to `false` when `OMAKASE_ENV=production`. |

## Accounts, OAuth and administration

| Variable | Default / requirement | Purpose |
| --- | --- | --- |
| `OMAKASE_SEED_EMAIL` | empty | Email used by `python -m omakase.plus.admin seed-user`. |
| `OMAKASE_SEED_PASSWORD` | empty | Password used by the seed command. |
| `OMAKASE_ADMIN_NEW_PASSWORD` | empty | Non-interactive input for `set-password`; empty prompts with hidden confirmation. |
| `ANILIST_CLIENT_ID` | empty | AniList OAuth application client id; empty disables the AniList connection. |
| `ANILIST_CLIENT_SECRET` | empty | AniList OAuth application client secret. |
| `MAL_CLIENT_ID` | empty | MyAnimeList OAuth client id; empty disables MAL tracking. |
| `MAL_CLIENT_SECRET` | empty | MyAnimeList OAuth client secret. |
| `GOOGLE_CLIENT_ID` | empty | Google OAuth client id; empty disables Google sign-in. |
| `GOOGLE_CLIENT_SECRET` | empty | Google OAuth client secret. |
| `GOOGLE_REDIRECT_URI` | empty | Google OAuth redirect URI. It must match the registered redirect exactly. |

## Keeping this reference honest

The test `tests/test_documented_environment.py` scans the application source for `os.getenv(...)` names and
Click `envvar=` options and fails when a variable is missing from this page. Add the variable here in the same
change that introduces it, and never put a real secret value in this file.

