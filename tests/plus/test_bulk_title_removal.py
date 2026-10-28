"""Batch composition tests for completed-title media cleanup."""

from __future__ import annotations

import sqlite3

import pytest

from omakase.plus import db as plus_db
from omakase.plus.media_store import (
    ensure_media_provider,
    record_sync_run,
    upsert_asset,
    upsert_monitor,
)
from omakase.plus.title_removal import (
    TitleRemovalError,
    bulk_title_removal_preview,
    request_bulk_title_removal,
)
from omakase.plus.watch.models import WatchEvent
from omakase.plus.watch.service import ingest_watch_event


@pytest.fixture
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(connection)
    try:
        yield connection
    finally:
        connection.close()


def _user(db: sqlite3.Connection, email: str = "owner@example.com") -> int:
    return int(
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, 'hash')", (email,)
        ).lastrowid
    )


def _title(
    db: sqlite3.Connection,
    *,
    user_id: int,
    anilist_id: int,
    title: str,
    completed: bool = True,
    rd_asset: bool = True,
    local_asset: bool = False,
) -> int:
    planning_id = int(
        db.execute(
            """INSERT INTO anilist_plannings (user_id, anilist_id, title, status)
               VALUES (?, ?, ?, ?)""",
            (user_id, anilist_id, title, "COMPLETED" if completed else "CURRENT"),
        ).lastrowid
    )
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=anilist_id,
        anilist_planning_id=planning_id,
        title=title,
        format="TV",
        release_status="FINISHED",
        season_number=1,
        expected_episodes=12,
        aired_episodes=12,
    )
    run_id = record_sync_run(
        db,
        monitor_id=monitor_id,
        outcome="complete",
        required_count=12,
        covered_count=12,
        accepted_count=int(rd_asset or local_asset),
        detail="ready",
        completed_at="2026-08-27T10:00:00Z",
    )
    if rd_asset:
        provider_id = ensure_media_provider(
            db,
            user_id=user_id,
            kind="realdebrid",
            label="Real-Debrid",
            config={},
            capabilities={"deletion": True},
        )
        upsert_asset(
            db,
            monitor_id=monitor_id,
            torrent_hash=f"{anilist_id:040X}"[-40:],
            source_title=f"{title} 1080p",
            provider_id=provider_id,
            provider_asset_id=f"rd-{anilist_id}",
            ownership="monitor_owned",
            provenance="omakase_created",
            created_by_run_id=run_id,
            planned_coverage=range(1, 13),
            usable_coverage=range(1, 13),
            inventory_fingerprint=f"{anilist_id:064x}"[-64:],
            inventory_verified_at="2026-08-27T10:00:00Z",
            status="downloaded",
            progress=100,
        )
    if local_asset:
        provider_id = ensure_media_provider(
            db,
            user_id=user_id,
            kind="local_import",
            label="Local library",
            config={},
            capabilities={"local_storage": True},
        )
        upsert_asset(
            db,
            monitor_id=monitor_id,
            torrent_hash=f"LOCAL{anilist_id:035d}"[-40:],
            source_title=f"{title} local copy",
            provider_id=provider_id,
            provider_asset_id=f"local-{anilist_id}",
            storage_backend="local",
            storage_locator=f"anime/{title}",
            ownership="external",
            provenance="adopted",
            planned_coverage=(1,),
            usable_coverage=(1,),
            inventory_fingerprint=f"{anilist_id + 1:064x}"[-64:],
            inventory_verified_at="2026-08-27T10:00:00Z",
            status="downloaded",
            progress=100,
        )
    db.commit()
    if completed:
        ingest_watch_event(
            db,
            user_id=user_id,
            monitor_id=monitor_id,
            event=WatchEvent.complete(source="manual", event_key=f"manual:{anilist_id}:complete"),
            observed_at="2026-08-27T11:00:00Z",
        )
    return monitor_id


def test_bulk_preview_accepts_only_owned_completed_titles(db: sqlite3.Connection) -> None:
    owner_id = _user(db)
    completed_id = _title(db, user_id=owner_id, anilist_id=11, title="Solo Leveling")
    active_id = _title(
        db,
        user_id=owner_id,
        anilist_id=12,
        title="Frieren",
        completed=False,
    )

    preview = bulk_title_removal_preview(
        db, user_id=owner_id, monitor_ids=(completed_id, active_id, 99)
    )

    assert preview["requested_count"] == 3
    assert preview["eligible_ids"] == (completed_id,)
    assert preview["blocked"] == (
        {"monitor_id": active_id, "reason": "Title is not completed"},
        {"monitor_id": 99, "reason": "Media title not found"},
    )
    assert preview["confirmation"] == "REMOVE 1 COMPLETED TITLES"


