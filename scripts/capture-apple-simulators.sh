#!/usr/bin/env bash
# Capture authentic Apple TV 4K and iPhone Pro SwiftUI Simulator screenshots & video loops.
set -euo pipefail
[[ $(uname -s) == Darwin ]] || { echo 'Requires macOS and full Xcode.' >&2; exit 69; }
root=$(cd "$(dirname "$0")/.." && pwd)
output=${1:?Usage: bash capture-apple-simulators.sh OUTPUT_DIR [tvos|ios|both]}
platform=${2:-both}
case "$platform" in
  tvos|ios|both) ;;
  *) echo "Invalid platform '$platform'; expected tvos, ios, or both" >&2; exit 64 ;;
esac

mkdir -p "$output"
output=$(cd "$output" && pwd -P)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/omakase-showcase.XXXXXXXX")

tv_device=''
ios_device=''
fixture_pid=''

stop_recorder() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 0
  python3 - "$pid_file" <<'PY'
import os, signal, sys, time
from pathlib import Path

pid_path = Path(sys.argv[1])
if not pid_path.is_file():
    raise SystemExit(0)
pid = int(pid_path.read_text().strip() or "0")
pid_path.unlink(missing_ok=True)
if pid <= 0:
    raise SystemExit(0)

def alive(p: int) -> bool:
    try:
        os.kill(p, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

for sig, wait_sec in ((signal.SIGINT, 10.0), (signal.SIGTERM, 3.0), (signal.SIGKILL, 1.0)):
    if not alive(pid):
        break
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        break
    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline and alive(pid):
        time.sleep(0.2)
PY
}

start_recorder() {
  local device="$1"
  local out_mp4="$2"
  local pid_file="$3"
  python3 - "$device" "$out_mp4" "$pid_file" <<'PY'
import signal, subprocess, sys
from pathlib import Path

device, out_mp4, pid_file = sys.argv[1], sys.argv[2], sys.argv[3]
# Restore SIG_DFL for SIGINT in child so POSIX non-interactive shell background
# SIG_IGN inheritance never prevents simctl recordVideo from finalizing MP4 atoms.
proc = subprocess.Popen(
    ["xcrun", "simctl", "io", device, "recordVideo", "--codec=h264", "--force", out_mp4],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
)
Path(pid_file).write_text(str(proc.pid))
PY
}

cleanup() {
  stop_recorder "$scratch/tv-rec.pid" || true
  stop_recorder "$scratch/ios-rec.pid" || true
  if [[ -n "$fixture_pid" ]] && kill -0 "$fixture_pid" 2>/dev/null; then
    kill "$fixture_pid" 2>/dev/null || true
  fi
  for dev in "$tv_device" "$ios_device"; do
    if [[ -n "$dev" ]]; then
      xcrun simctl shutdown "$dev" >/dev/null 2>&1 || true
      xcrun simctl delete "$dev" >/dev/null 2>&1 || true
    fi
  done
  rm -rf -- "$scratch"
}
trap cleanup EXIT

export_named_screenshots() {
  local xcresult="$1"
  local dest_dir="$2"
  local tmp_export="$scratch/export-$$"
  rm -rf "$tmp_export"
  mkdir -p "$tmp_export"
  xcrun xcresulttool export attachments --path "$xcresult" --output-path "$tmp_export" >/dev/null 2>&1 || true
  python3 - "$tmp_export" "$dest_dir" <<'PY'
import json, shutil, sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
dst.mkdir(parents=True, exist_ok=True)
manifest_path = src / "manifest.json"
copied = 0
if manifest_path.is_file():
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    for test_entry in data if isinstance(data, list) else data.get("testResults", [data]):
        for att in test_entry.get("attachments", []):
            exported = att.get("exportedFileName")
            suggested = att.get("suggestedHumanReadableName", "")
            if not exported:
                continue
            cand = src / exported
            if cand.is_file():
                base = suggested.split("_")[0] if suggested else Path(exported).stem
                target_name = base if base.endswith(".png") else f"{base}.png"
                shutil.copy2(cand, dst / target_name)
                copied += 1
if copied == 0:
    for p in src.rglob("*.png"):
        shutil.copy2(p, dst / p.name)
        copied += 1
print(f"Exported {copied} PNG screenshots into {dst}")
PY
}

# 1. Start throwaway HTTPS showcase server
mkdir -p "$scratch/tls"
chmod 700 "$scratch/tls"
cat > "$scratch/tls/ca.conf" <<'EOF'
[req]
distinguished_name = name
x509_extensions = ca
[name]
CN = Omakase Showcase CA
[ca]
basicConstraints = critical,CA:true,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
EOF
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 2 \
  -config "$scratch/tls/ca.conf" -subj '/CN=Omakase Showcase CA' \
  -keyout "$scratch/tls/ca.key" -out "$scratch/tls/ca.crt" >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj '/CN=localhost' \
  -keyout "$scratch/tls/server.key" -out "$scratch/tls/server.csr" >/dev/null 2>&1
cat > "$scratch/tls/leaf.conf" <<'EOF'
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost,IP:127.0.0.1
EOF
openssl x509 -req -in "$scratch/tls/server.csr" \
  -CA "$scratch/tls/ca.crt" -CAkey "$scratch/tls/ca.key" -CAcreateserial \
  -out "$scratch/tls/server.crt" -days 2 -sha256 -extfile "$scratch/tls/leaf.conf" >/dev/null 2>&1

