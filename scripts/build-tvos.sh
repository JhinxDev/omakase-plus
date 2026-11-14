#!/usr/bin/env bash
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 1; }
digest() { shasum -a 256 "$1" | awk '{print $1}'; }

snapshot=
if [[ ${1:-} == --snapshot ]]; then
    [[ $# == 6 ]] || fail "Usage: $0 --snapshot SOURCE.tar SHA256 COMMIT TREE OUTPUT_DIR"
    snapshot=$2
    source_sha256=$3
    commit=$4
    tree=$5
    output=$6
    [[ $source_sha256 =~ ^[0-9a-f]{64}$ && $commit =~ ^[0-9a-f]{40}$ && $tree =~ ^[0-9a-f]{40}$ ]] ||
        fail "Supply lowercase Git and SHA-256 hashes."
    [[ $(digest "$snapshot") == "$source_sha256" ]] || fail "Source archive SHA-256 mismatch."
    [[ $(git get-tar-commit-id < "$snapshot") == "$commit" ]] || fail "Source archive commit mismatch."
else
    [[ $# == 1 ]] || fail "Usage: $0 OUTPUT_DIR"
    output=$1
    repo=$(cd "$(dirname "$0")/.." && pwd)
    commit=$(git -C "$repo" rev-parse HEAD)
    tree=$(git -C "$repo" rev-parse 'HEAD^{tree}')
fi

[[ $(uname -s) == Darwin ]] || fail "This build requires macOS and full Xcode."
[[ -z ${GITHUB_SHA:-} || $GITHUB_SHA == "$commit" ]] || fail "Checked-out commit differs from the workflow SHA."
# Do not pass the optional private origin through the compiler environment.
server_origin=${OMAKASE_SERVER_ORIGIN:-}
unset OMAKASE_SERVER_ORIGIN
sdk_version=$(xcrun --sdk appletvos --show-sdk-version)
xcode_version=$(xcodebuild -version)

mkdir -p "$output"
output=$(cd "$output" && pwd -P)
[[ -z $(find "$output" -mindepth 1 -maxdepth 1 -print -quit) ]] || fail "Use an empty output directory."
work=$(mktemp -d "${TMPDIR:-/tmp}/omakase-tvos.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
if [[ -z $snapshot ]]; then
    snapshot="$work/source.tar"
    git -C "$repo" archive --format=tar "$commit" tv-source scripts/build-tvos.sh > "$snapshot"
    source_sha256=$(digest "$snapshot")
fi
mkdir "$work/source"
tar -xf "$snapshot" -C "$work/source"
source_dir="$work/source/tv-source"

artifact=TVVLCKit-3.7.3-319ed2c0-79128878.tar.xz
vendor_sha256=b5f90c226ed54d9dc1c03901c60dc7749b74a53caace2c3047e4c0b7a063e46c
vendor_url="https://download.videolan.org/pub/cocoapods/prod/$artifact"
vendor_archive="$work/$artifact"
if [[ -n ${TVVLCKIT_ARCHIVE:-} ]]; then
    cp "$TVVLCKIT_ARCHIVE" "$vendor_archive"
else
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 240 --max-filesize 121449476 --output "$vendor_archive" "$vendor_url"
fi
[[ $(stat -f '%z' "$vendor_archive") == 121449476 &&
    $(digest "$vendor_archive") == "$vendor_sha256" ]] || fail "TVVLCKit archive size or SHA-256 mismatch."
tar -xf "$vendor_archive" -C "$work" TVVLCKit-binary/TVVLCKit.xcframework TVVLCKit-binary/COPYING.txt
mv "$work/TVVLCKit-binary" "$source_dir/Vendor"

xcrun swift test --package-path "$source_dir/QualificationCore" --scratch-path "$work/swift-test"

# The XCFramework resolves the device slice. Each app must use the import and
# embed that slice as a dynamic library at its declared install name.
for target in OmakasePlusTV OmakaseQualification; do
    xcodebuild -project "$source_dir/OmakaseQualification.xcodeproj" \
        -scheme "$target" -configuration Release -sdk appletvos -destination 'generic/platform=tvOS' \
        -derivedDataPath "$work/DerivedData" \
        ARCHS=arm64 CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO \
        OMAKASE_SERVER_ORIGIN= build

    app="$work/DerivedData/Build/Products/Release-appletvos/$target.app"
    framework="$app/Frameworks/TVVLCKit.framework/TVVLCKit"
    [[ -x "$app/$target" && -f "$framework" ]] || fail "$target app or embedded framework missing."
    [[ $(xcrun lipo -archs "$app/$target") == arm64 &&
        $(xcrun lipo -archs "$framework") == arm64 ]] || fail "$target or TVVLCKit is not arm64."
    app_links=$(xcrun otool -L "$app/$target")
    framework_id=$(xcrun otool -D "$framework")
    [[ $app_links == *'@rpath/TVVLCKit.framework/TVVLCKit '* &&
        $framework_id == *'@rpath/TVVLCKit.framework/TVVLCKit' ]] ||
        fail "$target does not dynamically link its embedded TVVLCKit."
    [[ ! -e "$app/embedded.mobileprovision" && ! -e "$app/_CodeSignature" ]] ||
        fail "$target unexpectedly contains provisioning or an app signature."
done

release_app="$work/DerivedData/Build/Products/Release-appletvos/OmakasePlusTV.app"
shelf="$release_app/PlugIns/OmakaseTopShelf.appex"
[[ -x "$shelf/OmakaseTopShelf" && $(xcrun lipo -archs "$shelf/OmakaseTopShelf") == arm64 ]] ||
    fail "Missing arm64 Top Shelf app extension."
python3 - "$release_app/Info.plist" "$shelf/Info.plist" <<'PY'
import plistlib
import sys
from pathlib import Path

app, shelf = (plistlib.loads(Path(path).read_bytes()) for path in sys.argv[1:])
assert shelf["CFBundleIdentifier"] == app["CFBundleIdentifier"] + ".TopShelf"
assert shelf["CFBundleVersion"] == app["CFBundleVersion"]
assert shelf["CFBundleShortVersionString"] == app["CFBundleShortVersionString"]
assert shelf["NSExtension"]["NSExtensionPointIdentifier"] == "com.apple.tv-top-shelf"
PY
# Set an optional default after the unsigned build so its value does not
# appear in xcodebuild command output or the receipt.
OMAKASE_SERVER_ORIGIN="$server_origin" python3 - "$release_app/Info.plist" <<'PY'
import os
from pathlib import Path
import plistlib
import sys
from urllib.parse import urlsplit

origin = os.environ.get("OMAKASE_SERVER_ORIGIN", "")
if origin:
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

    path = Path(sys.argv[1])
    info = plistlib.loads(path.read_bytes())
    info["OmakaseServerOrigin"] = origin
    path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
PY

mkdir "$work/Payload"
cp -R "$release_app" "$work/Payload/"
ipa="$work/OmakasePlusTV-unsigned.ipa"
(cd "$work" && /usr/bin/zip -qr "$ipa" Payload)
unzip -tq "$ipa"
ipa_sha256=$(digest "$ipa")

OMAKASE_SERVER_ORIGIN="$server_origin" python3 - "$release_app/Info.plist" "$work/build-receipt.json" "$commit" "$tree" "$source_sha256" \
    "$xcode_version" "$sdk_version" "$vendor_url" "$vendor_sha256" "$ipa_sha256" <<'PY'
import json
import os
import plistlib
import sys
from pathlib import Path

(plist_path, receipt_path, commit, tree, source_sha256, xcode, sdk,
 vendor_url, vendor_sha256, ipa_sha256) = sys.argv[1:]
info = plistlib.loads(Path(plist_path).read_bytes())
assert info["CFBundleIdentifier"] == "dev.omakase.plus.tv"
assert info["CFBundleSupportedPlatforms"] == ["AppleTVOS"]
assert info.get("OmakaseServerOrigin", "") == os.environ.get("OMAKASE_SERVER_ORIGIN", "")
receipt = {
    "source_commit": commit,
    "git_tree": tree,
    "native_source_tar_sha256": source_sha256,
    "xcode": xcode,
    "appletvos_sdk": sdk,
    "architecture": "arm64",
    "server_origin_configured": bool(info.get("OmakaseServerOrigin")),
    "portable_swift_tests": "passed",
    "device_targets_built_and_link_verified": ["OmakasePlusTV", "OmakaseQualification"],
    "tvvlckit": {"version": "3.7.3", "url": vendor_url, "sha256": vendor_sha256, "bytes": 121449476},
    "ipa": {
        "file": "OmakasePlusTV-unsigned.ipa",
        "bundle_id": info["CFBundleIdentifier"],
        "version": info["CFBundleShortVersionString"],
        "build": info["CFBundleVersion"],
        "sha256": ipa_sha256,
        "signed": False,
    },
}
Path(receipt_path).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
PY

cp "$ipa" "$output/"
cp "$source_dir/Vendor/COPYING.txt" "$output/TVVLCKit-COPYING.txt"
cp "$work/build-receipt.json" "$output/"
cat "$output/build-receipt.json"

