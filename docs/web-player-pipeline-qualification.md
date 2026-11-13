# Synthetic production pipeline qualification

`scripts/qualify_web_player_pipeline.py` prepares one bounded Linux gate. Its
offline unit tests qualify the harness contracts; they do not prove native
pipeline performance or authorize original-media attempts.

The gate uses the actual player router, session authentication and CSRF checks,
`revalidate_source`, `revalidate_remote_source`, fresh atomic Start transaction,
`prepare_remux`, launcher limits, quota admission, and resource routes. It first
cancels a preparation after at most 64 KiB of the full fixture body, checks lease
release and absence of every owned worker/watchdog process group (including
zombies), then performs one complete preparation. It checks Start 202, prepare
201, exact input SHA-256, dual audio packet bounds, both ASS files, all eleven
font byte hashes, authenticated manifest HEAD/Range, ASS/font HEAD and close 204.
Known process-group absence is checked again after successful preparation with
a bounded 300 ms reap allowance. An init process is required.

The explicit qualification seam is a trusted synthetic resolver and a temporary
`_publication` adapter. That adapter **first calls the real whole-publication
validation and exact target selection**, then maps its validated tuple to the
synthetic fixture byte size and a digest containing the fixture SHA. Scope,
private credentials, credential generations and remote pre/post mutation guards
remain real. Mocked Plex metadata matches the synthetic source's exact Part and
stream indexes. This proves neither original-media byte identity nor live Plex
entitlement. Every upstream HTTP request uses one deterministic MockTransport;
all other request paths fail closed. No provider requests or original-media
reads occur. Source JSON, credentials, URLs and private target IDs are never
printed.

The original private metadata database is opened read-only and copied into an
in-memory SQLite connection. All authentication, binding, session and job writes
affect only that in-memory copy. Run with existing protected key material and
configuration references mounted read-only. Do not inspect or copy environment
values into receipts. Enable the private web-player feature only in this isolated
qualification process. No scheduler or acquisition worker is started.

The memory clone explicitly enables `PRAGMA foreign_keys=ON` after backup.
Production uses a fresh database connection for each HTTP request; the helper
must share its memory clone to retain writes, so a conservative plain ASGI
request-entry middleware clears every resolved source's publication memo once
per HTTP request. It does not clear within that request, preserving the actual
authorization callback's reuse. Request-boundary and clear counters are reported.
This prevents optimistic cross-request cache reuse from understating validation
cost. Unit regressions verify cross-request clearing, within-request retention,
and foreign-key enforcement on the clone.

Operator invocation in the existing isolated image:

```sh
python -I /qa/qualify_web_player_pipeline.py \
  --db /data/omakase-plus.db \
  --target /qa/source-auth-profile-target.json \
  --fixture /qa/fixture.json --media /qa/synthetic.mkv \
  --cache /run/omakase-playback
```

Use network mode `none`, `--init`, one CPU, 3 GiB container memory, an empty
dedicated 1 GiB tmpfs cache, existing production launcher binaries, a read-only
private database/key mount, and a read-only synthetic fixture. The code import
root is `/probe/src`. The helper rejects PID 1 and non-Linux CLI execution.
The coordinator retains its production 45 s deadline, sampled combined 30 CPU s
budget, and 256 MiB source limit; child limits remain unchanged. The harness
outer guard is 95 s and performs no retry. Root owns the one native execution.

The private target file has exactly the existing six keys: `actor_user_id`,
`library_id`, `anilist_id`, `season_number`, `episode_number`, `duration_ms`.
Its original duration is only target metadata; the synthetic descriptor supplies
the actual synthetic timeline. The fixture file has this exact schema:

```json
{
  "schema": "omakase-player-pipeline-v1",
  "bytes": 249000000,
  "sha256": "<64 lowercase hexadecimal characters>",
  "duration_ms": 1800233,
  "video": {"index": 0, "codec": "hevc", "language": "und", "width": 1920, "height": 1080},
  "audio": [
    {"index": 1, "codec": "opus", "language": "jpn", "default": true},
    {"index": 2, "codec": "opus", "language": "eng"}
  ],
  "subtitles": [
    {"index": 3, "codec": "ass", "language": "eng"},
    {"index": 4, "codec": "ass", "language": "jpn"}
  ],
  "fonts": [
    {"index": 5, "bytes": 100000, "sha256": "<font SHA-256>"}
  ],
  "min_audio_packets": 176000
}
```

The example `fonts` list is abbreviated: exactly eleven unique indexed fonts
are required, each <=8 MiB and together <=32 MiB. Stream indexes must be unique.
Optional `default`/`forced` track fields are booleans. The fixture must contain
HEVC 1920x1080, two Opus tracks, two original ASS streams, and eleven open fonts;
use the operator's Main10 fixture. Size must be >=215,000,000 bytes and <=256 MiB,
duration between 1700 and 2400 s, with >=176,000 actual audio packets.

Output is a compact JSON receipt with phase-scoped parent process CPU,
`RUSAGE_CHILDREN` user/system CPU before/after (therefore retained after reap),
sampled whole-container cgroup CPU deltas, bounded once-per-second numeric PID
state/cache/memory samples, inclusive validation counters, input hash and
aggregate resource/packet counts. Inclusive nested timings are not additive.
The completed pipeline must use <=30 s wall and <=20 s whole-container CPU to
pass the headroom gate. These are measured qualification gates, not new hard
runtime caps. Cancellation and setup are separately reported. No resource
filename inventory or credential-bearing exception messages are emitted.

Timeout and parent-death qualification may use separate small synthetic fixtures
under a separately reviewed finite helper; this helper does not silently add
those scenarios or any original-media attempt.
