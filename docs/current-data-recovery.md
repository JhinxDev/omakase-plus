# Update and recover without losing current data

Settings → About & updates checks release metadata; it never installs a release.
Administrators qualify and perform updates for their own installation.

The legacy `omakase-local-upgrade.ps1` and `omakase-local-rollback.ps1` helpers
are **disabled before any operation**, including path/configuration reads or
Docker calls. Old receipts do not establish safe recovery. There is no override
and no replacement automatic recovery engine in this public package.

## Prepare before activation

1. Identify exact current/candidate commits and images, the selected Compose
   file, enabled features, persistent data path, mounts and every relevant writer.
   App-only uses `compose.standalone.yaml`; connected local stacks use
   `compose.local.yaml`. Keep the existing data path and protected master key.
2. Create an application-consistent backup and verify a disposable restore.
   Record hashes, integrity/foreign-key checks and preserved account, history,
   rating, ownership and session records. Back up secrets separately and securely.
3. Qualify a fresh install and the combined upgrade on an isolated current-data
   copy. Assess migrations 033–036 together with every enabled app/worker/curator
   consumer. One additive migration or healthy HTTP endpoint is insufficient.
4. Save durable recovery metadata **before activation**: exact source/image and
   original configuration identities, resolved data/mount paths, tested backup,
   writer inventory, fence/resume plan and qualified recovery route. Mark it
   prepared; record activation, failure and verification separately.
5. Under the reviewed installation procedure, fence relevant timers, workers,
   curator/discovery jobs and API/CLI/MCP writers. Confirm the persistent path
   before initialization and SQLite's opened main path afterwards. Activate only
   the exact reviewed candidate.
6. Verify migrations, preserved records, authentication and affected journeys on
   the real target before resuming the recorded writers. Record deployed identity
   and results. Optional media connections and playback need separate proof.

Use SQLite's [online backup API](https://www.sqlite.org/backup.html) or a qualified
consistent stopped snapshot. Committed data can remain in the
[WAL](https://www.sqlite.org/wal.html#the_wal_file). Copying only an open main DB
file is not a complete backup or a safe restoration procedure.

## When the candidate fails

Keep the **current migrated database**. Preserve its WAL/SHM and failure evidence
under the reviewed fence. Do not copy a pre-upgrade backup over it, delete
sidecars, start unrestricted old writers, replay destructive actions or broadly
prune images/media to recover an app release.

Prefer a forward fix qualified against an isolated current-data copy. A prior
image may run only through a separately qualified recovery route for the combined
schema. Restricted read-only recovery must actually deny actions and unsafe or
dynamic routes, retain current data and keep writers fenced. A label, HTTP health
result or assumed read-only setting cannot establish this. If no safe route is
qualified, keep affected writers fenced and prepare a forward fix.

Retain current and immediate-prior recovery identities and original failure
evidence. Resume only the exact recorded writer states after real-target proof.

## Deliberate disaster restoration

Restoring after corruption or loss is a separate operator decision. It can discard
every change after the backup, including history, ratings, sessions and household
work, and disagree with later provider state. Review that data-loss boundary
explicitly; it is not routine image rollback.

Fence consumers and preserve the failed state first. Restore the tested backup
into a separate clean location, qualify compatible code/configuration and account
records there, then follow a separately reviewed cutover. Never overwrite an open
database or mix unrelated old/new SQLite sidecars. Image health does not authorize
resuming jobs or replaying actions.

Managed-library generation recovery is separate. It needs current manifests,
protected shared/adopted asset references and native convergence proof; changing
a library pointer does not rewind account history or external providers.

See the [release checklist](release-checklist.md), [Storage and Plex](local-storage-plex-setup.md),
and [operator documentation](https://omakase-plus.jhinx.dev/docs/backup-upgrade-recovery).
