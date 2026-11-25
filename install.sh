#!/usr/bin/env bash
set -euo pipefail

# Omakase Plus friend-friendly installer.
# Installs a prebuilt GHCR container setup (Bring Your Own Media software only).

DEFAULT_IMAGE="ghcr.io/jhinxdev/omakase-plus:latest"
DEFAULT_DIR="${HOME:-/tmp}/omakase-plus"
DEFAULT_PORT="8766"

INSTALL_DIR=""
PORT=""
BIND_ADDR=""
PUBLIC_URL=""
ALLOWED_HOSTS=""
MEDIA_PATH=""
IMAGE="$DEFAULT_IMAGE"
WITH_PLEX="false"
NON_INTERACTIVE="false"
NO_START="false"
DRY_RUN="false"

usage() {
  cat <<'EOF'
Omakase Plus Installer

Usage:
  install.sh [options]

Options:
  --dir PATH              Target installation directory (default: ~/omakase-plus)
  --port PORT             Host HTTP port to bind (default: 8766)
  --bind ADDR             Host bind IP (127.0.0.1 or 0.0.0.0; default: 127.0.0.1)
  --public-url URL        Public or LAN base URL (example: https://media.example.com)
  --allowed-hosts HOSTS   Comma-separated Host header allowlist
  --media-path PATH       Optional existing media folder on host (mounted read-only)
  --with-plex             Also include a local Plex Media Server container
  --image IMAGE           Container image reference (default: ghcr.io/jhinxdev/omakase-plus:latest)
  --non-interactive       Accept flags/defaults without prompting
  --no-start              Write config files only; do not pull or start containers
  --dry-run               Print planned actions without writing files or running Docker
  -h, --help              Show this help message
EOF
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '==> %s\n' "$*"
}

warn() {
  printf 'Warning: %s\n' "$*" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)
      [[ $# -ge 2 ]] || fail "--dir requires a path."
      INSTALL_DIR="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || fail "--port requires a port number."
      PORT="$2"
      shift 2
      ;;
    --bind)
      [[ $# -ge 2 ]] || fail "--bind requires an address."
      BIND_ADDR="$2"
      shift 2
      ;;
    --public-url)
      [[ $# -ge 2 ]] || fail "--public-url requires a URL."
      PUBLIC_URL="$2"
      shift 2
      ;;
    --allowed-hosts)
      [[ $# -ge 2 ]] || fail "--allowed-hosts requires host names."
      ALLOWED_HOSTS="$2"
      shift 2
      ;;
    --media-path)
      [[ $# -ge 2 ]] || fail "--media-path requires a directory path."
      MEDIA_PATH="$2"
      shift 2
      ;;
    --with-plex)
      WITH_PLEX="true"
      shift
      ;;
    --image)
      [[ $# -ge 2 ]] || fail "--image requires an image reference."
      IMAGE="$2"
      shift 2
      ;;
    --non-interactive)
      NON_INTERACTIVE="true"
      shift
      ;;
    --no-start)
      NO_START="true"
      shift
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1 (run with --help for usage)"
      ;;
  esac
done

prompt_input() {
  local prompt_text="$1"
  local default_val="$2"
  local reply=""

  if [[ "$NON_INTERACTIVE" == "true" ]]; then
    printf '%s' "$default_val"
    return 0
  fi

  if [[ -t 0 ]]; then
    printf '%s [%s]: ' "$prompt_text" "$default_val" >&2
    IFS= read -r reply || true
  elif [[ -r /dev/tty ]]; then
    printf '%s [%s]: ' "$prompt_text" "$default_val" >&2
    IFS= read -r reply < /dev/tty || true
  else
    printf '%s' "$default_val"
    return 0
  fi

  if [[ -z "$reply" ]]; then
    printf '%s' "$default_val"
  else
    printf '%s' "$reply"
  fi
}

detect_lan_ip() {
  local detected=""
  if command -v ip >/dev/null 2>&1; then
    detected="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)"
  fi
  if [[ -z "$detected" ]] && command -v hostname >/dev/null 2>&1; then
    detected="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  printf '%s' "$detected"
}

validate_port() {
  local p="$1"
  [[ "$p" =~ ^[0-9]+$ ]] && (( p >= 1 && p <= 65535 )) || fail "Invalid port: $p"
}

generate_master_key() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(32))'
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    python -c 'import secrets; print(secrets.token_hex(32))'
    return 0
  fi
  if [[ -r /dev/urandom ]] && command -v od >/dev/null 2>&1; then
    od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
    printf '\n'
    return 0
  fi
  fail "Cannot generate random OMAKASE_PLUS_MASTER_KEY (need openssl, python3, or /dev/urandom)."
}

extract_host_from_url() {
  local url="$1"
  local without_scheme="${url#http://}"
  without_scheme="${without_scheme#https://}"
  local authority="${without_scheme%%/*}"
  printf '%s' "${authority%%:*}"
}

if [[ "$NON_INTERACTIVE" != "true" ]] && { [[ -t 0 ]] || [[ -r /dev/tty ]]; }; then
  printf '\nOmakase Plus quick setup (Bring Your Own Media, software only)\n\n'
fi

if [[ -z "$INSTALL_DIR" ]]; then
  INSTALL_DIR="$(prompt_input 'Install directory' "$DEFAULT_DIR")"
fi

if [[ -z "$PORT" ]]; then
  PORT="$(prompt_input 'HTTP port' "$DEFAULT_PORT")"
fi
validate_port "$PORT"

SECURE_COOKIES="false"
if [[ -z "$BIND_ADDR" && -z "$PUBLIC_URL" ]]; then
  if [[ "$NON_INTERACTIVE" == "true" ]]; then
    BIND_ADDR="127.0.0.1"
  else
    printf 'Access mode:\n' >&2
    printf '  1) This computer only (127.0.0.1)\n' >&2
    printf '  2) Home network / LAN (0.0.0.0)\n' >&2
    printf '  3) Custom HTTPS domain behind a reverse proxy\n' >&2
    mode_choice="$(prompt_input 'Choose access mode (1-3)' '1')"
    case "$mode_choice" in
      1)
        BIND_ADDR="127.0.0.1"
        ;;
      2)
        BIND_ADDR="0.0.0.0"
        lan_ip="$(detect_lan_ip)"
        default_lan_url="http://${lan_ip:-localhost}:${PORT}"
        PUBLIC_URL="$(prompt_input 'LAN base URL (used for links & Apple TV/iOS pairing)' "$default_lan_url")"
        ;;
      3)
        BIND_ADDR="127.0.0.1"
        PUBLIC_URL="$(prompt_input 'Public HTTPS URL (example: https://omakase.example.com)' 'https://omakase.example.com')"
        ;;
      *)
        fail "Invalid access choice: $mode_choice"
        ;;
    esac
  fi
