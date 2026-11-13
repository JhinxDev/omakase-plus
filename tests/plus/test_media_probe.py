"""Bounded media and subtitle verification tests."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from omakase.plus.media_probe import ExternalSubtitle, probe_media_file
from omakase.plus.storage import StorageSafetyError, configure_root, file_receipt
from tests.helpers.synthetic_media import build_synthetic_mkv


def _runner(streams):
    def run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": streams}), stderr="")

    return run


def test_probe_preserves_all_embedded_subtitles_and_range_receipts(tmp_path):
    media = tmp_path / "Fixture Show S01E01.mkv"
    media.write_bytes(b"fixture-media-bytes")
    root = configure_root(tmp_path, owner_id=1)
    fingerprint = file_receipt(root, media.name).sha256
    result = probe_media_file(
        root,
        media.name,
        inventory_fingerprint=fingerprint,
        runner=_runner(
            [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 1920,
                    "height": 1080,
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "tags": {"language": "jpn"},
                },
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "ass",
                    "tags": {"language": "eng"},
                    "disposition": {"default": 1, "forced": 0},
                },
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "ass",
                    "tags": {"language": "fra"},
                    "disposition": {"default": 0, "forced": 0},
                },
            ]
        ),
    )

    assert result.usable is True
    assert [track["language"] for track in result.subtitle_tracks] == ["eng", "fra"]
    assert result.byte_range_receipt["file_bytes"] == len(b"fixture-media-bytes")
    assert len(result.byte_range_receipt["first_sha256"]) == 64


def test_unverified_external_english_subtitle_does_not_make_media_usable(tmp_path):
    media = tmp_path / "Fixture Show S01E01.mkv"
    subtitle = tmp_path / "Fixture Show S01E01.en.srt"
    media.write_bytes(b"fixture-media-bytes")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    root = configure_root(tmp_path, owner_id=1)
    fingerprint = file_receipt(root, media.name).sha256

    result = probe_media_file(
        root,
        media.name,
        inventory_fingerprint=fingerprint,
        external_subtitles=(
            ExternalSubtitle(
                locator=subtitle.name,
                language="eng",
                release_fingerprint=fingerprint,
                sync_state="unverified",
            ),
        ),
        runner=_runner(
            [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac"},
            ]
        ),
    )

    assert result.outcome == "unusable"
    assert result.subtitle_tracks[0]["sync_policy"] == "unverified_external"
    assert "verified selectable English" in result.detail


def test_verified_release_bound_external_english_is_usable(tmp_path):
    media = tmp_path / "Fixture Show S01E01.mkv"
    subtitle = tmp_path / "Fixture Show S01E01.en.srt"
    media.write_bytes(b"fixture-media-bytes")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    root = configure_root(tmp_path, owner_id=1)
    fingerprint = file_receipt(root, media.name).sha256

    result = probe_media_file(
        root,
        media.name,
        inventory_fingerprint=fingerprint,
        external_subtitles=(
            ExternalSubtitle(
                locator=subtitle.name,
                language="eng",
                release_fingerprint=fingerprint,
                sync_state="verified",
            ),
        ),
        runner=_runner(
            [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac"},
            ]
        ),
    )

    assert result.usable is True
    assert result.subtitle_tracks[0]["sync_policy"] == "verified_external"


def test_probe_refuses_media_changed_after_plan(tmp_path):
    media = tmp_path / "Fixture Show S01E01.mkv"
    media.write_bytes(b"first")
    root = configure_root(tmp_path, owner_id=1)
    fingerprint = file_receipt(root, media.name).sha256
    media.write_bytes(b"changed")

    with pytest.raises(StorageSafetyError, match="changed"):
        probe_media_file(
            root,
            media.name,
            inventory_fingerprint=fingerprint,
            runner=_runner([]),
        )


def test_probe_timeout_returns_secret_free_error(tmp_path):
    media = tmp_path / "Private Fixture S01E01.mkv"
    media.write_bytes(b"fixture")
    root = configure_root(tmp_path, owner_id=1)
    fingerprint = file_receipt(root, media.name).sha256

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ffprobe", 1)

    result = probe_media_file(
        root,
        media.name,
        inventory_fingerprint=fingerprint,
        runner=timeout,
    )

    assert result.outcome == "error"
    assert "Private Fixture" not in result.detail


def test_real_synthetic_mkv_exposes_switchable_english_and_french_subtitles(tmp_path):
    media = build_synthetic_mkv(tmp_path)
    root = configure_root(tmp_path, owner_id=1)
    fingerprint = file_receipt(root, media.name).sha256

    result = probe_media_file(
        root,
        media.name,
        inventory_fingerprint=fingerprint,
    )

    assert result.usable is True
    languages = {track["language"] for track in result.subtitle_tracks}
    assert {"eng", "fra"} <= languages
