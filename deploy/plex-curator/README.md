# Optional Plex anime curator

The curator consumes authenticated `manifest-v2`, plans without mutation, then applies
that exact plan under one global lock. It creates `Anime` and `Anime Movies` as
children of one configured versioned generation and atomically replaces only
its `current` publication pointer. Plex operations never delete media.

## Install one exact source revision

On your Linux media host, use your normal Git access. Do not put a
token in a command, URL, config file, or journal.

```bash
REV=<40-character-source-commit>
install -d -m 0755 /opt/omakase-plex-curator/releases
git clone --filter=blob:none --no-checkout \
  https://github.com/JhinxDev/omakase-plus.git \
  /opt/omakase-plex-curator/source
git -C /opt/omakase-plex-curator/source fetch origin "$REV"
git -C /opt/omakase-plex-curator/source worktree add --detach \
  "/opt/omakase-plex-curator/releases/$REV" "$REV"
ln -sfn "/opt/omakase-plex-curator/releases/$REV" \
  /opt/omakase-plex-curator/current

python3 -m venv /opt/omakase-plex-curator/venv
/opt/omakase-plex-curator/venv/bin/pip install --no-cache-dir \
  "/opt/omakase-plex-curator/releases/$REV"
apt-get update
apt-get install -y --no-install-recommends ffmpeg
install -d -m 0755 /etc/omakase \
  /srv/media/omakase-overlay
install -d -m 0700 /var/lib/omakase-plex-curator/backups /run/lock
install -m 0600 deploy/plex-curator/config.example.json \
  /etc/omakase/plex-curator.json
```

Edit only the mount aliases, existing Riven roots, and reviewed versioned
allowlist entries. A missing Riven root, unresolved symlink hop, path escape,
unknown schema, or incomplete manifest coverage fails closed.

`plex.library_names` assigns the four logical roles to the labels used on that
Plex server. The defaults are `Anime`, `Anime Movies`, `TV Shows`, and `Movies`,
but the labels are configuration—not section keys or code constants. Omakase
discovers their live Plex keys on every plan and refuses ambiguous or missing
names. Folder placement remains deterministic from AniList format and verified
metadata; no AI is allowed to guess a library or filesystem destination.

Collection convergence uses rich manifest genres or explicit reviewed entries
from `collection_overrides`. Action/Adventure wins mixed identities, followed
by Comedy/Slice of Life and then Drama/Mystery/Psychological/Thriller. Unknown
identity blocks dry-run and live apply; it never silently falls into Comedy.

Manifest-managed media also fails closed unless every staged file has a
full-dialogue English subtitle. The approved plan binds the exact release-muxed
standard, forced, and SDH streams when present, and re-probes them immediately
before the overlay swap. Plex and Infuse receive the original embedded tracks,
including any additional languages in the release, without separately matched
OpenSubtitles downloads. Keeping subtitles inside their source file also avoids
streaming an entire large remote remux merely to duplicate a small text track.

Validation failure is scoped to the single offending provider asset, not the
whole run. `build_overlay_plan` records an unreadable file, a missing
subtitle, a coverage mismatch, a destination conflict, an unavailable source
folder, or a policy exclusion as one `BlockedAsset` entry and continues
planning every other title. The current Plex generation only stays unchanged
if literally every manifest asset is blocked (`OverlayPlan.holds_current_generation`);
otherwise every healthy title still publishes in the same atomic swap. Each
live receipt (`curator-receipt-v2`) reports these entries under
`blocked_assets` with a stable `code`, a safe `detail`, and `retryable`.
Omakase Plus quarantines a retryable blocked asset on receipt so the affected
episode reopens for a fresh candidate search; a non-retryable block
(`DESTINATION_CONFLICT`, `POLICY_EXCLUDED`) is left as-is because searching
again cannot resolve it. This is what stops one locked or subtitle-invalid
release (for example, one unavailable release) from silently withholding
every other newly added anime from Plex.

