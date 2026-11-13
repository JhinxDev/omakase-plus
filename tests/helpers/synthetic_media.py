"""Build a tiny multi-subtitle MKV for real ffprobe verification."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def build_synthetic_mkv(directory: Path, *, name: str = "Fixture Show S01E01.mkv") -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    english = directory / "english.srt"
    french = directory / "french.srt"
    english.write_text(
        "1\n00:00:00,000 --> 00:00:00,800\nHello\n",
        encoding="utf-8",
    )
    french.write_text(
        "1\n00:00:00,000 --> 00:00:00,800\nBonjour\n",
        encoding="utf-8",
    )
    output = directory / name
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x180:r=24:d=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-i",
        str(english),
        "-i",
        str(french),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map",
        "2:0",
        "-map",
        "3:0",
        "-c:v",
        "mpeg4",
        "-c:a",
        "aac",
        "-c:s",
        "srt",
        "-metadata:s:a:0",
        "language=jpn",
        "-metadata:s:s:0",
        "language=eng",
        "-metadata:s:s:1",
        "language=fra",
        "-disposition:s:0",
        "default",
        "-y",
        str(output),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("ffmpeg could not build the synthetic media fixture")
    return output
