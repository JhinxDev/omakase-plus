"""Seek gate: can a packaged stream start cleanly at a requested position?

Remuxes from START with a single stream-copy FFmpeg session, then answers the
questions a start has to satisfy instead of only reporting numbers:

* the source has frames at or after the requested position, and the output covers
  that interval (coverage, not just "some frames"),
* the start lands on the containing keyframe, so the lead-in is bounded by one
  GOP and no larger,
* presentation times never duplicate and never jump by more than two frames,
* the output decodes without a single decoder error,
* the FFmpeg session itself exited 0.

A stream-copy seek lands on the keyframe containing the request, so the lead-in is
reported rather than hidden: the pass condition is clean media with a bounded
lead-in, not an exactly equal start. Exits non-zero on any failure.

Usage: python tools/native_seek_gate.py SOURCE START_SECONDS [WORKDIR] [--gop 6]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

FRAME_GAP_LIMIT = 2.0

# ffprobe and ffmpeg walk every frame of a real title, which takes minutes on a
# large source, so the deadline is generous but finite: a session still running
# after this long is a failure to report, never "nothing was produced".
MEDIA_TIMEOUT_SECONDS = 1800


def _tail(stream, limit: int = 400) -> str:
    if stream is None:
        return "<none>"
    text = stream.decode("utf-8", "replace") if isinstance(stream, bytes) else str(stream)
    return text[-limit:].strip() or "<empty>"


def _deadline_error(timeout: float, expired: subprocess.TimeoutExpired) -> RuntimeError:
    """A killed session owes the command, the deadline and what it captured."""
    return RuntimeError(
        f"external process exceeded its {timeout:g}s deadline: {expired.cmd!r}\n"
        f"captured stdout: {_tail(expired.stdout)}\n"
        f"captured stderr: {_tail(expired.stderr)}"
    )


def frames(path: Path) -> list[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=MEDIA_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as expired:
        raise _deadline_error(MEDIA_TIMEOUT_SECONDS, expired) from expired
    if result.returncode != 0:
        # An unchecked failure returned an empty list, which the caller read as
        # "the source has no frames" or "nothing was produced": never a defect.
        raise RuntimeError(f"ffprobe failed with exit {result.returncode}: {_tail(result.stderr)}")
    return sorted(float(row) for row in result.stdout.strip().splitlines() if row.strip())


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


def main(source: str, start: str, workdir: str = "", gop: float = 6.0) -> int:
    source_path = Path(source)
    start_at = float(start)
    base = Path(workdir) if workdir else source_path.parent / f"seek-{start_at:g}"
    base.mkdir(parents=True, exist_ok=True)
    for item in base.iterdir():
        if item.is_file():
            item.unlink()
    init = base / "init.mp4"
    try:
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
            timeout=MEDIA_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as expired:
        # The exit status and stderr below are this gate's own report, so the
        # deadline is the one failure that cannot be reported as a number.
        raise _deadline_error(MEDIA_TIMEOUT_SECONDS, expired) from expired
    joined = base / "joined.mp4"
    joined.write_bytes(
        b"".join(part.read_bytes() for part in [init, *sorted(base.glob("seg*.m4s"))])
        if init.exists()
        else b""
    )
    produced = frames(joined)
    source_frames = frames(source_path)
    expected = [value for value in source_frames if value >= start_at]
    frame = (source_frames[1] - source_frames[0]) if len(source_frames) > 1 else 0.04
    gaps = [round(second - first, 4) for first, second in zip(produced, produced[1:])]
    lead_in = max(0, len(produced) - len(expected))
    worst_gap = max(gaps) if gaps else None
    errors = decode_errors(joined) if produced else ""
    report = {
        "seek_seconds": start_at,
        "returncode": run.returncode,
        "source_frames": len(source_frames),
        "expected_from_offset": len(expected),
        "produced": len(produced),
        "lead_in_frames": lead_in,
        "lead_in_limit": int(round(gop / frame)) + 2,
        "duplicates": len(produced) - len(set(produced)),
        "max_gap": worst_gap,
        "gap_limit": round(frame * FRAME_GAP_LIMIT, 4),
        "gaps_over_two_frames": sum(1 for gap in gaps if gap > frame * FRAME_GAP_LIMIT),
        "decode_errors": errors[:300],
        "error": (run.stderr or "").strip()[:160],
    }
    reasons = []
    if run.returncode != 0 or report["error"]:
        reasons.append("ffmpeg session failed")
    if not expected:
        reasons.append("the source has no frames at or after the requested position")
    if not produced:
        reasons.append("nothing was produced")
    if report["duplicates"]:
        reasons.append("duplicated presentation times")
    if report["gaps_over_two_frames"]:
        reasons.append("a presentation gap larger than two frames")
    if errors:
        reasons.append("decoder errors")
    if len(produced) + FRAME_GAP_LIMIT < len(expected):
        reasons.append(
            f"coverage short by {len(expected) - len(produced)} frames of the source interval"
        )
    if lead_in > report["lead_in_limit"]:
        reasons.append(f"lead-in {lead_in} frames exceeds one GOP")
    report["result"] = "fail" if reasons else "pass"
    report["reason"] = "; ".join(reasons) if reasons else "clean media, bounded lead-in"
    print(json.dumps(report, indent=1))
    return 0 if not reasons else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("start")
    parser.add_argument("workdir", nargs="?", default="")
    parser.add_argument("--gop", type=float, default=6.0)
    args = parser.parse_args()
    raise SystemExit(main(args.source, args.start, args.workdir, args.gop))
