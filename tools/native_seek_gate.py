"""Seek gate: can a packaged stream start cleanly at a requested position?

Remuxes from START with a single stream-copy FFmpeg session, then reports how
many frames were produced, whether timestamps duplicate, and the largest gap.
A stream-copy seek lands on the keyframe containing the request, so the report
also states the lead-in (produced minus expected frames) instead of pretending
the start was exact. Clean media with bounded lead-in is the pass condition.

Usage: python tools/native_seek_gate.py SOURCE START_SECONDS [WORKDIR]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def frames(path: Path) -> list[float]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
    )
    return sorted(float(row) for row in result.stdout.strip().splitlines() if row.strip())


def main(source: str, start: str, workdir: str = "") -> int:
    source_path = Path(source)
    start_at = float(start)
    base = Path(workdir) if workdir else source_path.parent / f"seek-{start_at:g}"
    base.mkdir(parents=True, exist_ok=True)
    for item in base.iterdir():
        item.unlink()
    init = base / "init.mp4"
    run = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-ss", str(start_at), "-i", str(source_path),
            "-map", "0:v:0", "-c", "copy", "-tag:v", "hvc1",
            "-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod",
            "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", str(init),
            "-hls_segment_filename", str(base / "seg%04d.m4s"), str(base / "index.m3u8"),
        ],
        capture_output=True,
        text=True,
    )
    joined = base / "joined.mp4"
    joined.write_bytes(
        b"".join(part.read_bytes() for part in [init, *sorted(base.glob("seg*.m4s"))])
        if init.exists()
        else b""
    )
    produced = frames(joined)
    expected = [value for value in frames(source_path) if value >= start_at]
    gaps = [round(second - first, 4) for first, second in zip(produced, produced[1:])]
    report = {
        "seek_seconds": start_at,
        "returncode": run.returncode,
        "expected_from_offset": len(expected),
        "produced": len(produced),
        "lead_in_frames": max(0, len(produced) - len(expected)),
        "duplicates": len(produced) - len(set(produced)),
        "max_gap": max(gaps) if gaps else None,
        "gaps_over_two_frames": sum(1 for gap in gaps if gap > 0.084),
        "error": (run.stderr or "").strip()[:160],
    }
    report["result"] = (
        "pass"
        if run.returncode == 0
        and produced
        and not report["duplicates"]
        and report["gaps_over_two_frames"] == 0
        and not report["error"]
        else "fail"
    )
    print(json.dumps(report, indent=1))
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""))
