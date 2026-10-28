# Apple TV watch bridge

## Current decision

Keep `OMAKASE_PLUS_APPLE_TV_WATCH_ENABLED=false` for Crunchyroll on the current
Apple TV. Omakase Plus has a bounded, signed ingestion path, but the observed
Crunchyroll metadata is not precise enough to change progress safely.

The read-only 2026-08-27 calibration found one redacted Apple TV media-player
entity. Recent Crunchyroll states contained:

- `app_name`, `media_content_id`, `media_content_type`, `media_duration`, and
  `media_title`;
- an episode display title, but no `media_series_title`, `media_season`, or
  `media_episode`;
- a Crunchyroll episode content identifier that maps to an official watch page,
  but no documented, stable API contract that Omakase can use to resolve the
  owning series.

Home Assistant deliberately excludes `media_position` from recorder history,
so its absence in the SQLite evidence does not prove that it is absent from the
live entity. Exact series, season, and episode identity were absent from the
recorded state and are independently required, so automatic writes remain
unsafe either way. No Home Assistant entity, automation, credential, or app
setting was changed during calibration.

For now, Crunchyroll viewing is updated from the title page with **Mark episode
watched** or **Watched through episode**. Plex remains an exact automatic source.
The synthetic Apple TV confirmation in the browser receipt proves Omakase's
uncertain-event UI; it is not evidence that the current Crunchyroll app provides
enough metadata to generate that suggestion unattended.

## Confidence behavior

The endpoint accepts only Crunchyroll observations whose series title matches
exactly one title owned by the bridge user.

- Exact series, season, episode, and at least 85 percent playback may advance
  progress automatically.
- A known, unique series with incomplete episode identity creates a pending
  confirmation instead of changing progress.
- Unknown, ambiguous, non-Crunchyroll, stale, replayed, or malformed observations
  are ignored or rejected without changing progress.
- Automatic observations are monotonic. Only an explicit owner correction can
  move progress backward or reopen a completed title.

Completion updates the local lifecycle and the AniList outbox. Completed titles
move out of the default Library view into **Completed**; media cleanup remains a
separate owner-confirmed action.

## Signed request contract

Create or rotate the one-time bridge key under **Settings → Tracking
connection → Apple TV via Home Assistant**. Store the revealed secret only in a
private Home Assistant secret or equivalent runtime secret store.

Send `POST /plus/api/watch/apple-tv` with compact JSON no larger than 16 KiB:

```json
{
  "event_id": "stable-idempotency-key",
  "observed_at": "2026-08-27T12:00:00Z",
  "device_id": "private-device-alias",
  "app_name": "Crunchyroll",
  "series_title": "Exact Omakase library title",
  "season": 1,
  "episode": 7,
  "position_seconds": 1375,
  "duration_seconds": 1500
}
```

The request headers are:

- `X-Omakase-Key-Id`: the public integration key identifier;
- `X-Omakase-Timestamp`: current Unix time in seconds;
- `X-Omakase-Signature`: `sha256=<lowercase hex HMAC>`.

Calculate the HMAC-SHA256 over the exact bytes
`<timestamp>.<raw-json-body>` using the one-time secret. Omakase rejects a
signature or observation more than five minutes old, a disabled key, invalid raw
bytes, inconsistent duration, and bodies above 16 KiB. Reusing an `event_id` is
idempotent.

## Activation gate

Do not enable the environment variable until all of these are true for a fresh
Crunchyroll session:

1. Live Home Assistant state exposes the exact series, season, episode, position,
   and duration without scraping a private or undocumented Crunchyroll API.
2. A signed dry-run request is accepted with the expected title but is kept below
   the watched threshold, proving that progress does not change.
3. Replay, stale timestamp, wrong app, ambiguous title, and missing-episode cases
   remain fail-closed.
4. The secret is stored outside Git and can be rotated from Omakase Settings.
5. The private deployment has a verified pre-migration SQLite backup.

If a future tvOS, Crunchyroll, pyatv, or Home Assistant release changes the
metadata, repeat calibration. Do not infer safety from a previous app version.
