#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=scripts/lib/testflight_common.sh
source "$repo/scripts/lib/testflight_common.sh"

usage_text=$(cat <<'EOF'
Usage: deploy-testflight-tvos.sh --output DIR [--export-only] [--build-number N] [--prune-stale-api-dev-certs]

Archive and export/upload OmakasePlusTV via Xcode Cloud Signing and App Store Connect API keys.
Requires APPLE_TEAM_ID, ASC_KEY_ID, ASC_ISSUER_ID, and either ASC_KEY_PATH or ASC_KEY_P8_BASE64.
Optional OMAKASE_SERVER_ORIGIN embeds an HTTPS default origin in Info.plist before signing.
EOF
)

tf_parse_common_args "$usage_text" "$@"
tf_validate_common_env "$0"

commit=$(git -C "$repo" rev-parse HEAD)
tree=$(git -C "$repo" rev-parse 'HEAD^{tree}')
sdk_version=$(xcrun --sdk appletvos --show-sdk-version)
xcode_version=$(xcodebuild -version | tr '\n' ' ' | sed 's/ *$//')

# Do not leak the optional private origin through xcodebuild logs or receipts.
server_origin=${OMAKASE_SERVER_ORIGIN:-}
unset OMAKASE_SERVER_ORIGIN

tf_prepare_empty_output_dir

work=$(mktemp -d "${TMPDIR:-/tmp}/omakase-tvos-testflight.XXXXXXXX")
before_serials="$work/dev-serials-before.txt"
after_serials="$work/dev-serials-after.txt"
pre_cleanup_summary="$work/cert-pre-cleanup.json"
cleanup_summary="$work/cert-cleanup.json"
cleanup_done=0
key_file=""

on_exit() {
    local rc=$?
    trap - EXIT
    if [[ $cleanup_done -eq 0 && -n ${key_file:-} && -f ${key_file:-} && -f $before_serials ]]; then
        tf_cleanup_ephemeral_dev_certs \
            "$repo" \
            "$ASC_KEY_ID" \
            "$ASC_ISSUER_ID" \
            "$key_file" \
            "$before_serials" \
            "$after_serials" \
            "$prune_stale_api_dev_certs" \
            "$cleanup_summary" || true
    fi
    rm -rf -- "$work"
    exit "$rc"
}
trap on_exit EXIT

key_file=$(tf_stage_asc_key "$work" "$ASC_KEY_ID")
unset ASC_KEY_P8_BASE64 ASC_KEY_PATH

mkdir -p "$work/source"
cp -R "$repo/tv-source" "$work/source/tv-source"
source_dir="$work/source/tv-source"

tf_inject_server_origin "$source_dir/OmakasePlusTV/Info.plist" "$server_origin"

xcrun swift test --package-path "$source_dir/QualificationCore" --scratch-path "$work/swift-test"

tf_prune_stale_api_dev_certs "$repo" "$ASC_KEY_ID" "$ASC_ISSUER_ID" "$key_file" "$pre_cleanup_summary" "$prune_stale_api_dev_certs" || true
tf_snapshot_apple_dev_serials "$repo" "$before_serials"

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
assert "arm64" in (app.get("UIRequiredDeviceCapabilities") or []), "app Info.plist must require arm64"
assert "arm64" in (shelf.get("UIRequiredDeviceCapabilities") or []), "TopShelf Info.plist must require arm64"
PY

destination="upload"
if [[ $mode == export-only ]]; then
    destination="export"
fi

export_plist="$work/ExportOptions.plist"
tf_write_export_options "$export_plist" "$destination" "$APPLE_TEAM_ID"

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

tf_cleanup_ephemeral_dev_certs \
    "$repo" \
    "$ASC_KEY_ID" \
    "$ASC_ISSUER_ID" \
    "$key_file" \
    "$before_serials" \
    "$after_serials" \
    "$prune_stale_api_dev_certs" \
    "$cleanup_summary" || true
cleanup_done=1
certs_revoked=$(tf_read_revoked_count "$pre_cleanup_summary" "$cleanup_summary")

OMAKASE_SERVER_ORIGIN="$server_origin" python3 - \
    "$archived_app/Info.plist" \
    "$output/testflight-receipt.json" \
    "$commit" \
    "$tree" \
    "$mode" \
    "$xcode_version" \
    "$sdk_version" \
    "$ipa_file" \
    "$ipa_sha256" \
    "$certs_revoked" <<'PY'
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
    certs_revoked,
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
    "ephemeral_dev_certs_revoked": int(certs_revoked),
}
if ipa_file:
    receipt["ipa"] = {"file": ipa_file, "sha256": ipa_sha256, "signed": True}
Path(receipt_path).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
PY

cat "$output/testflight-receipt.json"
