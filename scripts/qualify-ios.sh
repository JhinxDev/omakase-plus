#!/bin/bash
# Synthetic-only qualification: full Xcode and an installed iPhone runtime required.
set -euo pipefail
if [[ $# != 1 || "$(uname -s)" != Darwin ]]; then
  echo 'Usage on macOS: bash scripts/qualify-ios.sh NEW_EVIDENCE_DIRECTORY' >&2
  exit 2
fi
umask 077
mkdir -m 700 -- "$1"
evidence="$(cd -- "$1" && pwd -P)"
cd -- "$(dirname "$0")/.."
root="$PWD"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/omakase-ios.XXXXXXXX")"
device=''
cleanup() {
  local status=$?
  if [[ -n "$device" ]]; then
    xcrun simctl shutdown "$device" >/dev/null 2>&1 || true
    xcrun simctl delete "$device" || status=1
  fi
  # Remove only this process's private scratch; keep all test evidence.
  if [[ -d "$scratch" && "$(basename "$scratch")" == omakase-ios.* ]]; then
    rm -rf -- "$scratch" || status=1
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
{
  git rev-parse HEAD
  git status --porcelain --untracked-files=no
  xcodebuild -version
  swift --version
} > "$evidence/toolchain.txt"
xcrun simctl list runtimes -j > "$scratch/runtimes.json"
# Pin an installed runtime via OMAKASE_IOS_RUNTIME; otherwise choose the newest.
template="$(python3 - "$scratch/runtimes.json" "${OMAKASE_IOS_RUNTIME:-}" <<'PY'
import json
import sys
rows = json.load(open(sys.argv[1]))['runtimes']
choices = []
for row in rows:
    if not row.get('isAvailable') or '.iOS-' not in row['identifier']:
        continue
    version = row['version']
    if sys.argv[2] and version != sys.argv[2]:
        continue
    for device in row.get('supportedDeviceTypes', []):
        if device['name'].startswith('iPhone'):
            choices.append((tuple(map(int, version.split('.'))), device['identifier'], row['identifier']))
if not choices:
    sys.exit('No matching installed iPhone Simulator runtime. Install one before retrying.')
_, kind, runtime = max(choices)
print(kind + '\t' + runtime)
PY
)"
IFS=$'\t' read -r device_type runtime <<< "$template"
printf 'Device type: %s\nRuntime: %s\n' "$device_type" "$runtime" > "$evidence/simulator.txt"
device="$(xcrun simctl create "Omakase Offline Qualification" "$device_type" "$runtime")"
[[ "$device" =~ ^[[:xdigit:]]{8}(-[[:xdigit:]]{4}){3}-[[:xdigit:]]{12}$ ]]
xcrun simctl bootstatus "$device" -b > "$evidence/boot.log" 2>&1
# Build all current shared-core tests for the host before compiling the iOS shell.
swift test --package-path "$root/tv-source/QualificationCore" --scratch-path "$scratch/core" \
  2>&1 | tee "$evidence/core-tests.log"
xcodebuild test \
  -project "$root/ios-source/OmakasePlusPhone.xcodeproj" \
  -scheme OmakasePlusPhone -configuration Debug \
  -destination "platform=iOS Simulator,id=$device" \
  -derivedDataPath "$scratch/derived" \
  -resultBundlePath "$evidence/PrototypeJourney.xcresult" \
  -parallel-testing-enabled NO CODE_SIGNING_ALLOWED=NO \
  2>&1 | tee "$evidence/ios-tests.log"
# Only synthetic UI is present, but do not export broad host inventories or caches.
xcrun xcresulttool get test-results summary --path "$evidence/PrototypeJourney.xcresult" \
  > "$evidence/test-summary.json"
