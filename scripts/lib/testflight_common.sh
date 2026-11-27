# Shared helpers for TestFlight archive, export, and certificate cleanup scripts.
# Sourced by scripts/deploy-testflight-ios.sh and scripts/deploy-testflight-tvos.sh.

fail() { printf '%s\n' "$*" >&2; exit 1; }
digest() { shasum -a 256 "$1" | awk '{print $1}'; }

output=""
mode="upload"
build_number=""
prune_stale_api_dev_certs=0

tf_parse_common_args() {
    local usage_text=$1
    shift
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --output)
                [[ $# -ge 2 ]] || fail "--output requires a directory path."
                output=$2
                shift 2
                ;;
            --export-only)
                mode="export-only"
                shift
                ;;
            --build-number)
                [[ $# -ge 2 ]] || fail "--build-number requires a value."
                build_number=$2
                shift 2
                ;;
            --prune-stale-api-dev-certs)
                prune_stale_api_dev_certs=1
                shift
                ;;
            -h|--help)
                printf '%s\n' "$usage_text"
                exit 0
                ;;
            *)
                fail "Unknown argument: $1"
                ;;
        esac
    done
}

tf_validate_common_env() {
    local script_name=$1
    [[ -n $output ]] || fail "Usage: $script_name --output EMPTY_OUTPUT_DIR [--export-only] [--build-number N] [--prune-stale-api-dev-certs]"
    [[ $(uname -s) == Darwin ]] || fail "TestFlight archive and export require macOS and full Xcode."
    [[ -n ${APPLE_TEAM_ID:-} ]] || fail "APPLE_TEAM_ID is required."
    [[ -n ${ASC_KEY_ID:-} ]] || fail "ASC_KEY_ID is required."
    [[ -n ${ASC_ISSUER_ID:-} ]] || fail "ASC_ISSUER_ID is required."

    if [[ -n $build_number && ! $build_number =~ ^[1-9][0-9]{0,7}$ ]]; then
        fail "--build-number must be a positive integer."
    fi
}

tf_prepare_empty_output_dir() {
    mkdir -p "$output"
    output=$(cd "$output" && pwd -P)
    [[ -z $(find "$output" -mindepth 1 -maxdepth 1 -print -quit) ]] || fail "Use an empty output directory."
}

tf_stage_asc_key() {
    local dest_dir=$1
    local key_id=$2
    local key_file=""
    if [[ -n ${ASC_KEY_PATH:-} ]]; then
        [[ -f $ASC_KEY_PATH ]] || fail "ASC_KEY_PATH does not point to an existing file."
        key_file="$dest_dir/AuthKey_${key_id}.p8"
        cp "$ASC_KEY_PATH" "$key_file"
        chmod 600 "$key_file"
    elif [[ -n ${ASC_KEY_P8_BASE64:-} ]]; then
        key_file="$dest_dir/AuthKey_${key_id}.p8"
        ASC_KEY_P8_BASE64_VALUE="$ASC_KEY_P8_BASE64" python3 - "$key_file" <<'PY'
import base64
import binascii
import os
import sys
from pathlib import Path

raw = os.environ.get("ASC_KEY_P8_BASE64_VALUE", "").strip()
try:
    decoded = base64.b64decode(raw, validate=True)
except (binascii.Error, ValueError) as exc:
    sys.exit(f"Invalid ASC_KEY_P8_BASE64 payload: {exc}")
if b"BEGIN PRIVATE KEY" not in decoded:
    sys.exit("ASC_KEY_P8_BASE64 does not decode to a PEM private key (.p8).")
path = Path(sys.argv[1])
path.write_bytes(decoded)
os.chmod(path, 0o600)
PY
    else
        fail "Set ASC_KEY_PATH or ASC_KEY_P8_BASE64 for App Store Connect authentication."
    fi
    unset ASC_KEY_P8_BASE64 ASC_KEY_PATH
    printf '%s\n' "$key_file"
}