fi

if [[ -z "$BIND_ADDR" ]]; then
  BIND_ADDR="127.0.0.1"
fi

if [[ -z "$PUBLIC_URL" ]]; then
  if [[ "$BIND_ADDR" == "0.0.0.0" ]]; then
    lan_ip="$(detect_lan_ip)"
    PUBLIC_URL="http://${lan_ip:-127.0.0.1}:${PORT}"
  else
    PUBLIC_URL="http://127.0.0.1:${PORT}"
  fi
fi

if [[ "$PUBLIC_URL" == https://* ]]; then
  SECURE_COOKIES="true"
fi

if [[ -z "$ALLOWED_HOSTS" ]]; then
  url_host="$(extract_host_from_url "$PUBLIC_URL")"
  if [[ -n "$url_host" && "$url_host" != "localhost" && "$url_host" != "127.0.0.1" ]]; then
    ALLOWED_HOSTS="localhost,127.0.0.1,${url_host}"
  else
    ALLOWED_HOSTS="localhost,127.0.0.1"
  fi
fi

if [[ "$NON_INTERACTIVE" != "true" && -z "$MEDIA_PATH" ]]; then
  MEDIA_PATH="$(prompt_input 'Existing media folder on this host (leave blank for app-only or Plex/cloud)' '')"
fi

if [[ "$NON_INTERACTIVE" != "true" && "$WITH_PLEX" == "false" ]]; then
  plex_reply="$(prompt_input 'Also include a bundled Plex container? (y/N)' 'N')"
  case "$plex_reply" in
    y|Y|yes|YES) WITH_PLEX="true" ;;
    *) WITH_PLEX="false" ;;
  esac
fi

if [[ "$NO_START" != "true" && "$DRY_RUN" != "true" ]]; then
  command -v docker >/dev/null 2>&1 || fail "Docker is required. Install Docker Engine first: https://docs.docker.com/engine/install/"
  docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is required ('docker compose version' failed)."
fi

PUID="$(id -u 2>/dev/null || printf '1000')"
PGID="$(id -g 2>/dev/null || printf '1000')"

