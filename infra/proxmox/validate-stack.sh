#!/usr/bin/env bash
set -euo pipefail

stack_root="${1:-/opt/omakase/source}"
compose_file="${stack_root}/compose.local.yaml"
env_file="${stack_root}/.env.local"
receipt="${2:-${stack_root}/data/post-install-receipt.json}"

test -f "${compose_file}"
test -f "${env_file}"
test -f "${stack_root}/.env.local.secrets"
test "$(stat -c '%a' "${stack_root}/.env.local.secrets")" = "600"

docker compose --env-file "${env_file}" -f "${compose_file}" config --quiet
app_state="$(docker inspect omakase-local-app --format '{{.State.Status}}|{{.State.Health.Status}}')"
revision="$(docker inspect omakase-local-app --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
doctor_output="$(docker compose --env-file "${env_file}" -f "${compose_file}" exec -T \
  omakase-plus omakase plus doctor --data-dir /home/omakase/data \
  --import-root /media/import --library-root /media/library)"

python3 - "${receipt}" "${revision}" "${app_state}" "${doctor_output}" <<'PY'
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
doctor = sys.argv[4]
payload = {
    "schema_version": "omakase-post-install-receipt-v1",
    "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "revision": sys.argv[2],
    "container_state": sys.argv[3],
    "doctor_output_sha256": hashlib.sha256(doctor.encode()).hexdigest(),
    "doctor_passed": True,
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
os.chmod(path, 0o600)
print(path)
PY
