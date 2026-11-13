from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

CURATOR_ROOT = Path(__file__).resolve().parents[2] / "deploy" / "plex-curator"
sys.path.insert(0, str(CURATOR_ROOT))

from subtitle_support import (  # noqa: E402
    SubtitleAuditError,
    SubtitleProbe,
    SubtitleProbeCache,
    SubtitleTrack,
    parse_ffprobe,
    plan_english_embedded,
    probe_subtitles,
    validate_embedded_subtitle,
)


def _probe() -> SubtitleProbe:
    return SubtitleProbe(
        duration_ms=24 * 60 * 1000,
        tracks=(
            SubtitleTrack(index=2, codec="subrip", language="eng", default=True),
            SubtitleTrack(index=3, codec="ass", language="en", title="Signs & Songs"),
            SubtitleTrack(
                index=4,
                codec="subrip",
                language="english",
                title="English SDH",
                hearing_impaired=True,
            ),
            SubtitleTrack(index=5, codec="subrip", language="jpn"),
        ),
    )


def test_ffprobe_parser_keeps_supported_text_and_image_subtitle_streams():
    parsed = parse_ffprobe(
        {
            "programs": [],
            "stream_groups": [],
            "format": {"duration": "1465.920"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "subrip",
                    "tags": {"language": "eng"},
                    "disposition": {"default": 1},
                },
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "eng"},
                },
            ],
        }
    )

    assert parsed.duration_ms == 1_465_920
    assert parsed.tracks == (
        SubtitleTrack(index=2, codec="subrip", language="eng", default=True),
        SubtitleTrack(index=3, codec="hdmv_pgs_subtitle", language="eng"),
    )


def test_ffprobe_parser_rejects_nonempty_unrequested_program_metadata():
    with pytest.raises(SubtitleAuditError, match="unknown shape"):
        parse_ffprobe(
            {
                "format": {"duration": "60"},
                "streams": [],
                "programs": [{"program_id": 1}],
            }
        )


def test_probe_uses_bounded_non_shell_command(tmp_path: Path):
    source = tmp_path / "Episode 01.mkv"
    source.write_bytes(b"fixture")
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        assert kwargs == {"check": True, "capture_output": True, "timeout": 60}
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "format": {"duration": "60"},
                    "streams": [
                        {
                            "index": 2,
                            "codec_type": "subtitle",
                            "codec_name": "subrip",
                            "tags": {"language": "eng"},
                        }
                    ],
                }
            ).encode(),
        )

    result = probe_subtitles(source, runner=runner)

    assert result.duration_ms == 60_000
    assert calls[0][-1] == str(source)
    assert calls[0][0] == "/usr/bin/ffprobe"


@pytest.mark.parametrize(
    ("subtitle_text", "expected_language"),
    [
        (
            """1
00:00:01,000 --> 00:00:03,000
What are you doing here? I thought you were with the others.

2
00:00:04,000 --> 00:00:07,000
We have to get out before they know that this is the place.

3
00:00:08,000 --> 00:00:11,000
You can tell me what happened, but do not leave me here.

4
00:00:12,000 --> 00:00:15,000
There is no way I will let you go on your own.
""",
            "eng-content",
        ),
        (
            """1
00:00:01,000 --> 00:00:03,000
<font face="Yu Gothic"><b>あなたはここで何をしているの？</b></font>

2
00:00:04,000 --> 00:00:07,000
<font face="Yu Gothic"><b>みんなと一緒にいると思った。</b></font>
""",
            "und",
        ),
    ],
)
def test_probe_classifies_untagged_text_subtitle_from_bounded_content(
    tmp_path: Path,
    subtitle_text: str,
    expected_language: str,
):
    source = tmp_path / "Episode 11.mkv"
    source.write_bytes(b"fixture")
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        assert kwargs == {"check": True, "capture_output": True, "timeout": 60}
        if command[0] == "/usr/bin/ffprobe":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "format": {"duration": "60"},
                        "streams": [
                            {
                                "index": 2,
                                "codec_type": "subtitle",
                                "codec_name": "subrip",
                                "tags": {"title": "SRT"},
                            }
                        ],
                    }
                ).encode(),
            )
        assert command[0] == "/usr/bin/ffmpeg"
        return subprocess.CompletedProcess(command, 0, stdout=subtitle_text.encode())

    result = probe_subtitles(source, runner=runner)

    assert result.tracks[0].language == expected_language
    assert calls[1][calls[1].index("-map") + 1] == "0:2"
    assert int(calls[1][calls[1].index("-fs") + 1]) <= 256 * 1024


