# Connected tvOS qualification fixture

A synthetic companion server for
`tv-source/OmakasePlusTVUITests/ConnectedQualification.swift`. The UI test drives
the real Omakase Plus Apple TV app against this fixture, so the app under test is
unmodified: it is only pointed at a loopback origin.

Everything here is local and free. No server, no provider, no sign-in, no Apple
TV and no microunits are involved.

| file | role |
| --- | --- |
| `connected_fixture.py` | the fixture server, standard library only, Python 3.10+ |
| `start_connected_fixture.sh` | macOS launcher: runtime TLS, background child, CA install, readiness poll |
| `hls/` | the synthetic 21 second HLS sample (fMP4 video, one audio rendition, two VTT captions) |
| `check_xcodeproj_structure.py` | structural check of `OmakaseQualification.xcodeproj` (no `plutil` on Windows) |

Nothing in this directory requires the repository's venv. The fixture imports no
third-party package and takes no dependency on `src/omakase`.

## Why the sample is longer than 21 seconds

The audited sample is 21 seconds. A 45 second pause outlived it, which is one of
the defects the review found. The fixture serves generated playlists that repeat
the vendored sample `PLAYLIST_REPEATS` (8) times behind
`#EXT-X-DISCONTINUITY`, so the served clip is 168 seconds and a 45 second hold
cannot end it. The repeated boundaries are legal HLS, not remuxed media: no
segment is modified and no encoder is needed at run time. `--self-test` asserts
the total.

The master playlist advertises **two** audio renditions (`media-a0.m3u8` English,
`media-a1.m3u8` Commentary) and **two** subtitle renditions
(`captions-0.m3u8` English, `captions-1.m3u8` French). The second audio rendition
reuses the first one's segments, so the fixture can tell which rendition the
player requested by playlist name without shipping a second encode.

## Routes

`/plus/api/native-tv-test/v1/library/*` is the app API. It mirrors the shapes the
real client validates in
`tv-source/QualificationCore/Sources/QualificationCore/LiveLibraryClient.swift`;
the values are synthetic.

| route | method | purpose |
| --- | --- | --- |
| `device` | POST | start pairing: `device_token`, `user_code`, `expires_in` |
| `device` | GET | pairing approval poll: `expires_in` |
| `profile`, `libraries` | GET | one personal library, one profile |
| `catalog` | GET | Movies / Anime page, with Continue Watching |
| `for-you`, `catalog-search` | GET | recommendation rows and request cards |
| `for-you/request`, `for-you/request/check` | POST | request a title, then check it |
| `episodes`, `watchlist`, `details` | GET | saved series details (`Synthetic saved series details.`) |
| `unlink` | POST | surrender the presented credential |
| `play` | POST | open one fresh media pass |
| `media/{id}/access` | GET | grant and plan status (see below) |
| `media/{id}/resume` | POST | checkpoint `{seq, position_seconds}` |
| `media/{id}/close` | POST | close that pass |
| `diagnostic` | POST | bounded playback diagnostic (stored as a counter only) |

`/plus/api/native-tv-test/v1/media/{id}/{asset}` serves the playlists and segments
under the pass-scoped `omakase_tv_test_media` cookie. The sample assets are
`hls/`; `index.m3u8`, `media-video.m3u8`, `media-a0.m3u8`, `media-a1.m3u8`,
`captions-0.m3u8` and `captions-1.m3u8` are generated at request time.

### Access outcomes are distinct

| situation | `/access` | meaning |
| --- | --- | --- |
| healthy | `200 {"allowed": true}` | the pass serves |
| plan reclaimed, grant still authorized | `200 {"allowed": true, "stream": "expired", "recover": "reopen"}` | an idle viewer needs Play to reopen; not revocation |
| closed, withdrawn, replaced or unknown | `404 {}` | the pass is gone: the client must treat it as revocation or a changed copy |

Media bytes are served only while the plan is live, so an expired plan really
cannot be revived: the client has to open a fresh pass.

## Controls

Everything that changes fixture state is a **POST**. A GET on a control route
rewrites nothing, so a mistyped control cannot silently mutate a run. The audited
fixture answered `GET /qa/withdraw-playing-copy` on a POST-only handler and
withdrew the *current* copy, which proved nothing about stale closes.

