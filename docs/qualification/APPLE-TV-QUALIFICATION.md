# Apple TV qualification

How an Omakase Plus Apple TV change becomes accepted, in four steps that prove
different things. Each step is bounded and stops at the first failure.

The audit that motivated this document is
`Projects/omakase/evidence/2026-11-15-product-audit/AUDIT.md` section 7: the last
qualification used eight NoMac sessions and 630,000 microunits, partly because
the connected fixture, the saved-start probe and the artifact-transfer receipts
existed only in temporary directories. Everything that can be local is local
now, and the paid step is explicit.

Read the steps in order. **A passing step never implies the next one.**

---

## Step 1: Portable logic (this host, free, seconds)

**What it does.** Runs the off-device gates that catch most regressions before
any paid work:

- the phase-probe self-test (the reader accepts good receipts and rejects the
  known-bad shapes)
- the connected fixture self-test (plan expiry stays authorized, a stale close
  leaves the successor live, checkpoint carry-over, rendition accounting)
- `pytest` over the native TV, route, pause-recovery and subtitle gates
- `swift test` in `tv-source/QualificationCore`, the portable half of the
  tvOS logic (plan recovery, playback lifecycle, seeking, track selection,
  fixture and live clients)

**What it proves.** The Python server logic, the connected fixture's state
machine and the portable Swift logic behave as asserted, including the negative
cases those suites carry.

**What it does not prove.** Nothing about a tvOS build, a signature, an install,
the Apple TV, UIKit, AVFoundation, the player surface, remote-focus behaviour, or
the Top Shelf extension. `QualificationCore` deliberately contains no UIKit or
AVFoundation import; a test there cannot stand in for a device test. The
connected UI fixture in `tv-source/OmakasePlusTVUITests` is Swift that only a
tvOS toolchain can compile and only a simulator or device can run.

**Command** (repository root, from the worktree):

```powershell
pwsh -File scripts/qualify-tvos.ps1
```

It writes `build/qualification/qualification-receipt.json` and prints the next
command. Optional arguments:

| argument | default | meaning |
| --- | --- | --- |
| `-OutputPath` | `build/qualification/qualification-receipt.json` | receipt destination |
| `-Skip` | none | gate ids to skip, comma-separated: `-Skip 'swift-qualification,pytest-native-tv'` |
| `-PythonPath` | `.venv/Scripts/python.exe` | interpreter with pytest installed |
| `-SwiftPath` | `swift` from `PATH`, else a discovered local toolchain | Swift toolchain |
| `-BaseCommit` | `6c44a99fc8356278892a0bb51d213845f561ca08` | commit the snapshot digest diffs against |
| `-PytestTimeoutSeconds` | 1800 | deadline per child |
| `-SwiftTimeoutSeconds` | 1200 | deadline per child |

Each child runs with a hidden window, captured stdout/stderr, a finite deadline
and a recorded exit status. A timeout is killed with its process tree and
reported as `timed_out: true`, never silently retried.

### Outcomes and exit codes

The runner distinguishes four outcomes, and only one of them is a pass:

| `result` | meaning | exit |
| --- | --- | --- |
| `passed` | every planned gate executed and passed | 0 |
| `partial` | some gates executed and passed, at least one was skipped | 3 |
| `failed` | an executed gate failed | 1 |
| `skipped` | no gate executed at all: nothing was proven | 2 |

`checks_run` counts gates that actually executed, so a run cannot claim more
checks than it ran. A run with every gate skipped is `skipped` and exits 2: the
audited runner returned 0 and wrote `{"result":"passed","checksRun":0}` in exactly
that situation. A missing toolchain makes its gate `skipped` with the reason
`toolchain_missing:<tool>`, never a failure of the code under test and never a
pass.

### Snapshot digest

`source.snapshot_sha256` binds the tested source, not a directory name:

```
sha256("omakase-tvos-qualification-snapshot-v1
        base=<BaseCommit> commit=<HEAD> branch=<branch>
        index_sha256=<git ls-files -s> diff_sha256=<git diff --binary base>
        untracked_sha256=<sha256 of each untracked file>")
```

