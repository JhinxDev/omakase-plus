# Release tooling

This directory replaces the out of repository, hand edited release path that
built the live Omakase Plus image. That path pinned a mutable base tag, kept a
hand maintained list of changed files, hard coded image digests and revision
strings into a temporary runner script, and had no way to prove that the running
container still matched a commit.

The versioned path has three commands and one property: an image is built from a
git commit tree, the running container is compared against a commit at any time,
and a cutover is refused unless every guard passes.

## What this replaces

| Old path | New path |
| --- | --- |
| `E:\Temp\qazws\omakase-rd-complete-20261111\release.py` with `base_tag` plus `changed = ("debrid_stream.py",)` | `build_image.py` builds the whole tree at a commit, no base tag and no file list |
| Revision derived from a hand maintained file map | Revision derived from the tree of `src/omakase/plus` |
| `E:\Temp\qazws\omakase-tv-seek-overlap-20261111\promote.py` with hard coded image ids, revisions, schema names and expected file hashes | `promote.py` takes those from the signed release manifest and verified live state |
| No drift detection | `check_drift.py` compares live container files and the revision label against a commit |
| Manual, undocumented release steps | `.github/workflows/deploy.yml` and this document |

The old scripts are untouched and still work.

## Files

| File | Purpose |
| --- | --- |
| `build_image.py` | Build an image from an exact git commit tree and write a manifest with per file sha256 |
| `check_drift.py` | Compare a running container against a commit, read only |
| `promote.py` | Guarded cutover with a restore-tested database backup and fenced failure handling |
| `../../.github/workflows/deploy.yml` | Ruff plus unit checks of manifest, drift and recovery guards |

## build_image.py

The build context is the commit tree read object by object from git, so no end
of line filter, clean filter or working tree change can alter a release. A dirty
checkout cannot leak into an image, and the same commit produces the same bytes
on Windows and on Linux.

The revision is derived from `src/omakase/plus` only:

1. read the exact bytes of every file in that tree at the commit,
2. hash each file with sha256,
3. hash the sorted path and hash map with sha256,
4. take the first 12 hex characters and prefix `debrid-reliability-`.

Two commits with the same application tree therefore share a revision, and any
change to a shipped file produces a new one. The revision is written to the
`org.opencontainers.image.revision` label and the manifest records the git
commit and a sha256 for every shipped file.

Write a manifest without building, which needs no Docker daemon:

```powershell
.venv\Scripts\python.exe ops/release/build_image.py --dry-run --manifest "$env:TEMP\manifest.json"
```

Build, tag and verify an image:

```powershell
.venv\Scripts\python.exe ops/release/build_image.py --commit <sha> --manifest "$env:TEMP\manifest.json"
```

Build on the Docker host instead of the workstation. The commit archive is
streamed over ssh, so the host never needs the repository or a checkout, and the
host free space is checked against the floor before the build starts:

```powershell
.venv\Scripts\python.exe ops/release/build_image.py --commit <sha> `
    --remote omakase-ct --readback --manifest "$env:TEMP\manifest.json"
