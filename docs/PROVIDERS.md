# Playback providers

Omakase Plus plays one media provider at a time per household. The registry in
`src/omakase/plus/providers/playback.py` is the single place that knows provider
ids, provider capabilities and how a saved choice resolves. No other module
compares provider ids.

## Capability matrix

| Provider id | Label in Settings | catalog | play | subtitles | seek | direct_file | requires_token | requires_link | Playback route |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `realdebrid` | Real-Debrid | yes | yes | yes | yes | no | yes | no | cloud file inventory |
| `plex` | Plex | yes | yes | yes | yes | no | yes | yes | managed Plex publication |
| `local` | Local storage or NAS | yes | no | no | no | yes | no | yes | local file inventory, delivery not implemented |

Column meanings:

- `catalog`: the household catalog can be built for this provider without
  calling another provider.
- `play`: the provider can serve playback today.
- `subtitles`: subtitle tracks are available from this provider.
- `seek`: bounded seeking is supported on this provider path.
- `direct_file`: the provider exposes a direct file locator that does not need a
  remote API call to identify the file.
- `requires_token`: a saved account token or API key is required before the
  provider is selectable.
- `requires_link`: a saved server address or storage backend registration is
  required before the provider is selectable.

`local` has `play` false and `subtitles` false today. Local imported files are
published into the managed Plex source root and are streamed back through the
Plex gateway, so a NAS-only household cannot play yet. That is a known gap, not
a hidden fallback: the web player refuses a selected local provider with an
explicit message instead of borrowing Plex.

## Selection order

`providers.playback.select(db, user_id)` returns a `Selection` with the resolved
provider, the saved value, a source (`saved`, `auto`, `unavailable`, `household`)
and, when relevant, a reason.

1. A valid saved choice wins while its link requirement is satisfied. Valid
   stored values are `auto`, `realdebrid`, `plex` and `local`; any other stored
   value is treated as `auto`.
2. A saved choice whose link is missing falls back to the automatic order and
   records `source == "unavailable"` plus a reason like `Plex is not connected.`
3. The automatic order is `realdebrid`, then `plex`, then `local`.
4. When nothing is linked the selection has no provider, and the legacy
   `playback_provider` helper returns `auto` so older call sites keep their
   previous behaviour.

Consequences:

- A household with a connected Real-Debrid account plays from Real-Debrid even
  when Plex is also linked and no explicit choice was saved.
- Plex is selected only by households that linked Plex and did not pick another
  provider explicitly.
- A household with only a registered local storage backend can select
  `local` in Settings and browse its catalog.
- Shared household libraries always play through the managed publication; that
  is the `shared_publication()` selection and it is not user selectable.

## Isolation guarantee

Every provider path is expected to touch only its own client:

- `realdebrid`: catalog, details, play, subtitles and progress go through the
  Real-Debrid library, the cloud routes and the provider-neutral progress
  tables. No Plex client call is allowed.
- `plex`: catalog, details, play, subtitles and progress go through the managed
  Plex publication.
- `local`: catalog, details and progress read only Omakase's own tables. Play is
  refused until direct local delivery exists.

`tests/plus/test_provider_isolation.py` proves this. Its `plex_tripwire` fixture
patches every Plex seam (`connections._values`, every `plex*` credential read
except the link probe, `playback_proof` transport, `web_playback_source`,
`web_playback_catalog`, `web_playback_navigation`, `personal_plex_movies`,
`MovieService` and `PlexClient`) so any Plex use raises `PlexNotAllowed`. The
Real-Debrid test then exercises catalog, details, three play flows with HLS
segments, a native resume checkpoint, a cloud progress save, a cloud playback
session, byte delivery and the subtitle route with a connected Real-Debrid
account and no Plex link at all.

The same file pins the capability matrix, the automatic order, backwards
compatibility of saved values, the Plex-only web player path and the local
catalog path, and it fails if a policy module compares a provider id directly.

## Adding a provider

Two edits, both inside `src/omakase/plus/providers/playback.py`:

1. One module, one link check and one route constant. A provider needs a module
   that owns its adapter (catalog, play, subtitles, seek) and a
   `linked(db, user_id) -> bool` function that answers whether the household's
   link is present. Reuse `CLOUD_ROUTE`, `PUBLICATION_ROUTE` or `LOCAL_ROUTE`
   when the new provider plays like an existing one; add a new route constant
   only when playback needs a genuinely different transport.
2. One registry entry in `PROVIDERS` with `id`, `label`, `auto_rank`, `route`,
   `PlaybackCapabilities` and `linked`.

Nothing else needs to change for the provider to appear in Settings: the
template renders `provider_options()`. Call sites ask `Selection.supports(...)`
or `Selection.cloud_route` / `Selection.publication_route`, so they need no
provider name. Give the provider an `auto_rank` that reflects where it belongs
in the automatic order.

## Migration note for existing saved settings

No database schema change and no migration are required.

- The saved value lives in the existing encrypted per-user secret
  `preferred_playback_provider`, written by Settings, playback source.
- Every value that was valid before (`auto`, `realdebrid`, `plex`) stays valid.
  `local` is newly valid and is rejected only by deployments that have not
  registered a local storage backend.
- The meaning of `auto` changes for households that had both Real-Debrid and
  Plex linked with no explicit choice. Before this change those households took
  the implicit path (the web player resolved through Plex and the shared
  catalog merged Real-Debrid inventory). After this change `auto` resolves to
  Real-Debrid, which is the owner requirement. Households that want Plex must
  save `plex` explicitly.
- A saved `plex` or `realdebrid` whose link is missing now falls back to the
  automatic order instead of failing inside the other route. The Settings page
  shows the resolved provider and the reason.

## Phase 2: removing the Plex dependency from local playback

Exact coupling points today:

- `src/omakase/plus/materialize.py:1`: publishes local files into a Plex source
  root, so imported files are visible through a Plex server.
- `src/omakase/plus/web_playback_source.py:376-406`: `_snapshot` requires the
  managed Plex root, the Plex base URL and token, and a `plex_machine_id`.
- `src/omakase/plus/web_playback_delivery.py:175-242`: `open_part` streams bytes
  from the Plex server with `X-Plex-Token` and `part_key`.
- `src/omakase/plus/web_playback_catalog.py:211`: `resolve_catalog_source`
  resolves movie and TV sources from Plex metadata.
- `src/omakase/plus/web_playback_routes.py:343-352`: the web player accepts only
  the publication route and refuses other providers.
- `src/omakase/plus/native_tv_test.py:1382,1458,1702`: native TV still branches
  on the legacy provider id string and calls `personal_plex_movies` for any id
  that is not `realdebrid`, which includes a selected `local`. This file is
  owned by the Lead and still needs the capability-based migration.

Delivering local playback means adding a file source that resolves a
`storage_locator` through `omakase.plus.storage.require_media_file`, serving it
with `web_playback_delivery.open_local`, deriving tracks with `media_probe`, and
keeping progress in the existing provider-neutral `web_playback_*` tables. Until
that exists, `local` stays catalog only and `play` stays false in the matrix.