Tracked blob identities, the uncommitted diff against the base commit and every
untracked file are all hashed, excluding `build/`, `__pycache__` and `*.pyc`. The
component digests are recorded too, along with `base_commit_known` and
`source.tested_paths` (the files and directories the executed gates read). Tool
locations are recorded portably: workspace-relative inside the checkout,
otherwise only the generic file name, never a home directory.

Bind downstream receipts to this one by `source.snapshot_sha256`,
`source.commit` and the artifact hash, never by a directory name.

### Saved-start startup phases

The saved-position open is where a "slow resume" argument actually gets decided.
The server already attributes it, so do not add instrumentation: read the
existing receipt.

```powershell
# correctness of the reader itself
.venv/Scripts/python.exe scripts/fixtures/native-startup-phases/native_startup_phases.py --self-test

# attribution for a captured server log
.venv/Scripts/python.exe scripts/fixtures/native-startup-phases/native_startup_phases.py `
  --log <captured-server.log> --require-complete --max-first-media-ms 6000 `
  --json build/qualification/phases.json
```

Keys, in reporting order: `resolve`, `plan`, `index`, `issue`, `captions`,
`timeline`, `opening`, `first_media`. They come from `NativePlan.phases`
(`src/omakase/plus/native_hls.py`) recorded in `debrid_stream.py` and completed
in `native_tv_test.py`, and appear in the log as:

```
native phases media=<media_id> resolve=..ms plan=..ms ... first_media=..ms
```

`first_media` is the total, written once per pass when the first playable media
response is delivered. See
[`scripts/fixtures/native-startup-phases/README.md`](../../scripts/fixtures/native-startup-phases/README.md).

### Connected fixture self-test

The fixture the Step 4 UI suite drives is vendored in
[`scripts/fixtures/tvos-connected/`](../../scripts/fixtures/tvos-connected/README.md)
and its pure state machine runs on this host as the `tvos-fixture-self-test`
gate:

```powershell
.venv/Scripts/python.exe scripts/fixtures/tvos-connected/connected_fixture.py --self-test
```

It needs no TLS, no socket, no simulator and no macOS. It asserts the sequences
the audited fixture could not express: `/access` answers
`{"allowed": true, "stream": "expired", "recover": "reopen"}` while the grant
stays authorized, a stale close of the older pass leaves the successor live with
its gate untouched, a reopen reports the last checkpoint, and rendition reads are
attributed per pass.

---

## Step 2: tvOS build (NoMac, one session, no device)

**What it does.** Builds and links the device targets on macOS with full Xcode
from a source snapshot whose commit and tree are recorded.

**What it proves.** The tvOS targets compile and link for `arm64`, and the
portable suite passes inside that build. It does **not** prove a signature, an
install, or any on-device behaviour.

**Command** (macOS with full Xcode, from the repository root):

```bash
bash scripts/build-tvos.sh build/tvos-$(git rev-parse --short=12 HEAD)
```

The script refuses a non-Darwin host, verifies the source archive against its
SHA-256 and commit when `--snapshot` is used, and writes a build receipt
(`source_commit`, `git_tree`, `xcode`, `appletvos_sdk`, `tvvlckit`, `ipa` with
`version`, `build` and `sha256`).

The local runner never runs this. It prints the exact command instead, because
building is a separate authorized action.

---

## Step 3: Sign and install (NoMac + tvsign, separate authorization)

**What it does.** Signs the built artifact with the provisioning identity and
installs it on the Apple TV, then reads the installed version back from the
device.

**What it proves.** The artifact by that hash is signed and installed, and the
device reports the expected version and build. It does **not** prove that
playback or focus behave correctly; only a person watching does that.

**Exact bounded commands.** They live with the signing tooling the owner holds
(`tvsign`), not in this repository. Bind them to the Step 2 receipt rather than
to a directory name:

1. read back the installed app to confirm the starting state
2. sign the `ipa.sha256` from the build receipt
3. install while the device is quiet
4. read the installed `version` and `build` back and record them with the same
   artifact hash

