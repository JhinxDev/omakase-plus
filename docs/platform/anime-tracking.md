# AniList and watch tracking

## Recommendation history

Omakase reads the user's AniList or MyAnimeList history for recommendation
evidence. A public username is sufficient for public list reads. Remote list
writes require an explicit OAuth connection.

In Settings, connect AniList using an AniList OAuth application whose callback
is:

```text
https://your-private-omakase-host/plus/integrations/anilist/callback
```

The OAuth handshake uses PKCE, an owner-scoped single-use `state`, a ten-minute
expiry, and durable server-side flow state so a restart or multiple web workers
does not break the callback. The resulting access token is encrypted at rest.

## “Already watched” means My Anime List

The Discover action performs two coordinated operations:

1. Immediately records the recommendation disposition so the title is excluded
   from future local recommendation prompts.
2. Creates or updates the connected AniList entry as `COMPLETED`, with an
   optional 1–10 score.

The remote mutation is durable and idempotent. If AniList is disconnected or
temporarily unavailable, Activity shows a failed/pending list action with a
safe retry. Omakase does not pretend a local hide is a successful remote list
update.

The tracker service is provider-neutral, but MyAnimeList writes are not enabled
in this release; unsupported actions are recorded honestly rather than dropped.

## Plex playback tracking

Plex playback and recommendation dispositions solve different problems.
AniBridge or another dedicated connector may update AniList from Plex watch
progress. If enabled:

- scope it to the Anime and Anime Movies libraries;
- use stable AniList/Plex identity mapping;
- decide the completion threshold explicitly;
- preserve its own credentials outside Omakase;
- monitor failed syncs and conflicts;
- do not let it overwrite an intentional Omakase score silently.

Omakase remains authoritative for recommendation feedback and request history.
The tracker remains authoritative for list state. Plex remains authoritative
for actual playback position. Conflicts should be visible, not resolved by
guessing.