def test_bulk_preview_deduplicates_ids_and_rejects_more_than_one_hundred(
    db: sqlite3.Connection,
) -> None:
    owner_id = _user(db)
    monitor_id = _title(db, user_id=owner_id, anilist_id=11, title="Solo Leveling")

    preview = bulk_title_removal_preview(db, user_id=owner_id, monitor_ids=(monitor_id, monitor_id))

    assert preview["requested_count"] == 1
    assert preview["eligible_ids"] == (monitor_id,)
    with pytest.raises(TitleRemovalError, match="100"):
        bulk_title_removal_preview(db, user_id=owner_id, monitor_ids=range(1, 102))


def test_bulk_preview_reports_mixed_provider_retention(db: sqlite3.Connection) -> None:
    owner_id = _user(db)
    monitor_id = _title(
        db,
        user_id=owner_id,
        anilist_id=11,
        title="Solo Leveling",
        rd_asset=True,
        local_asset=True,
    )

    preview = bulk_title_removal_preview(db, user_id=owner_id, monitor_ids=(monitor_id,))

    assert preview["eligible"][0]["eligible_provider_count"] == 1
    assert preview["eligible"][0]["retained_asset_count"] == 1


def test_bulk_preview_blocks_shared_plex_title_and_non_rd_media(
    db: sqlite3.Connection,
) -> None:
    owner_id = _user(db)
    shared_id = _title(db, user_id=owner_id, anilist_id=11, title="Shared Show")
    local_id = _title(
        db,
        user_id=owner_id,
        anilist_id=12,
        title="Local Show",
        rd_asset=False,
        local_asset=True,
    )
    other_id = _user(db, "other@example.com")
    _title(db, user_id=other_id, anilist_id=11, title="Shared Show", local_asset=True)

    preview = bulk_title_removal_preview(db, user_id=owner_id, monitor_ids=(shared_id, local_id))

    assert preview["eligible_ids"] == ()
    assert {item["reason"] for item in preview["blocked"]} == {
        "Shared Plex title",
        "No eligible Real-Debrid media",
    }


def test_bulk_request_requires_exact_phrase_and_preserves_anilist(
    db: sqlite3.Connection,
) -> None:
    owner_id = _user(db)
    first_id = _title(db, user_id=owner_id, anilist_id=11, title="Solo Leveling")
    second_id = _title(db, user_id=owner_id, anilist_id=12, title="Frieren")

    with pytest.raises(TitleRemovalError, match="exact batch confirmation"):
        request_bulk_title_removal(
            db,
            user_id=owner_id,
            monitor_ids=(first_id, second_id),
            confirmation="REMOVE ALL",
            requested_at="2026-08-27T12:00:00Z",
        )
    result = request_bulk_title_removal(
        db,
        user_id=owner_id,
        monitor_ids=(first_id, second_id),
        confirmation="REMOVE 2 COMPLETED TITLES",
        requested_at="2026-08-27T12:00:00Z",
    )

    assert result["requested"] == 2
    assert result["failed"] == 0
    assert db.execute("SELECT count(*) FROM media_title_removals").fetchone()[0] == 2
    assert {
        row["status"] for row in db.execute("SELECT status FROM anilist_plannings").fetchall()
    } == {"COMPLETED"}


def test_bulk_request_is_idempotent_for_open_removal(db: sqlite3.Connection) -> None:
    owner_id = _user(db)
    monitor_id = _title(db, user_id=owner_id, anilist_id=11, title="Solo Leveling")
    first = request_bulk_title_removal(
        db,
        user_id=owner_id,
        monitor_ids=(monitor_id,),
        confirmation="REMOVE 1 COMPLETED TITLES",
        requested_at="2026-08-27T12:00:00Z",
    )
    second = request_bulk_title_removal(
        db,
        user_id=owner_id,
        monitor_ids=(monitor_id,),
        confirmation="REMOVE 1 COMPLETED TITLES",
        requested_at="2026-08-27T12:01:00Z",
    )

    assert first["results"][0]["idempotent"] is False
    assert second["results"][0]["idempotent"] is True
    assert db.execute("SELECT count(*) FROM media_title_removals").fetchone()[0] == 1


def test_bulk_request_returns_partial_failure_receipts(db: sqlite3.Connection, monkeypatch) -> None:
    owner_id = _user(db)
    first_id = _title(db, user_id=owner_id, anilist_id=11, title="Solo Leveling")
    second_id = _title(db, user_id=owner_id, anilist_id=12, title="Frieren")
    from omakase.plus import title_removal

    original = title_removal.request_title_removal

    def sometimes_fails(*args, **kwargs):
        if kwargs["monitor_id"] == second_id:
            raise TitleRemovalError("Synthetic per-title failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(title_removal, "request_title_removal", sometimes_fails)

    result = request_bulk_title_removal(
        db,
        user_id=owner_id,
        monitor_ids=(first_id, second_id),
        confirmation="REMOVE 2 COMPLETED TITLES",
        requested_at="2026-08-27T12:00:00Z",
    )

    assert result["requested"] == 1
    assert result["failed"] == 1
    assert [item["monitor_id"] for item in result["results"]] == [first_id, second_id]
