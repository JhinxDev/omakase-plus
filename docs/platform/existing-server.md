# Existing Debian or Docker server

Use this path for a Debian 12/13 host, a Debian VM, or a server that already
runs Docker and Plex. Rootless Docker is not currently a supported deployment
target because media ownership and optional device/network handling differ.

## 1. Prepare Docker

Install Docker Engine and the Compose plugin from Docker's official Debian
repository. The included Ansible role performs only that preparation and
creates the Omakase directories:

```bash
cp infra/ansible/inventory.example.yml infra/ansible/inventory.yml
# Edit the copied inventory.
ansible-playbook -i infra/ansible/inventory.yml infra/ansible/prepare-host.yml --check
ansible-playbook -i infra/ansible/inventory.yml infra/ansible/prepare-host.yml
```

Membership in the `docker` group is root-equivalent; the reference setup runs
administrative Compose commands as root and never mounts `/var/run/docker.sock`
into Omakase.

## 2. Install one exact release

Use the intended source destination and publication status in the repository
README. Verify the exact revision or archive SHA-256 before extraction. Keep code,
mutable data, imports, and managed library separate:

```text
/opt/omakase/source       exact release and Compose files
/srv/omakase/data         SQLite, setup state, receipts
/srv/omakase/import       read-only source media in the app
/srv/omakase/library      Omakase-managed Plex layout
/srv/omakase/plex         bundled Plex configuration (optional)
/srv/omakase/transcode    disposable bundled Plex transcode data
```

Copy `.env.local.example` to `.env.local` and
`.env.local.secrets.example` to `.env.local.secrets`. Use absolute `/srv`
paths, set the exact `OMAKASE_REVISION`, and restrict secrets:

```bash
chmod 0600 .env.local.secrets
docker compose --env-file .env.local -f compose.local.yaml config --quiet
```

Generate the Plus master key locally and place it in the secret file without
passing it as a command argument or saving it in shell history. Configure the
public base URL, allowed hosts, and Secure cookie flag together; use HTTPS
before exposing the app beyond loopback.

## 3. Start the selected topology

Existing Plex:

```bash
docker compose --env-file .env.local -f compose.local.yaml up -d --build omakase-plus
```

Bundled Plex:

```bash
docker compose --env-file .env.local -f compose.local.yaml \
  --profile plex up -d --build
```

Do not start `qbittorrent-vpn` during the first install. Finish login, AniList,
Plex mapping, an import-only synthetic or owned-media test, and backup first.

## 4. Prove the installation

```bash
docker inspect omakase-local-app \
  --format '{{.State.Health.Status}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
omakase plus doctor \
  --data-dir /srv/omakase/data \
  --import-root /srv/omakase/import \
  --library-root /srv/omakase/library \
  --acquisition-mode import-only \
  --vpn-mode none \
  --plex-url http://127.0.0.1:32400
sudo infra/proxmox/validate-stack.sh /opt/omakase/source
```

The post-install receipt contains hashes and readiness outcomes, not credentials
or filenames. Complete the media proof in
[Local Storage + Plex setup](../local-storage-plex-setup.md).

Primary references: [Docker Engine on Debian](https://docs.docker.com/engine/install/debian/),
[Docker Linux post-installation](https://docs.docker.com/engine/install/linux-postinstall),
and [Docker Compose profiles](https://docs.docker.com/reference/compose-file/profiles/).