Record, in one receipt: artifact SHA-256, app version and build, the signer
version, the install result, and the device readback. The audited temporary
directories show the shape:

```json
{ "tracked_version": "0.6.10", "status": "ok", "installed": ["0.6.10", "43"] }
```

**Rollback identity.** Keep these in the acceptance record, not in chat:

| role | identity |
| --- | --- |
| app, current qualification target | build 43 |
| app, immediate rollback | **build 42, verified as the immediate rollback for build 43** |
| server image | `omakase-plus:saved-start-8ddffdd-scoped` |
| server image digest | `sha256:f37cd423ecd509beaa4b836c49f18677108a44550fc27db2126f5715221c4fbb` |

Rollback is only a rollback if it is the recorded build 42 re-installed from its
own artifact hash. An experimental change plus a revert is not a restoration:
capture the hash you actually installed and the version the device read back.

---

## Step 4: Physical acceptance (Apple TV, watched, one session)

**What it does.** Runs the connected UI fixture against the app on the device.

**What it proves.** That a person's session on real hardware shows the behaviour
the fixture asserts. What it still does not prove is anything it did not
exercise: this fixture does not test HEVC, long-form continuous playback, or
every scroll path.

**Fixture.** `tv-source/OmakasePlusTVUITests/ConnectedQualification.swift`,
compiled in the tvOS UI test target. The project's UI-test Sources phase lists
both `RemoteFocusTests.swift` and `ConnectedQualification.swift`; nothing else
publishes or compiles it.

| test | asserts |
| --- | --- |
| 01 `test01CatalogRequestAndCheck` | catalog request and status check, focus on Back, focus return |
| 02 `test02CatalogSearchAndFocusReturn` | typed search reaches the server, focus returns |
| 03 `test03ContinueWatchingPlaybackAndFocus` | a real pass with a checkpoint, then the release on return |
| 04 `test04TopShelfDetails` | Top Shelf details deep link and the saved-series overlay's own Close control |
| 05 `test05TopShelfResume` | Top Shelf resume deep link, external-open consent, then playback |
| 06 `test06LongPausePlanExpiryWaitsForPlay` | paused player stays on screen (`AVKit` shows Play), no pass while idle, exactly one pass on Play at the saved checkpoint, old pass released, no second pass |
| 07 `test07CarriedSubtitleOffSurvivesReopen` | an explicit subtitle Off is carried: the reopened pass fetches no captions rendition |
| 08 `test08CarriedTracksSurviveReopen` | chosen French subtitles and Commentary audio are carried across the reopen |
| 09 `test09StaleCloseDoesNotEndPlayingSession` | a close for the older pass leaves the successor live, gate untouched, checkpoint advancing |

Playback and focus are asserted in **separate groups**: a playback start cannot
cover a missing focus return, and a focused card is not a playback proof. Every
identifier the fixture uses already exists under `tv-source/OmakasePlusTV/`; the
fixture introduces no new route and no new identifier.

**Skips are explicit.** Only the Top Shelf tests can skip, and only when the
simulator really does not expose the app-group snapshot: the message names the
reason returned by `/qa/shelf`. A skip is reported by `XCTSkip`, which marks the
test skipped, never passed. A fixture that is down, or a control that answers a
non-200 status, **fails** the test: the audited helper turned non-200 into a skip,
so a broken control read as "not proven" while the suite exited green.

**The companion fixture is vendored**, with TLS material generated at run time:

```bash
# 1. start the fixture for a booted simulator and trust its throwaway CA
bash scripts/fixtures/tvos-connected/start_connected_fixture.sh \
  <simulator-udid> build/fixture-scratch

# 2. run the connected suite against that origin (App installed on the same simulator)
xcodebuild test \
  -project tv-source/OmakaseQualification.xcodeproj \
  -scheme OmakasePlusTV \
  -destination 'platform=tvOS Simulator,id=<simulator-udid>' \
  -only-testing:OmakasePlusTVUITests/ConnectedQualification \
  OMAKASE_QUALIFICATION_ORIGIN=https://127.0.0.1:8854 \
  OMAKASE_LONG_PAUSE_SECONDS=45

# 3. stop the fixture
kill "$(cat build/fixture-scratch/server.pid)"
```

