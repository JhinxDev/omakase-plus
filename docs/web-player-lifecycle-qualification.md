# Synthetic deadline and restart gate

`scripts/qualify_web_player_lifecycle.py` prepares two native lifecycle cases and
one restored-admission prefix cancellation. Unit tests validate helper contracts;
only the isolated native execution qualifies watchdog/death/reconciliation.
There are no original-media requests, private credentials, or production edits.

Run under Tini (`--init`) with network mode `none`, one CPU, 3 GiB memory, pids 64,
and an empty dedicated 1 GiB tmpfs cache. Mount the accepted source at `/probe/src`
and the existing 626512-byte synthetic H264/dual-Opus seed read-only. Supply a new
empty writable QA work directory for two file-backed SQLite databases and receipts.
No private metadata database is needed.

```sh
python -I /qa/qualify_web_player_lifecycle.py \
  --work /qa-state --media /qa/seed.mkv --cache /run/omakase-playback
```

The helper refuses PID 1/non-Linux and nonempty/symlink QA/cache directories. A
30 s outer timer and 28 s async orchestration bound apply. Each owned nested
coordinator invokes actual `prepare_remux`, actual quota admission and unchanged
launcher/child limits with a permitted shorter 2 s deadline. MockTransport serves
only the known synthetic Part. Prefix probing gets the small seed; the full
stream yields exactly 64 KiB and stalls, so no COPY completes or becomes READY.
The synthetic source has known indexed H264/Opus tracks and a conservative 10 s
upper timeline solely for these interrupted cases; no source-duration acceptance
is claimed. Authorization is a standalone synthetic true callback; full real
authorization already belongs to the separate production pipeline gate.

Deadline case: after exact worker identities and both worker/watchdog groups are
observed, the orchestrator verifies its own coordinator PID/start ticks/boot ID
then sends that PID SIGSTOP. It signals no media process. It observes each media
worker become zombie/absent around the original 2 s deadline while the coordinator
remains stopped, records first-observed monotonic exits, then identity-verifies
and SIGCONTs the coordinator. Production teardown must reap its children and
remove its lease/partial. Every owned group must disappear in all states,
including watchdog zombies (bounded 500 ms allowance).
Both media workers must be non-zombies with exact identities immediately before
the signal. The final coordinator receipt records every child's return code;
both remux and observer must have `-SIGKILL`, and the actual preparation must
report `RemuxError/deadline_exceeded`. Zombie/absence observations alone do not
pass the watchdog gate.

Parent-death case: after the same stall/identity prerequisites, the helper sends
SIGKILL to its verified coordinator PID alone. Both media workers must still be
non-zombies and at least 1 s must remain before the original watchdog deadline.
All owned groups must disappear before that original deadline, and both timing
margins are recorded. This excludes ordinary deadline expiry as the explanation
for the parent-death result. Actual PDEATHSIG and watchdog
behavior must remove all owned worker groups; Tini must reap them. The helper
reopens the preserved SQLite database, confirms the RUNNING source hold, and
confirms ordinary cleanup cannot release the lease before the real recorded
deadline plus 5 s grace. It waits actual wall time (no forged `now`), then invokes
actual cleanup. A temporary diagnostic version hold makes the reconciled ERROR
row observable with zero RUNNING/READY reservation; it grants no byte access.
The following ordinary unheld cleanup must remove that known partial and row.

The next admission uses **that same reconciled database**, actual preparation
and limits. Its next prefix stalls after its first chunk and is finitely
cancelled, proving admission is restored without another complete COPY. No full
source request is allowed in this final check. Its workers/lease/hold must clear.

An unknown sentinel directory/file is created before all cases. Its exact bytes
must survive every cleanup, and only that unknown directory may remain in the
cache at the end. The SQLite files and compact ready-event receipts remain in
the owned QA work directory. No deletion of unrelated paths or process discovery
is used as kill authority. The helper may terminate only a still-identity-matching
coordinator it directly spawned when unwinding a failed case; production
PDEATH/watchdog behavior remains responsible for descendants.

Output contains exact numeric worker/group identities, observed monotonic exit
times, per-case parent process CPU, `RUSAGE_CHILDREN` user/system deltas and
conservative whole-container cgroup CPU deltas. Timing observations are sampled,
not exact kernel death timestamps. It includes only safe synthetic counters,
sentinel/helper hashes and pass/failure classifications. It never reads process
arguments/environments or prints provider credentials. Review and freeze this
helper before the operator's single isolated execution. A failure is evidence
to inspect at the owning backend; it does not authorize a runtime edit, another
native attempt, cap increase, or original-media resumption.

The first native execution passed the deadline case (2.449 s wall, 0.728505 s
container CPU; both media children -SIGKILL, actual deadline error, all groups
gone). Its later parent-death/reconciliation failure had only a generic
ValueError and no phase. That result does not establish a backend defect; a
separate media-free init/cwd scan refuted the suspected PID 1 cwd restriction.
The passed deadline case is retained and must not be repeated unchanged.

For the next affected check use the same invocation with **`--cases parentdeath`**.
This runs only parent death/reconciliation followed by restored admission; it
does not run or re-claim the prior deadline proof. The default `--cases all`
remains available for a genuinely new full qualification, requiring its own
execution authority.

The helper now records a named phase before signals, timing/lease assertions,
each cleanup and restored admission. It atomically preserves a bounded
`lifecycle-trace.json` in the new owned QA work directory. Before and after
cleanup it records the synthetic row's state/phase/deadline/PIDs, active
reservation count, source hold, owned marker/file counts, numeric process
states, actual cwd absence scan and sentinel state. These observations do not
change backend reconciliation logic or clocks. On failure the preserved
SQLite/cache remain available; the failure receipt includes the failed phase,
named invariant where available, and a sanitized traceback containing only
basename/function/line, never exception text, arguments, frame locals or
credential-bearing SQL. Unexpected child diagnostics are preserved through the
parent's event reader with the same restricted fields.
