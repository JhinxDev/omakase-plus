"""Real ffprobe calling convention and bounded decoder contract; no provider I/O."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from omakase.plus.media_probe import probe_media_file
from omakase.plus.publication_probe import bounded_probe_runner, local_only_probe_runner
from omakase.plus.storage import configure_root, file_receipt


def test_real_local_runner_accepts_media_probe_utf8_options(tmp_path, monkeypatch):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("Existing ffmpeg and ffprobe required; no automatic installation")
    subtitle = tmp_path / "synthetic.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:00,900\nBonjour, test uniquement.\n", encoding="utf-8"
    )
    media = tmp_path / "Synthetic S01E01.mkv"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=10",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-i",
            str(subtitle),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:s:0",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-c:s",
            "srt",
            "-t",
            "1",
            "-metadata:s:a:0",
            "language=fra",
            "-metadata:s:s:0",
            "language=fra",
            "-disposition:s:0",
            "0",
            str(media),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=True,
    )
    commands = []
    real_popen = subprocess.Popen

    def record_spawn(command, **kwargs):
        commands.append(command)
        return real_popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record_spawn)
    root = configure_root(tmp_path, owner_id=1)
    before = file_receipt(root, media.name)
    result = probe_media_file(
        root,
        media.name,
        inventory_fingerprint=before.sha256,
        ffprobe_path=ffprobe,
        runner=local_only_probe_runner,
    )
    assert len(commands) == 1
    command = commands[0]
    assert command[command.index("-protocol_whitelist") + 1] == "file"
    assert command[command.index("-probesize") + 1] == "1048576"
    assert command[command.index("-analyzeduration") + 1] == "5000000"
    assert result.outcome != "error"
    assert not result.usable  # Legacy English gate remains unchanged.
    assert result.language_evidence is not None
    assert {track.language for track in result.language_evidence.tracks} == {"fr"}
    assert file_receipt(root, media.name) == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"encoding": "utf-16"},
        {"encoding": None},
        {"errors": "ignore"},
        {"errors": None},
        {"text": False},
        {"capture_output": False},
        {"check": True},
        {"timeout": 0},
        {"timeout": float("inf")},
        {"timeout": True},
    ],
)
def test_unsupported_decode_or_execution_options_fail_before_spawn(monkeypatch, kwargs):
    def forbidden(*args, **options):
        raise AssertionError("Unsupported options must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ValueError):
        bounded_probe_runner([sys.executable, "-c", "pass"], **kwargs)


def test_supported_replacement_is_applied_not_ignored():
    command = [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'\\xff')"]
    result = bounded_probe_runner(command, encoding="utf-8", errors="replace")
    assert result.stdout == "\ufffd"
    with pytest.raises(UnicodeDecodeError):
        bounded_probe_runner(command, encoding="utf-8", errors="strict")