`OMAKASE_QUALIFICATION_ORIGIN` overrides the loopback origin (default
`https://127.0.0.1:8854`); `OMAKASE_LONG_PAUSE_SECONDS` bounds the long-pause hold
(default 45). The fixture's served sample is 168 seconds, so a 45 second hold
cannot outlive it. Controls (`POST /qa/expire-plan`, `POST /qa/stale-close`,
`GET /qa/state`, `/qa/shelf`, `/qa/open-shelf`) and every counter are documented in
[`scripts/fixtures/tvos-connected/README.md`](../../scripts/fixtures/tvos-connected/README.md).
No certificate or key is committed: the launcher generates a throwaway CA and
server certificate under the scratch directory and installs the CA into that
simulator's keychain.

Physical Home Screen rendering of the Top Shelf extension remains a separate,
human-verified step.

---

## Receipt schema

`scripts/qualify-tvos.ps1` writes `build/qualification/qualification-receipt.json`
(schema `omakase_tvos_qualification_receipt_v2`):

```json
{
  "schema": "omakase_tvos_qualification_receipt_v2",
  "kind": "portable-gates",
  "result": "passed | partial | failed | skipped",
  "failed_gate": null,
  "checks_run": 4,
  "checks_skipped": 0,
  "checks_planned": 4,
  "exit_code": 0,
  "started_at": "<ISO-8601 UTC>",
  "finished_at": "<ISO-8601 UTC>",
  "elapsed_seconds": 40.2,
  "host": "<computer name>",
  "source": {
    "root": "<checkout path>",
    "branch": "integrate/hardening-tv",
    "commit": "<40-hex>",
    "commit_subject": "<subject>",
    "tree": "<40-hex>",
    "base_commit": "6c44a99fc8356278892a0bb51d213845f561ca08",
    "base_commit_known": true,
    "snapshot_sha256": "<64-hex>",
    "index_sha256": "<64-hex>",
    "diff_sha256": "<64-hex>",
    "untracked_sha256": "<64-hex>",
    "untracked_count": 44,
    "dirty_count": 18,
    "dirty_paths": ["..."],
    "tested_paths": ["tests/plus/test_native_tv_test.py", "tv-source/QualificationCore"],
    "digest_scope": ["..."]
  },
  "toolchains": {
    "pwsh": "7.7.0", "os": "...", "git": "...",
    "python": "Python 3.12.10", "python_path": ".venv/Scripts/python.exe",
    "python_source": "workspace-relative",
    "swift": "Swift version 6.4.1 (swift-6.4.1-RELEASE)", "swift_path": "swift.exe",
    "swift_source": "local-toolchain"
  },
  "gates": [
    {
      "id": "tvos-fixture-self-test",
      "description": "...",
      "passed": true, "skipped": false, "reason": null,
      "exit_code": 0, "timed_out": false, "elapsed_seconds": 0.16,
      "started_at": "<ISO-8601 UTC>",
      "command": [".venv/Scripts/python.exe", "-B", "scripts/fixtures/...", "--self-test"],
      "cwd": ".",
      "log": "<build/qualification/logs/tvos-fixture-self-test.log>",
      "tail": ["<last 40 output lines>"],
      "tested_paths": ["scripts/fixtures/tvos-connected/connected_fixture.py"]
    },
    {
      "id": "swift-qualification",
      "passed": null, "skipped": true, "reason": "toolchain_missing:swift",
      "elapsed_seconds": 0.0
    }
  ],
  "not_run": [
    { "action": "tvOS build", "why": "requires macOS and full Xcode",
      "next_command": "bash scripts/build-tvos.sh build/tvos-<sha>" },
    { "action": "simulator UI tests", "why": "requires macOS, the tvOS simulator and the connected fixture",
      "next_command": "..." },
    { "action": "sign and install", "why": "separate authorized action", "next_command": "..." },
    { "action": "physical acceptance", "why": "requires the Apple TV and a watched session", "next_command": "..." }
  ],
  "next_command": "bash scripts/build-tvos.sh build/tvos-<sha>",
  "notes": ["Portable gates prove logic only..."]
}
```

