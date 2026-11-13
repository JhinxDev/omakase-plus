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

### Cloud files

On a private installation, open **Settings → Connections → Real-Debrid →
Browse cloud files**. The Cloud library lists your account's torrents and
download history. Open a torrent to browse its files. Browse and search refresh
the inventory after a short cache interval. A pending torrent can appear before
its files are ready to play.

Choose **Play** to ask Real-Debrid for an Apple HLS stream of the selected file.
Omakase relays that stream through the authenticated app so the provider token
and generated links stay on the server. Compatible releases of the Apple TV
and iPhone apps expose the same library under **Cloud**. Availability, audio and
subtitle choices depend on the stream Real-Debrid supplies.

Cloud files belong to the account that saved the connection. Household
libraries do not inherit it. The Cloud list uses filenames and torrent folders.

### Personal library playback

Choose **Real-Debrid only** under **Settings → Connections → Playback source**
to open your personal library through Real-Debrid. This setting applies to the
Apple TV and iPhone title player and to web watch links. Choose **Plex only** for
a Plex-backed library. **Automatic** allows the native player to try Real-Debrid
before Plex. The web Cloud library is always available with a Real-Debrid connection.

Omakase supplies title identities, artwork, episode metadata and watch history.
Real-Debrid supplies file availability and the compatible stream. Anime playback
uses verified episode mappings. Movies and TV episodes use a matching title,
release year for movies, and exact episode numbers for TV. A Play request
rechecks the current selected files. Browse results refresh after a short cache
interval. Files without a confirmed catalog match are available to browse in
Cloud under their original filenames.

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
