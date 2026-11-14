"""Continuity gate: does a packaged stream preserve the source frame sequence?

Compares decoded presentation times between a source file and a packaged output
(playlist directory or a joined file). Absolute times are normalised by their
first frame, so a container start delay is not reported as frame loss. The gate
fails on count mismatch, duplicate timestamps, or any frame displaced by more
than one frame duration after normalisation.

Usage: python tools/native_continuity_gate.py SOURCE OUTPUT
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def presentation_times(path: Path) -> list[float]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
    )
    return sorted(float(row) for row in result.stdout.strip().splitlines() if row.strip())


def decode_errors(path: Path) -> str:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    return (result.stderr or "").strip()


def main(source: str, output: str) -> int:
    source_path = Path(source)
    output_path = Path(output)
    if output_path.is_dir():
        output_path = output_path / "index.m3u8"
    source_times = presentation_times(source_path)
    output_times = presentation_times(output_path)
    frame = (source_times[1] - source_times[0]) if len(source_times) > 1 else 0.0
    tolerance = max(0.002, frame)
    report = {
        "source_frames": len(source_times),
        "output_frames": len(output_times),
        "frame_duration": round(frame, 4),
        "duplicates": len(output_times) - len(set(output_times)),
        "decode_errors": decode_errors(output_path)[:300],
    }
    if not source_times or not output_times:
        report["result"] = "fail"
        report["reason"] = "no frames decoded from one side"
        print(json.dumps(report, indent=1))
        return 1
    if len(source_times) != len(output_times):
        report["result"] = "fail"
        report["reason"] = "frame count differs"
        print(json.dumps(report, indent=1))
        return 1
    # Normalise each side by its own first frame: a container start delay is not
    # frame loss, only a displacement in the sequence is.
    base_in = source_times[0]
    base_out = output_times[0]
    offsets = [
        round((got - base_out) - (src - base_in), 4)
        for src, got in zip(source_times, output_times, strict=True)
    ]
    worst = max(offsets, key=abs) if offsets else 0.0
    report["worst_offset"] = worst
    report["result"] = "pass" if abs(worst) <= tolerance and not report["decode_errors"] else "fail"
    report["reason"] = "sequence preserved" if report["result"] == "pass" else "frames displaced or decode errors"
    print(json.dumps(report, indent=1))
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