`dirty_paths` is part of the identity: a receipt from a dirty tree is honest
about being dirty, and the paths are listed rather than summarised. Logs for each
gate are kept under `build/qualification/logs/` and pointed at from the receipt.

Bind downstream receipts to this one by `source.snapshot_sha256`,
`source.commit`, `source.tree` and the artifact hash, never by a directory name.

---

## Cleanup rules

These are part of qualification, not an afterthought. The last run cost eight
sessions partly because cleanup and accounting were improvised.

1. **Stop sessions in a `finally` path.** A qualification session that raises
   still has to release its media passes, stop its player and dismiss its host
   view controller. Put teardown in `defer` / `finally` so a failed assertion
   cannot leak a paid session.
2. **Verify provider cleanup, do not assume it.** After the session stops, read
   back that the provider released the stream and that no child process remains.
   The audited container receipt records the shape to assert:
   `"ffmpeg_children_after_close": 0`. A zero you did not read is not a zero.
3. **Record the actual charge.** Write the measured microunits and session count
   into the acceptance record next to the receipt. The previous run's
   630,000 microunits over eight sessions is the number to beat or explain, not
   a budget to quote back.
4. **Keep Windows jobs hidden.** Every spawned process runs with a hidden window
   and a recorded PID and exit code. `scripts/qualify-tvos.ps1` does this
   internally (`UseShellExecute = false`, `CreateNoWindow = true`, finite
   deadline, killed with its tree on timeout). For anything outside it, use
   `C:/Users/qazws/.dsh/bin/run-task.ps1` rather than a bare `Start-Process`, and
   never open a terminal window for qualification work.
5. **Leave the tree as you found it.** Do not commit, push, merge, rebase, reset
   or check out during qualification. Remove only artifacts the run owns, and
   check free space before a build and after it.

---

## Current local status

Run on this host at `integrate/hardening-tv` @ `6c44a99fc835`, dirty tree
preserved as found:

| gate | result | elapsed |
| --- | --- | --- |
| `phase-probe-self-test` | pass | 0.11 s |
| `tvos-fixture-self-test` | pass | 0.16 s |
| `pytest-native-tv` | pass | 35.10 s |
| `swift-qualification` | pass | 3.98 s |

`result: passed` (exit 0), `checks_run: 4`. Toolchains: PowerShell 7.7.0, Python
3.12.10, Swift 6.4.1. Total 40 s, no NoMac session, no microunits, nothing built or
installed.

The exact `source.snapshot_sha256` for a run is in the receipt it wrote, not in
this paragraph: any edit here changes the digest the next run computes, so a
hard-coded value would be wrong the moment it was quoted. Read it back with:

```powershell
(Get-Content build/qualification/qualification-receipt.json -Raw | ConvertFrom-Json).source.snapshot_sha256
```

The four outcomes were also exercised directly on this host: every gate skipped
gives `skipped`/exit 2 with `checks_run: 0`, one skipped gate gives
`partial`/exit 3, and a gate that exits non-zero gives `failed`/exit 1 with
`failed_gate` naming it.

Deliberately **not** automated here: the tvOS build (macOS + Xcode), the simulator
UI suite (macOS + tvOS simulator + the connected fixture), sign and install
(tvsign + device), physical acceptance (a person watching), and physical Top
Shelf rendering. `ConnectedQualification.swift` has not been compiled or run on
Windows: it imports `XCTest` and drives `XCUIApplication` / `XCUIRemote`, so only a
tvOS toolchain can verify it. Its recovery assertions additionally depend on
AVKit's own transport state and on the track-selection surface, which are the two
things a macOS run must confirm first if it fails.

Next command, exact:

```bash
bash scripts/build-tvos.sh build/tvos-6c44a99fc835
```
