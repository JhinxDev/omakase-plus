# Optional authenticated web player

The anime player is opt-in. Keep Plex/Infuse available for unsupported files,
browsers and subtitle formats. Enabling a flag is not playback qualification for
a particular device or installation. Movies, TV and combined episode files keep
their existing playback actions.

## Viewing

Home and episode actions open the exact selected episode. Playback starts only
after Play. The player supplies seek, pause, volume, speed from 0.25× to 2×,
audio selection and container fullscreen. A genuinely played partial episode can
appear in Continue watching without marking earlier episodes watched. Exact
episode completion requires 85% server-accepted played coverage. Seeking alone,
opening a page and paused pings do not complete an episode.

Progress saved means the latest intended position has been acknowledged. A
terminal save or access error stops playback until deliberate recovery. Leaving
and quickly returning preserves the final paused seek without treating that jump
as watched time.

Resume and track choices belong to this viewer, library, episode and exact media
version. A different encoding starts without the old offset. A later manual
correction/reopen fences old completion evidence; a new explicit viewing can
complete the episode again. Watching episode 5 does not imply episodes 1–4 were
watched. The legacy aggregate advances only through contiguous known completion
above existing explicit progress. Original events and tracker consent remain.

MP4/WebM use authenticated ranged delivery. Eligible MKV files use a full copy-only
static DASH preparation: at most 256 MiB, 40 minutes and 45 seconds. No video/audio
encoder or subtitle burn is a fallback. Preparation offers Cancel and the exact
Plex action as soon as resolution finishes. It can take tens of seconds; this is
prepared playback rather than immediate streaming. A resource/format failure
keeps the external action and does not retry or increase a limit automatically.

Installations that enable \`OMAKASE_PLAYBACK_STREAMING=true\` use authenticated
copy-only HLS for qualified MKV sources. A transient startup timeout gets one
bounded retry. Preparation, source-access and codec failures have separate
messages. Styled ASS extraction runs after video is ready. The subtitle menu
shows preparation or unavailability without interrupting video playback.

ASS/SSA uses the original script and admitted embedded fonts in a derived JASSUB
renderer. It requires the isolated player document, shared WASM memory,
OffscreenCanvas, Worker and native video-frame callbacks. Unsupported browsers,
bitmap subtitles or unknown stream mappings retain Plex. Styled subtitles appear
in the page/player-container fullscreen; native video fullscreen and Picture in
Picture do not contain the canvas. Turn styled subtitles Off for PiP.

## Operator prerequisites

Both `OMAKASE_PLUS_PRIVATE=true` and `OMAKASE_PLUS_WEB_PLAYER_ENABLED=true` are
required. Set `OMAKASE_PLUS_PLEX_MANAGED_ROOT` to the actual trusted managed root
inside the connected Plex server. This is an operator setting, never a browser
path. The viewer needs their own current Plex account/server/library access;
an owner's connector is not a member credential grant.

Direct delivery does not require the copy cache. MKV preparation requires Linux,
the installed FFmpeg/FFprobe, cgroupv2 memory accounting and a dedicated bounded
cache at `OMAKASE_PLAYBACK_CACHE_ROOT` (default `/run/omakase-playback`). Never
point that cache at originals, the application database or a general storage
directory. The initial bounded envelope uses one ASGI worker, one global copy
job, a 1 GiB dedicated tmpfs with 8192 inodes, a 3 GiB container memory/no-swap ceiling,
one CPU and bounded processes. For a UID 1000 container the mount options are:

```text
rw,noexec,nosuid,nodev,size=1g,nr_inodes=8192,uid=1000,gid=1000,mode=0700
```

Run the container with an init/reaper (`init: true` in Compose or `--init` for
Docker run). The coordinator's watchdog and parent-death limits require reaping
orphaned descendants, including zombies. Verify the installed container's actual
`HostConfig.Init`, single ASGI worker, limits and cache mount at release; a Compose
source file alone does not prove those controls are active.

Admission checks the real mount/quota and available cgroup memory. The COPY child
and source-packet observer each limit address space to 512 MiB, CPU time to
30 seconds, descriptors to 64 and each file to 768 MiB. Combined container CPU is
sampled against a 30-second budget, including reaped children and the parent;
this sampled threshold is distinct from each child's hard CPU limit. A separate
watchdog enforces the absolute deadline. Aggregate
storage is hard-bounded by the mount; per-job byte/file observations run once per
second and before publication. Unknown, partial or failed output is never served.
Packet-evidence files remain private internal resources. There is no work queue.

READY generations are immutable, retain at most two generations and expire after
an hour of idle reuse. Admission can evict the least recently used unheld READY
generation for the next episode; active, partial and unknown generations remain
protected. Valid playback holds protect active generations; copying
also protects source cleanup until worker absence is proved. Periodic maintenance
runs under the existing media-sync lease. Unknown files/directories are preserved.
Busy capacity returns the external route. GET/HEAD and metadata never start work.

Each byte, segment, subtitle and font checks the current login, library policy,
source generation and personal Plex entitlement. Long transfers revalidate.
Starts/preparations have immediate global/per-viewer admission; reads have
global/per-viewer/per-session limits and bounded rate records. This in-process
admission contract requires a single ASGI worker; do not multiply workers without
a separately qualified shared admission mechanism.

Terminal close retains one writer per session. An identical in-flight final
snapshot joins that save through separate bounded replay admission; conflicting
snapshots are rejected. Writer and replay each admit 16 globally, two per viewer,
one per session and 30 per viewer/minute. Replay waits at most one second, without
cancelling the writer or creating a detached database task.

## Upgrade and recovery

Follow [current-data recovery](current-data-recovery.md) before activation. Keep
the current database/key and account/history/outbox/consent intact. The raw prior
binary rejects new `web_player` provenance. Feature disablement in compatible
code and the [retained-image compatibility overlay](../deploy/player-recovery/README.md)
are different recovery paths. Qualify the exact image, overlay, read-only route
and writer/network fences before use; do not restore a stale database or relabel
viewing records.

## Dependency source and verification

The supplied Shaka 5.3.3 UI is self-hosted. The explicitly modified JASSUB 2.5.23
graph has mixed third-party licenses; the application retains its MIT license.
See [renderer notices/source index](../THIRD-PARTY/jassub-2.5.23/README.md).
The served `ass/LICENSES.txt`, `ass/SOURCE.json` and complete
`ass/corresponding-source.tar.gz` provide notices, source, patches and rebuild/
relink instructions alongside the binaries. Exact native binary/source
correspondence is not claimed to be bit-reproduced.

Maintained component/HTTP checks are `tests/plus/test_web_playback_*.py`; browser
checks include `tests/browser/web_player_browser.cjs`, `web_player_native.cjs`,
`web_player_exit_browser.cjs`, `web_player_failure_browser.cjs`,
`web_player_controls_browser.cjs` and `web_ass_client.test.cjs`.
Native QA requires an explicitly supplied isolated
review URL and authorized existing media. It mutes before playback. Keep decoded
frames, real track/ASS/font behavior, cold preparation latency, keyboard/touch,
physical devices, subjective listening and deployment acceptance distinct.