| control | method | effect |
| --- | --- | --- |
| `/qa/health` | GET | fixture identity, `clip_seconds`, `plan_ttl_seconds` |
| `/qa/state` | GET | every counter the test asserts (below) |
| `/qa/expire-plan` | POST | keep the grant authorized, reclaim the media plan. `/access` then answers the `expired`/`reopen` body and media reads 404 |
| `/qa/stale-close` | POST | close the **older** of two passes and leave the successor live. A client that correctly released the pass it replaced leaves nothing older to close, so the fixture re-registers that oldest pass first and reports `reused_older: true`. Returns `stale_close`, `older_registered`, `successor`, `successor_live`, `gate_owner` |
| `/qa/withdraw-older-pass` | POST | withdraw the older pass's bytes (the changed-copy edge), successor untouched |
| `/qa/close-current` | POST | close the pass that owns the gate |
| `/qa/shelf` | GET | Top Shelf app-group snapshot: `available`, `actions` |
| `/qa/open-shelf?action=details\|resume` | GET | deliver the same external deep link the Top Shelf extension emits |
| `/qa/result` | POST | host-side result readback |
| `/qa/reset` | POST | re-initialise counters between runs |

`/qa/shelf` and `/qa/open-shelf` read the fixture app's **real** app-group
snapshot with `xcrun simctl get_app_container`, then deliver the link with
`xcrun simctl openurl`. When `OMAKASE_SIMULATOR_UDID` is unset or the snapshot is
missing they answer `{"available": false, "reason": ...}` and the test skips that
case with the reason named. They never seed a snapshot: a seeded one would report
a shelf that nothing wrote.

### `/qa/state` fields the tests use

| field | meaning |
| --- | --- |
| `fresh_passes_opened` | how many `play` calls opened a pass |
| `stale_close_received` | a close arrived for a pass that a newer pass had superseded |
| `stale_close_media` | which older pass that was |
| `older_pass_registered` | is the oldest pass still registered (false after a stale close) |
| `successor_media` | the newest pass id |
| `gate_owner` | the pass that currently owns the playback gate |
| `checkpoint`, `checkpoint_seq`, `first_checkpoint` | last and first `resume` checkpoints |
| `resume_position` | the position a *fresh* pass will report: the last checkpoint |
| `passes[]` | per pass: `plan` (`live`/`expired`/`closed`), `registered`, `renditions` |
| `renditions` | global per-asset read counts (`media-a1.m3u8`, `captions-1.m3u8`, ...) |
| `qa_calls` | `play`, `release`, `request`, `check`, `catalog-search`, `diagnostic` |
| `details_reads`, `shelf_probe` | surface counters |

Rendition reads are the track evidence. The same segment set is reused, so a
client that carried an explicit subtitle **Off** fetches no `captions-*.m3u8` at
all, and a client that carried French fetches `captions-1.m3u8` and not
`captions-0.m3u8`.

## Self-test (runs on this Windows host)

```powershell
.venv/Scripts/python.exe scripts/fixtures/tvos-connected/connected_fixture.py --self-test
```

It drives the pure `FixtureState` with a fixed clock: live and expired access
outcomes, registration after expiry, checkpoint carry-over, stale close of an
older pass, gate ownership, withdrawal, unknown-pass rejection, and the
per-pass rendition record. It needs no TLS, no socket, no simulator and no macOS.
Exit status 0 means every check passed; the failing line names the check.

`scripts/qualify-tvos.ps1` runs this as the `tvos-fixture-self-test` gate, so the
receipt counts it as an executed check.

## Running the simulator suite (macOS only)

```bash
# 1. start the fixture against a booted simulator and trust the throwaway CA
bash scripts/fixtures/tvos-connected/start_connected_fixture.sh \
  <simulator-udid> build/fixture-scratch

# 2. run the connected suite against that origin
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

Environment: `OMAKASE_FIXTURE_PORT` (default 8854),
`OMAKASE_PLAN_TTL_SECONDS` (default 120), `OMAKASE_SIMULATOR_UDID` (enables
`/qa/shelf`), `OMAKASE_QUALIFICATION_ORIGIN` and `OMAKASE_LONG_PAUSE_SECONDS`
(read by the UI test).

## Limits

- A loopback fixture is not the live server. It proves the app's client behaviour
  and the response shapes it consumes.
- The sample is synthetic and repeated. It does not test HEVC, long-form
  continuity, or provider behaviour.
- Simulator Top Shelf is an app-group snapshot, not the physical Home Screen.
- `ConnectedQualification.swift` has not been compiled or run on Windows: it
  imports `XCTest` and drives `XCUIApplication`/`XCUIRemote`, so only a tvOS
  toolchain can verify it. The fixture and the self-test above are the parts that
  are checkable on this host.
- No certificate, key or CA is vendored. `start_connected_fixture.sh` generates
  them under the scratch directory, which must stay out of Git.
