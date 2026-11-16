"""Known-bad, synthetic playback receipts must fail the release gate."""

from copy import deepcopy

import pytest

from ops.accept_playback import verify_acceptance


@pytest.fixture
def evidence():
    identity = {"target": "fixture-tv-1-2", "generation": "a" * 64}
    cues = [{"id": "line-1", "start": 6.5, "end": 7.5}]
    segments = [
        {
            "index": index,
            "start": index * 6,
            "end": (index + 1) * 6,
            "first_frame": str(index + 1) * 64,
            "last_frame": str(index + 4) * 64,
            "content": str(index + 7) * 64,
        }
        for index in range(3)
    ]
    reference = {
        **identity,
        "master_variant": "stream.m3u8",
        "subtitle_variant": "subtitles0.m3u8",
        "calibration_offset": 0.25,
        "segments": segments,
        "cues": cues,
        "seek_targets": [2, 8, 14],
        "completion_position": 17,
    }
    observed = {
        **identity,
        "master_variant": "stream.m3u8",
        "subtitle_variant": "subtitles0.m3u8",
        "calibration": {**identity, "offset": 0.25, "status": "hit"},
        "timings": {"open": 1.1, "playlist": 1.2, "seek": 1.3},
        "segments": deepcopy(segments),
        "cues": deepcopy(cues),
        "seeks": [{"target": target, "landed": target} for target in (2, 8, 14)],
        "completion": {
            **identity,
            "position": 17,
            "readback_position": 17,
            "credits_before": 0,
            "credits_after": 1,
            "credits_after_retry": 1,
        },
    }
    return reference, observed


def test_independent_reference_and_observations_pass(evidence):
    assert verify_acceptance(*evidence)["result"] == "PASS"


@pytest.mark.parametrize(
    ("check", "corrupt"),
    [
        ("master", lambda data: data.update(master_variant="other.m3u8")),
        ("identity", lambda data: data.update(generation="b" * 64)),
        ("calibration", lambda data: data["calibration"].update(status="failed")),
        ("calibration", lambda data: data["calibration"].update(target="other")),
        ("calibration", lambda data: data["calibration"].update(offset=1.0)),
        ("cues", lambda data: data["cues"][0].update(start=5.5, end=6.5)),
        (
            "cues",
            lambda data: data["cues"].append(
                {"id": "unexpected-overlay", "start": 6.5, "end": 7.5}
            ),
        ),
        ("timeline", lambda data: data["segments"][1].update(start=5.25)),
        ("timeline", lambda data: data["segments"][0].update(start=0.75, end=6.75)),
        (
            "timeline",
            lambda data: data["segments"][1].update(content=data["segments"][0]["content"]),
        ),
        (
            "timeline",
            lambda data: data["segments"][1].update(first_frame=data["segments"][0]["last_frame"]),
        ),
        ("timeline", lambda data: data["segments"].pop()),
        ("seek", lambda data: data["seeks"][2].update(landed=8)),
        ("completion", lambda data: data["completion"].update(credits_after_retry=2)),
        ("completion", lambda data: data["completion"].update(readback_position=None)),
        ("deadlines", lambda data: data["timings"].update(open=float("nan"))),
        ("deadlines", lambda data: data["timings"].update(seek=100)),
    ],
)
def test_release_gate_rejects_known_defect(evidence, check, corrupt):
    reference, observed = evidence
    corrupt(observed)
    result = verify_acceptance(reference, observed)
    assert result["result"] == "FAIL"
    assert check in result["failed_checks"]


def test_static_reference_content_across_adjacent_segments_can_pass(evidence):
    reference, observed = evidence
    for receipt in (reference, observed):
        receipt["segments"][1]["content"] = receipt["segments"][0]["content"]
    assert verify_acceptance(reference, observed)["result"] == "PASS"


def test_negative_completion_position_cannot_pass(evidence):
    reference, observed = evidence
    reference["completion_position"] = -1
    observed["completion"].update(position=-1, readback_position=-1)
    assert verify_acceptance(reference, observed)["failed_checks"] == ["completion"]


def test_a_legacy_smoke_pass_is_not_acceptance(evidence):
    assert verify_acceptance(evidence[0], {"result": "PASS", "passed": 14})["result"] == "FAIL"
