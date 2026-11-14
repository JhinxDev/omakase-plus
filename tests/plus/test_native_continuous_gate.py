"""The architecture gates as tests, so the qualification cannot drift from the suite."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.plus.test_native_hls import _build_fixture


def _frames(path: Path) -> list[float]:
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
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return sorted(
        float(row.strip().rstrip(",")) for row in result.stdout.strip().splitlines() if row.strip()
    )


def _continuous_remux(source: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    for item in out.iterdir():
        item.unlink()
    init = out / "init.mp4"
    run = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-tag:v",
            "hvc1",
            "-f",
            "hls",
            "-hls_time",
            "6",
            "-hls_playlist_type",
            "vod",
            "-hls_segment_type",
            "fmp4",
            "-hls_fmp4_init_filename",
            str(init),
            "-hls_segment_filename",
            str(out / "seg%04d.m4s"),
            str(out / "index.m3u8"),
        ],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr[-400:]
    assert init.exists()
    return out / "index.m3u8"


def test_continuous_remux_preserves_every_source_frame(tmp_path):
    source = _build_fixture(tmp_path)
    playlist = _continuous_remux(source, tmp_path / "hls")
    joined = tmp_path / "joined.mp4"
    parts = [playlist.parent / "init.mp4", *sorted(playlist.parent.glob("seg*.m4s"))]
    joined.write_bytes(b"".join(part.read_bytes() for part in parts))
    source_times = _frames(source)
    output_times = _frames(joined)
    assert len(output_times) == len(source_times), "continuous remux must not lose frames"
    assert len(output_times) == len(set(output_times)), "no duplicated presentation times"
    worst = max(
        abs((got - output_times[0]) - (src - source_times[0]))
        for src, got in zip(source_times, output_times, strict=True)
    )
    frame = source_times[1] - source_times[0]
    assert worst <= frame * 1.01, f"frames displaced by {worst:.3f}s"
