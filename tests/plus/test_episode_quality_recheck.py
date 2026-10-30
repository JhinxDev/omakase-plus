"""Episode evidence and resurfacing never borrow certainty from aggregate counts."""

import pytest

from omakase.plus.manifest import store_curator_receipt
from omakase.plus.media_store import upsert_asset
from omakase.plus.product_views import home_view, library_titles, title_detail
from tests.plus.test_language_publication import data as data
from tests.plus.test_product_views import _curator_receipt
from tests.plus.test_viewing_history import db as db
from tests.plus.test_viewing_history import entry, event


def asset(db, monitor, suffix, episodes, quality="720p", status="downloaded"):
    return upsert_asset(
        db,
        monitor_id=monitor,
        torrent_hash=suffix * 40,
        source_title=f"One Piece S01 [{quality}]",
        source_folder=f"One Piece {suffix}",
        quality=quality,
        ownership="monitor_owned",
        provenance="adopted",
        downloaded_once=status == "downloaded",
        planned_coverage=episodes,
        usable_coverage=episodes if status == "downloaded" else (),
        inventory_fingerprint=suffix.lower() * 64,
        inventory_verified_at="2026-10-29T12:00:00Z" if status == "downloaded" else None,
        status=status,
        progress=100 if status == "downloaded" else 20,
    )


def test_unconfirmed_higher_releases_do_not_displace_the_existing_fallback(db):
    monitor, _ = entry(db)
    asset(db, monitor, "A", (1, 2, 3))
    asset(db, monitor, "B", (2,), "1080p")
    asset(db, monitor, "C", (3,), "2160p")
    card = title_detail(db, user_id=1, monitor_id=monitor)
    assert card["fallback_episodes"] == [1, 2, 3]
    quality = card["episode_qualities"]
    assert [(quality[n]["quality"], quality[n]["source"]) for n in (1, 2, 3)] == [
        ("720p", "release"),
        ("720p", "release"),
        ("720p", "release"),
    ]
    assert quality[1]["label"] == "720p fallback release"
    assert quality[2]["alternates"] == ["1080p"]
    assert quality[3]["alternates"] == ["2160p"]
    assert 4 not in quality


@pytest.mark.parametrize("status", ["downloading", "quarantined", "error"])
def test_unusable_preferred_copy_does_not_hide_fallback(db, status):
    monitor, _ = entry(db)
    asset(db, monitor, "A", (1,))
    asset(db, monitor, "B", (1,), "1080p", status=status)
    card = title_detail(db, user_id=1, monitor_id=monitor)
    assert card["fallback_episodes"] == [1]
    assert card["episode_qualities"][1]["label"] == "720p fallback release"


def test_aggregate_plex_count_cannot_establish_the_exact_next_episode(db):
    monitor, watch = entry(db)
    db.execute("UPDATE watch_entries SET progress_episodes=1 WHERE id=?", (watch,))
    asset(db, monitor, "A", (1, 3, 4), "1080p")
    db.commit()
    store_curator_receipt(db, _curator_receipt(anilist_id=21, season=1, episodes=4))
    card = library_titles(db, user_id=1)[0]
    assert card["next_episode"] == 2
    assert not card["new_episode_ready"]
    assert not card["new_episode_available"]
    assert "Episode 2 is ready" not in card["status_label"]


def caught_up(db, *, anilist_id=21, title="One Piece", episodes=(1, 2, 3)):
    monitor, watch = entry(db, anilist_id=anilist_id, title=title)
    db.execute(
        "UPDATE media_monitors SET release_status='RELEASING',aired_episodes=3,expected_episodes=12 WHERE id=?",
        (monitor,),
    )
    db.execute(
        "UPDATE watch_entries SET status='completed',progress_episodes=2,episode_total=2,completed_at='2026-10-28T20:00:00Z' WHERE id=?",
        (watch,),
    )
    asset(db, monitor, "A", episodes, "1080p")
    return monitor, watch


def test_exact_new_usable_episode_resurfaces_caught_up_without_rewriting_history(db):
    monitor, watch = caught_up(db)
    event(db, watch, episode=2, stamp="2026-10-28T20:00:00Z", key="actual:last")
    before = tuple(db.execute("SELECT * FROM watch_entries WHERE id=?", (watch,)).fetchone())
    changes = db.total_changes
    cards = home_view(db, user_id=1)["continue_watching"]
    assert [card["id"] for card in cards] == [monitor]
    assert cards[0]["watch_status"] == "completed"
    assert cards[0]["next_episode"] == 3
    assert cards[0]["new_episode_available"]
    assert not cards[0]["new_episode_ready"]
    assert cards[0]["episode_availability_label"] == "Plex availability unconfirmed"
    assert cards[0]["resume_action_label"] == "View new episode"
    assert (
        tuple(db.execute("SELECT * FROM watch_entries WHERE id=?", (watch,)).fetchone()) == before
    )
    assert db.total_changes == changes


