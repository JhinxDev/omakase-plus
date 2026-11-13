# Omakase Stack operator guide

This guide takes a clean Debian server or Proxmox installation to a private
anime discovery, acquisition, verification, and Plex setup. It is designed for
trusted home use and deliberately separates required capabilities from optional
providers.

## Choose the smallest useful stack

| Situation | Recommended path |
|---|---|
| A Docker server and Plex already exist | [Existing server](existing-server.md) |
| A trusted home Proxmox node, few users | [Compact Proxmox LXC](proxmox-reference.md) |
| Public/untrusted users or stronger isolation | Debian VM, then [existing server](existing-server.md) |
| Media already exists on disk | Import-only; no torrent client or VPN |
| Local BitTorrent is explicitly wanted | qBittorrent sharing Gluetun's network namespace |
| Real-Debrid already exists | Keep the existing provider; local storage is optional |

The safe first install is import-only, loopback/LAN-only, and either an existing
Plex server or the bundled official Plex profile. Nothing in the examples
opens a router port, creates an account, downloads media, or enables the
qBittorrent profile implicitly.

## Capability map

```text
AniList history + taste profile
              |
         Omakase Plus
        /      |       \
 import-only  Real-Debrid  qBittorrent + Gluetun
        \      |       /
     identity, episode, media, subtitle verification
                    |
          deterministic Plex library
                    |
             Plex / Infuse clients
```

Optional watch tracking can feed Plex activity back to AniList. Omakase's
recommendation “Already watched” action independently writes a real
`COMPLETED` AniList list entry and optional score, then records a retryable
receipt.

Continue with:

- [Storage and acquisition modes](storage-modes.md)
- [AniList and watch tracking](anime-tracking.md)
- [Security, backup, upgrades, and recovery](security-backup-operations.md)
- [release and recovery guide](../release-checklist.md)
- [Detailed Local Storage + Plex commands](../local-storage-plex-setup.md)

## Definition of ready

A green container is not enough. A ready installation has a rendered Compose
configuration, restricted secret file, healthy Omakase container, reachable
Plex target, writable managed library, read-only import root, valid tracker
connection when selected, and a passing `omakase plus doctor` receipt. A ready
title additionally has correct identity and season mapping, required episode
coverage, readable media, selectable release-muxed subtitles, Plex visibility,
and representative byte-range playback.