USE_MEDIA_MOUNTS="false"
if [[ -n "$MEDIA_PATH" || "$WITH_PLEX" == "true" ]]; then
  USE_MEDIA_MOUNTS="true"
fi

if [[ "$DRY_RUN" == "true" ]]; then
  info "Dry run summary (no files written, no containers started):"
  printf '  Install directory : %s\n' "$INSTALL_DIR"
  printf '  Image             : %s\n' "$IMAGE"
  printf '  Bind & port       : %s:%s\n' "$BIND_ADDR" "$PORT"
  printf '  Public URL        : %s\n' "$PUBLIC_URL"
  printf '  Allowed hosts     : %s\n' "$ALLOWED_HOSTS"
  printf '  Media folder      : %s\n' "${MEDIA_PATH:-none (app-only)}"
  printf '  Bundled Plex      : %s\n' "$WITH_PLEX"
  exit 0
fi

info "Preparing workspace in $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data"
if [[ "$USE_MEDIA_MOUNTS" == "true" ]]; then
  mkdir -p "$INSTALL_DIR/library"
  if [[ "$WITH_PLEX" == "true" ]]; then
    mkdir -p "$INSTALL_DIR/plex/config" "$INSTALL_DIR/plex/transcode"
  fi
fi

SECRETS_FILE="$INSTALL_DIR/.env.local.secrets"
EXISTING_MASTER_KEY=""
if [[ -f "$SECRETS_FILE" ]]; then
  EXISTING_MASTER_KEY="$(sed -n 's/^OMAKASE_PLUS_MASTER_KEY=//p' "$SECRETS_FILE" | head -n 1 | tr -d '\r\n')"
fi

if [[ -n "$EXISTING_MASTER_KEY" ]]; then
  MASTER_KEY="$EXISTING_MASTER_KEY"
  info "Preserving existing OMAKASE_PLUS_MASTER_KEY in $SECRETS_FILE"
else
  MASTER_KEY="$(generate_master_key)"
  umask 077
  printf 'OMAKASE_PLUS_MASTER_KEY=%s\n' "$MASTER_KEY" > "$SECRETS_FILE"
  chmod 600 "$SECRETS_FILE" 2>/dev/null || true
  info "Generated new OMAKASE_PLUS_MASTER_KEY in $SECRETS_FILE"
fi

ENV_FILE="$INSTALL_DIR/.env.local"
cat > "$ENV_FILE" <<EOF
# Omakase Plus configuration (keep secrets in .env.local.secrets)
OMAKASE_IMAGE=$IMAGE
OMAKASE_PUID=$PUID
OMAKASE_PGID=$PGID
OMAKASE_BIND_ADDRESS=$BIND_ADDR
OMAKASE_PORT=$PORT
OMAKASE_PLUS_URL=$PUBLIC_URL
OMAKASE_ALLOWED_HOSTS=$ALLOWED_HOSTS
OMAKASE_SESSION_COOKIE_SECURE=$SECURE_COOKIES
OMAKASE_DATA_PATH=./data
TZ=${TZ:-Etc/UTC}
EOF

if [[ "$USE_MEDIA_MOUNTS" == "true" ]]; then
  EFFECTIVE_IMPORT="${MEDIA_PATH:-./library}"
  cat >> "$ENV_FILE" <<EOF
OMAKASE_IMPORT_PATH=$EFFECTIVE_IMPORT
OMAKASE_LIBRARY_PATH=./library
EOF
fi

COMPOSE_FILE="$INSTALL_DIR/compose.yaml"
cat > "$COMPOSE_FILE" <<'EOF'
name: omakase-plus

