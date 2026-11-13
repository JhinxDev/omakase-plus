# Local Storage + Plex setup

For app-only exploration with no media mounts, start with
[Getting started](getting-started.md) and `compose.standalone.yaml`.
This guide adds optional storage and separate media services to Omakase Plus
without requiring Real-Debrid. The safe default imports media already present on disk. A bundled
Plex server and qBittorrent behind a provider-neutral VPN gateway are optional.

## Pick a topology

| Need | Start command | Notes |
|---|---|---|
| Explore first | `docker compose --env-file .env.local -f compose.standalone.yaml up -d` | App data only; prepare configuration first as described in Getting started |
| Existing Plex, import only | `docker compose --env-file .env.local -f compose.local.yaml up -d` | After preparing import/library paths |
| Include Plex | add `--profile plex` | Plex is loopback-only until you deliberately choose a LAN bind |
| Search more release sources | add `--profile catalogs` | Optional Prowlarr UI is loopback-only; Omakase still verifies every result |
| Add qBittorrent | add `--profile qbittorrent-vpn` | qBittorrent has no independent network namespace or host ports |
| Existing tunneled qBittorrent | no qBittorrent profile | Doctor verifies the exact qBittorrent interface binding |

Import-only mode does not use BitTorrent and does not require a VPN. With
BitTorrent, swarm peers can observe the peer IP. Omakase does not choose a
commercial provider or change legal obligations. Its generic Gluetun profile
supports Gluetun's provider settings as well as custom WireGuard/OpenVPN;
provider credentials stay in Gluetun's environment, not Omakase code.

## 1. Prepare the host

Use Docker Engine with the Compose plugin. Create three explicit directories:

```bash
install -d -m 0700 /opt/omakase
install -d -m 0750 /srv/omakase/import
install -d -m 0750 /srv/omakase/library
cd /opt/omakase
```

Clone the private release while Plus remains private, or copy a release archive
whose SHA-256 you verified. Do not mix the public recommendation-only branch
with a private Plus deployment.

Copy the examples:

```bash
cp .env.local.example .env.local
cp .env.local.secrets.example .env.local.secrets
chmod 0600 .env.local.secrets
```

Set absolute host paths, UID/GID, timezone, and deployment mode in
`.env.local`. Generate the master key out of band:

```bash
python -c 'import secrets; print(secrets.token_hex(32))'
```

Put its output in `OMAKASE_PLUS_MASTER_KEY` inside
`.env.local.secrets`. Never pass a token or password as a CLI argument.

Render before starting:

```bash
docker compose --env-file .env.local -f compose.local.yaml config --quiet
docker compose --env-file .env.local -f compose.local.yaml build --pull omakase-plus
docker compose --env-file .env.local -f compose.local.yaml up -d
docker inspect omakase-local-app --format '{{.State.Health.Status}}'
```

The app binds to `127.0.0.1:8766` by default. Change the bind address to a
specific trusted LAN address only after the host firewall and login are ready.
The example does not publish Plex or Omakase to the Internet.

## 2. Connect Plex

### Existing Plex

Mount `/srv/omakase/library` into the Plex host/container and note the path Plex
sees. The Omakase container sees `/media/library`; Plex might see `/data`,
`/media/anime`, or another stable path. Plex setup requests must use the Plex
path, not the host path.

Set `OMAKASE_PLEX_URL` in `.env.local` and `OMAKASE_PLEX_TOKEN` in the secret
file. Plan the exact library topology:

```json
{
  "schema_version": "plex-setup-request-v1",
  "libraries": [
    {"title": "Anime", "section_type": "show", "path": "/data/anime"},
    {"title": "Anime Movies", "section_type": "movie", "path": "/data/anime-movies"}
  ]
}
```

```bash
docker compose --env-file .env.local -f compose.local.yaml exec -T omakase-plus \
  omakase plus plan-plex-setup \
  --plex-url "$OMAKASE_PLEX_URL" \
  --request /home/omakase/data/plex-request.json \
  --plan /home/omakase/data/plex-plan.json
```

Review the plan. It can create a library or update one library path; it cannot
delete a section. Apply using the printed hash:

