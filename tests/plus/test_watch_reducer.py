"""Pure state-transition tests for watch progress."""

from __future__ import annotations

import pytest

from omakase.plus.watch.models import WatchEntry, WatchEvent
from omakase.plus.watch.reducer import reduce_watch_event

NOW = "2026-08-27T12:00:00Z"


def test_exact_episode_advances_and_completes_known_series() -> None:
    entry = WatchEntry.new(anilist_id=151807, season_number=1, episode_total=12)

    decision = reduce_watch_event(
        entry,
        WatchEvent.progress(
            source="plex",
            episode=12,
            confidence="exact",
            event_key="plex:session-7:e12",
        ),
        observed_at=NOW,
    )

    assert decision.disposition == "accepted"
    assert decision.entry.status == "completed"
    assert decision.entry.progress_episodes == 12
    assert decision.entry.completed_at == NOW
    assert decision.entry.revision == 1
    assert decision.enqueue_tracker_action == "complete"


def test_exact_episode_advances_monotonically() -> None:
    entry = WatchEntry.new(anilist_id=1, season_number=1, episode_total=12)

    decision = reduce_watch_event(
        entry,
        WatchEvent.progress(source="plex", episode=4, event_key="plex:e4"),
        observed_at=NOW,
    )

    assert decision.disposition == "accepted"
    assert decision.entry.status == "watching"
    assert decision.entry.progress_episodes == 4
    assert decision.entry.last_source == "plex"
    assert decision.entry.last_observed_at == NOW
    assert decision.enqueue_tracker_action == "progress"


def test_same_or_lower_automatic_progress_is_ignored() -> None:
    entry = WatchEntry.new(
        anilist_id=1,
        season_number=1,
        episode_total=12,
        progress_episodes=4,
        last_observed_at="2026-08-27T11:00:00Z",
    )

    for episode in (4, 3):
        decision = reduce_watch_event(
            entry,
            WatchEvent.progress(source="apple_tv", episode=episode, event_key=f"apple:{episode}"),
            observed_at=NOW,
        )
        assert decision.disposition == "ignored"
        assert decision.entry == entry
        assert decision.enqueue_tracker_action is None


def test_playback_below_completion_threshold_is_ignored() -> None:
    entry = WatchEntry.new(anilist_id=1, season_number=1, episode_total=12)

    decision = reduce_watch_event(
        entry,
        WatchEvent.progress(
            source="apple_tv",
            episode=3,
            playback_fraction=0.849,
            event_key="apple:e3:low",
        ),
        observed_at=NOW,
    )

    assert decision.disposition == "ignored"
    assert decision.reason == "playback_below_completion_threshold"
    assert decision.entry.progress_episodes == 0


def test_playback_at_completion_threshold_advances() -> None:
    entry = WatchEntry.new(anilist_id=1, season_number=1, episode_total=12)

    decision = reduce_watch_event(
        entry,
        WatchEvent.progress(
            source="apple_tv",
            episode=3,
            playback_fraction=0.85,
            event_key="apple:e3:complete",
        ),
        observed_at=NOW,
    )

    assert decision.disposition == "accepted"
    assert decision.entry.progress_episodes == 3


def test_inferred_identity_requires_confirmation_without_advancing() -> None:
    entry = WatchEntry.new(anilist_id=1, season_number=1, episode_total=12)

    decision = reduce_watch_event(
        entry,
        WatchEvent.progress(
            source="apple_tv",
            episode=3,
            confidence="inferred",
            event_key="apple:e3:inferred",
        ),
        observed_at=NOW,
    )

    assert decision.disposition == "confirmation_required"
    assert decision.reason == "inferred_identity"
    assert decision.entry == entry
    assert decision.enqueue_tracker_action is None


def test_unknown_total_stays_watching_until_explicit_completion() -> None:
    entry = WatchEntry.new(anilist_id=1, season_number=1)
    progressed = reduce_watch_event(
        entry,
        WatchEvent.progress(source="plex", episode=24, event_key="plex:e24"),
        observed_at=NOW,
    )

    completed = reduce_watch_event(
        progressed.entry,
        WatchEvent.complete(source="manual", event_key="manual:complete", force=True),
        observed_at="2026-08-27T12:05:00Z",
    )

    assert progressed.entry.status == "watching"
    assert progressed.enqueue_tracker_action == "progress"
    assert completed.entry.status == "completed"
    assert completed.entry.progress_episodes == 24
    assert completed.enqueue_tracker_action == "complete"


def test_completed_state_is_sticky_for_automatic_sources() -> None:
    entry = WatchEntry.new(
        anilist_id=1,
        season_number=1,
        episode_total=12,
        progress_episodes=12,
        completed_at="2026-08-27T11:00:00Z",
    )

    decision = reduce_watch_event(
        entry,
        WatchEvent.progress(source="plex", episode=7, event_key="plex:late:e7"),
        observed_at=NOW,
    )

    assert decision.disposition == "ignored"
    assert decision.reason == "completed_is_sticky"
    assert decision.entry == entry


def test_explicit_manual_reopen_can_lower_progress() -> None:
    entry = WatchEntry.new(
        anilist_id=1,
        season_number=1,
        episode_total=12,
        progress_episodes=12,
        completed_at="2026-08-27T11:00:00Z",
    )

    decision = reduce_watch_event(
        entry,
        WatchEvent.reopen(episode=3, event_key="manual:reopen", force=True),
        observed_at=NOW,
    )

    assert decision.disposition == "accepted"
    assert decision.entry.status == "watching"
    assert decision.entry.progress_episodes == 3
    assert decision.entry.completed_at is None
    assert decision.entry.last_source == "manual"
    assert decision.enqueue_tracker_action == "progress"


def test_manual_reopen_requires_force() -> None:
    with pytest.raises(ValueError, match="force"):
        WatchEvent.reopen(episode=3, event_key="manual:reopen", force=False)


def test_out_of_order_observation_is_ignored() -> None:
    entry = WatchEntry.new(
        anilist_id=1,
        season_number=1,
        episode_total=12,
        progress_episodes=4,
        last_observed_at=NOW,
    )

    decision = reduce_watch_event(
        entry,
        WatchEvent.progress(source="plex", episode=5, event_key="plex:old:e5"),
        observed_at="2026-08-27T11:59:59Z",
    )

    assert decision.disposition == "ignored"
    assert decision.reason == "out_of_order_observation"
    assert decision.entry == entry


def test_progress_beyond_known_total_is_rejected() -> None:
    entry = WatchEntry.new(anilist_id=1, season_number=1, episode_total=12)

    with pytest.raises(ValueError, match="episode_total"):
        reduce_watch_event(
            entry,
            WatchEvent.progress(source="plex", episode=13, event_key="plex:e13"),
            observed_at=NOW,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source": "web", "episode": 1, "event_key": "bad"}, "source"),
        ({"source": "plex", "episode": 0, "event_key": "bad"}, "episode"),
        ({"source": "plex", "episode": 1, "event_key": ""}, "event_key"),
        (
            {
                "source": "plex",
                "episode": 1,
                "event_key": "bad-fraction",
                "playback_fraction": 1.01,
            },
            "playback_fraction",
        ),
    ],
)
def test_progress_event_validates_input(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        WatchEvent.progress(**kwargs)
