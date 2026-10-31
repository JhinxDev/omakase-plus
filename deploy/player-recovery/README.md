# Accepted-image compatibility with retained player data

Prepared compatibility overlay, **not activated**. This directory does not build,
start, deploy or roll back an application. The disabled legacy recovery helpers
remain disabled. Follow [current-data recovery](../../docs/current-data-recovery.md).

The accepted source is exactly
`c9d491ccbdfa68aedd31692028da5820fcd6638d`. Its original watch model rejects
`web_player` provenance in a current migrated database. Turning the player flag
off in the new binary does not prove that the independent accepted old binary can
read that data. This overlay addresses the old binary's parsing and read labels.

## Exact contents and identity

[manifest.json](manifest.json) records the accepted commit, each original module
SHA-256, each complete patched module SHA-256, each minimal unified patch SHA-256
and the aggregate overlay identity:

`88d625272ab828b373d9c1d0d5a54691a037743c1bd3dff5a89420461dd0bf8d`

The complete files are derived from read-only `git show` at that exact commit:

| Overlay file | Expected Python 3.12 container target |
| --- | --- |
| `overlay/omakase/plus/watch/models.py` | `/usr/local/lib/python3.12/site-packages/omakase/plus/watch/models.py` |
| `overlay/omakase/plus/product_views.py` | `/usr/local/lib/python3.12/site-packages/omakase/plus/product_views.py` |
| `overlay/omakase/plus/viewing_history.py` | `/usr/local/lib/python3.12/site-packages/omakase/plus/viewing_history.py` |

The watch model only adds `web_player` to the typed source and runtime source set.
Product views only add its label to four existing dictionaries. Viewing history
only permits the retained source in the existing event query and labels it
`Web player`. Existing time semantics stay unchanged: a retained web event is
Recorded; this recovery overlay does not invent a watched timestamp. No player
module, renderer, route, playback writer or new dependency is copied into the old
application. All other accepted source remains intact.

Verify the checked-in artifacts without modifying them:

```sh
python deploy/player-recovery/generate.py --check
```

Regeneration without `--check` writes only the fixed files inside this package.
The generator requires the exact accepted base, checks fixed source anchors and
does not accept arbitrary input/output paths, access SQLite, reset Git or invoke
Docker. Review the [patches](patches) and manifest before any operator use.

## Operator qualification before use

1. Retain the **current** SQLite database, WAL/SHM and separately protected key.
   Keep migrations 037–042, watch provenance, personal resumes, completion-cycle
   records, notifications, pending/failed tracker outbox and consent unchanged.
   Do not restore an older database or change consent to make recovery work.
2. Record the accepted image's immutable ID/digest and verify its actual Python
   version, site-package layout and all three original file hashes against the
   manifest. A tag or source revision is insufficient. A different image/layout
   requires its own qualification; do not copy the new application into it.
3. Fence schedulers, tracker/outbox/bridge dispatch, acquisition, curator/removal,
   API/CLI/MCP writers and relevant timers using the reviewed operator plan.
   Keep external provider traffic fenced. `OMAKASE_ACQUISITION_MODE=import-only`
   is additional policy, not a substitute for stopping writers. Preserve recorded
   consent and original writer states for later explicit resumption.
4. Record `OMAKASE_PLUS_WEB_PLAYER_ENABLED=false` and keep the player absent/off.
   This flag is not a read-only boundary. Expose no unrestricted old router while
   qualifying recovery; enforce the reviewed read-only route/network boundary,
   read-only data clone and frozen writer inventory. Even old authentication can
   renew or delete sessions: prove the actual session/read routes under that fence.
5. Bind only the three exact verified files above, individually **read-only**.
   Verify inside-container effective bytes and native mount read-only state.
   Prefer `PYTHONDONTWRITEBYTECODE=1`; do not edit installed packages or rebuild
   the accepted image. Verify imported module paths and effective source behavior.
6. Qualify this exact old image plus overlay against a private current-data clone:
   integrity/foreign keys; authenticated Home, Library, title detail and Activity;
   web/manual labels and actor isolation; all player rows/resumes, outbox and
   consent preserved; no provider requests or writes. Keep feature disable in the
   current binary as a separate proof. Root owns this native image/clone acceptance
   before any activation, along with recovery and writer-resumption authority.

The image ID remains the accepted old image ID. The **effective recovery identity**
is that ID **plus** the aggregate/per-file overlay hashes, verified read-only
mount set, flag/fence configuration and retained schema/data identity. Do not label
it an unchanged accepted image or a newly built release. An effective source overlay
does not authorize activating it, bypassing authentication or resuming old writers.

## Maintained offline proof and its limits

`tests/plus/test_player_recovery_overlay.py` extracts accepted source into an
isolated subprocess, first demonstrates the unpatched old repository rejecting a
retained `web_player` row, then applies exactly these three files. It uses a
populated current synthetic database opened read-only, including two
owners, manual and web provenance, resumes, sessions/events, remux metadata,
coverage/completions, retained confirmation/outbox retry and enabled consent.

The retained old model/repository and read models pass. Its actual accepted router
serves authenticated `/plus/home`, `/plus/library`, `/plus/library/2` and
`/plus/activity` with HTTP 200 using a synthetic existing cookie. Activity contains
the accepted version's viewing feed; that version has no `/plus/history` page.
Synopsis reads use its own cache, provider HTTP is guarded and no scheduler starts.
Logical rows, consent and complete database bytes remain identical; integrity and
foreign-key checks pass. No new player module imports. This is source-component
proof using the local Python environment, **not** native old-image, deployment,
real-account/session or installed recovery acceptance.
