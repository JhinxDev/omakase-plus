"""Release quality must describe usable inventory, never pending search results."""

import pytest

from omakase.plus.media_store import upsert_asset
from omakase.plus.product_views import library_titles, title_detail
from tests.plus.test_product_views import _seed_ready_title, _user
from tests.plus.test_product_views import db as _product_db


@pytest.fixture
def db():
    yield from _product_db.__wrapped__()


def test_mixed_verified_quality_is_visible_and_unverified_release_is_excluded(db):
    user = _user(db, "owner@example.com")
    monitor, high = _seed_ready_title(db, user)
    db.execute("UPDATE media_assets SET usable_coverage_json='[1,2]' WHERE id=?", (high,))
    for identity, quality, status, verified, coverage in (
        ("b", "720p", "downloaded", "2026-07-26T11:58:00Z", (3,)),
        ("c", "2160p", "queued", None, (1, 2, 3)),
        ("d", "2160p", "downloaded", None, (1,)),
        ("e", "2160p", "quarantined", "2026-07-26T11:58:00Z", (2,)),
        ("f", "2160p", "downloaded", "2026-07-26T11:58:00Z", (99,)),
    ):
        upsert_asset(
            db,
            monitor_id=monitor,
            torrent_hash=identity * 40,
            source_title=f"Fixture {quality}",
            quality=quality,
            planned_coverage=coverage,
            usable_coverage=coverage,
            inventory_fingerprint="a" * 64 if verified else "",
            inventory_verified_at=verified,
            status=status,
        )
    card = library_titles(db, user_id=user)[0]
    detail = title_detail(db, user_id=user, monitor_id=monitor)
    assert card["release_qualities"] == ["1080p", "720p"]
    assert card["fallback_episodes"] == [3]
    assert card["usable"] == 3
    assert detail["release_qualities"] == card["release_qualities"]


def test_malformed_inventory_cannot_claim_a_verified_720p_release(db):
    user = _user(db, "owner@example.com")
    monitor, asset = _seed_ready_title(db, user)
    db.execute(
        "UPDATE media_assets SET quality='720p', inventory_verified_at='broken' WHERE id=?",
        (asset,),
    )
    card = library_titles(db, user_id=user)[0]
    assert card["usable"] == 0
    assert card["release_qualities"] == []
    assert card["fallback_episodes"] == []


def test_superseded_720p_asset_does_not_leak_onto_1080p_release_qualities(db):
    user = _user(db, "owner@example.com")
    monitor, high = _seed_ready_title(db, user)
    db.execute(
        "UPDATE media_assets SET planned_coverage_json='[1,2,3]', usable_coverage_json='[1,2,3]' WHERE id=?",
        (high,),
    )
    upsert_asset(
        db,
        monitor_id=monitor,
        torrent_hash="9" * 40,
        source_title="Older 720p batch",
        quality="720p",
        planned_coverage=(2, 3),
        usable_coverage=(2, 3),
        inventory_fingerprint="b" * 64,
        inventory_verified_at="2026-07-26T11:58:00Z",
        status="downloaded",
    )
    card = library_titles(db, user_id=user)[0]
    detail = title_detail(db, user_id=user, monitor_id=monitor)
    assert card["release_qualities"] == ["1080p"]
    assert card["fallback_episodes"] == []
    assert detail["release_qualities"] == ["1080p"]
    assert detail["fallback_episodes"] == []
