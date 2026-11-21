# Offline iPhone qualification prototype

This is a small iOS 17+ simulator target in the canonical Omakase Plus repository.
It is an engineering starting point, not a production iPhone app or an accepted
phone design. The web and tvOS targets remain independent.

Open `OmakasePlusPhone.xcodeproj` and its shared `OmakasePlusPhone` scheme on a
macOS build host with full Xcode and an installed iPhone Simulator runtime.
The owner does not need a personal Mac; an authorized existing macOS CI or build
host can run the same command. No rental or paid session is started by this code.

From the repository root:

```sh
bash scripts/qualify-ios.sh /absolute/path/to/new-ios-evidence
```

The evidence directory must not exist. Set `DEVELOPER_DIR` to the installed Xcode
and optionally `OMAKASE_IOS_RUNTIME` to an installed runtime version. The script
selects a compatible iPhone, creates a disposable simulator, runs current
QualificationCore tests and iOS UI tests, retains logs and an xcresult bundle,
and removes only its simulator and scratch directory. No signing, device install,
certificate trust change, fixture server, account or provider access is involved.
The manually dispatched `ios-simulator.yml` workflow checks out its exact event
SHA. Adding it does not run it. A run needs separately authorized execution.

## Reused contracts and scope

The original prototype's separate phone target, tab-independent catalog/search
concepts, synthetic journey qualification and independent bundle identity are
retained in a smaller shell. The project and shared scheme were adapted from the
previous prototype; its complete source/history is preserved privately outside
this public repository. No raw legacy history or private receipt is imported.

The app links `../tv-source/QualificationCore` directly and bundles the existing
silent synthetic `baseline.mp4` by reference. There is no copied core or media.
The only shared package change declares iOS 17 support alongside existing tvOS
and macOS support. Catalog identity, media version and session resume validation
come from the latest `LibraryFixture` and `ResumeLedger`. Search, title detail,
UI tests are provided for AVKit playback, seeking and per-title session resume.
Progress deliberately stays in memory and resets at process relaunch.

The old live UI depended on an older `PlaybackSession`/player copy and older
closed target enums. Current core adds anime-film and cloud targets and
viewer-scoped resume identity. Blindly restoring those old switches or player
files would lose current contract coverage and duplicate tvOS behavior. The old
live pairing, Keychain profiles, server-address injection, live-provider tests,
fixture server/upstream copies and private evidence are intentionally excluded.

## Next implementation boundary

Choose and review the actual phone experience before expanding this shell.
Use the current shared core for pairing and library identity; make a dedicated
iOS adapter for account persistence and playback lifecycle rather than copying
tvOS screens or stale player files. Add synthetic account switching, household
scope denial, revocation, background interruption, track selection, next episode
and resume synchronization tests before any authorized live check. Cover all
current target cases explicitly. Keep production and prototype bundle IDs distinct.

A simulator pass establishes compilation and a synthetic journey only. Physical
iPhone layout, safe-area and rotation behavior, VoiceOver, Dynamic Type,
background audio, codec/HDR/subtitle behavior, real authorization and distribution
remain separate acceptance work. No release or device IPA is produced here.

## Consolidation verification, November 21, 2026

The current shared package compiled on Windows with Swift 6.4.1: 111 tests,
zero failures, one expected fixture-server integration skip. The Windows SDK
emitted existing WinSDK modularization warnings. Both new Swift files passed
syntax parsing. Shell syntax, project reference IDs, shared-scheme XML, canonical
package/resource paths and the synthetic clip SHA-256 were checked. A scoped
source audit found no endpoints, credentials, private fixtures or imported history.

The new iOS app has **not** been compiled with an Apple SDK or run in an iPhone
simulator in this consolidation. The two UI tests and macOS runner are prepared,
not passing evidence. Run the manual workflow on the reviewed exact commit only
after execution is authorized. No provider, physical device or rental was used.
