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


def test_read_concurrency_has_short_typed_backoff_without_admitting_a_ninth():
    limits = Admission()
    releases = [limits.acquire("read", 1, "same") for _ in range(8)]
    with pytest.raises(Busy) as error:
        limits.acquire("read", 1, "same")
    assert error.value.retry_after == 1
    assert limits.active[("read", 1, "same")] == 8
    assert len(limits.rates[("read", 1)]) == 8
    for release in releases:
        release()
    assert not limits.active


def test_rate_and_map_backoff_take_priority_over_concurrency():
    limits = Admission(clock=lambda: 0)
    for _ in range(176):
        limits.acquire("read", 1, "same")()
    releases = [limits.acquire("read", 1, "same") for _ in range(4)]
    with pytest.raises(Busy) as error:
        limits.acquire("read", 1, "same")
    assert error.value.retry_after == 10
    for release in releases:
        release()
    other = Admission(clock=lambda: 0)
    for viewer in range(1024):
        other.acquire("read", viewer, "same")()
    releases = [other.acquire("read", viewer, "same") for viewer in range(4) for _ in range(4)]
    with pytest.raises(Busy) as error:
        other.acquire("read", 2000, "same")
    assert error.value.retry_after == 10
    assert other.active[("read",)] == 16 and len(other.rates) == 1024
    for release in releases:
        release()


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


def test_seek_overlap_and_subtitle_font_share_existing_viewer_budget():
    limits = Admission()
    # Old audio/video bodies may still be closing when the next seek starts its
    # audio/video init+segment requests and one authenticated subtitle/font read.
    purposes = (
        "old-video",
        "old-audio",
        "new-video-init",
        "new-audio-init",
        "new-video",
        "new-audio",
        "subtitle-font",
        "manifest",
    )
    releases = [limits.acquire("read", 1, "playing") for _ in purposes]
    assert limits.active[("read", 1, "playing")] == 8
    with pytest.raises(Busy) as error:
        limits.acquire("read", 1, "other-session")
    assert error.value.retry_after == 1
    # The aggregate server allowance stays sixteen; another viewer can still
    # use its eight slots, and the next viewer is then rejected.
    others = [limits.acquire("read", 2, "other") for _ in range(8)]
    with pytest.raises(Busy):
        limits.acquire("read", 3, "third")
    for release in releases + others:
        release()
    assert not limits.active


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