```

`--remote` refuses the build when the Docker root has less than
`--min-free-bytes` free (default 4 GiB). `--readback` additionally proves that
`tesseract`, `mkvmerge`, `ffmpeg`, `faster_whisper` and `pgsrip` resolve inside
the built image and reports their versions.

A real build additionally proves that the image content equals the commit tree.
It hashes every installed file inside the built image, using `python -B` in a
read only, networkless container, and compares both the file set and each hash
against the manifest. A mismatch fails the build. Exit code 0 means success and
1 means a guarded step failed. The comparison covers shipped source files;
interpreter bytecode that pip compiles during install is ignored, while any
other extra file is a hard failure.

## Subtitle runtime parity

The production image also needs OCR and speech recognition tooling. The out of
repo release path added it with a live overlay, which is exactly the silent
regression this directory exists to remove. The versioned image now declares it:

- `Dockerfile` installs `tesseract-ocr` and `mkvtoolnix` next to `ffmpeg` and
  installs `requirements-subtitles.txt` with `--require-hashes`.
- `requirements-subtitles.txt` pins `faster-whisper==1.2.1` and `pgsrip==0.3.0`
  plus every transitive dependency, with sha256 hashes for the linux/amd64
  Python 3.12 wheels.
- `anyio` and `typing-extensions` are pinned to the application versions, so
  the subtitle install cannot rewrite the app stack. The resolved set and
  versions were compared against the live container and match it exactly.
- `tests/infra/test_release_tooling.py` asserts that the Dockerfile keeps
  declaring all four tools and that every subtitle pin is hashed, so CI fails
  if this parity is quietly dropped again.
- `--readback` proves the same facts inside the real built image.

Manifest shape:

```json
{
  "schema": "omakase.release.manifest/1",
  "git_commit": "<40 hex characters>",
  "revision": "debrid-reliability-<12 hex characters>",
  "source": "https://github.com/JhinxDev/omakase-plus",
  "package_tree": "src/omakase/plus",
  "file_count": 348,
  "files": {"__init__.py": "<64 hex characters>", "native_tv_hls.py": "<64 hex characters>"}
}
```

The manifest is deterministic: two runs over the same tree produce identical
bytes, so its own sha256 can be compared between operators.

## check_drift.py

Compares the running container against a commit over ssh. Only read operations
leave the workstation:

1. `docker inspect` for the live image id, revision label and compose chain,
2. one `python -B` call inside the container to locate the installed package,
3. one `python -B` pass over the installed package files that prints two sha256
   digests per file: the raw bytes, and the same bytes with carriage returns
   removed.

No application code is executed, no bytecode is written and nothing is changed.
Interpreter bytecode inside `__pycache__` is ignored because the running
application generates it; it is not shipped content.

### Line endings

The second digest exists because an installed copy can differ from the commit in
its line endings alone. Git stores this repository's text with LF
(`.gitattributes` sets `* text=auto eol=lf`), so a file that reaches
`site-packages` with CRLF did not come from the commit tree unchanged. Such a
file is reported as `line-endings` rather than `content`: the bytes still differ,
so it is still drift and the exit code is still 1, but the summary says the
source itself did not change. A path whose CR-stripped digest also differs stays
`content`.

```powershell
# compare HEAD with the live container, show only differences
.venv\Scripts\python.exe ops/release/check_drift.py

# compare a specific commit and trust a manifest, machine readable
.venv\Scripts\python.exe ops/release/check_drift.py --commit <sha> --manifest manifest.json --json

# show matching files as well
.venv\Scripts\python.exe ops/release/check_drift.py --all
```

Exit codes: 0 when every file and the revision label match, 1 when the container
has drifted, 2 when the check could not be completed.

Example table from the first measured run:

```text
STATUS    FILE                    COMMIT        LIVE
--------  ----------------------  ------------  ------------
content   native_tv_hls.py        da6ec7a13308  f858e542a7ab

5 content, 0 missing, 0 unexpected, 343 matching
REVISION DRIFT: image label debrid-reliability-e3b9bb65a50f is not debrid-reliability-603dfd1088e5
DRIFT DETECTED: the live container does not match the commit tree
```

That run is the reason this directory exists. The live image was built as base
tag `4ade7caa8818` plus an overlay of `debrid_stream.py`, so it never received
commit `c680ccc`, which advertises realistic HEVC bandwidth in the native HLS
master. `native_tv_hls.py` in the container has no `BANDWIDTH=20000000`
substitution, while the same file at the compared commit does.

### The deployed `review44` image, measured again

Running the same read-only check against the image that has been serving
production (`review44-web-26619cc3cd85`, image `sha256:750b7f68`) at commit
`7b1f85a41c27` gives every difference the same name:

```text
STATUS        FILE                                     COMMIT        LIVE
------------  ---------------------------------------  ------------  ------------
line-endings  direct_stream.py                         864749d19ad8  98b4d43c4b6c
line-endings  discover_hub.py                          c7a41087b2e7  27848b6e5516
line-endings  routes.py                                ebdc1483368b  6cde688dcce3
line-endings  static/dashboard.js                      9b3f68f97300  4cffe382077a
line-endings  templates/discover_hub.html              a2c444a83202  2ae46f90670f
line-endings  tv_service.py                            7aa4b46d7698  b518047b42a1
line-endings  viewing_history.py                       733063b10547  ca0240f7a71c

