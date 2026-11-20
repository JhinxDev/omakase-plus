"""Continuity gate: does a packaged stream preserve the source frame sequence?

Compares decoded presentation times *and* decoded picture hashes between a source
file and a packaged output (playlist directory or a joined file). Absolute times
are normalised by their first frame, so a container start delay is not reported
as frame loss. The gate fails on count mismatch, duplicate timestamps, any frame
displaced by more than one frame duration, any decoder error, or any picture that
does not hash to the source's picture at that position: timestamps alone cannot
show that the right pictures were kept in the right order.

Usage: python tools/native_continuity_gate.py SOURCE OUTPUT
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# ffprobe and ffmpeg decode every frame of a real title, which takes minutes on a
# large source, so the deadline is generous but finite: a probe still running after
# this long is a failure to report, never a short sequence to blame on the media.
MEDIA_TIMEOUT_SECONDS = 1800


def _tail(stream, limit: int = 400) -> str:
    if stream is None:
        return "<none>"
    text = stream.decode("utf-8", "replace") if isinstance(stream, bytes) else str(stream)
    return text[-limit:].strip() or "<empty>"


def _deadline_error(timeout: float, expired: subprocess.TimeoutExpired) -> RuntimeError:
    """A killed probe owes the command, the deadline and what it captured."""
    return RuntimeError(
        f"external process exceeded its {timeout:g}s deadline: {expired.cmd!r}\n"
        f"captured stdout: {_tail(expired.stdout)}\n"
        f"captured stderr: {_tail(expired.stderr)}"
    )


def presentation_times(path: Path) -> list[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "frame=pts_time",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=MEDIA_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as expired:
        raise _deadline_error(MEDIA_TIMEOUT_SECONDS, expired) from expired
    if result.returncode != 0:
        # An unchecked failure returned an empty list, which the caller then
        # reported as missing frames: a broken probe must not look like a defect.
        raise RuntimeError(f"ffprobe failed with exit {result.returncode}: {_tail(result.stderr)}")
    return sorted(float(row) for row in result.stdout.strip().splitlines() if row.strip())


def frame_hashes(path: Path) -> list[str]:
    """Decoded picture hashes, in presentation order, from ffmpeg's framemd5."""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-f",
                "framemd5",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=MEDIA_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as expired:
        raise _deadline_error(MEDIA_TIMEOUT_SECONDS, expired) from expired
    if result.returncode != 0:
        # A partial or empty hash list read as "pictures differ from the source".
        raise RuntimeError(f"ffmpeg failed with exit {result.returncode}: {_tail(result.stderr)}")
    hashes = []
    for row in result.stdout.splitlines():
        line = row.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 6:
            continue
        hashes.append(parts[-1].strip())
    return hashes


def decode_errors(path: Path) -> str:
    # ffmpeg exits non-zero exactly when it has decoder errors to report, and that
    # stderr is this gate's own reason, so the exit status stays a reported fact
    # rather than becoming an exception; only the deadline is enforced here.
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=MEDIA_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as expired:
        raise _deadline_error(MEDIA_TIMEOUT_SECONDS, expired) from expired
    return (result.stderr or "").strip()


def main(source: str, output: str) -> int:
    source_path = Path(source)
    output_path = Path(output)
    if output_path.is_dir():
        output_path = output_path / "index.m3u8"
    source_times = presentation_times(source_path)
    output_times = presentation_times(output_path)
    source_hashes = frame_hashes(source_path)
    output_hashes = frame_hashes(output_path)
    frame = (source_times[1] - source_times[0]) if len(source_times) > 1 else 0.0
    tolerance = max(0.002, frame)
    mismatched = [
        index
        for index, (want, got) in enumerate(zip(source_hashes, output_hashes, strict=False))
        if want != got
    ]
    report = {
        "source_frames": len(source_times),
        "output_frames": len(output_times),
        "frame_duration": round(frame, 4),
        "duplicates": len(output_times) - len(set(output_times)),
        "source_hashes": len(source_hashes),
        "output_hashes": len(output_hashes),
        "picture_mismatches": len(mismatched),
        "first_mismatch": mismatched[0] if mismatched else None,
        "decode_errors": decode_errors(output_path)[:300],
    }
    reasons = []
    if not source_times or not output_times:
        report["result"] = "fail"
        report["reason"] = "no frames decoded from one side"
        print(json.dumps(report, indent=1))
        return 1
    if len(source_times) != len(output_times):
        reasons.append("frame count differs")
    if report["duplicates"]:
        reasons.append("duplicated presentation times")
    if report["decode_errors"]:
        reasons.append("decoder errors")
    if len(source_hashes) != len(output_hashes):
        reasons.append("decoded picture count differs")
    if mismatched:
        reasons.append(f"{len(mismatched)} pictures differ from the source")
    # Normalise each side by its own first frame: a container start delay is not
    # frame loss, only a displacement in the sequence is.
    base_in = source_times[0]
    base_out = output_times[0]
    offsets = [
        round((got - base_out) - (src - base_in), 4)
        for src, got in zip(source_times, output_times, strict=False)
    ]
    worst = max(offsets, key=abs) if offsets else 0.0
    report["worst_offset"] = worst
    if abs(worst) > tolerance:
        reasons.append("frames displaced along the timeline")
    report["result"] = "fail" if reasons else "pass"
    report["reason"] = "; ".join(reasons) if reasons else "sequence and pictures preserved"
    print(json.dumps(report, indent=1))
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
