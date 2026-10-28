# Storage and acquisition modes

## Import-only

Best first install and the only mode needed when media is copied to the server
through another trusted workflow. Omakase receives a read-only import root,
hashes and probes exact files, then materializes verified media into its managed
library. No BitTorrent traffic occurs and no VPN is needed.

Use imports to establish the Plex, subtitle, ownership, backup, and recovery
contract before adding another provider.

## Real-Debrid

Real-Debrid remains a provider adapter, not the core data model. Existing
assets are adopted by stable hash and verified inventory. Omakase does not
claim that a debrid provider changes local law or provider terms. The current
private reference keeps Real-Debrid and Zurg on its established hosts; a new
friend install can omit both.

## qBittorrent through Gluetun

This is opt-in. qBittorrent shares Gluetun's network namespace and has no
independent host ports. Omakase is limited to one category and save root.
Ordinary delete is disabled; even the exceptional provider operation never
deletes local files.

Gluetun is provider-neutral: configure a supported service or a custom
WireGuard/OpenVPN profile directly in Gluetun. Omakase does not contain
commercial VPN-provider integrations. Its Doctor requires tunnel health,
namespace posture, and kill-switch evidence before declaring this lane ready.

A VPN is recommended for local public-swarm BitTorrent because peers can see
the peer IP. It is not needed for import-only. A VPN is not a guarantee of
anonymity or permission, and Omakase never silently falls back to the normal
host network.

## Subtitle contract

Readiness prefers selectable subtitle streams embedded in the exact media mux
and keeps every language so Plex and Infuse can offer choices. A release-muxed
track shares the same video cut and timing.

External subtitles are fallback evidence only. They must be associated with the
exact release fingerprint and explicitly verified. A same-title file from
another encode may have a different intro, recap, frame rate, or commercial
break and can drift against dialogue. Omakase neither guesses a global delay
nor marks an unverified external track ready.

## Filesystem rules

- Import root: read-only inside Omakase.
- Library root: the only Omakase-managed media write path.
- App data and SQLite: local filesystem with working file locks.
- Plex config: local filesystem; back up consistently while Plex is stopped or
  through a supported snapshot.
- Transcode: separate, disposable, capacity-bounded path.
- Never expose `/`, `/etc`, `/var`, a Docker socket, or a broad NAS root.