If a text subtitle lacks a language tag, the probe decodes at most four tracks
for a bounded 15-minute, 256 KiB content sample. A conservative English-word
classifier can mark the track as content-verified English; Japanese or
inconclusive text remains ineligible. Image subtitles still require trustworthy
language metadata because their text cannot be inspected safely this way.

Season-specific AniList titles can differ from the series identity expected by
Plex. Put only reviewed TVDB, TMDB, or IMDb mappings in
`metadata_match_hints`; the curator writes a plan-bound `.plexmatch` beside the
staged seasons. Each entry includes the exact overlay `canonical_title`,
`season_number`, `source` (`tvdb`, `tmdb`, or `imdb`), and `source_id`. A stale
mapping that does not identify a staged series blocks planning. This gives Plex
a stable provider identity, poster, summary, and season mapping without manual
Fix Match state. After an atomic overlay change, the curator compares each
series subtree with the retained generation and requests Plex's supported
partial-directory scan for only changed or added series. A removed series or
section-level topology change deliberately falls back to a full section scan.
Dry-run and live apply refuse to start while any Plex section scan is active;
retry after that scan finishes. This prevents slow Plex database work from
turning a local request timeout into an ambiguous curator mutation.

Create `/etc/omakase/plex-curator.env` without echoing its value:

```bash
install -m 0600 /dev/null /etc/omakase/plex-curator.env
editor /etc/omakase/plex-curator.env
# OMAKASE_LIBRARY_MANIFEST_TOKEN=<dedicated machine credential>
chown root:root /etc/omakase/plex-curator.env
chmod 0600 /etc/omakase/plex-curator.env
```

## Required rollback receipt

Before any live invocation, back up the Plex database/config, section inventory,
current overlay state, curator config, and units. Keep Plex running while copying
only its SQLite backup produced with `.backup`; stop Plex for a raw database
restore.

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="/var/lib/omakase-plex-curator/backups/$STAMP"
install -d -m 0700 "$BACKUP"

docker exec plex '/usr/lib/plexmediaserver/Plex SQLite' \
  '/config/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db' \
  ".backup '/tmp/plex-library-$STAMP.db'"
docker cp "plex:/tmp/plex-library-$STAMP.db" "$BACKUP/plex-database.db"
cp --preserve=mode,timestamps \
  '/srv/plex/config/Library/Application Support/Plex Media Server/Preferences.xml' \
  "$BACKUP/Preferences.xml"
TOKEN=$(sed -n 's/.*PlexOnlineToken="\([^"]*\)".*/\1/p' \
  '/srv/plex/config/Library/Application Support/Plex Media Server/Preferences.xml')
printf 'header = "X-Plex-Token: %s"\n' "$TOKEN" > "$BACKUP/plex-curl.conf"
chmod 0600 "$BACKUP/plex-curl.conf"
curl --config "$BACKUP/plex-curl.conf" --fail --silent --show-error \
  http://127.0.0.1:32400/library/sections > "$BACKUP/section-inventory.xml"
rm -f "$BACKUP/plex-curl.conf"
unset TOKEN
tar -C /srv/media \
  -cf "$BACKUP/overlay-state.tar" omakase 2>/dev/null || \
  tar -cf "$BACKUP/overlay-state.tar" --files-from /dev/null
cp /etc/omakase/plex-curator.json "$BACKUP/curator-config.json"
tar -C /etc/systemd/system -cf "$BACKUP/systemd-units.tar" \
  plex-anime-curator.service plex-anime-curator.timer 2>/dev/null || \
  tar -cf "$BACKUP/systemd-units.tar" --files-from /dev/null
