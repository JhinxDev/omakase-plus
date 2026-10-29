# MyAnimeList connection and personal AniList reconciliation

**Released optional capability.** Personal tracker reconciliation is included in
the release. New-install feature flags default off; each installation needs
operator qualification before activation. Real MyAnimeList application credentials,
provider readiness and each user's own connected identities are required. No real
MyAnimeList write or automatic account connection is established by this guide.

## Start with a preview

1. An administrator registers the separate MAL and AniList OAuth clients and
   configures the exact private HTTPS callbacks. Keep secrets out of chat/Git.
2. Each person connects their own accounts in **Settings → Tracking → Personal
   tracker sync**. Verify the returned identities; a household is not consent.
3. Read the complete first preview. Choose conflicts and any lossy score rounding;
   confirm only the selected changes. Missing entries are never deleted.
4. After that review, opt in to ongoing checks if desired. The administrator must
   separately enable the qualified bounded worker. RetryAfter means wait; an
   uncertain write waits for review rather than being replayed automatically.

For example, progress changed only in AniList can advance MAL after fresh checks.
If both services changed the score, neither silently wins. Dates and score clearing
remain unsupported write fields. Local history survives disconnecting either side.


This implements a personal connection and preview/apply workflow, with mocked
provider acceptance and additive migration035. Real OAuth registration/consent,
combined deployment compatibility and actual provider readback remain required.
It does not make household lists shared. Connect/login never synchronizes a list.

## Current capability

MyAnimeList uses its documented authorization-code flow with plain PKCE, same-user
and session-bound one-use10minute state, encrypted verifier and encrypted rotating
token bundle. Numeric authenticated `/users/@me` identity is required before the
connection is marked verified. One remote identity cannot silently link to another
Omakase user. Refresh is leased and atomically replaces access/refresh/expiry;
returned expires_in controls expiry, rather than inconsistent documentation prose.
Tokens, verifiers and raw provider bodies are not returned to pages or safe errors.

AniList uses the existing OAuth connection, then an explicit Viewer verification
for the bridge. Its tokens do not support refresh. The bridge checks that the
existing credential still matches; deleting/replacing it stops use of the saved
bearer copy. Disconnect/relink invalidates generation, pending plans/jobs and the
current baseline, preserving personal watch history and list data. Local unlink
is not a claim of remote token revocation; MAL documents no revoke endpoint here.

`Read a sync preview` explicitly reads both personal lists, all status groups and
bounded complete pages. Two pages per provider are saved per step, encrypted and
scoped to owner/connection generation, with a15minute scan limit. Continue reading
resumes pages. Partial/error/private/invalid responses never become empty complete
lists or executable plans. Exact AniList idMal mappings are used; at most10 missing
mapping lookups per preview, with bounded public mapping cache and fresh mapping
checks before writes. Missing mappings remain visible; titles are never authority.

The first preview sends no list mutations. New entries are review-only proposals;
existing different fields wait for explicit source/keep-separate choices. Choices
produce a new immutable preview, not a write. Select desired changes and confirm.
Both source and destination are reread, including absence before creates. Previews
expire15minutes after creation, and bodies are bounded. On explicit subsequent
checks, expired bulk snapshots are retired in bounded owner-only batches; only
minimal uncertain outbox intents remain. Completed/cancelled metadata can expire
after7days. This is not full-list historical backup storage. Existing ratings are not
automatically overwritten during first sync. Missing entries never cause deletion
or automatic recreation. Date writes and score clearing are unsupported; those
fields stay separate rather than sending speculative provider parameters.

Acknowledged per-field baselines support ongoing opt-in one-sided updates.
Conflicting concurrent edits wait for review; ambiguous progress/rewatch decreases
are conservative. Lossy AniList scores require an exact-value rounding choice;
acknowledged75/100 versus8/10 representations do not bounce back. A safe progress
acknowledgement never acknowledges an unresolved rating conflict on that title.
User ratings always show explicit /10 labels in the review UI.

Apply dispatches at most10 previously approved items per job with a25second HTTP
budget. Local durable outbox records intent before dispatch; a crash, transport
uncertainty or failed post-write readback never blindly replays a write. Retry-After
persists a pause for the personal pair, not merely one outbox item. The next explicit
job rereads preconditions; completed rows do not block later approved batches.
Some uncertain outcomes require a fresh review; no provider exactly-once or atomic
CAS is promised. Separate services can still race an external edit after reread.

`Enable after first preview approval` records an explicit preference for the
configured worker. `sync_tick(user_id)` is one bounded worker unit; it never enables
itself. New entries, unsupported fields and conflicts remain review-only. No
scheduler is installed by this module, and no provider check runs on a header,
Settings/status/plan GET or merely opening a page. Incoming tracker observations do
not call completion cleanup, media acquisition or source deletion.

## Primary wire evidence

Official sources read2026-10-28:

