# Media library and connections

Omakase Plus keeps anime identities and movie identities separate. Anime uses
AniList; movies use exact IMDb/TMDB mappings. A search, request, library entry,
playback check and watched status are different facts.

## Start with an existing server

Follow [installation and storage setup](local-storage-plex-setup.md), then create
your private account. Keep the database and encryption key together in backups.
Do not reuse someone else's deployment credentials or copy their data directory.

In **Settings → Connections**, save and test the services you use:

| Connection | Purpose | Needed for movies? |
| --- | --- | --- |
| Plex | Library metadata, owner-token playback checks and explicit watched import | Yes |
| Riven | Existing movie request and acquisition backend | Yes |
| Seerr | Movie catalog, genres and exact IMDb/TMDB metadata | Yes |
| Real-Debrid | Optional anime acquisition provider | No; configure Riven's own provider separately |
| Prowlarr | Optional anime catalog aggregation and selected indexers | No |

A green test describes the saved configuration at its check time. Every actual
movie operation still validates its destination and handles authentication,
outages, rate limits and incompatible responses. Prowlarr indexer cooldowns are
shown independently of the server connection.

Blank address/key fields keep saved values. Replacing a server origin requires
a replacement key; disconnecting removes only that integration. Saved credentials
are encrypted and are not returned to the browser. A service's own installation,
provider subscription and library setup are separate from connecting it here.

### Trusted network policy

Public service URLs are validated. A private deployment can opt in to LAN
connections with `OMAKASE_ALLOW_PRIVATE_CATALOG_URLS=true`. Loopback additionally
requires `OMAKASE_ALLOW_LOOPBACK_MEDIA_URLS=true`. Keep these disabled when they
are unnecessary; these are server policy settings, not per-user bypass switches.
Cloud metadata, link-local, multicast and unspecified destinations remain blocked.
Each runtime operation validates DNS answers and pins the selected IP while
preserving the original HTTP Host and TLS identity. Redirects and environment
proxies are not used to forward credentials.

## Find and watch

**Search** looks up a specific title. AniList suggestions and movie results show
the exact identity, your recorded history and a details link. No search acquires
media or marks anything watched. Missing history means unknown, not unwatched.
Anime OAuth list entries are accepted only when the returned Viewer, list owner
and media identity all agree.

**For You** keeps recommendation browsing separate from direct search. Movie
genre matching uses saved local ratings and explains that basis; it does not
invent an AI fit percentage. Completed movies are excluded by default.

In **Movies**, requesting a title submits its exact identity to the configured
backend. Duplicate and uncertain submissions are protected. Acknowledgement does
not mean the movie is available. Use **Check request** for current backend status
and **Verify in Plex** for bounded playback access checks.

Ready requires the exact IMDb GUID in the correct Movies library, a compatible
file with selected English audio, and HTTP 206 returning exactly 65,536 requested
bytes. Verification expires and is invalidated when the file or selected streams
change. It proves bounded file access, not that the entire film has been decoded
or watched. 4K is preferred by the backend's configured quality policy; an HD
fallback is labelled. The interface does not silently alter global provider or
quality settings, clear blacklists or retry provider-restricted files.

## History and ratings

Save a movie's watched status and optional rating from its details. Ratings use
1–10 with half steps. Status and rating have independent provenance. Clearing a
rating is explicit; omitting a field preserves it. A rating alone does not assert
that the movie was completed.

**Sync watched from Plex** reads the playback/rating fields visible to that
account's configured Plex token. Use the actual viewer's token; an administrator's
token is not another household member's history. Batches are bounded and the next
sync continues from a per-user, token-scoped cursor. New identity mappings are
also bounded. The UI reports remaining/skipped items rather than claiming a full
import after one batch. Manual overrides survive sync. Import never writes Plex.

The history export is owner-scoped JSON with identities, status, ratings and
provenance. It contains no service credentials. Integration with an external
tracker remains explicit: local Omakase movie ratings are not silently written
to SIMKL, AniList or Plex.

Viewing history separates actual watch observations, manual marks and imported
progress checkpoints from operational download/sync logs. Episode names appear
only when available. A season snapshot's latest activity is not the watch date
of its highest watched episode, so those snapshots are labelled **Recorded**.
Acquisition, delivery, ratings and bulk tracker imports do not create invented
episode watch dates.

## Account and profile

Ordinary Google sign-in returns Home; linking/removing Google is under sign-in
settings. Linked accounts remain separate from the server's Google OAuth setup.
Sessions persist for 90 days by default and renew during active use near expiry.
Logout revokes the session. `OMAKASE_SESSION_DAYS` accepts 7–365 days; invalid
values use the 90-day default.

Profile pictures accept still JPEG, PNG and WebP up to 8 MiB and 16 million pixels.
The server decodes and re-encodes a metadata-free 192 px PNG with a 256 KiB storage
cap. Images are account-scoped; SVG, animation and remote URL uploads are refused.
Removal restores the account initial. Pictures and their source files do not
belong in the source repository.

## Agent and operator controls

Commands run inside the private runtime and use its encrypted saved connections:

```sh
omakase plus movies --user-id 1 list
omakase plus movies --user-id 1 search 'The Thing 1982'
omakase plus movies --user-id 1 request --imdb-id tt0084787 --tmdb-id 1091
omakase plus movies --user-id 1 verify tt0084787
omakase plus movies --user-id 1 history
omakase plus movies --user-id 1 sync-history
omakase plus movies --user-id 1 rate --imdb-id tt0084787 --tmdb-id 1091 --status completed --rating 8.5
```

Requesting, syncing or rating examples are real actions, not setup checks. Use an
actual owner's instruction and exact identity. Commands return JSON; credentials
are not command-line parameters. The optional sync offset is for deliberate
operator control; the default cursor normally handles continuation.

## Reproducible build and verification

The collection includes native Plex/Riven titles, explicit requests and saved
watching/rating records. Searching or opening a public catalog title does not
add it to the collection. Exact IMDb searches use Seerr's `imdb:` syntax.

Readiness requires exact identity and bounded file access. A release-year
difference of at most one year is accepted only when both IMDb and TMDB match.
Known Plex codecs include VC-1; a Ready badge confirms accessible media, while
the selected Plex client determines whether it can play directly or transcode.

The image pins its Python 3.12 base digest. Runtime and build-tool requirements
are exported separately from `uv.lock` and installed with required artifact
hashes. After changing dependencies, regenerate both files:

```sh
uv lock
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements-runtime.txt
uv export --frozen --only-group build --no-emit-project --format requirements-txt --output-file requirements-build.txt
uv run --frozen --extra dev python -m pytest -q
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv run --frozen --extra dev python scripts/audit_locked_dependencies.py --output dependency-audit.json
```

The advisory lookup sends only public PyPI lock names/versions to OSV. A failed or
incomplete lookup is not a clean audit. Test results, native service acceptance,
browser/accessibility checks and measured performance must be tied to the exact
candidate. See [release and recovery guide](release-checklist.md). Public
publication is a separate decision; this work does not publish a private server
or its repository.