```

Create `receipt.json` with `schema_version` set to
`plex-backup-receipt-v1`, a whole-second UTC `created_at` formatted as
`YYYY-MM-DDTHH:MM:SSZ`, and exactly these artifact keys: `plex-database`, `plex-preferences`,
`section-inventory`, `overlay-state`, `curator-config`, and `systemd-units`.
Each value is `{ "path": "<absolute backup path>", "sha256": "<sha256sum>" }`.
The runtime re-reads every artifact and verifies its hash before Plex mutation.
After inspection, atomically update the stable service receipt:

```bash
ln -sfn "$BACKUP" /var/lib/omakase-plex-curator/backups/latest.tmp
mv -Tf /var/lib/omakase-plex-curator/backups/latest.tmp \
  /var/lib/omakase-plex-curator/backups/latest
```

## Dry-run, live apply, and timers

```bash
set -a
. /etc/omakase/plex-curator.env
set +a
RUNTIME=/opt/omakase-plex-curator/current/deploy/plex-curator/curator_runtime.py
PYTHON=/opt/omakase-plex-curator/venv/bin/python
PLAN=/var/lib/omakase-plex-curator/approved-plan.json

"$PYTHON" "$RUNTIME" dry-run --config /etc/omakase/plex-curator.json --plan "$PLAN"
jq '{plan_hash,manifest_generated_at,overlay:.overlay_plan.plan_hash,topology:.topology_plan.plan_hash}' "$PLAN"

"$PYTHON" "$RUNTIME" live --config /etc/omakase/plex-curator.json \
  --plan "$PLAN" \
  --backup-receipt /var/lib/omakase-plex-curator/backups/latest/receipt.json

install -m 0644 deploy/systemd/plex-anime-curator.service \
  /etc/systemd/system/plex-anime-curator.service
install -m 0644 deploy/systemd/plex-anime-curator.timer \
  /etc/systemd/system/plex-anime-curator.timer
