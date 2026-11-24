#!/usr/bin/env bash
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 1; }
digest() { shasum -a 256 "$1" | awk '{print $1}'; }

output=""
mode="upload"
build_number=""

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
        -h|--help)
            cat <<'EOF'
Usage: deploy-testflight-tvos.sh --output DIR [--export-only] [--build-number N]

Archive and export/upload OmakasePlusTV via Xcode Cloud Signing and App Store Connect API keys.
Requires APPLE_TEAM_ID, ASC_KEY_ID, ASC_ISSUER_ID, and either ASC_KEY_PATH or ASC_KEY_P8_BASE64.
Optional OMAKASE_SERVER_ORIGIN embeds an HTTPS default origin in Info.plist before signing.
EOF
            exit 0
            ;;
        *)
            fail "Unknown argument: $1"
            ;;
    esac
done

[[ -n $output ]] || fail "Usage: $0 --output EMPTY_OUTPUT_DIR [--export-only] [--build-number N]"
[[ $(uname -s) == Darwin ]] || fail "TestFlight archive and export require macOS and full Xcode."
[[ -n ${APPLE_TEAM_ID:-} ]] || fail "APPLE_TEAM_ID is required."
[[ -n ${ASC_KEY_ID:-} ]] || fail "ASC_KEY_ID is required."
[[ -n ${ASC_ISSUER_ID:-} ]] || fail "ASC_ISSUER_ID is required."

if [[ -n $build_number && ! $build_number =~ ^[1-9][0-9]{0,7}$ ]]; then
    fail "--build-number must be a positive integer."
fi

repo=$(cd "$(dirname "$0")/.." && pwd)
commit=$(git -C "$repo" rev-parse HEAD)
tree=$(git -C "$repo" rev-parse 'HEAD^{tree}')
sdk_version=$(xcrun --sdk appletvos --show-sdk-version)
xcode_version=$(xcodebuild -version | tr '\n' ' ' | sed 's/ *$//')

# Do not leak the optional private origin through xcodebuild logs or receipts.
server_origin=${OMAKASE_SERVER_ORIGIN:-}
unset OMAKASE_SERVER_ORIGIN

mkdir -p "$output"
output=$(cd "$output" && pwd -P)
[[ -z $(find "$output" -mindepth 1 -maxdepth 1 -print -quit) ]] || fail "Use an empty output directory."

