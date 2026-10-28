"""User-requested Plex and provider removal workflow tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from omakase.plus import db as plus_db
from omakase.plus.manifest import build_library_manifest
from omakase.plus.media_store import (
    ensure_media_provider,
    record_sync_run,
    upsert_asset,
    upsert_monitor,
)
from omakase.plus.product_views import library_titles, title_detail
from omakase.plus.title_removal import (
    TitleRemovalError,
    confirm_plex_removals,
    process_provider_removals,
    request_title_removal,
    title_removal_preview,
)


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(connection)
    yield connection
    connection.close()


def _seed_title(db, *, provenance: str = "omakase_created") -> tuple[int, int, int]:
    user_id = int(
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES ('owner@example.com', 'hash')"
        ).lastrowid
    )
    planning_id = int(
        db.execute(
            """INSERT INTO anilist_plannings
               (user_id, anilist_id, title, status)
               VALUES (?, 151807, 'Solo Leveling', 'COMPLETED')""",
            (user_id,),
        ).lastrowid
    )
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=151807,
        anilist_planning_id=planning_id,
        title="Solo Leveling",
        search_titles=("Ore dake Level Up na Ken",),
        format="TV",
        release_status="FINISHED",
        season_number=1,
        expected_episodes=12,
        aired_episodes=12,
    )
    provider_id = ensure_media_provider(
        db,
        user_id=user_id,
        kind="realdebrid",
        label="Real-Debrid",
        capabilities={"deletion": True},
        config={},
    )
    run_id = record_sync_run(
        db,
        monitor_id=monitor_id,
        outcome="complete",
        required_count=12,
        covered_count=12,
        accepted_count=1,
        detail="ready",
        completed_at="2026-07-28T08:00:00Z",
    )
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Solo Leveling S01 1080p",
        source_folder="Solo.Leveling.S01",
        provider_id=provider_id,
        provider_asset_id="rd-solo-leveling",
        storage_backend="zurg",
        storage_locator="Solo.Leveling.S01",
        quality="1080p",
        ownership="monitor_owned" if provenance == "omakase_created" else "external",
        provenance=provenance,
        created_by_run_id=run_id if provenance == "omakase_created" else None,
        planned_coverage=range(1, 13),
        usable_coverage=range(1, 13),
        inventory_fingerprint="b" * 64,
        inventory_verified_at="2026-07-28T08:00:00Z",
        status="downloaded",
        progress=100,
    )
    db.commit()
    return user_id, monitor_id, asset_id


def _curator_receipt(*, include_title: bool) -> dict:
    coverage = (
        [
            {
                "anilist_id": 151807,
                "section": "Anime",
                "season_number": 1,
                "usable_count": 12,
                "plex_visible_count": 12,
            }
        ]
        if include_title
        else []
    )
    counts = {
        "Anime": 1 if include_title else 0,
        "Anime Movies": 0,
        "TV Shows": 0,
        "Movies": 0,
    }
    return {
        "schema_version": "curator-receipt-v2",
        "run_id": "remove-solo-20260728",
        "started_at": "2026-07-28T08:06:00Z",
        "completed_at": "2026-07-28T08:06:08Z",
        "outcome": "success",
        "mode": "live",
        "topology_converged": True,
        "manifest_generated_at": "2026-07-28T08:05:30Z",
        "observed_sections": [
            {"key": index, "title": title, "count": count}
            for index, (title, count) in enumerate(counts.items(), start=1)
        ],
        "converged_section_counts": counts,
        "title_coverage": coverage,
        "blocked_assets": [],
        "errors": [],
    }


def _render_removal(preview):
    templates = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader(
                    {
                        "base.html": "{% block page_actions %}{% endblock %}{% block content %}{% endblock %}"
                    }
                ),
                FileSystemLoader(templates),
            ]
        ),
        autoescape=True,
    )
    return env.get_template("title_remove.html").render(removal=preview, csrf_token="fixture")


def test_removal_review_names_only_eligible_deletion_destination_and_protects_history(db):
    owner, monitor, _ = _seed_title(db, provenance="pre_existing")
    preview = title_removal_preview(db, user_id=owner, monitor_id=monitor)
    html = _render_removal(preview)
    assert preview["progress"] is None
    assert preview["eligible_provider_count"] == 0
    assert preview["retained_asset_count"] == 1
    assert ">Remove from Plex</button>" in html
    assert "Remove from Plex and Real-Debrid" not in html
    assert "Watch history and ratings are kept" in html
    assert 'name="confirmation"' in html and 'name="csrf_token"' in html
    assert "autofocus" in html and ">Cancel</a>" in html
    assert "proof-ordered" not in html and "curator" not in html
    preview["shared_plex_title"] = True
    blocked = _render_removal(preview)
    assert "Removal is blocked." in blocked and "disabled" in blocked


@pytest.mark.parametrize(
    "status,deleted,expected",
    [
        ("awaiting_plex", 0, "Queued · waiting for Plex"),
        ("plex_removed", 0, "In progress · removed from Plex"),
        ("provider_failed", 0, "Cleanup failed"),
        ("provider_failed", 1, "Partially completed"),
        ("complete", 1, "Completed"),
    ],
)
def test_removal_result_states_do_not_offer_duplicate_destructive_form(
    db, status, deleted, expected
):
    owner, monitor, _ = _seed_title(db)
    preview = title_removal_preview(db, user_id=owner, monitor_id=monitor)
    preview["progress"] = dict(
        status=status,
        scope="plex_and_provider",
        deleted_count=deleted,
        failed_count=1,
        pending_count=0,
        retained_count=1,
        detail="Synthetic state <script>alert(1)</script>",
    )
    html = _render_removal(preview)
    assert expected in html
    assert "<form" not in html
    assert "1 source item kept" in html
    assert "AniList is unchanged" in html
    assert "&lt;script&gt;" in html and "<script>" not in html


def test_preview_and_retry_preserve_completed_provider_operations(db):
    owner, monitor, original_asset = _seed_title(db)
    provider_id = db.execute("SELECT id FROM media_providers").fetchone()[0]
    upsert_asset(
        db,
        monitor_id=monitor,
        torrent_hash="F" * 40,
        source_title="Second owned item",
        provider_id=provider_id,
        provider_asset_id="rd-second",
        ownership="monitor_owned",
        provenance="omakase_created",
        status="downloaded",
        created_by_run_id=db.execute(
            "SELECT created_by_run_id FROM media_assets WHERE id = ?", (original_asset,)
        ).fetchone()[0],
    )
    request_title_removal(
        db,
        user_id=owner,
        monitor_id=monitor,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
    )
    confirm_plex_removals(db, _curator_receipt(include_title=False))

    class FakeProvider:
        def __init__(self):
            self.deleted = []
            self.fail_second = True

        async def get_item(self, external_id):
            return object()

        async def delete_item(self, external_id):
            self.deleted.append(external_id)
            return not (self.fail_second and external_id == "rd-second")

    provider = FakeProvider()
    for failed in (True, False):
        provider.fail_second = failed
        asyncio.run(
            process_provider_removals(
                db, provider_factory=lambda _: provider, completed_at="2026-07-28T08:07:00Z"
            )
        )
        progress = title_removal_preview(db, user_id=owner, monitor_id=monitor)["progress"]
        assert progress["status"] == ("provider_failed" if failed else "complete")
        assert progress["deleted_count"] == (1 if failed else 2)
        assert progress["failed_count"] == (1 if failed else 0)
    assert provider.deleted == ["rd-solo-leveling", "rd-second", "rd-second"]


def test_request_unpublishes_media_without_touching_completed_anilist_state(db):
    """Changing the local planning row would recreate the reported AniList regression."""
    user_id, monitor_id, asset_id = _seed_title(db)

    result = request_title_removal(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
    )

    monitor = db.execute(
        "SELECT active, next_check_at FROM media_monitors WHERE id = ?", (monitor_id,)
    ).fetchone()
    asset = db.execute(
        "SELECT status, last_error FROM media_assets WHERE id = ?", (asset_id,)
    ).fetchone()
    planning = db.execute(
        "SELECT status FROM anilist_plannings WHERE user_id = ? AND anilist_id = 151807",
        (user_id,),
    ).fetchone()
    removal_asset = db.execute(
        """SELECT outcome FROM media_title_removal_assets
           WHERE removal_id = ? AND asset_id = ?""",
        (result["id"], asset_id),
    ).fetchone()

    assert tuple(monitor) == (0, None)
    assert tuple(asset) == ("quarantined", "USER_REMOVAL_REQUESTED")
    assert planning["status"] == "COMPLETED"
    assert removal_asset["outcome"] == "pending"
    assert build_library_manifest(db)["titles"] == []
    card = library_titles(db, user_id=user_id)[0]
    detail = title_detail(db, user_id=user_id, monitor_id=monitor_id)
    assert (card["state"], card["status_label"]) == ("acquiring", "Removing from Plex")
    assert card["last_result"] == "Waiting for a converged Plex curator receipt"
    assert detail["removal"]["status_label"] == "Removing from Plex"


def test_visible_curator_receipt_cannot_unlock_provider_deletion(db):
    """Deleting before Plex proves absence would reintroduce broken library links."""
    user_id, monitor_id, _asset_id = _seed_title(db)
    request_title_removal(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
    )

    assert confirm_plex_removals(db, _curator_receipt(include_title=True)) == ()


def test_curator_receipt_completed_before_request_cannot_unlock_deletion(db):
    """Distributed clock anomalies cannot bypass the proof ordering constraint."""
    user_id, monitor_id, _asset_id = _seed_title(db)
    request_title_removal(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
    )
    receipt = _curator_receipt(include_title=False)
    receipt["started_at"] = "2026-07-28T08:04:00Z"
    receipt["completed_at"] = "2026-07-28T08:04:08Z"

    assert confirm_plex_removals(db, receipt) == ()


def test_global_curator_error_cannot_prove_plex_absence(db):
    """A section-wide scan failure makes every missing title ambiguous."""
    user_id, monitor_id, _ = _seed_title(db)
    removal = request_title_removal(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
    )
    receipt = _curator_receipt(include_title=False)
    receipt["outcome"] = "degraded"
    receipt["errors"] = [
        {
            "code": "PLEX_SCAN_FAILED",
            "anilist_id": None,
            "detail": "Anime section scan did not complete",
        }
    ]

    assert confirm_plex_removals(db, receipt) == ()
    assert (
        db.execute(
            "SELECT status FROM media_title_removals WHERE id = ?",
            (removal["id"],),
        ).fetchone()[0]
        == "awaiting_plex"
    )
    assert (
        db.execute("SELECT status FROM media_title_removals").fetchone()["status"]
        == "awaiting_plex"
    )


def test_absent_curator_receipt_deletes_only_owned_realdebrid_media(db):
    """Weakening eligibility would delete adopted or shared provider content."""
    user_id, monitor_id, owned_asset_id = _seed_title(db)
    provider_id = db.execute(
        "SELECT provider_id FROM media_assets WHERE id = ?", (owned_asset_id,)
    ).fetchone()["provider_id"]
    adopted_asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="C" * 40,
        source_title="Solo Leveling adopted backup",
        source_folder="Solo.Leveling.Adopted",
        provider_id=provider_id,
        provider_asset_id="rd-adopted",
        storage_backend="zurg",
        storage_locator="Solo.Leveling.Adopted",
        ownership="external",
        provenance="adopted",
        planned_coverage=(1,),
        usable_coverage=(1,),
        inventory_fingerprint="d" * 64,
        inventory_verified_at="2026-07-28T08:00:00Z",
        status="downloaded",
        progress=100,
    )
    db.commit()
    removal = request_title_removal(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
    )

    assert confirm_plex_removals(db, _curator_receipt(include_title=False)) == (removal["id"],)

    class FakeProvider:
        def __init__(self):
            self.deleted: list[str] = []

        async def get_item(self, external_id: str):
            return object()

        async def delete_item(self, external_id: str) -> bool:
            self.deleted.append(external_id)
            return True

    provider = FakeProvider()
    processed = asyncio.run(
        process_provider_removals(
            db,
            provider_factory=lambda requested_user_id: provider,
            completed_at="2026-07-28T08:06:09Z",
        )
    )

    rows = {
        row["asset_id"]: row["outcome"]
        for row in db.execute(
            """SELECT asset_id, outcome FROM media_title_removal_assets
               WHERE removal_id = ?""",
            (removal["id"],),
        ).fetchall()
    }
    assert processed == 1
    assert provider.deleted == ["rd-solo-leveling"]
    assert rows == {owned_asset_id: "deleted", adopted_asset_id: "retained"}
    assert tuple(
        db.execute(
            "SELECT status, completed_at FROM media_title_removals WHERE id = ?",
            (removal["id"],),
        ).fetchone()
    ) == ("complete", "2026-07-28T08:06:09Z")


def test_provider_completion_timestamp_never_precedes_plex_proof(db):
    """A slower host clock cannot violate the durable removal chronology."""
    user_id, monitor_id, _asset_id = _seed_title(db)
    removal = request_title_removal(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
    )
    assert confirm_plex_removals(db, _curator_receipt(include_title=False)) == (removal["id"],)

    class MissingProviderItem:
        async def get_item(self, external_id: str):
            return None

        async def delete_item(self, external_id: str) -> bool:
            raise AssertionError("An absent provider item must not be deleted")

    asyncio.run(
        process_provider_removals(
            db,
            provider_factory=lambda requested_user_id: MissingProviderItem(),
            completed_at="2026-07-28T08:05:59Z",
        )
    )

    row = db.execute(
        """SELECT plex_removed_at, completed_at
           FROM media_title_removals WHERE id = ?""",
        (removal["id"],),
    ).fetchone()
    assert tuple(row) == ("2026-07-28T08:06:08Z", "2026-07-28T08:06:08Z")


def test_confirmation_and_shared_plex_title_are_checked_before_mutation(db):
    """Removing a shared title would make another user's Plex entry disappear."""
    user_id, monitor_id, _asset_id = _seed_title(db)
    other_user = int(
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES ('friend@example.com', 'hash')"
        ).lastrowid
    )
    other_monitor = upsert_monitor(
        db,
        user_id=other_user,
        anilist_id=151807,
        title="Solo Leveling",
        format="TV",
        release_status="FINISHED",
        season_number=1,
        expected_episodes=12,
        aired_episodes=12,
    )
    provider_id = ensure_media_provider(
        db,
        user_id=other_user,
        kind="local_import",
        label="Friend library",
        capabilities={"local_storage": True},
        config={},
    )
    upsert_asset(
        db,
        monitor_id=other_monitor,
        torrent_hash="E" * 40,
        source_title="Solo Leveling friend copy",
        provider_id=provider_id,
        provider_asset_id="friend-copy",
        storage_backend="local",
        storage_locator="Solo Leveling/Episode 01.mkv",
        ownership="external",
        provenance="adopted",
        planned_coverage=(1,),
        usable_coverage=(1,),
        inventory_fingerprint="f" * 64,
        inventory_verified_at="2026-07-28T08:00:00Z",
        status="downloaded",
        progress=100,
    )
    db.commit()

    with pytest.raises(TitleRemovalError, match="shared Plex title"):
        request_title_removal(
            db,
            user_id=user_id,
            monitor_id=monitor_id,
            confirmation="Solo Leveling",
            requested_at="2026-07-28T08:05:00Z",
        )

    assert db.execute("SELECT COUNT(*) FROM media_title_removals").fetchone()[0] == 0
    assert (
        db.execute("SELECT active FROM media_monitors WHERE id = ?", (monitor_id,)).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("provenance", ["omakase_created", "pre_existing"])
def test_plex_only_retains_assets_and_never_opens_provider(db, provenance):
    user_id, monitor_id, asset_id = _seed_title(db, provenance=provenance)
    before = dict(db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone())
    result = request_title_removal(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
        scope="plex_only",
    )
    assert (
        dict(db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone())
        == before
    )
    assert db.execute("SELECT outcome FROM media_title_removal_assets").fetchone()[0] == "retained"
    assert confirm_plex_removals(db, _curator_receipt(include_title=True)) == ()
    stale = _curator_receipt(include_title=False)
    stale["manifest_generated_at"] = "2026-07-28T08:05:00Z"
    assert confirm_plex_removals(db, stale) == ()
    assert confirm_plex_removals(db, _curator_receipt(include_title=False)) == ()
    _native_snapshot(db)
    assert confirm_plex_removals(db, _curator_receipt(include_title=False)) == (result["id"],)

    def forbidden_provider(user_id):
        raise AssertionError("Plex-only cleanup must never instantiate a provider")

    assert (
        asyncio.run(
            process_provider_removals(
                db,
                provider_factory=forbidden_provider,
                completed_at="2026-07-28T08:07:00Z",
            )
        )
        == 0
    )
    removal = db.execute("SELECT * FROM media_title_removals").fetchone()
    assert removal["scope"] == "plex_only" and removal["status"] == "complete"
    assert "retained" in removal["detail"]
    assert (
        dict(db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone())
        == before
    )


def test_existing_manual_removal_default_scope_is_preserved(db):
    user_id, monitor_id, _ = _seed_title(db)
    request_title_removal(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
    )
    assert db.execute("SELECT scope FROM media_title_removals").fetchone()[0] == "plex_and_provider"


def _native_snapshot(
    db,
    *,
    generated_at="2026-07-28T08:06:10Z",
    season=None,
    section="Anime",
    media_type="show",
    path_evidence=False,
):
    from omakase.plus.watch.plex_sync import store_plex_library_snapshot

    items = (
        []
        if season is None
        else [
            {
                "rating_key": "123",
                "section": section,
                "media_type": media_type,
                "title": "Solo Leveling",
                "season_number": season,
                "episode_total": 12,
                "watched_through": 0,
                "last_viewed_at": None,
            }
        ]
    )
    if path_evidence and items:
        items[0].update(
            managed_paths=[{"canonical_title": "Solo Leveling", "season_number": season}],
            path_identity_complete=True,
        )
    store_plex_library_snapshot(
        db,
        {
            "schema_version": "plex-library-snapshot-v1",
            "generated_at": generated_at,
            "items": items,
        },
        received_at=generated_at,
    )


@pytest.mark.parametrize("mode", ["missing", "stale", "present", "ambiguous_section"])
def test_plex_only_native_snapshot_required_before_complete(db, mode):
    owner, monitor, _ = _seed_title(db)
    request_title_removal(
        db,
        user_id=owner,
        monitor_id=monitor,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
        scope="plex_only",
    )
    if mode == "stale":
        _native_snapshot(db, generated_at="2026-07-28T08:06:00Z")
    elif mode != "missing":
        _native_snapshot(db, season=1, section="Movies" if mode == "ambiguous_section" else "Anime")
    assert confirm_plex_removals(db, _curator_receipt(include_title=False)) == ()
    assert db.execute("SELECT status FROM media_title_removals").fetchone()[0] == "awaiting_plex"


def test_plex_only_native_snapshot_other_season_does_not_block_exact_removal(db):
    owner, monitor, _ = _seed_title(db)
    result = request_title_removal(
        db,
        user_id=owner,
        monitor_id=monitor,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
        scope="plex_only",
    )
    _native_snapshot(db, season=2, path_evidence=True)
    assert confirm_plex_removals(db, _curator_receipt(include_title=False)) == (result["id"],)
    assert db.execute("SELECT season_number FROM plex_library_items").fetchone()[0] == 2


@pytest.mark.parametrize(
    "refs,complete,native_season,allowed",
    [
        ([{"canonical_title": "Solo Leveling", "season_number": 1}], True, 1, False),
        ([{"canonical_title": "Solo Leveling", "season_number": 1}], True, 2, False),
        ([], False, 2, False),
        (None, None, 2, False),
        ([{"canonical_title": "Unrelated", "season_number": 1}], True, 1, True),
        ([], True, 1, True),
    ],
)
def test_plex_withdrawal_keeps_path_presence_and_uncertainty_even_after_native_rename(
    db, refs, complete, native_season, allowed
):
    from omakase.plus.watch.plex_sync import store_plex_library_snapshot

    owner, monitor, _ = _seed_title(db)
    removal = request_title_removal(
        db,
        user_id=owner,
        monitor_id=monitor,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
        scope="plex_only",
    )
    item = {
        "rating_key": "123",
        "section": "Anime",
        "media_type": "show",
        "title": "Renamed display title",
        "season_number": native_season,
        "episode_total": 12,
        "watched_through": 0,
        "last_viewed_at": None,
    }
    if refs is not None:
        item.update(managed_paths=refs, path_identity_complete=complete)
    store_plex_library_snapshot(
        db,
        {
            "schema_version": "plex-library-snapshot-v1",
            "generated_at": "2026-07-28T08:06:10Z",
            "items": [item],
        },
        received_at="2026-07-28T08:06:10Z",
    )
    assert confirm_plex_removals(db, _curator_receipt(include_title=False)) == (
        (removal["id"],) if allowed else ()
    )


def test_plex_only_shared_owner_withdrawals_preserve_native_shared_title(db):
    user_id, monitor_id, _asset_id = _seed_title(db)
    other_user = int(
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES ('friend@example.com', 'hash')"
        ).lastrowid
    )
    other_monitor = upsert_monitor(
        db,
        user_id=other_user,
        anilist_id=151807,
        title="Solo Leveling",
        format="TV",
        release_status="FINISHED",
        season_number=1,
        expected_episodes=12,
        aired_episodes=12,
    )
    provider_id = ensure_media_provider(
        db,
        user_id=other_user,
        kind="local_import",
        label="Friend library",
        capabilities={"local_storage": True},
        config={},
    )
    upsert_asset(
        db,
        monitor_id=other_monitor,
        torrent_hash="E" * 40,
        source_title="Solo Leveling S01 1080p",
        source_folder="Solo.Leveling.S01",
        quality="1080p",
        provider_id=provider_id,
        provider_asset_id="friend-copy",
        storage_backend="local",
        storage_locator="Solo Leveling/Episode 01.mkv",
        ownership="external",
        provenance="adopted",
        planned_coverage=(1,),
        usable_coverage=(1,),
        inventory_fingerprint="f" * 64,
        inventory_verified_at="2026-07-28T08:00:00Z",
        status="downloaded",
        progress=100,
    )
    db.commit()

    original_friend = dict(
        db.execute("SELECT * FROM media_monitors WHERE id=?", (other_monitor,)).fetchone()
    )
    first = request_title_removal(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
        scope="plex_only",
    )
    assert (
        dict(db.execute("SELECT * FROM media_monitors WHERE id=?", (other_monitor,)).fetchone())
        == original_friend
    )
    assert len(build_library_manifest(db)["titles"]) == 1
    _native_snapshot(db, season=1)
    assert confirm_plex_removals(db, _curator_receipt(include_title=False)) == ()
    second = request_title_removal(
        db,
        user_id=other_user,
        monitor_id=other_monitor,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:06:20Z",
        scope="plex_only",
    )
    assert build_library_manifest(db)["titles"] == []
    _native_snapshot(db, generated_at="2026-07-28T08:07:00Z")
    receipt = _curator_receipt(include_title=False)
    receipt["manifest_generated_at"] = "2026-07-28T08:06:30Z"
    receipt["completed_at"] = "2026-07-28T08:06:50Z"
    assert confirm_plex_removals(db, receipt) == (first["id"], second["id"])
    assert [row[0] for row in db.execute("SELECT status FROM media_assets")] == [
        "downloaded",
        "downloaded",
    ]


def test_plex_only_movie_native_absence_requires_exact_movie_identity(db):
    owner, monitor, _ = _seed_title(db)
    db.execute(
        "UPDATE media_monitors SET format='MOVIE', season_number=NULL WHERE id=?", (monitor,)
    )
    db.commit()
    removal = request_title_removal(
        db,
        user_id=owner,
        monitor_id=monitor,
        confirmation="Solo Leveling",
        requested_at="2026-07-28T08:05:00Z",
        scope="plex_only",
    )
    _native_snapshot(db, season=0, section="Anime Movies", media_type="movie")
    assert confirm_plex_removals(db, _curator_receipt(include_title=False)) == ()
    _native_snapshot(db, generated_at="2026-07-28T08:06:20Z")
    assert confirm_plex_removals(db, _curator_receipt(include_title=False)) == (removal["id"],)
    assert (
        db.execute("SELECT completed_at FROM media_title_removals").fetchone()[0]
        == "2026-07-28T08:06:20Z"
    )
