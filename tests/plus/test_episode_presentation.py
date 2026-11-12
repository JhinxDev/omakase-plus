"""Optional episode metadata stays bounded and separate from playback proof."""

import pytest

from omakase.plus.tv_service import episode_runtime


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        (30, "seconds", 30),
        (30.1, "seconds", 31),
        ("1441000", "milliseconds", 1441),
        (14400, "seconds", 14400),
        (None, "seconds", None),
        (True, "seconds", None),
        (False, "seconds", None),
        (0, "milliseconds", None),
        (-1, "seconds", None),
        (float("nan"), "seconds", None),
        (float("inf"), "seconds", None),
        (10**1000, "seconds", None),
        (14400001, "milliseconds", None),
        (14401, "seconds", None),
        ("unknown", "seconds", None),
        ("9" * 100, "seconds", None),
        ({"runtime": 24}, "seconds", None),
    ],
)
def test_optional_runtime_never_invents_or_overflows_a_duration(value, unit, expected):
    assert episode_runtime(value, unit=unit) == expected
