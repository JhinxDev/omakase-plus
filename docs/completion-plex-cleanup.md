# Completed anime in Plex

An owner can choose **Remove completed anime from Plex** in Settings. The policy
is off for every account until that owner explicitly enables it. Enabling it does
not sweep historical completions. A newly accepted completion withdraws that
exact monitored season or movie, stops acquisition, and keeps the watched ledger,
optional rating, AniList sync intent and source/provider media.

The completion form offers **Keep this anime in Plex for rewatching** as a
per-title exception. Updating an existing completed title's score does not open
another withdrawal. Imported history, duplicate events and uncertain playback
observations do not trigger removal. A confirmed exact final episode can trigger
the same policy through the shared watch service.

Plex-only withdrawals remain in the Completed watchlist, including their title
page, rating and history. Removal progress distinguishes queued, needs attention
and confirmed absent states. A blocked completion can retry its own withdrawal;
there is no automatic retrospective batch. Existing separately confirmed full
media cleanup retains its previous eligible-provider deletion behavior.

## Proof and durability

Migration 021 gives removal records an explicit scope and stores owner policy and
per-monitor exceptions. The accepted watch transition and Plex-only request share
one transaction, including nested savepoints. Plex-only cleanup never constructs
a deletion provider and never changes an existing source asset's status.

All manifest selection paths exclude withdrawn monitors. Shared publication stays
available through another owner's non-withdrawn monitor. A withdrawal completes
only after a later converged curator receipt omits its canonical identity and a
fresh native Plex inventory proves that exact season/movie absent. The native
curator posts the receipt before its inventory; the inventory endpoint retries
confirmation using the latest receipt. Manifest omission by itself is insufficient.

Both normal and failed scheduling paths reject withdrawal records and blocked
completion cleanup. Selected work rechecks withdrawal around catalog/provider
awaits. An already-sent provider acquisition cannot be retroactively cancelled;
its returned source identity is retained, with subsequent selection and deletion
stopped. No database transaction spans these network waits.

## Verification

Backend and migration checks cover owner isolation, default-off behavior,
transaction rollback, uncertain/duplicate/score-only events, empty pending titles,
keep exceptions, sequential shared-owner withdrawal, stale/present native
inventory, exact seasons/movies, preserved source assets and provider non-use.
The media-sync regressions also complete a title while a catalog request is
suspended, and exercise acquisition/selection/info cancellation and failed
scheduling.

Actual authenticated route tests cover persisted Settings, completion/rating,
unchecked keep exceptions and invalid-score atomicity. A disposable local route
fixture covers Settings, completion, queued withdrawal, confirmed removal and
the Completed archive in isolated headless Edge at 1440, 390 and 320 pixels.
Fifteen Axe audits found no A/AA violations or horizontal overflow. This is local
browser evidence, not physical-device or production field-performance proof.

Keep exact source/image identities, administrator preferences, removal receipts
and scheduled-run results in each installation's protected operational records.
See the [release and recovery guide](release-checklist.md). No provider-content
deletion or broad Plex trash emptying is required for this policy.