systemctl daemon-reload
systemctl enable --now plex-anime-curator.timer
systemctl start plex-anime-curator.service
systemctl status --no-pager plex-anime-curator.service
systemctl list-timers --all plex-anime-curator.timer
```

If live apply reports an unreadable or broken `/library/...` path while the
matching source mount is healthy, Plex may be holding a stale FUSE bind after
a remount. Use your deployment's documented mount and consumer recovery order.
Restarting only a mount process may leave running consumers attached to a stale
mount. Re-run dry-run after recovery so live apply uses fresh evidence.

Legacy library-topology removal is not part of a fresh installation. The
`--removal-proof` interface requires an exact, independently reviewed topology
and playback proof; do not substitute generic examples or perform broad library
cleanup as a setup step. Normal publication retains existing media and history.

## Active Plex scans

An active native Plex scan is an expected deferral. The runtime checks this
before planning or publishing changes, closes the client and releases the
curator lock. The CLI emits `outcome: deferred`, `code: PLEX_SCAN_ACTIVE`,
`changed: false` and `retryable: true`, with exit status 0. This is not a
successful publication receipt; no live receipt or watch snapshot is submitted
by the deferral. The existing timer retries on its next scheduled run after the
scan finishes. An explicit command also reports the deferral and can be retried.

In `auto`, planning keeps the proposed plan, receipt and subtitle probe updates
in memory until the live phase has passed its own scan guard and verified the
recomputed plan. If a scan starts between the two phases, the previous approved
plan and cache stay unchanged and neither phase submits a receipt. On an idle
run, both normal receipts are still submitted and cached probes are reused.

Only the typed native scan-busy condition uses this path. Invalid configuration,
plan or proof, provider/network errors and unreadable media still fail. Lock
contention retains exit 75. No systemd success-status exception or timer change
is needed. Keep the current generation and any previously approved plan while
Plex is busy; do not stop the scan or remove library files to clear this state.

## Retained generations

A new approved plan can return to the retained prior generation. The runtime
first stages that plan again, including source and subtitle validation, then
compares the complete retained tree with this fresh stage. Directory names,
raw link targets and exact `.plexmatch` bytes must agree; unexpected entries,
unsafe types and symlinked directories fail. The hash in a directory name alone
does not approve reuse. Verification removes only its own successfully created
stage; a pre-existing stage is preserved and fails the run.

An exact match can be selected through the normal live publication controls.
On publication or pointer-sync failure, the prior selection is restored and both
retained generations are preserved. Different content remains a real validation
failure. Scan deferral, backup/proof checks and the generation layout are unchanged.

## Qualified whole-file quality selection

`quality-fallback-v1` negotiates `manifest-v4`, `overlay-plan-v8` and
`curator-receipt-v4`. It requires both the existing language-policy capability
and positive operator qualification on the app and curator. The quality gate is
`OMAKASE_PLUS_QUALITY_PUBLICATION_QUALIFIED=true`; it defaults off. Install the
matching app package and all curator modules, including `quality_selection.py`,
then qualify the pair in isolation before enabling it. A query parameter or peer
header cannot grant operator qualification. Old peers retain their v2/v3 rules.

Each candidate keeps its full verified inventory and current connector binding.
The curator qualifies complete source files before it chooses an exact physical
file partition. Its receipt includes every qualified candidate file with an
explicit selected flag, so the app independently recomputes the same selection.
A failed preferred copy claims no episodes. Separate fallback files can fill the
remaining episodes. If a single fallback file contains E1–E2, keep that whole
file when a preferred E1 alone would lose E2; unrelated titles can still publish.
Hold the generation only when no safe complete partition for an overlapping
upgrade title is available. Existing legacy specials stay with selected sources.

Approved language policy applies to its exact overlapping episode scope.
Conflicting approved policies on the same episode fail closed. A different
policy on a disjoint episode does not erase its existing qualification. Movies
retain every successfully qualified version rather than the series partition.

The importer binds current inventory, source, logical identity and exact parsed
episode ranges. Only selected policy-bound files update current file proofs;
v3 still requires full coverage. Held attempts preserve current selection and
append scoped failure evidence through existing run/candidate history without
inventing a provider observation. Release labels do not claim measured video
resolution, and selected files do not prove exact Plex indexing or playback.

Before any v4-capable runtime starts, a paired rollback may select a verified
prior compatible pair with writers fenced and the new capability disabled.
Preserve current database contents and verified retained generations.

The release operator must fsync the permanent `publication-v4-started.json`
marker before the first v4-capable runtime starts. After that marker exists, or
v4 use cannot be ruled out, disabling a flag does not make legacy runtimes safe.
Do not resume legacy writers, restore an older database or automatically return
to the earlier application/curator pair. Retain CURRENT data and media, keep all
writers fenced, and use a forward fix or a separately qualified compatible pair.
This source contract is separate from native qualification and release acceptance.

## Rollback

The generation-selection example below does not override the v4 boundary above.
After possible v4 use, keep application, curator and other writers fenced until
the CURRENT data and selected media are qualified together.

Stop the timer, then atomically select the retained prior generation:

```bash
systemctl stop plex-anime-curator.timer
readlink -f /srv/media/omakase-overlay/current
find /srv/media/omakase-overlay/generations \
  -mindepth 1 -maxdepth 1 -type d -print
ln -s "generations/<verified-prior-generation>" \
  /srv/media/omakase-overlay/.current.rollback
mv -Tf /srv/media/omakase-overlay/.current.rollback \
  /srv/media/omakase-overlay/current
```

Restore section definitions from `section-inventory.xml`. Restore the Plex
database and Preferences only with Plex stopped. Restore the versioned source by
repointing `/opt/omakase-plex-curator/current` to the previous verified release.
Real-Debrid and zurg content are never deleted by curator rollback.

Proof of installed source:

```bash
readlink -f /opt/omakase-plex-curator/current
git -C /opt/omakase-plex-curator/current rev-parse HEAD
systemctl cat plex-anime-curator.service plex-anime-curator.timer
```