tf_inject_server_origin() {
    local plist_path=$1
    local server_origin=$2
    [[ -n $server_origin ]] || return 0
    OMAKASE_SERVER_ORIGIN="$server_origin" python3 - "$plist_path" <<'PY'
import os
import plistlib
import sys
from pathlib import Path
from urllib.parse import urlsplit

origin = os.environ.get("OMAKASE_SERVER_ORIGIN", "")
try:
    parts = urlsplit(origin)
    valid = (
        len(origin) <= 512
        and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in origin)
        and parts.scheme == "https"
        and parts.hostname
        and parts.username is None
        and parts.password is None
        and parts.path in ("", "/")
        and "?" not in origin
        and "#" not in origin
        and (parts.port is None or 1 <= parts.port <= 65535)
    )
except ValueError:
    valid = False
if not valid:
    sys.exit("OMAKASE_SERVER_ORIGIN must be an HTTPS origin without credentials.")

plist_path = Path(sys.argv[1])
info = plistlib.loads(plist_path.read_bytes())
info["OmakaseServerOrigin"] = origin
plist_path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML))
PY
}

tf_write_export_options() {
    local plist_path=$1
    local destination=$2
    local team_id=$3
    python3 - "$plist_path" "$destination" "$team_id" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path, destination, team_id = sys.argv[1:]
options = {
    "method": "app-store-connect",
    "destination": destination,
    "signingStyle": "automatic",
    "teamID": team_id,
    "manageAppVersionAndBuildNumber": False,
}
Path(plist_path).write_bytes(plistlib.dumps(options, fmt=plistlib.FMT_XML))
PY
}

tf_snapshot_apple_dev_serials() {
    local repo=$1
    local out_file=$2
    local pem_bundle=""
    pem_bundle=$(security find-certificate -a -c "Apple Development" -p 2>/dev/null || true)
    if [[ -z $pem_bundle ]]; then
        : > "$out_file"
        return 0
    fi
    printf '%s\n' "$pem_bundle" \
        | python3 "$repo/scripts/asc_cleanup_ephemeral_dev_certs.py" --extract-serials-stdin \
        | LC_ALL=C sort -u > "$out_file"
}

tf_cleanup_ephemeral_dev_certs() {
    local repo=$1
    local key_id=$2
    local issuer_id=$3
    local key_file=$4
    local before_file=$5
    local after_file=$6
    local prune_stale=$7
    local summary_file=$8

    tf_snapshot_apple_dev_serials "$repo" "$after_file"
    local cleanup_args=(
        --key-id "$key_id"
        --issuer-id "$issuer_id"
        --key-path "$key_file"
        --json-out "$summary_file"
    )
    local new_serial=""
    while IFS= read -r new_serial; do
        [[ -n $new_serial ]] || continue
        cleanup_args+=(--serial "$new_serial")
    done < <(comm -13 "$before_file" "$after_file")

    if [[ $prune_stale -eq 1 ]]; then
        cleanup_args+=(--prune-stale-api-dev-certs)
    fi

    if python3 "$repo/scripts/asc_cleanup_ephemeral_dev_certs.py" "${cleanup_args[@]}"; then
        return 0
    fi
    printf 'Warning: ephemeral Apple Development certificate cleanup failed; continuing.\n' >&2
    return 1
}

tf_prune_stale_api_dev_certs() {
    local repo=$1
    local key_id=$2
    local issuer_id=$3
    local key_file=$4
    local summary_file=$5
    local prune_flag=$6
    [[ $prune_flag -eq 1 ]] || return 0
    if python3 "$repo/scripts/asc_cleanup_ephemeral_dev_certs.py" \
        --key-id "$key_id" \
        --issuer-id "$issuer_id" \
        --key-path "$key_file" \
        --prune-stale-api-dev-certs \
        --json-out "$summary_file"; then
        return 0
    fi
    printf 'Warning: pre-archive stale Apple Development certificate prune failed; continuing.\n' >&2
    return 1
}

tf_read_revoked_count() {
    local total=0
    local path=""
    for path in "$@"; do
        [[ -f $path ]] || continue
        local count=0
        count=$(python3 - "$path" <<'PY'
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    print(int(data.get("revoked", 0) or 0))
except Exception:
    print(0)
PY
)
        total=$((total + count))
    done
    printf '%s\n' "$total"
}