work=$(mktemp -d "${TMPDIR:-/tmp}/omakase-tvos-testflight.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT

key_file=""
if [[ -n ${ASC_KEY_PATH:-} ]]; then
    [[ -f $ASC_KEY_PATH ]] || fail "ASC_KEY_PATH does not point to an existing file."
    key_file="$work/AuthKey_${ASC_KEY_ID}.p8"
    cp "$ASC_KEY_PATH" "$key_file"
    chmod 600 "$key_file"
elif [[ -n ${ASC_KEY_P8_BASE64:-} ]]; then
    key_file="$work/AuthKey_${ASC_KEY_ID}.p8"
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

mkdir -p "$work/source"
cp -R "$repo/tv-source" "$work/source/tv-source"
source_dir="$work/source/tv-source"

if [[ -n $server_origin ]]; then
    OMAKASE_SERVER_ORIGIN="$server_origin" python3 - "$source_dir/OmakasePlusTV/Info.plist" <<'PY'
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
fi

xcrun swift test --package-path "$source_dir/QualificationCore" --scratch-path "$work/swift-test"

archive="$work/OmakasePlusTV.xcarchive"
archive_args=(
    -project "$source_dir/OmakaseQualification.xcodeproj"
    -scheme OmakasePlusTV
    -configuration Release
    -sdk appletvos
    -destination 'generic/platform=tvOS'
    -archivePath "$archive"
    -derivedDataPath "$work/DerivedData"
    ARCHS=arm64
    "DEVELOPMENT_TEAM=$APPLE_TEAM_ID"
    CODE_SIGN_STYLE=Automatic
    -allowProvisioningUpdates
    -authenticationKeyPath "$key_file"
    -authenticationKeyID "$ASC_KEY_ID"
    -authenticationKeyIssuerID "$ASC_ISSUER_ID"
)
if [[ -n $build_number ]]; then
    archive_args+=("OMAKASE_TV_BUILD_NUMBER=$build_number")
fi

xcodebuild archive "${archive_args[@]}"

archived_app="$archive/Products/Applications/OmakasePlusTV.app"
[[ -x "$archived_app/OmakasePlusTV" ]] || fail "Archived OmakasePlusTV executable missing."
[[ $(xcrun lipo -archs "$archived_app/OmakasePlusTV") == arm64 ]] || fail "OmakasePlusTV is not arm64."
[[ ! -e "$archived_app/Frameworks/TVVLCKit.framework" ]] || fail "Production OmakasePlusTV unexpectedly embeds TVVLCKit.framework."
app_links=$(xcrun otool -L "$archived_app/OmakasePlusTV")
[[ $app_links != *TVVLCKit* ]] || fail "Production OmakasePlusTV unexpectedly links TVVLCKit."
[[ ! -e "$archived_app/baseline.mp4" && ! -e "$archived_app/track-check.mp4" ]] ||
    fail "Production OmakasePlusTV unexpectedly bundles qualification video fixtures."

shelf="$archived_app/PlugIns/OmakaseTopShelf.appex"
[[ -x "$shelf/OmakaseTopShelf" && $(xcrun lipo -archs "$shelf/OmakaseTopShelf") == arm64 ]] ||
    fail "Missing arm64 Top Shelf app extension."

python3 - "$archived_app/Info.plist" "$shelf/Info.plist" <<'PY'
import plistlib
import sys
from pathlib import Path

app, shelf = (plistlib.loads(Path(path).read_bytes()) for path in sys.argv[1:])
assert app["CFBundleIdentifier"] == "dev.omakase.plus.tv"
assert app.get("ITSAppUsesNonExemptEncryption") is False, "ITSAppUsesNonExemptEncryption must be false"
assert shelf["CFBundleIdentifier"] == app["CFBundleIdentifier"] + ".TopShelf"
assert shelf["CFBundleVersion"] == app["CFBundleVersion"]
assert shelf["CFBundleShortVersionString"] == app["CFBundleShortVersionString"]
assert shelf["NSExtension"]["NSExtensionPointIdentifier"] == "com.apple.tv-top-shelf"
PY

destination="upload"
if [[ $mode == export-only ]]; then
    destination="export"
fi

export_plist="$work/ExportOptions.plist"
python3 - "$export_plist" "$destination" "$APPLE_TEAM_ID" <<'PY'
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

export_dir="$work/export"
mkdir -p "$export_dir"
xcodebuild -exportArchive \
    -archivePath "$archive" \
    -exportOptionsPlist "$export_plist" \
    -exportPath "$export_dir" \
    -allowProvisioningUpdates \
    -authenticationKeyPath "$key_file" \
    -authenticationKeyID "$ASC_KEY_ID" \
    -authenticationKeyIssuerID "$ASC_ISSUER_ID"

ipa_file=""
ipa_sha256=""
if [[ $mode == export-only ]]; then
    exported_ipa=$(find "$export_dir" -maxdepth 2 -name '*.ipa' -print -quit)
    [[ -n $exported_ipa && -f $exported_ipa ]] || fail "Exported IPA not found in $export_dir."
    ipa_name=$(basename "$exported_ipa")
    cp "$exported_ipa" "$output/$ipa_name"
    ipa_file="$ipa_name"
    ipa_sha256=$(digest "$output/$ipa_name")
fi

OMAKASE_SERVER_ORIGIN="$server_origin" python3 - \
    "$archived_app/Info.plist" \
    "$output/testflight-receipt.json" \
    "$commit" \
    "$tree" \
    "$mode" \
    "$xcode_version" \
    "$sdk_version" \
    "$ipa_file" \
    "$ipa_sha256" <<'PY'
import json
import os
import plistlib
import sys
from pathlib import Path

(
    plist_path,
    receipt_path,
    commit,
    tree,
    mode,
    xcode,
    sdk,
    ipa_file,
    ipa_sha256,
) = sys.argv[1:]
info = plistlib.loads(Path(plist_path).read_bytes())
receipt = {
    "source_commit": commit,
    "git_tree": tree,
    "mode": mode,
    "xcode": xcode,
    "appletvos_sdk": sdk,
    "architecture": "arm64",
    "bundle_id": info["CFBundleIdentifier"],
    "version": info["CFBundleShortVersionString"],
    "build": info["CFBundleVersion"],
    "its_app_uses_non_exempt_encryption": bool(info.get("ITSAppUsesNonExemptEncryption")),
    "embeds_tvvlckit": False,
    "server_origin_configured": bool(os.environ.get("OMAKASE_SERVER_ORIGIN")),
    "portable_swift_tests": "passed",
}
if ipa_file:
    receipt["ipa"] = {"file": ipa_file, "sha256": ipa_sha256, "signed": True}
Path(receipt_path).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
PY

cat "$output/testflight-receipt.json"
