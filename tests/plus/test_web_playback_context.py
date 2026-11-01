"""Exact-source marker projection; no provider or original media access."""

from types import SimpleNamespace

import pytest

from omakase.plus.web_playback_context import normalize_skip_segments


def sample():
    source = SimpleNamespace(
        source_identity="file-A",
        metadata_key="20",
        season_number=1,
        episode_number=2,
        media_version="version-A",
        duration_ms=60000,
    )
    source._skip_evidence = vars(source).copy() | {
        "segments": [{"kind": "credits", "start_seconds": 45, "end_seconds": 55}]
    }
    return source


@pytest.mark.parametrize(
    "field,new",
    [
        ("source_identity", "file-B"),
        ("metadata_key", "21"),
        ("season_number", 2),
        ("episode_number", 3),
        ("media_version", "version-B"),
    ],
)
def test_evidence_never_crosses_source_episode_or_version(field, new):
    source = sample()
    assert normalize_skip_segments(source)["segments"]
    setattr(source, field, new)
    assert normalize_skip_segments(source)["segments"] == []


def test_public_marker_envelope_excludes_private_bindings_and_preserves_end():
    source = sample()
    value = normalize_skip_segments(source)
    assert set(value) == {"media_version", "episode_number", "segments"}
    assert value["segments"][0]["end_seconds"] == 55
    del source._skip_evidence["source_identity"]
    assert normalize_skip_segments(source)["segments"] == []
