# Native startup phases probe

Reads the server's own saved-start startup-phase receipt and reports where the
wait before the first playable segment went.

This is the saved-start probe the audit asked for. It adds no instrumentation:
the server already records the phases on `NativePlan.phases` and already logs
them once per media pass, and this probe parses exactly that line.

## What it measures

For a saved-position ("Continue Watching") open, the server attributes the wait
to eight phases, each in whole milliseconds:

| key | recorded by | meaning |
| --- | --- | --- |
| `resolve` | `debrid_stream.py` | resolve the provider file for the title |
| `plan` | `debrid_stream.py` | build the stream plan |
| `index` | `debrid_stream.py` | read the index / timeline of the source |
| `issue` | `debrid_stream.py` | issue the media pass the player will use |
| `captions` | `debrid_stream.py:1046` | hold until caption tracks are ready |
| `timeline` | `debrid_stream.py:1050` | map the saved position onto the timeline |
| `opening` | `debrid_stream.py:1060` | prepare the first playable window |
| `first_media` | `native_tv_test.py:2946` | total to the first playable media response |

`first_media` is the total, and it is written exactly once per pass, when the
first `.m4s` media response for that pass is delivered. That is the moment the
player can actually begin, not the moment the pass opened.

The source line is emitted at `native_tv_test.py:2949`:

```
native phases media=<media_id> resolve=31ms plan=12ms index=240ms issue=9ms captions=410ms timeline=15ms opening=880ms first_media=1597ms
```

The canonical key set and the "whole milliseconds, never negative" contract are
the same ones `tests/plus/test_pause_recovery_and_gate.py:180` already asserts.

## How to run it

All commands run from the repository root. They are bounded, offline and
non-interactive: the probe reads text and exits.

Prove the parser first. It checks good lines and rejects the known-bad shapes,
so a green run of the gate later means something:

```powershell
.venv/Scripts/python.exe scripts/fixtures/native-startup-phases/native_startup_phases.py --self-test
```

Read a captured server log. Capture the log with a log route you already have
read-only access to; this probe never connects to the server itself:

```powershell
.venv/Scripts/python.exe scripts/fixtures/native-startup-phases/native_startup_phases.py `
  --log <captured-server.log> `
  --require-complete `
  --max-first-media-ms 6000 `
  --json build/qualification/phases.json
```

Or pipe the lines in:

```powershell
Select-String -Path <captured-server.log> -Pattern 'native phases media=' |
  .venv/Scripts/python.exe scripts/fixtures/native-startup-phases/native_startup_phases.py --require-complete
```

Exit codes: `0` the receipt is complete and inside budget, `2` a gate failure
(details on stderr, full report in the JSON), `3` a log could not be read.

## What the report contains

`build/qualification/phases.json` carries the schema marker
`native_startup_phases_v1`, the canonical key list, and per pass:

- `phases_ms`: the parsed keys and values
- `missing`: canonical keys the server did not record
- `unknown`: keys the server recorded that are not in the canonical set
- `malformed`: unparsed fragments or duplicate keys
- `phase_share_of_first_media`: each phase as a fraction of the total
- `slowest_phase`: the phase that dominated this open

Only the **latest** line per media id decides the gate. A re-open supersedes an
earlier attempt, so an old incomplete line cannot fail a new run.

## What it does not prove

It reads one log line. It does not prove decoding, continuous playback, the
absence of stalls earlier in the timeline, a tvOS build, a signature, an install,
or anything about the Apple TV. A green gate here means the server attributed a
saved-position open to the eight phases, completely and within budget.

If the log contains no `native phases media=` line at all, treat that as a
failure to explain: either the saved-start open never reached a playable
segment, or the log predates the phase receipt. The probe reports that case
instead of returning an empty success.