OMAKASE_FIXTURE_PORT=8854 python3 "$root/scripts/fixtures/simulator-showcase/showcase_fixture.py" \
  --cert "$scratch/tls/server.crt" --key "$scratch/tls/server.key" > "$output/fixture.log" 2>&1 &
fixture_pid=$!

for _ in $(seq 1 25); do
  if curl -sk --cacert "$scratch/tls/ca.crt" https://127.0.0.1:8854/qa/health | grep -q '"ok": true'; then
    break
  fi
  sleep 1
done

# 2. Resolve runtimes and create only requested simulators
xcrun simctl list runtimes --json > "$scratch/runtimes.json"

if [[ "$platform" == "tvos" || "$platform" == "both" ]]; then
  tv_runtime=$(python3 - "$scratch/runtimes.json" <<'PY'
import json, sys
r = [x for x in json.load(open(sys.argv[1]))['runtimes'] if x.get('isAvailable') and '.tvOS-' in x['identifier']]
print(max(r, key=lambda x: tuple(map(int, x['version'].split('.'))))['identifier'])
PY
)
  tv_device=$(xcrun simctl create "Omakase Showcase TV" "com.apple.CoreSimulator.SimDeviceType.Apple-TV-4K-3rd-generation-1080p" "$tv_runtime")
  xcrun simctl boot "$tv_device"
  xcrun simctl bootstatus "$tv_device" -b
  xcrun simctl keychain "$tv_device" add-root-cert "$scratch/tls/ca.crt"

  artifact=TVVLCKit-3.7.3-319ed2c0-79128878.tar.xz
  vendor_sha=b5f90c226ed54d9dc1c03901c60dc7749b74a53caace2c3047e4c0b7a063e46c
  if [[ ! -d "$root/tv-source/Vendor/TVVLCKit.xcframework" ]]; then
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
      --output "$scratch/$artifact" "https://download.videolan.org/pub/cocoapods/prod/$artifact"
    [[ $(shasum -a 256 "$scratch/$artifact" | awk '{print $1}') == "$vendor_sha" ]] || exit 65
    tar -xf "$scratch/$artifact" -C "$scratch" TVVLCKit-binary/TVVLCKit.xcframework TVVLCKit-binary/COPYING.txt
    mkdir -p "$root/tv-source/Vendor"
    cp -R "$scratch/TVVLCKit-binary/." "$root/tv-source/Vendor/"
  fi

  start_recorder "$tv_device" "$output/tvos-tour.mp4" "$scratch/tv-rec.pid"
  TEST_RUNNER_OMAKASE_SHOWCASE_ORIGIN="https://127.0.0.1:8854" \
  xcodebuild test -project "$root/tv-source/OmakaseQualification.xcodeproj" \
    -scheme OmakasePlusTV -configuration Debug -sdk appletvsimulator \
    -destination "platform=tvOS Simulator,id=$tv_device" \
    -derivedDataPath "$scratch/DerivedTV" -resultBundlePath "$scratch/TVShowcase.xcresult" \
    -parallel-testing-enabled NO \
    -only-testing:OmakasePlusTVUITests/ShowcaseCaptureTests \
    CODE_SIGN_IDENTITY=- OMAKASE_SERVER_ORIGIN= \
    2>&1 | tee "$output/tvos-xcodebuild.log"
  stop_recorder "$scratch/tv-rec.pid"
  export_named_screenshots "$scratch/TVShowcase.xcresult" "$output"
fi

if [[ "$platform" == "ios" || "$platform" == "both" ]]; then
  ios_choice=$(python3 - "$scratch/runtimes.json" <<'PY'
import json, sys
rows = [x for x in json.load(open(sys.argv[1]))['runtimes'] if x.get('isAvailable') and '.iOS-' in x['identifier']]
rt = max(rows, key=lambda x: tuple(map(int, x['version'].split('.'))))
devs = [d['identifier'] for d in rt.get('supportedDeviceTypes', []) if 'iPhone' in d['name']]
pro = [d for d in devs if 'Pro' in d and 'Max' not in d] or devs
print(pro[-1] + '\t' + rt['identifier'])
PY
)
  IFS=$'\t' read -r ios_dev_type ios_runtime <<< "$ios_choice"
  ios_device=$(xcrun simctl create "Omakase Showcase Phone" "$ios_dev_type" "$ios_runtime")
  xcrun simctl boot "$ios_device"
  xcrun simctl bootstatus "$ios_device" -b
  xcrun simctl keychain "$ios_device" add-root-cert "$scratch/tls/ca.crt"

  start_recorder "$ios_device" "$output/ios-tour.mp4" "$scratch/ios-rec.pid"
  TEST_RUNNER_OMAKASE_SHOWCASE_ORIGIN="https://127.0.0.1:8854" \
  xcodebuild test -project "$root/ios-source/OmakasePlusPhone.xcodeproj" \
    -scheme OmakasePlusPhone -configuration Debug \
    -destination "platform=iOS Simulator,id=$ios_device" \
    -derivedDataPath "$scratch/DerivedIOS" -resultBundlePath "$scratch/iOSShowcase.xcresult" \
    -parallel-testing-enabled NO \
    -only-testing:OmakasePlusPhoneUITests/PhoneShowcaseCaptureTests \
    CODE_SIGNING_ALLOWED=NO \
    2>&1 | tee "$output/ios-xcodebuild.log"
  stop_recorder "$scratch/ios-rec.pid"
  export_named_screenshots "$scratch/iOSShowcase.xcresult" "$output"
fi

ls -lh "$output"
