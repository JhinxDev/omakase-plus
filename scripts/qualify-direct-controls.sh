#!/usr/bin/env bash
# Run the production direct-control regression on an existing tvOS Simulator.
set -euo pipefail

[[ $# == 2 ]] || { echo "Usage: $0 SIMULATOR_UDID OUTPUT_DIR" >&2; exit 2; }
simulator=$1
output=$2
[[ $simulator =~ ^[0-9A-Fa-f-]{36}$ ]] || { echo "Invalid Simulator UDID." >&2; exit 2; }
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/omakase-controls.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$output"
output=$(cd "$output" && pwd -P)
[[ ! -e "$output/direct-controls.xcresult" ]] || { echo "Select a fresh output directory." >&2; exit 2; }
mkdir "$work/tv-source"
rsync -a --exclude Vendor --exclude .build --exclude DerivedData "$repo/tv-source/" "$work/tv-source/"
source_dir="$work/tv-source"

fixtures="$repo/scripts/fixtures/tvos-direct-controls"
cp "$fixtures/LibraryApp.swift" "$source_dir/OmakasePlusTV/"
cp "$fixtures/PlaybackSession.swift" "$fixtures/VLCPlayerEngine.swift" "$source_dir/OmakaseQualification/"
python3 - "$source_dir/OmakaseQualification.xcodeproj/project.pbxproj" <<'PY'
from pathlib import Path
import sys

project = Path(sys.argv[1])
source = project.read_text()
# The production PlayerScreen stays byte-for-byte untouched. The scratch app
# has no VLC decoding, server, pairing or signed-device dependencies.
for anchor, replacement in (
    ("000000000000000000000054,000000000000000000000086,", "000000000000000000000054,"),
    ("files = (000000000000000000000088,);", "files = ();"),
):
    if source.count(anchor) != 1:
        sys.exit("The tvOS fixture project binding changed.")
    source = source.replace(anchor, replacement)
project.write_text(source)
PY

python3 - "$source_dir" "$simulator" "$work" "$output" <<'PY'
from pathlib import Path
import subprocess
import sys

source, simulator, work, output = sys.argv[1:]
command = [
    "xcodebuild", "test", "-project", str(Path(source) / "OmakaseQualification.xcodeproj"),
    "-scheme", "OmakasePlusTV", "-configuration", "Debug",
    "-destination", "platform=tvOS Simulator,id=" + simulator, "-destination-timeout", "45",
    "-parallel-testing-enabled", "NO", "-derivedDataPath", str(Path(work) / "DerivedData"),
    "-resultBundlePath", str(Path(output) / "direct-controls.xcresult"),
    "-only-testing:OmakasePlusTVUITests/DirectControlsLayoutTests",
    "EXCLUDED_SOURCE_FILE_NAMES=NetworkLibrary.swift LiveLibraryScreen.swift TVProfiles.swift AVPlayerEngine.swift",
    "OMAKASE_SERVER_ORIGIN=", "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO",
]
try:
    result = subprocess.run(command, timeout=300)
except subprocess.TimeoutExpired:
    sys.exit("Direct-controls qualification exceeded its 300-second build/test limit.")
sys.exit(result.returncode)
PY
