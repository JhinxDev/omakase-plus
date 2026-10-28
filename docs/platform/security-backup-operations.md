# Security, backup, upgrades, and recovery

## Exposure and secrets

- Bind Omakase and bundled Plex to loopback until a trusted LAN address,
  firewall, and HTTPS route are intentionally configured.
- Keep Plus private and authenticated. Public recommendation deployments must
  not contain Plus code or routes.
- Store `.env.local.secrets` as mode `0600`; do not pass credentials on command
  lines or include them in support bundles.
- Configure the public URL, allowed hosts, HTTPS, and Secure session cookie as
  one change.
- Do not mount the Docker socket into Omakase.
- Treat membership in the host `docker` group as root-equivalent.

## What to back up

| Data | Method | Restore importance |
|---|---|---|
| Omakase SQLite/data | SQLite online backup or stopped-volume snapshot | Required |
| `.env.local` and secret file | encrypted secrets backup | Required |
| Managed library | filesystem/NAS backup or reproducible source plan | User choice |
| Import root | source-owner backup | User choice |
| Plex config/database | stopped or application-consistent snapshot | Required for metadata/watch state |
| Plex transcode | none | Disposable |
| Gluetun/qBittorrent config | encrypted config backup | Required only when enabled |
| Plan/apply/upgrade receipts | normal backup | Required for audit/rollback |

Proxmox bind-mount contents are not included in `vzdump`; back them up
separately. Test a restore to a disposable path before relying on it.

## Upgrade

1. Run Doctor and capture current health/revision.
2. Verify the exact new private commit or archive hash.
3. Render every selected Compose profile.
4. Back up and integrity-check SQLite and Plex config.
5. Build/pull without replacing the running stack.
6. Deploy the exact revision and wait for health.
7. Prove OCI revision, migrations, login, title readiness, subtitle choices,
   scoped Plex scan, and representative HTTP 206 playback.
8. Preserve the rollback receipt.

The included PowerShell upgrade/rollback scripts implement this flow on
PowerShell-capable Docker hosts. Do not update by editing tags and recreating
containers manually.

## Recovery order

1. Stop mutation: disable the Omakase sync timer/client profile before changing
   files or databases.
2. Capture the failing logs/config/revision without secrets.
3. Restore Omakase data and exact code/image together.
4. Restore Plex config before rebuilding libraries.
5. Verify mounts and media reads before starting Plex scans.
6. Start import-only first; re-enable qBittorrent/VPN only after tunnel proof.
7. Run Doctor and post-install validation, then replay only idempotent pending
   tracker/media actions.

Support bundles intentionally omit SQLite, magnets, credentials, tracker URLs,
full filenames, and watch history.
