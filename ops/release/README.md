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
| `promote.py` | Guarded cutover with a durable database backup and automatic rollback |
| `../../.github/workflows/deploy.yml` | Ruff plus unit checks of manifest, drift and rollback logic |

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
1 means a guarded step failed.

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
3. one `sha256sum` pass over the installed package files.

No application code is executed, no bytecode is written and nothing is changed.
Interpreter bytecode inside `__pycache__` is ignored because the running
application generates it; it is not shipped content.

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

`--execute` is required before anything changes:

```powershell
.venv\Scripts\python.exe ops/release/promote.py --manifest "$env:TEMP\manifest.json" --execute
```

Rollback restores the compose chain recorded for the running release:

```powershell
.venv\Scripts\python.exe ops/release/promote.py --manifest "$env:TEMP\manifest.json" --rollback --execute
```

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
5. no native playback or browser playback is active, no movie cleanup work is
   running, and shared memory can hold the database snapshot,
6. free space covers the reserve plus the measured backup,
7. the linked native device count is recorded for after the cutover.

### Cutover sequence

With `--execute`:

1. the release lock is taken exclusively and non blocking,
2. the reviewed compose chain is recorded under `backups/<revision>/`,
3. the compose override that changes only the image and revision is written there,
4. the guards are re read immediately before the swap,
5. the service is stopped,
6. the SQLite database is snapshotted with the sqlite backup API, checked with
   `PRAGMA quick_check`, compressed to `backups/<revision>/pre-swap.db.xz`,
   fsynced together with its directory and pinned by a sha256 receipt,
7. the candidate compose chain starts with `up -d --no-deps --force-recreate --no-build`,
8. the new image, health status and health revision are read back,
9. private HTTPS is verified for health, device validation and catalog, media,
   resume and library authorization, including `no-store` on the responses that
   must not be cached,
10. the migration set, database integrity, foreign keys, linked device count and
    every shipped file hash are verified,
11. a receipt is written to `backups/<revision>/receipt.json` and printed.

If any step fails after the swap begins, the previous compose chain is recreated
with the same command and the previous image and revision are read back healthy
before the original error is re raised. The rendered override stored in the
release directory is the exact input that was used.

### Options that matter

| Option | Meaning |
| --- | --- |
| `--execute` | Perform the cutover. Without it the command is a dry run |
| `--check` | Print read only host readiness facts and exit |
| `--rollback` | Restore the recorded previous chain for the running release |
| `--allow-migrations NAME` | Allow exactly this migration to appear, repeatable |
| `--expect-last-migration NAME` | Refuse unless the live schema ends with this migration |
| `--reserve-bytes` | Free space that must remain after the backup, default 4 GiB |
| `--backup-bytes` | Conservative backup size for the pre backup gate, default 26 MiB |
| `--origin` | Public HTTPS origin used for authorization checks |
| `--local` | Run on this host, only when already on the Docker host |

`OMAKASE_RELEASE_FORCE_IDLE=1` exists because the original script had the same
escape hatch. It disables the playback idle check and prints a warning. Do not
use it while a player session is in use.

## Safety model

- `check_drift.py` is read only by construction and is the only command that
  should be pointed at production routinely.
- `promote.py` is a dry run unless `--execute` is present. A dry run cannot
  stop, start, recreate or modify a container, and cannot create its lock file.
- A cutover holds an exclusive lock, so two releases cannot interleave.
- The durable database backup is verified, compressed, fsynced and hashed before
  the service is replaced, so a rollback never depends on a file in flight.
- Any failure after the swap begins restores the previous chain automatically.
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
5. Only then run the same command with `--execute`.
6. Keep `backups/<revision>/receipt.json`, `pre-swap.db.xz` and
   `pre-swap.db.sha256` until the release has been accepted.
7. Re run `check_drift.py --commit <sha>` to prove the container now matches.