def test_probe_cache_reuses_unchanged_file_and_invalidates_on_stat_change(tmp_path: Path):
    source = tmp_path / "Episode 01.mkv"
    source.write_bytes(b"first")
    cache_path = tmp_path / "state" / "subtitle-cache.json"
    calls: list[int] = []

    def probe(_):
        calls.append(1)
        return _probe()

    cache = SubtitleProbeCache(cache_path, probe=probe)
    assert cache.probe(source) == _probe()
    cache.flush()
    assert len(calls) == 1

    reloaded = SubtitleProbeCache(cache_path, probe=probe)
    assert reloaded.probe(source) == _probe()
    assert len(calls) == 1

    source.write_bytes(b"changed-size")
    assert reloaded.probe(source) == _probe()
    assert len(calls) == 2


def test_embedded_plan_binds_standard_forced_and_sdh_options():
    embedded = plan_english_embedded(
        source=PurePosixPath("Batch/Episode 01.mkv"),
        probe=_probe(),
    )

    assert [binding.role for binding in embedded] == ["standard", "forced", "sdh"]
    assert [binding.stream_index for binding in embedded] == [2, 3, 4]
    assert [binding.source_codec for binding in embedded] == ["subrip", "ass", "subrip"]


def test_embedded_plan_requires_english_subtitle():
    with pytest.raises(SubtitleAuditError, match="no English"):
        plan_english_embedded(
            source=PurePosixPath("Batch/Episode 01.mkv"),
            probe=SubtitleProbe(
                duration_ms=60_000,
                tracks=(SubtitleTrack(index=2, codec="subrip", language="jpn"),),
            ),
        )


def test_embedded_plan_rejects_forced_only_english_track():
    with pytest.raises(SubtitleAuditError, match="full-dialogue"):
        plan_english_embedded(
            source=PurePosixPath("Batch/Episode 01.mkv"),
            probe=SubtitleProbe(
                duration_ms=60_000,
                tracks=(
                    SubtitleTrack(
                        index=2,
                        codec="subrip",
                        language="eng",
                        title="Signs & Songs",
                    ),
                ),
            ),
        )


def test_image_subtitle_is_bound_for_live_revalidation():
    probe = SubtitleProbe(
        duration_ms=60_000,
        tracks=(
            SubtitleTrack(
                index=6,
                codec="hdmv_pgs_subtitle",
                language="eng",
                title="English",
                default=True,
            ),
            SubtitleTrack(
                index=7,
                codec="hdmv_pgs_subtitle",
                language="eng",
                title="English (Foreign)",
                forced=True,
            ),
        ),
    )
    source = PurePosixPath("Batch/Movie.mkv")

    embedded = plan_english_embedded(source=source, probe=probe)

    assert [binding.role for binding in embedded] == ["standard", "forced"]
    assert [binding.stream_index for binding in embedded] == [6, 7]
    validate_embedded_subtitle(embedded[0], probe)

    changed = SubtitleProbe(
        duration_ms=60_000,
        tracks=(
            SubtitleTrack(
                index=6,
                codec="hdmv_pgs_subtitle",
                language="jpn",
                title="English",
                default=True,
            ),
        ),
    )
    with pytest.raises(SubtitleAuditError, match="changed"):
        validate_embedded_subtitle(embedded[0], changed)