0 content, 18 line-endings, 0 missing, 0 unexpected, 367 matching
LINE ENDINGS: 18 live file(s) match the commit except for carriage returns. Text stored in git uses LF, so those copies did not come from this commit tree. This still counts as drift; rebuilding from the commit removes it.
REVISION DRIFT: image label 26619cc3cd85847db7b5c9f8181b7ff17fe14399 is not debrid-reliability-a7706860aef2
DRIFT DETECTED: the live container does not match the commit tree
```

Seven of the 18 rows are shown; the count, note and drift lines are copied
verbatim. Nothing in the container was missing from the commit and no
source line differed: each byte delta equalled that file's carriage-return count,
and `git diff --no-index` reported no line differences for them. The container
had simply been installed from a wheel built on Windows. The revision label is a
separate matter: the older release path stamped the image with a commit sha,
while this toolchain stamps `debrid-reliability-<12 hex over the tree>`, so the
label only agrees once an image is built here.

## promote.py

Promote requires a manifest from `build_image.py` for the commit that was
reviewed. The manifest is the only accepted source of the revision and the
expected file hashes.

The default is a dry run:

```powershell
.venv\Scripts\python.exe ops/release/promote.py --manifest "$env:TEMP\manifest.json"
```

A read only host readiness check needs no manifest and changes nothing. Use it
first to prove the ssh path, the Docker host and the live health endpoint:

```powershell
.venv\Scripts\python.exe ops/release/promote.py --check
```

Confirm native playback has closed and fence scheduled, API and ad hoc writers
before using `--execute`. Keep the fence in place through verification.

```powershell
.venv\Scripts\python.exe ops/release/promote.py --manifest "$env:TEMP\manifest.json" --execute --native-playback-closed --writers-fenced
```

A failed cutover requires the [current-data recovery procedure](../../docs/current-data-recovery.md).

The historical Compose chain receives only two explicit interpolation bindings,
`PLUS_UI_IMAGE_ID` and `PLUS_UI_REVISION`, from the verified live container.
The same values are used for the prior and candidate chains and recorded in
`prepared.json` for recovery. A prior Compose image tag must resolve locally to
the exact running image ID and revision, including at the final pre-stop check.

The workstation entry sends this script and the manifest to the Docker host over
ssh and runs it there. Nothing is copied to the host and no file is written to
it except by the cutover itself. A dry run writes one scratch compose override
under `/dev/shm` and removes it; it does not create the release lock, and it does
not stop, start or recreate anything.

### Guards

The dry run performs every read only guard, so a green dry run means the
cutover would be attempted without refusing:

1. the running service reports healthy on the expected image and revision label,
2. the candidate image exists on the host and carries the manifest revision label,
3. the live compose chain is the reviewed one,
4. the compose diff is the image plus `OMAKASE_REVISION` and nothing else, and
   other services and top level fields are unchanged, with key paths only in any
   refusal (never values),
5. the last five minutes contain no playback traffic, no active or paused web
   lease remains, no movie cleanup work is running, and shared memory can hold
   the database backup and isolated restore,
6. free space covers the reserve plus the measured backup,
7. the linked native device count is recorded for after the cutover.

### Cutover sequence

With `--execute`:

1. the release lock is taken exclusively and non blocking,
2. the reviewed Compose chain is recorded in
   `backups/<revision>/original-compose-files.txt` with mode 0600,
3. the compose override that changes only the image and revision is written there,
4. the guards are re read immediately before the swap,
5. the service is stopped,
6. the SQLite database is snapshotted with the SQLite backup API, compressed to
   `backups/<revision>/pre-swap.db.xz`, independently restored and checked for
   hash, integrity, foreign-key and migration-ledger equality. A fsynced
   `prepared.json` binds this backup to the live and candidate releases,
7. the candidate compose chain starts with `up -d --no-deps --force-recreate --no-build`,
8. the new image, health status and health revision are read back,
9. private HTTPS is verified for health, device validation and catalog, media,
   resume and library authorization, including `no-store` on the responses that
   must not be cached,
10. the migration set, database integrity, foreign keys, linked device count and
    every shipped file hash are verified,
11. a receipt is written to `backups/<revision>/receipt.json` and printed.

If a candidate start or verification fails, the tool attempts to stop the
candidate and records the failure phase in `failure.json`. Preserve the
current database and keep ingress and writers fenced during recovery.

### Options that matter

| Option | Meaning |
| --- | --- |
| `--execute` | Perform the cutover. Without it the command is a dry run |
| `--check` | Print read only host readiness facts and exit |
| `--native-playback-closed` | Confirm the owner closed native playback |
| `--writers-fenced` | Confirm relevant scheduled, API and ad hoc writes are fenced |
| `--allow-migrations NAME` | Allow exactly this migration to appear, repeatable |
| `--expect-last-migration NAME` | Refuse unless the live schema ends with this migration |
| `--reserve-bytes` | Free space that must remain after the backup, default 8 GiB |
| `--backup-bytes` | Conservative backup size for the pre backup gate, default 26 MiB |
| `--origin` | Public HTTPS origin used for authorization checks |
| `--local` | Run on this host, only when already on the Docker host |

The quiet-window guard is mandatory for cutover. The current native service
keeps paused passes in memory, so confirm playback closure with the owner.

## Safety model

- `check_drift.py` is read only by construction and is the only command that
  should be pointed at production routinely.
- `promote.py` is a dry run unless `--execute` is present. A dry run cannot
  stop, start, recreate or modify a container, and cannot create its lock file.
- A cutover holds an exclusive lock, so two releases cannot interleave.
- The durable database backup is verified, compressed, fsynced and hashed before
  the service is replaced, so a rollback never depends on a file in flight.
- After a candidate start attempt, preserve the current migrated database and
  keep writers fenced until recovery is qualified.
- The compose diff is restricted to the reviewed image and revision. A changed
  port, volume, other service or unrelated environment key refuses the cutover
  and reports key paths, never values.
- CI never uses ssh and never needs Docker. It runs ruff and the unit tests, then
  proves a deterministic manifest from the checked out commit.
- The tooling never pushes to GitHub, never tags a remote and never edits the
  application source.

## CI

`.github/workflows/deploy.yml` runs on changes to `ops/release`,
`tests/infra` or itself:

1. `ruff check` and `ruff format --check` on `ops` and `tests/infra`,
2. `pytest -q tests/infra`,
3. a deterministic manifest proof from the checked out commit, with no Docker
   and no SSH,
4. a `--help` import check for all three commands.

## Cutover runbook

1. Confirm the commit to release and build the manifest with `--dry-run`.
2. On the Docker host, build the image with `--commit <sha>`. The build itself
   verifies the image content against the manifest.
3. Run `check_drift.py --commit <sha>` and read the table. Drift is expected
   before a cutover; the table is the evidence of what the cutover replaces.
4. Run `promote.py --manifest <manifest>` with no `--execute` and read the plan.
5. Confirm native playback closure and fence the writers. Run the same command
   with `--execute --native-playback-closed --writers-fenced`.
6. Keep `backups/<revision>/receipt.json`, `pre-swap.db.xz` and
   `pre-swap.db.sha256` until the release has been accepted.
7. Re run `check_drift.py --commit <sha>` to prove the container now matches.

## retention.py

Release debris accumulates in two places: `backups/`, `receipts/`, `releases/`,
`reviews/` and `qualification/` under the stack directory, and old release
images. The critical constraint is that **every release appends an overlay file
to the live compose invocation**, and those files live inside historical release
directories. Deleting by age alone breaks the next restart.

`retention.py` therefore reads the running container's
`com.docker.compose.project.config_files` label, treats the directory of every
one of those files as protected, keeps the newest few unprotected directories per
root as a rollback window, and only then deletes. It also removes old
`omakase-plus-debrid-reliability:*` images (keeping the live image plus the two
newest), stopped probe containers, and known one-off media artifacts in `data/`.

```powershell
# report only, always safe
.venv\Scripts\python.exe ops/release/retention.py --remote omakase-ct
# reclaim
.venv\Scripts\python.exe ops/release/retention.py --remote omakase-ct --execute
```

Measured on 2026-11-13: 139 live compose inputs protected, 0 missing after the
run, 200 unprotected directories and 49 old images reclaimed, host free space
from 2.5 GB to 23 GB, and disk usage from 97 percent to 73 percent. Verify after
any run with:

```bash
docker inspect omakase-plus \
  --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
```

and confirm every listed path still exists. The compose chain is the reason this
file exists: collapsing it to one compose file plus a release manifest is the
durable fix, and until that lands retention must never delete a referenced path.
