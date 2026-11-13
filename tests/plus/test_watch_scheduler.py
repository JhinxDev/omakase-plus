"""Production scheduler wiring must retry watch sync without replaying history."""

from unittest.mock import AsyncMock, patch

from omakase.plus.media_cli import sync_media_once


def test_media_scheduler_retries_only_previously_attempted_tracker_work(monkeypatch):
    monkeypatch.delenv("OMAKASE_PLUS_REVIEW_MODE", raising=False)
    db = object()
    receipt = object()
    with (
        patch("omakase.plus.media_cli.get_db", return_value=db),
        patch("omakase.plus.watch.anilist_sync.process_tracker_outbox") as tracker,
        patch("omakase.plus.media_cli.run_media_sync", new=AsyncMock(return_value=receipt)),
    ):
        assert sync_media_once() is receipt
    tracker.assert_called_once_with(db, attempted_only=True)


def test_review_scheduler_never_sends_tracker_updates(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_REVIEW_MODE", "true")
    with (
        patch("omakase.plus.media_cli.get_db"),
        patch("omakase.plus.watch.anilist_sync.process_tracker_outbox") as tracker,
        patch("omakase.plus.media_cli.run_media_sync", new=AsyncMock()),
    ):
        sync_media_once()
    tracker.assert_not_called()