- [MAL authorization](https://myanimelist.net/apiconfig/references/authorization):
  plain PKCE only43–128characters; form-urlencoded token/refresh requests, client
  authentication and expiry/rotation. Server app uses registered confidential
  client ID/secret. The older access-lifetime examples are internally inconsistent;
  honor expires_in. No remote revoke endpoint is invented.
- [MAL API v2](https://myanimelist.net/apiconfig/references/api/v2): its embedded
  `__redoc_state.spec.data` OpenAPI3 defines PATCH
  `/anime/{anime_id}/my_list_status`; the rendered PUT curl is an inconsistent
  example. Outbound progress is `num_watched_episodes`; the AnimeListStatus read
  schema uses `num_episodes_watched`. The adapter refuses missing canonical read
  data instead of silently defaulting a spelling mismatch to0. User dates are
  nullable read fields but absent from the documented update request. Update only
  supplied fields. No numeric MAL rate or conditional-write contract is assumed.
- [AniList auth docs](https://github.com/AniList/docs/blob/master/docs/guide/auth/index.md),
  [mutations](https://docs.anilist.co/reference/mutation) and
  [rate limits](https://github.com/AniList/docs/blob/master/docs/guide/rate-limiting.md):
  no refresh tokens, exact-ID writes and dynamic rate restrictions. The current
  degraded-rate notice is not permission to hardcode a higher request budget.
- [AniList terms](https://docs.anilist.co/guide/terms-of-use): keep this a minimal
  complementary integration for each user's own accounts, not a catalog hoard or
  backup/mirror product. Applicability must be assessed for actual continuous/public
  operation; this is not a universal separate-approval requirement or proof of
  provider endorsement.

## Operator setup and root wiring

No client values are bundled. Redact OAuth callback query strings in upstream
access logs; app callback scope is cleared after parsing and pages use no-referrer,
but the application cannot control an external proxy's earlier access logs. An administrator registers the MAL application and
configures `MAL_CLIENT_ID`, `MAL_CLIENT_SECRET`, persistent
`OMAKASE_PLUS_MASTER_KEY` and the validated external `OMAKASE_PLUS_URL` through
secure runtime plumbing. The registered exact callback is
`<OMAKASE_PLUS_URL>/plus/integrations/myanimelist/callback`.
HTTPS is required except loopback development. Each user logs in and consents at
the provider themselves. Missing client setup renders a truthful administrator
message, never a fake Connected label. Existing AniList developer setup remains
separate. Do not place real credentials in source/examples/receipts.

The shared Plus router now includes `create_mal_router(templates, _product_context)`,
registers `tracker-bridge.css` in the static allowlist and links the existing
tracking settings to `/plus/settings/myanimelist`. `tracker_bridge.html` is a
standalone page. `_settings_mal.html` contains forms and must not be nested in the
existing Settings form.

**Keep `OMAKASE_TRACKER_BRIDGE_WRITERS_READY` off until native qualification.**
Source integration serializes every existing AniList writer: completed-history
actions, watch outbox processing, best-effort planning, explicit planning and
planning removal. They use `tracker_sync.writer_lease(db, user_id)` and recheck
`require_writer_lease(db, user_id, token)` immediately before dispatch and
acknowledgement. The default TTL is 300 seconds. Commit local intents before
acquisition; no SQLite transaction is held over HTTP. Contention defers remote
work and preserves local personal history and the existing local-first planning
semantics. Watch-outbox failures after dispatch require review rather than an
automatic retry of an uncertain mutation.

`anilist_credential_fingerprint(db, user_id)` reads only the local encrypted
credential representation. `require_anilist_credential(db, user_id, expected)`
checks that representation and the saved account rate pause without an extra
provider request. A concurrent credential change blocks dispatch or acknowledgement.
The original AniList callback and disconnect invalidate bridge generations through
`BridgeService(...).disconnect(user_id, Provider.ANILIST)` before replacing or
removing credentials. They do not imply a newly verified bridge identity.

The lease coordinates Omakase writers only; it cannot lock an external app or
make the provider's read-check-write sequence atomic. The activation flag alone
is not evidence of safe deployment. The combined R1 gate below remains mandatory.

## Ongoing bounded synchronization

After the combined release gate, operators may set
`OMAKASE_TRACKER_BRIDGE_WORKER_ENABLED=true` alongside the existing private-mode
and writer-readiness flags. All default off for bridge work. Each account must
separately approve its first complete preview and explicitly opt in. Connecting
accounts or opening Settings does not opt anyone in.

The existing `omakase plus sync-media` runner now invokes a bounded tracker rotation.
No new daemon, timer, job service or dependency is installed. An operator can also
run exactly one finite unit:

```powershell
omakase plus sync-trackers --max-accounts 2 --budget-seconds 60
```

`--data-dir` accepts a portable existing data-directory location. The CLI emits only
aggregate outcome counts. With any global gate off, it does not open/migrate a DB
or construct an HTTP client. With no opted-in accounts it performs no provider work.
Review mode also disables automatic tracker work.

The035 singleton scheduler row stores only an expiring lease and last attempted
user ID. Indexed enabled-account selection rotates fairly across invocations and
advances before an attempt, so a busy/rate-paused account does not starve others.
A unit attempts at most ten distinct accounts (default two) and at most120 seconds
(default60). Each account gets at most50 seconds, further limited by the remaining
unit budget; both provider stages share that absolute deadline. Network operation
timeouts are bounded by the adapters, rather than a process-kill guarantee.
No SQLite transaction spans provider HTTP. Exact lease tokens protect release
from deleting a replacement worker's lease.

The existing account writer lease, current credential/generation checks and saved
RetryAfter still apply. The worker rechecks global gates, its lease, its deadline
and account opt-in immediately before apply, dispatch and acknowledgement. Opt-out
or expiry before dispatch prevents a write; after dispatch, an unconfirmed outcome
stays ambiguous. Any existing ambiguous/interrupted dispatch blocks new automatic
plans for that user until reviewed, so a fresh preview cannot silently replay it.
New entries, lossy scores, dates and conflicting edits continue to wait for review.
An unchanged inventory causes no provider mutations. Local history is preserved.
This is eventual checked synchronization; provider APIs offer no external atomic CAS.

`run_tracker_bridge` is the bounded embedding API. `sync_trackers_once` owns and
closes a fresh connection; it never closes the existing CLI connection cache.
Unexpected failures remain visible to the operator rather than printing provider
bodies or credentials. No real job/provider call was run for source qualification.

## Docker cache qualification

The builder installs `requirements-build.txt` with required hashes before copying
application source/metadata. Runtime installs the selected locked runtime/agent
requirements before copying the built wheel, then installs that wheel separately
with `--no-deps`. Base digests, lock bytes, UID1000 and default agent tools are
unchanged. The maintained structural test guards those cache boundaries; it does
not claim a native build receipt. Root must build the exact candidate, confirm
source-only rebuild reuses dependency layers, and confirm a changed relevant lock
invalidates its dependency layer, then repeat image/runtime/R1 acceptance.

## Mandatory combined upgrade and rollback gate (R1)

Migration035 creates only independent tables/indexes with user foreign keys and
owner-bound plan/outbox links; it does not rebuild existing media/watch tables.
Its fixture verifies pre-existing row/schema preservation and old-migrator use on
a database containing035. That is **not** a native all-consumer acceptance receipt.
Household033, language034 and movie proof036 have their own compatibility requirements; don't
extrapolate035 additivity to the combined schema.

Before any production schema change or bridge activation, root must bind the exact
app, worker and curator candidates/rollback identities, dependencies/config/mounts
and an owned pre-upgrade backup. Qualify a fresh install AND an isolated upgrade of
the actual current schema with migrations 033–036. Record migrations, integrity/foreign-key
checks, protected history/rating/ownership/session hashes and consumer startup.
Exercise all three consumers against the upgraded clone with provider mutations
blocked. Also run the exact prior app/worker/curator versions against that upgraded
clone under the intended additive code-only rollback. A032-only or UI-only receipt
cannot satisfy this combined gate. Do not restore a stale DB after writers resume.
If any consumer cannot safely use the combined schema, stop promotion and repair
that owning compatibility issue or document a separately safe recovery before
activation. Preserve previous releases/backups; never use a broad cleanup/prune.

This task does not run migration035 on production, register clients, log in, write
real trackers, deploy, enable jobs, or claim native/provider acceptance.

## Focused verification

```text
python -m pytest tests/plus/test_mal.py tests/plus/test_mal_routes.py tests/plus/test_tracker_sync.py tests/plus/test_tracker_bridge_schema.py tests/plus/test_tracker_reconciliation.py -q
python -m ruff check src/omakase/plus/mal.py src/omakase/plus/mal_routes.py src/omakase/plus/tracker_sync.py tests/plus/test_mal.py tests/plus/test_mal_routes.py tests/plus/test_tracker_sync.py tests/plus/test_tracker_bridge_schema.py tests/plus/render_tracker_bridge.py
```

Maintained isolated browser check (existing Playwright/Edge, no install):

```text
node tests/js/tracker_bridge_qa.cjs --python <existing-venv-python> --modules <existing-node-module-root> --browser <existing-edge-executable> --output <owned-synthetic-evidence-folder>
```

It renders actual templates and styles from synthetic fixtures, intercepts every
request, covers320/390/1440 setup/connected/reading/review/applied/error layouts,
keyboard focus and a mock form journey. The authenticated MockTransport tests
separately cover real callback/verify/preview/apply routing with three local
accounts and no real provider traffic. These are not physical-device/live-list
acceptance and don't replace the native combined gate or first real preview.