services:
  omakase-plus:
    container_name: omakase-plus
    image: ${OMAKASE_IMAGE:-ghcr.io/jhinxdev/omakase-plus:latest}
    restart: unless-stopped
    user: "${OMAKASE_PUID:-1000}:${OMAKASE_PGID:-1000}"
    ports:
      - "${OMAKASE_BIND_ADDRESS:-127.0.0.1}:${OMAKASE_PORT:-8766}:8765"
    env_file:
      - path: ./.env.local.secrets
        required: true
    environment:
      OMAKASE_ENV: production
      OMAKASE_PLUS_PRIVATE: "true"
      OMAKASE_PLUS_URL: ${OMAKASE_PLUS_URL:-http://127.0.0.1:8766}
      OMAKASE_ALLOWED_HOSTS: ${OMAKASE_ALLOWED_HOSTS:-localhost,127.0.0.1}
      OMAKASE_SESSION_COOKIE_SECURE: ${OMAKASE_SESSION_COOKIE_SECURE:-false}
      OMAKASE_ALLOW_PRIVATE_LLM_URLS: ${OMAKASE_ALLOW_PRIVATE_LLM_URLS:-false}
      OMAKASE_ALLOW_PRIVATE_CATALOG_URLS: ${OMAKASE_ALLOW_PRIVATE_CATALOG_URLS:-false}
      OMAKASE_DATA_DIR: /home/omakase/data
      OMAKASE_ACQUISITION_MODE: import-only
      OMAKASE_VPN_MODE: none
      TZ: ${TZ:-Etc/UTC}
EOF

if [[ "$USE_MEDIA_MOUNTS" == "true" ]]; then
  cat >> "$COMPOSE_FILE" <<'EOF'
      OMAKASE_IMPORT_ROOT: /media/import
      OMAKASE_LIBRARY_ROOT: /media/library
    volumes:
      - type: bind
        source: ${OMAKASE_DATA_PATH:-./data}
        target: /home/omakase/data
      - type: bind
        source: ${OMAKASE_IMPORT_PATH}
        target: /media/import
        read_only: true
      - type: bind
        source: ${OMAKASE_LIBRARY_PATH:-./library}
        target: /media/library
EOF
else
  cat >> "$COMPOSE_FILE" <<'EOF'
    volumes:
      - type: bind
        source: ${OMAKASE_DATA_PATH:-./data}
        target: /home/omakase/data
EOF
fi

cat >> "$COMPOSE_FILE" <<'EOF'
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - >-
          import urllib.request;
          urllib.request.urlopen('http://127.0.0.1:8765/plus/health', timeout=4).read()
      interval: 30s
      timeout: 5s
      start_period: 15s
      retries: 3
EOF

if [[ "$WITH_PLEX" == "true" ]]; then
  cat >> "$COMPOSE_FILE" <<'EOF'

  plex:
    container_name: omakase-plex
    image: plexinc/pms-docker:${PLEX_IMAGE_TAG:-1.43.2.10687-563d026ea}
    restart: unless-stopped
    ports:
      - "${OMAKASE_BIND_ADDRESS:-127.0.0.1}:32400:32400/tcp"
    environment:
      TZ: ${TZ:-Etc/UTC}
      PLEX_UID: ${OMAKASE_PUID:-1000}
      PLEX_GID: ${OMAKASE_PGID:-1000}
    volumes:
      - type: bind
        source: ./plex/config
        target: /config
      - type: bind
        source: ./plex/transcode
        target: /transcode
      - type: bind
        source: ${OMAKASE_LIBRARY_PATH:-./library}
        target: /data
        read_only: true
EOF
fi

if [[ "$NO_START" == "true" ]]; then
  info "Wrote $COMPOSE_FILE, $ENV_FILE, and $SECRETS_FILE (--no-start enabled)."
  printf 'Start later with:\n  cd %s && docker compose --env-file .env.local up -d\n' "$INSTALL_DIR"
  exit 0
fi

cd "$INSTALL_DIR"
info "Validating Compose configuration"
docker compose --env-file .env.local config --quiet

info "Pulling prebuilt image ($IMAGE)"
docker compose --env-file .env.local pull

info "Starting Omakase Plus"
docker compose --env-file .env.local up -d

info "Waiting for container healthcheck"
healthy="false"
for _ in $(seq 1 30); do
  status="$(docker inspect omakase-plus --format '{{.State.Health.Status}}' 2>/dev/null || printf 'starting')"
  if [[ "$status" == "healthy" ]]; then
    healthy="true"
    break
  fi
  sleep 2
done

if [[ "$healthy" != "true" ]]; then
  warn "Container did not become healthy within 60s (last status: ${status:-unknown})."
  warn "Check logs with: cd $INSTALL_DIR && docker compose --env-file .env.local logs --tail=50"
fi

printf '\nOmakase Plus is running!\n\n'
printf '  Web UI   : %s/plus\n' "${PUBLIC_URL%/}"
printf '  Directory: %s\n' "$INSTALL_DIR"
printf '  Secrets  : %s/.env.local.secrets (back up with %s/data)\n' "$INSTALL_DIR" "$INSTALL_DIR"
printf '  Update   : cd %s && docker compose --env-file .env.local pull && docker compose --env-file .env.local up -d\n\n' "$INSTALL_DIR"
