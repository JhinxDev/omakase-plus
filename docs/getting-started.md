# Getting started with Omakase Plus

Plus is an authenticated self-hosted application in an early source-release
candidate. See the repository README for its intended public source destination
and publication status; the recommendation-only package is a different edition.

## Choose how much to set up

Omakase is the discovery and orchestration app. Plex is a separate media server
and player ecosystem. Real-Debrid, Riven, Seerr and Prowlarr are separate optional
services; Omakase does not install them from the browser. With no saved integrations, you can create an account, finish Setup and search
anime through the public AniList catalog (internet required). Movie and TV
catalog search requires configured Seerr. Personal planning and existing records
do not imply live catalog or playback access. Anime AI recommendations require your own AI connection. There is no
native Omakase media player or automatic TV acquisition in app-only mode.

| Start here | Deployment file | What you need |
|---|---|---|
| Explore with Omakase only | `compose.standalone.yaml` | Docker Compose, private app configuration and writable app data |
| Connect an existing stack | Standalone for connections only; `compose.local.yaml` for local imports/publication | Your existing services; explicit media mounts only for import/publication |
| Set up optional Plex | `compose.local.yaml --profile plex` | Prepared media directories and manual Plex sign-in/claim |

These are deployment choices. Local/NAS versus cloud media is a separate
preference that can be decided later. Saving either choice in Setup records
account guidance; it never installs software or changes the running deployment.

### App-only first install

Use a reviewed source checkout on a host with Docker Engine and Compose already
installed. From the repository root, copy the configuration examples:

```bash
cp .env.local.example .env.local
cp .env.local.secrets.example .env.local.secrets
chmod 0600 .env.local.secrets
```

Choose `OMAKASE_DATA_PATH` in `.env.local` and create that directory, writable
by the configured `OMAKASE_PUID`/`OMAKASE_PGID` (default 1000:1000). It holds
the private database and account state. Generate an encryption key with
`python -c 'import secrets; print(secrets.token_hex(32))'` and save it as
`OMAKASE_PLUS_MASTER_KEY` in `.env.local.secrets`. Keep that key and the database
backed up together; never commit the secret file. Media path values in the example
are unused by the standalone file.

```bash
docker compose --env-file .env.local -f compose.standalone.yaml config --quiet
docker compose --env-file .env.local -f compose.standalone.yaml build --pull omakase-plus
docker compose --env-file .env.local -f compose.standalone.yaml up -d
docker inspect omakase-local-app --format '{{.State.Health.Status}}'
```

Open [Omakase on this host](http://127.0.0.1:8766/plus). The default bind is
loopback: a remote browser needs your deliberately configured private access
route. Do not publish the port to the Internet. The standalone file runs one
app container with one app-data mount and requires no Plex or media directories.
Use it on its own, not merged with `compose.local.yaml`.

### Add a media stack later

Follow [Local Storage + Plex setup](local-storage-plex-setup.md) for host paths,
UID/GID, optional Plex and VPN profiles, import planning and recovery. Stop the
standalone service before switching files, preserve the same project name,
`OMAKASE_DATA_PATH` and secret file, then validate/start the local file. Never
run both files as competing app deployments against one database. No storage
migration is needed when those app-data settings stay the same. The local
upgrade helper targets `compose.local.yaml`; do not use it on an app-only install.
For a clean Docker or Proxmox host, see the [operator guide](platform/README.md).

The three paths have separate purposes:

| Directory | Purpose | Access |
|---|---|---|
| App data | SQLite database and application state | Private, writable by the app |
| Import root | Your existing source files, including an administrator-mounted NAS | Read-only in the app container |
| Library root | Verified media published for Plex | Managed by the configured pipeline |

A NAS share must already be mounted on the server. A path in your browser's
machine is not a path inside the Omakase container. Setup does not mount SMB/NFS
shares or change server permissions. Never use an application browser form as a
substitute for a reviewed storage mount.

## Create an account

After the container is healthy, open `/plus` at your configured app address and
choose **Create account**. Account creation is available only when the server's
private/invite configuration permits it. Use your own email and a unique
password. Google sign-in is available only when the administrator configured it.

Keep the app private. Setting private mode enables private features and account
creation; it is not a network firewall or a replacement for protected deployment.

## Follow the four steps

1. **Choose:** select Explore with Omakase only, Connect an existing stack, or
   Set up optional Plex. Optionally choose Cloud downloads, Local or NAS library,
   or Explore first as your separate media preference.
   This stores only your account's preferred guidance. It does not switch the
   download adapter, buy anything, acquire media or alter server mounts.
2. **Connect:** open the appropriate Settings connection, save the credentials,
   and explicitly choose **Test connection**. Blank credential fields keep saved
   values. Return to the guide through Setup when you are ready.
3. **Review:** check saved connection results and their timestamps. The guide
   makes no automatic provider calls. Use System health for further diagnostics
   and ask the administrator to run `omakase plus doctor` for server readiness.
4. **Enjoy:** finish and go Home. Missing optional services do not trap you in
   setup. You can reopen the guide without clearing history or saved connections.

## Connect only what you use

- **Plex:** playback/library access. Movie watch history is an explicit import.
- **Real-Debrid:** cloud downloading and caching for supported sources across
  anime and movies. It is neither a catalog nor guaranteed permanent storage.
- **Riven and Seerr:** movie automation and requests, together with tested Plex.
  Movie setup remains separate from anime readiness.
- **Seerr:** supplies movie and TV catalog metadata from TMDB through its configured backend.
  **Prowlarr:** configured release-source search.
- **AniList:** anime tracking and progress sync. Review tracking preferences
  before enabling writes to your connected list.
- **AI credentials:** the model connection for anime recommendations. Movie
  suggestions use local rating/genre ranking rather than generative movie AI.

Credentials belong in your own account's Settings or the documented deployment
secret file, never in a screenshot, issue, source example or committed `.env`.
The database and encryption key both matter for recovery; store backups safely.

## Add media deliberately

A watchlist entry is a plan, not a claim that files exist. Review the title and
season before intake. Local imports use the CLI's plan/apply workflow; cloud or
optional torrent acquisition uses the configured server adapter. Playback is
ready only after the relevant identity, inventory, subtitle and Plex checks.

For problems, start with saved connection results, System health and the
[read-only Doctor and support-bundle procedure](local-storage-plex-setup.md).
Review redacted bundles before sharing them. Do not paste tokens or raw private
service URLs into public reports.
