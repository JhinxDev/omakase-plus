# Release and recovery checklist

Use an exact reviewed revision and keep deployment-specific access and recovery
records outside source control. A public source release is not a hosted service.

1. Review intentional source changes, public links, documentation and the locked
   dependency graph. Exclude secrets, databases, private endpoints and personal data.
2. Run relevant tests, Ruff checks and package/container builds. Use isolated fake
   providers and safe public/synthetic screenshots.
3. Verify a clean app-only install with compose.standalone.yaml and explicit
   storage setups with compose.local.yaml. Confirm migrations and database health.
4. Record the currently running revision/image (`python ops/release/check_drift.py`,
   which is read-only) and a tested database/config backup
   before changing a live installation. Preserve the master key through a separate
   protected backup. Keep current and immediate-prior recovery images until acceptance.
   Qualify enabled consumers against an isolated combined current-schema copy and
   save durable prepared recovery metadata before activation.
5. Verify exact source identity, authentication, settings persistence and affected
   desktop/phone/keyboard journeys on the target. Catalog or inventory checks do
   not prove full playback, a physical device or human creative acceptance.
6. If target verification fails, retain current migrated data and fence relevant
   writers. Use a qualified forward fix or restricted read-only recovery.
   Unrestricted prior-image writes and a database rewind are unsafe. Deliberate
   disaster restoration is a separate operator/data-loss decision. Never delete
   shared media for release cleanup.

Legacy automatic upgrade/rollback scripts are disabled before any operation.
Follow [current-data recovery](current-data-recovery.md) for manual qualification,
backup, activation/failure records and recovery boundaries. HTTP health alone
does not prove migration, persistence or writer compatibility.

See [Getting started](getting-started.md), [Updates](updates.md), and
[Security](../SECURITY.md). Optional Plex curator deployment is documented in
[its guide](../deploy/plex-curator/README.md).

## Live revision and the release toolchain

A running container states its own revision, so read that first rather than
inferring it from an image tag. Tags are names; the revision label is the fact:

```bash
python ops/release/check_drift.py --json
```

That command is read-only. It reports the live image id, the
`org.opencontainers.image.revision` label, the effective compose chain and every
installed file that differs from the commit. Exit 0 means the live container
matches the commit, 1 means it has drifted, 2 means the check could not be
completed. `OMAKASE_REVISION` inside the container carries the same value.

A difference is reported as `content` when the source changed and as
`line-endings` when the installed copy differs from the commit only by carriage
returns. Both are drift and both exit 1; the name only says whether the source
changed. Git stores this repository's text with LF, so a CRLF file in
`site-packages` came from the install step, not from the commit. The measured
example, and what "18 line-endings, 0 content" meant for the image that has been
serving production, is in [the tooling README](../ops/release/README.md#the-deployed-review44-image-measured-again).

The revision label is only comparable between images this toolchain built. The
older release path stamped images with a commit sha, so a legacy image reports
`REVISION DRIFT` even when every shipped file matches the commit.

The release toolchain ships in this repository under `ops/release`. The full
tool-by-tool map is in [its README](../ops/release/README.md); the short version:

| Step | Command, from the repository root |
| --- | --- |
| Plan a build without touching Docker | `python ops/release/build_image.py --dry-run --manifest release/manifest.json` |
| Build an image from a commit | `python ops/release/build_image.py --commit <sha> --manifest release/manifest.json` |
| Build on the Docker host and read it back | the same command plus `--remote omakase-ct --readback` |
| Compare the live container against a commit | `python ops/release/check_drift.py --commit <sha> --all` |
| Cut over, dry run first | `python ops/release/promote.py --manifest release/manifest.json` |

The build context is the archive of one git commit, never the working tree, so an
image can only contain committed content and uncommitted work cannot leak into a
release. The revision is derived from the `src/omakase/plus` tree alone: two
commits that ship the same application tree share one revision, and any change to
a shipped file produces a new one. A real build proves that the image equals the
commit tree by hashing every installed file inside the image.

`promote.py` is a dry run unless `--execute` is passed, and it refuses the swap
unless every guard passes, including a reviewed compose chain and a verified,
hashed database snapshot taken before the swap. The toolchain is covered by
[its contract suite](../tests/infra/test_release_tooling.py), which the
[release-tooling workflow](../.github/workflows/deploy.yml) runs before it will
even plan a build.

## Native direct playback ingress

A native original-file playback URL contains a short-lived credential in its
path. Before enabling native direct playback, verify that every hop serving it
keeps the request target and Referer out of logs and telemetry. Apply the
[host-scoped Nginx Proxy Manager location](../ops/nginx/direct-stream.conf) inside
the Omakase proxy host's server block. Verify the effective configuration with
`nginx -t` and `nginx -T`, including inherited access controls and proxy settings.
Keep the location out of global shared-server includes.

Send requests with distinct synthetic markers in the direct path, query and
Referer through the actual ingress. Check normal and rejected requests,
including a successful synthetic range request, a 404, and a 416. Verify that
neither the app nor the ingress records the markers in access or error logs,
and that ordinary routes still produce their usual access records. Verify
that upstream buffering is off and the request still reaches Omakase with
the configured host and scheme. Cover any edge layers or logging that run
before Nginx location selection, including request parsing failures. Keep
native direct playback private and disabled until this check passes.