@pytest.mark.parametrize("episodes", [(), (1, 2), (1, 2, 4)])
def test_metadata_or_other_episode_cannot_resurface_completed_title(db, episodes):
    monitor, _ = caught_up(db, episodes=episodes)
    assert all(card["id"] != monitor for card in home_view(db, user_id=1)["continue_watching"])


def test_resurfaced_title_keeps_actual_viewing_order_and_same_hero(db):
    older, watch = caught_up(db)
    event(db, watch, episode=2, stamp="2026-10-20T20:00:00Z", key="actual:older")
    newer, watch = entry(db, anilist_id=22, title="Zed latest viewing")
    event(db, watch, episode=3, stamp="2026-10-29T20:00:00Z", key="actual:newer")
    db.execute(
        "UPDATE media_monitors SET updated_at='2099-01-01T00:00:00Z',last_checked_at='2099-01-01T00:00:00Z' WHERE id=?",
        (older,),
    )
    cards = home_view(db, user_id=1)["continue_watching"]
    assert [card["id"] for card in cards] == [newer, older]
    from tests.plus.test_streaming_templates import render

    html = render("home.html", **home_view(db, user_id=1))
    hero = html.split('<section class="cinema-hero"', 1)[1].split("</section>", 1)[0]
    assert "Zed latest viewing" in hero


@pytest.mark.parametrize("blocked", [False, True])
def test_completed_cleanup_block_prevents_resurfacing(db, monkeypatch, blocked):
    from omakase.plus import completion_cleanup
    from omakase.plus.title_removal import TitleRemovalError
    from omakase.plus.watch.models import WatchEvent
    from omakase.plus.watch.service import ingest_watch_event

    monitor, _ = caught_up(db)
    db.execute(
        "UPDATE media_monitors SET release_status='FINISHED',expected_episodes=2,aired_episodes=2 WHERE id=?",
        (monitor,),
    )
    db.execute(
        "UPDATE watch_entries SET status='watching',progress_episodes=1,completed_at=NULL WHERE monitor_id=?",
        (monitor,),
    )
    if blocked:

        def unavailable(*args, **kwargs):
            raise TitleRemovalError("Fixture native prerequisite unavailable")

        monkeypatch.setattr(completion_cleanup, "request_title_removal", unavailable)
    completion_cleanup.set_completion_cleanup_preference(db, user_id=1, remove_from_plex=True)
    db.commit()
    result = ingest_watch_event(
        db,
        user_id=1,
        monitor_id=monitor,
        event=WatchEvent.complete(source="manual", event_key="q9-cleanup"),
        score_ten=9,
        observed_at="2026-10-29T13:00:00Z",
    )
    assert result.entry.status == "completed"
    table = "completion_plex_cleanup_blocks" if blocked else "media_title_removals"
    assert db.execute(f"SELECT 1 FROM {table} WHERE monitor_id=?", (monitor,)).fetchone()
    db.execute(
        "UPDATE media_monitors SET release_status='RELEASING',expected_episodes=12,aired_episodes=3 WHERE id=?",
        (monitor,),
    )
    assert all(card["id"] != monitor for card in home_view(db, user_id=1)["continue_watching"])


def test_airing_caught_up_title_returns_only_when_exact_new_episode_is_usable(db):
    monitor, watch = entry(db)
    db.execute(
        "UPDATE media_monitors SET release_status='RELEASING',aired_episodes=3 WHERE id=?",
        (monitor,),
    )
    asset(db, monitor, "A", (1, 2, 3), "1080p")
    assert not home_view(db, user_id=1)["continue_watching"]
    asset(db, monitor, "B", (4,), "1080p", status="downloading")
    db.execute("UPDATE media_monitors SET aired_episodes=4 WHERE id=?", (monitor,))
    assert not library_titles(db, user_id=1)[0]["new_episode_available"]
    # Metadata may show the next missing episode, but cannot announce newly available media.
    asset(db, monitor, "B", (4,), "1080p")
    changes = db.total_changes
    cards = home_view(db, user_id=1)["continue_watching"]
    assert [card["id"] for card in cards] == [monitor]
    assert cards[0]["next_episode"] == 4 and cards[0]["new_episode_available"]
    assert not cards[0]["new_episode_ready"]
    assert db.total_changes == changes


@pytest.mark.parametrize("stale", [False, True])
def test_video_resolution_requires_current_source_bound_file_proof(data, monkeypatch, stale):
    from tests.plus.test_language_publication import build, receipt

    original, _ = data
    db, _, _, _, _, _, _, asset_id = original
    _, plan = build(data)
    db.commit()
    store_curator_receipt(db, receipt(plan, monkeypatch), language_qualification=True)
    if stale:
        db.execute(
            "UPDATE language_publication_current_files SET observation_id=NULL WHERE asset_id=?",
            (asset_id,),
        )
    changes = db.total_changes
    quality = title_detail(db, user_id=1, monitor_id=1)["episode_qualities"][1]
    assert quality["source"] == ("release" if stale else "file")
    if not stale:
        assert quality["dimensions"] == [1920, 1080]
        assert quality["label"] == "1080p verified"
    assert db.total_changes == changes
