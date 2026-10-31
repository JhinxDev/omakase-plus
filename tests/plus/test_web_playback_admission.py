import pytest

from omakase.plus.web_playback_admission import Admission, Busy


def test_viewer_global_session_limits_and_idempotent_release():
    limits = Admission()
    releases = []
    for viewer in range(4):
        for _ in range(4):
            releases.append(limits.acquire("read", viewer, "one"))
    with pytest.raises(Busy):
        limits.acquire("read", 5, "new")
    releases[0]()
    releases[0]()
    release = limits.acquire("read", 5, "new")
    with pytest.raises(Busy):
        limits.acquire("read", 1, "one")
    for end in releases:
        end()
    release()
    assert not limits.active


def test_rate_window_expires_without_retaining_unbounded_viewers():
    now = [0]
    limits = Admission(clock=lambda: now[0])
    for _ in range(6):
        limits.acquire("start", 1)()
    with pytest.raises(Busy):
        limits.acquire("start", 1)
    for viewer in range(2, 1025):
        limits.acquire("start", viewer)()
    with pytest.raises(Busy):
        limits.acquire("start", 1025)
    now[0] = 61
    limits.acquire("start", 1)()
    assert len(limits.rates) == 1


@pytest.mark.parametrize("kind", ["close", "close_replay"])
def test_terminal_writer_and_exact_replay_have_separate_finite_holds(kind):
    limits = Admission()
    releases = [limits.acquire(kind, viewer, "one") for viewer in range(16)]
    with pytest.raises(Busy):
        limits.acquire(kind, 20, "new")
    with pytest.raises(Busy):
        limits.acquire(kind, 0, "one")
    for release in releases:
        release()
    writer = limits.acquire("close", 1, "same")
    replay = limits.acquire("close_replay", 1, "same")
    with pytest.raises(Busy):
        limits.acquire("close_replay", 1, "same")
    writer()
    replay()
    assert not limits.active