```bash
docker compose --env-file .env.local -f compose.local.yaml exec -T omakase-plus \
  omakase plus apply-plex-setup \
  --plex-url "$OMAKASE_PLEX_URL" \
  --plan /home/omakase/data/plex-plan.json \
  --expected-plan-hash <64-character-hash>
```

### Bundled Plex

Plex remains a separate optional service. This profile starts the official Plex
container; it does not install Docker, mount a NAS, claim an account or create a
ready media library for you. First prepare the directories and `.env.local`
configuration above. For this profile, set `OMAKASE_PLEX_URL=http://plex:32400`
so the app can reach Plex over the Compose network (the browser uses the
published address instead). Review and start explicitly:

```bash
docker compose --env-file .env.local -f compose.local.yaml --profile plex config --quiet
docker compose --env-file .env.local -f compose.local.yaml --profile plex up -d
```

Open `http://127.0.0.1:32400/web` on the Docker host and complete Plex sign-in
and server claiming yourself. For a remote host, use your reviewed private
access route. If you choose the official image's claim-token flow instead,
put `PLEX_CLAIM` only in `.env.local.secrets` for the first claim and remove it
once Plex is owned. Do not paste claim tokens into commands or the Omakase guide.
Create libraries pointing at Plex's `/data` paths, add your Plex URL and token
in Omakase **Settings → Connections**, and explicitly **Test connection**.
Keep movie and TV libraries distinct; the bundled server does not add automatic
TV acquisition to Omakase.

The official Plex image stores its database under the configured `/config`; keep that path on a local filesystem
that supports file locking. The example publishes only TCP 32400 on loopback.
Set a specific LAN bind/advertised URL if clients on the LAN need direct access.
Remote Access remains a manual Plex decision.

## 3. Import media

Place or mount source files below the configured import root. Omakase receives
relative locators—never arbitrary absolute paths. Each request links a file to
an existing monitored title:

```json
{
  "schema_version": "local-import-request-v1",
  "requests": [
    {
      "monitor_id": 42,
      "locator": "Hell's Paradise/Season 02/Hell's Paradise S02E01.mkv",
      "quality": "1080p",
      "external_subtitles": []
    }
  ]
}
```

```bash
omakase plus plan-local-import \
  --user-id 1 \
  --root /media/import \
  --request /home/omakase/data/import-request.json \
  --plan /home/omakase/data/import-plan.json

omakase plus apply-local-import \
  --plan /home/omakase/data/import-plan.json \
  --root /media/import \
  --backup /home/omakase/data/backups/pre-import.db \
  --expected-plan-hash <64-character-hash>
```

Planning hashes every file but changes nothing. Apply re-hashes the inventory,
creates a validated SQLite backup, runs bounded `ffprobe`, and either imports
the exact bytes or keeps the file out of the library with a plain-language
reason. Paths cannot escape the configured root;
symlink escapes, content changes, identity ambiguity, and cross-title hash
collisions fail closed.

### Subtitle timing and selection

Omakase keeps every embedded subtitle track so Plex and Infuse can expose
language choices. Readiness requires a switchable English track from the same
mux. This avoids the common problem where an external subtitle belongs to a
different encode, recap length, frame rate, or commercial cut and slowly drifts
against dialogue.

External `.ass`, `.srt`, `.ssa`, or `.vtt` files are fallback-only. They must
carry the exact media release fingerprint and an explicit `verified` sync state
in the import request. Omakase does not guess a delay or silently bless a
same-title subtitle. Unverified/mismatched tracks remain visible as evidence but
cannot make an episode ready.

## 4. Optional expanded release search

Omakase always has its built-in anime release source. The optional `catalogs`
profile adds Prowlarr so an owner can enable additional public or private
sources without adding provider-specific code to Omakase.

Set these non-secret choices in `.env.local`:

```dotenv
OMAKASE_ALLOW_PRIVATE_CATALOG_URLS=true
PROWLARR_BIND_ADDRESS=127.0.0.1
PROWLARR_PORT=9696
PROWLARR_CONFIG_PATH=/opt/omakase/data/prowlarr
```

Start the profile:

```bash
docker compose --env-file .env.local -f compose.local.yaml \
  --profile catalogs up -d
```

Open `http://127.0.0.1:9696` from the host, add only sources you are authorized
to use, and copy the API key from **Settings → General**. In Omakase
**Settings → Search more places**, save:

