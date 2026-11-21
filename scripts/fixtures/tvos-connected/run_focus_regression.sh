#!/usr/bin/env bash
# macOS only. Creates and deletes its own simulator; touches no existing devices.
set -euo pipefail
[[ $(uname -s) == Darwin ]] || { echo 'Requires macOS and full Xcode.' >&2; exit 69; }
root=$(cd "$(dirname "$0")/../../.." && pwd)
output=${1:?Usage: bash run_focus_regression.sh EMPTY_OUTPUT_DIR}
mkdir -p "$output"
output=$(cd "$output" && pwd -P)
[[ -z $(find "$output" -mindepth 1 -maxdepth 1 -print -quit) ]] || { echo 'Use an empty output directory.' >&2; exit 64; }
scratch=$(mktemp -d "${TMPDIR:-/tmp}/omakase-focus.XXXXXXXX")
device=
cleanup() {
  if [[ -f "$scratch/fixture/server.pid" ]]; then kill "$(cat "$scratch/fixture/server.pid")" 2>/dev/null || true; fi
  if [[ -n "$device" ]]; then
    xcrun simctl shutdown "$device" >/dev/null 2>&1 || true
    xcrun simctl delete "$device" >/dev/null 2>&1 || true
  fi
  # Copy diagnostics only. Never retain throwaway keys, certificates or pairing tokens.
  if [[ -f "$scratch/fixture/server.log" ]]; then cp "$scratch/fixture/server.log" "$output/fixture.log"; fi
  rm -rf -- "$scratch"
}
trap cleanup EXIT
xcodebuild -version > "$output/xcode.txt"
xcrun simctl list runtimes --json > "$output/runtimes.json"
xcrun simctl list devicetypes --json > "$output/device-types.json"
# TVOS_RUNTIME pins a replay to the recorded exact identifier. Without it, select
# the newest available tvOS runtime and record it; never silently download another.
runtime=$(python3 - "$output/runtimes.json" <<'PY'
import json, os, sys
runtimes = [r for r in json.load(open(sys.argv[1]))['runtimes']
            if r.get('isAvailable') and '.tvOS-' in r['identifier']]
pin = os.environ.get('TVOS_RUNTIME', '')
if pin:
    runtimes = [r for r in runtimes if r['identifier'] == pin]
if not runtimes:
    sys.exit('Requested tvOS runtime unavailable. See runtimes.json; provision it explicitly.')
print(max(runtimes, key=lambda r: tuple(map(int, r['version'].split('.'))))['identifier'])
PY
)
device_type=${TVOS_DEVICE_TYPE:-com.apple.CoreSimulator.SimDeviceType.Apple-TV-4K-3rd-generation}
device=$(xcrun simctl create "Omakase focus ${RANDOM}" "$device_type" "$runtime")
xcrun simctl boot "$device"
xcrun simctl bootstatus "$device" -b
artifact=TVVLCKit-3.7.3-319ed2c0-79128878.tar.xz
vendor_sha=b5f90c226ed54d9dc1c03901c60dc7749b74a53caace2c3047e4c0b7a063e46c
if [[ ! -d "$root/tv-source/Vendor/TVVLCKit.xcframework" ]]; then
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 15 --max-time 240 --max-filesize 121449476 \
    --output "$scratch/$artifact" "https://download.videolan.org/pub/cocoapods/prod/$artifact"
  [[ $(stat -f '%z' "$scratch/$artifact") == 121449476 &&
     $(shasum -a 256 "$scratch/$artifact" | awk '{print $1}') == "$vendor_sha" ]] || exit 65
  tar -xf "$scratch/$artifact" -C "$scratch" TVVLCKit-binary/TVVLCKit.xcframework TVVLCKit-binary/COPYING.txt
  mkdir -p "$root/tv-source/Vendor"
  cp -R "$scratch/TVVLCKit-binary/." "$root/tv-source/Vendor/"
else
  echo 'Existing Vendor found; use a fresh checkout to prove the pinned dependency.' >&2
  exit 65
fi
python3 "$root/scripts/fixtures/tvos-connected/check_xcodeproj_structure.py" > "$output/project-check.txt"
bash "$root/scripts/fixtures/tvos-connected/start_connected_fixture.sh" "$device" "$scratch/fixture" > "$output/fixture-start.txt"
git -C "$root" rev-parse HEAD > "$output/source-commit.txt"
git -C "$root" diff --binary HEAD > "$output/source.diff"
printf '%s\n' "$runtime" > "$output/runtime-id.txt"
printf '%s\n' "$device_type" > "$output/device-type.txt"
printf '%s\n' "$vendor_sha" > "$output/vendor-sha256.txt"
# Keep pipefail: a failed pixel assertion, launch or focus test must fail the job.
xcodebuild test -project "$root/tv-source/OmakaseQualification.xcodeproj" \
  -scheme OmakasePlusTV -configuration Debug -sdk appletvsimulator \
  -destination "platform=tvOS Simulator,id=$device" \
  -derivedDataPath "$scratch/DerivedData" -resultBundlePath "$output/Focus.xcresult" \
  -parallel-testing-enabled NO \
  -only-testing:OmakasePlusTVUITests/RemoteFocusRenderingTests \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO OMAKASE_SERVER_ORIGIN= \
  2>&1 | tee "$output/xcodebuild.log"