- Address: `http://prowlarr:9696`
- API key: the Prowlarr API key
- Source IDs: leave empty to search every enabled source
- Categories: leave the defaults unless your sources use different anime/TV
  categories

The key is encrypted in the Omakase database and is never returned to the
browser. Omakase sends it only in the `X-Api-Key` header. It records a
secret-free success/failure receipt for each catalog on Health, merges and
deduplicates results, applies the owner-validated policy shown under
**Settings → Download criteria**, and stops searching lower-ranked files after
a complete verified release is found. Restore tested defaults there if a custom
resolution, seeder, size, subtitle, trusted-group, or batch preference makes
troubleshooting unclear.
Prowlarr expands the configured search; it does not bypass Omakase title,
season, subtitle, or playback checks.

## 5. Optional qBittorrent through Gluetun

Read qBittorrent's legal notice and set `QBT_LEGAL_NOTICE=confirm` only if you
accept it. Configure Gluetun using its official provider or custom tunnel
instructions. Then:

```bash
docker compose --env-file .env.local -f compose.local.yaml \
  --profile qbittorrent-vpn up -d
docker logs omakase-local-qbittorrent
```

qBittorrent 4.6.1+ prints a temporary WebUI password on first start. Sign in,
replace it, and put the chosen API username/password only in
`.env.local.secrets`. Omakase creates/uses one exact `omakase` category rooted
at `/downloads/omakase`. It refuses matching torrents outside that category.
Deletion is off by default; even an explicitly enabled delete removes the
torrent job with `deleteFiles=false`.

The Compose profile gives qBittorrent `network_mode: service:gluetun`, publishes
the WebUI through Gluetun, and gives qBittorrent no direct host ports. Doctor
requires Gluetun health, `/dev/net/tun`, `NET_ADMIN`, the built-in firewall, the
shared namespace, and no qBittorrent port binding. Run the VPN portion of Doctor
from the Docker host because the application container intentionally has no
Docker socket:

```bash
omakase plus doctor \
  --data-dir /opt/omakase/data/omakase \
  --import-root /srv/omakase/import \
  --library-root /srv/omakase/library \
  --acquisition-mode qbittorrent \
  --vpn-mode gluetun \
  --plex-url http://127.0.0.1:32400 \
  --qbittorrent-url http://127.0.0.1:8080
```

For an existing qBittorrent behind another VPN, use `--vpn-mode external` and
`--external-vpn-interface <exact-name>`. Doctor reads qBittorrent preferences
and refuses readiness unless both the interface and interface address are
explicit. Omakase does not manage host routes.

## 6. Diagnose, support, upgrade, recover

`omakase plus doctor` performs read-only directory, capacity, filesystem,
ffprobe, SQLite, Plex, provider, and selected VPN checks. A failure includes a
stable reason code and remediation. It never creates a category, library, file,
or database.

Create a shareable diagnostic:

```bash
omakase plus support-bundle --output ./omakase-support.zip <same Doctor options>
```

The default ZIP excludes SQLite, secrets, magnets, tracker URLs, full
filenames, and watch history. Paths/names found in included logs are replaced by
short hashes.

The legacy PowerShell automatic upgrade and rollback helpers are disabled before
any operation. They cannot qualify combined-schema consumers, and their former
database restore could overwrite newer account/history/shared state. Old receipts
are historical metadata, not permission to replay those operations.

Use [current-data recovery](current-data-recovery.md) and the
[release checklist](release-checklist.md) for a manually qualified update of your
selected Compose setup. Preserve current data and WAL/SHM, the protected master
key, exact current/prior images and recorded writer states. Save prepared recovery
metadata before activation; record failure and actual verification separately.
Do not resume unrestricted old writers against migrated shared data. Deliberate
disaster restoration requires its own data-loss decision.

## Primary references

- [Official Plex Docker image](https://github.com/plexinc/pms-docker)
- [Official qBittorrent-nox image](https://github.com/qbittorrent/docker-qbittorrent-nox)
- [Official Gluetun project](https://github.com/passteque/gluetun)
- [Connect a container to Gluetun](https://github.com/qdm12/gluetun-wiki/blob/main/setup/connect-a-container-to-gluetun.md)
- [Docker Compose service reference](https://docs.docker.com/reference/compose-file/services/)
