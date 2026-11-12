"""Synthetic navigation never grants a source or acquires another episode."""

import json
from types import SimpleNamespace

import pytest

from omakase.plus import households
from omakase.plus import web_playback_navigation as nav
from omakase.plus.product_views import home_view
from tests.plus.test_streaming_templates import render
from tests.plus.test_viewing_history import db as db
from tests.plus.test_viewing_history import entry
from tests.plus.test_web_playback_source import fixture as fixture


def _enable(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_WEB_PLAYER_ENABLED", "true")


def _source(db):
    return SimpleNamespace(
        _scope=households.resolve_scope(db, viewer_id=2, library_id="library"),
        actor_user_id=2,
        library_id="library",
        anilist_id=123,
        season_number=1,
        episode_number=1,
    )


def _next_file(db, receipt, **changes):
    receipt["publication_files"].append(
        {**receipt["publication_files"][0], "episodes": [2], **changes}
    )
    db.execute("UPDATE media_curator_receipt_latest SET receipt_json=?", (json.dumps(receipt),))


def test_next_is_exact_same_title_season_and_read_only(fixture, monkeypatch):
    db, _, receipt, _ = fixture
    _enable(monkeypatch)
    _next_file(db, receipt)
    changes = db.total_changes
    assert (
        nav.exact_next_url(db, source=_source(db), monitor_id=1)
        == "/plus/watch/1/2?library_id=library&play=1"
    )
    assert db.total_changes == changes


@pytest.mark.parametrize(
    "changes",
    [
        {"anilist_id": 999},
        {"season_number": 2},
        {"episodes": [3]},
        {"episodes": [1, 2]},
        {"selected": False},
        {"source_generation": "old"},
    ],
)
def test_next_missing_or_other_identity_never_falls_back(fixture, monkeypatch, changes):
    db, _, receipt, _ = fixture
    _enable(monkeypatch)
    _next_file(db, receipt, **changes)
    assert nav.exact_next_url(db, source=_source(db), monitor_id=1) is None


def test_revoked_shared_access_and_wrong_monitor_have_no_next(fixture, monkeypatch):
    db, _, receipt, _ = fixture
    _enable(monkeypatch)
    _next_file(db, receipt)
    source = _source(db)
    assert nav.exact_next_url(db, source=source, monitor_id=99) is None
    db.execute("UPDATE household_memberships SET state='revoked'")
    assert nav.exact_next_url(db, source=source, monitor_id=1) is None


def test_revoked_binding_has_no_next(fixture, monkeypatch):
    db, _, receipt, _ = fixture
    _enable(monkeypatch)
    _next_file(db, receipt)
    db.execute("UPDATE library_items SET state='removed'")
    assert nav.exact_next_url(db, source=_source(db), monitor_id=1) is None


def test_default_off_preserves_overview_and_last_episode_has_no_next(fixture, monkeypatch):
    db, _, receipt, _ = fixture
    monkeypatch.delenv("OMAKASE_PLUS_WEB_PLAYER_ENABLED", raising=False)
    assert not nav.enabled()
    assert nav.exact_next_url(db, source=_source(db), monitor_id=1) is None
    _enable(monkeypatch)
    assert nav.exact_next_url(db, source=_source(db), monitor_id=1) is None


def test_personal_legacy_candidate_never_creates_a_binding(fixture, monkeypatch):
    db, _, _, _ = fixture
    _enable(monkeypatch)
    db.execute("UPDATE libraries SET kind='personal'")
    db.execute("DELETE FROM library_items")
    db.execute("DELETE FROM library_resources")
    scope = households.resolve_scope(db, viewer_id=1, library_id="library")
    monitor = db.execute("SELECT * FROM media_monitors WHERE id=1").fetchone()
    changes = db.total_changes
    assert nav._candidates(db, scope, monitor) == {1: None}
    assert db.total_changes == changes
    assert db.execute("SELECT COUNT(*) FROM library_resources").fetchone()[0] == 0


def test_ambiguous_published_file_and_changed_inventory_have_no_candidate(fixture):
    db, _, receipt, _ = fixture
    scope = _source(db)._scope
    monitor = db.execute("SELECT * FROM media_monitors WHERE id=1").fetchone()
    _next_file(db, receipt, episodes=[1])
    assert nav._candidates(db, scope, monitor) == {}
    receipt["publication_files"].pop()
    db.execute("UPDATE media_curator_receipt_latest SET receipt_json=?", (json.dumps(receipt),))
    db.execute("UPDATE media_assets SET inventory_fingerprint='changed'")
    assert nav._candidates(db, scope, monitor) == {}


def test_anime_movie_publication_does_not_offer_native_series_play(fixture):
    db, _, _, _ = fixture
    scope = _source(db)._scope
    db.execute("ALTER TABLE media_monitors ADD COLUMN format TEXT DEFAULT 'MOVIE'")
    monitor = db.execute("SELECT * FROM media_monitors WHERE id=1").fetchone()
    assert nav._candidates(db, scope, monitor) == {}


def _partial(db, user_id, library_id, anilist_id):
    db.execute(
        "INSERT INTO web_playback_watch(user_id,library_id,anilist_id,season_number,episode_number,media_version,covered_seconds,duration_seconds,revision,last_played_at) VALUES(?,?,?,1,1,?,30,1400,1,'2026-10-31T12:00:00Z')",
        (user_id, library_id, anilist_id, "a" * 64),
    )


def test_partial_first_episode_with_zero_progress_is_home_continue_even_flag_off(db, monkeypatch):
    monkeypatch.delenv("OMAKASE_PLUS_WEB_PLAYER_ENABLED", raising=False)
    library_id = households.ensure_personal_library(db, user_id=1)
    monitor, watch = entry(db)
    db.execute("UPDATE watch_entries SET progress_episodes=0 WHERE id=?", (watch,))
    _partial(db, 1, library_id, 21)
    changes = db.total_changes
    context = home_view(db, user_id=1)
    card = context["continue_watching"][0]
    assert card["id"] == monitor and card["partial_playback"]
    assert card["progress_episodes"] == 0
    assert not card.get("play_url")
    html = render("home.html", **context)
    assert "Episode in progress" in html and "More info" in html
    assert f"/plus/library/{monitor}#episode-1" in html
    assert db.total_changes == changes


def test_unrelated_actor_partial_does_not_create_personal_continue(db):
    households.ensure_personal_library(db, user_id=1)
    other = households.ensure_personal_library(db, user_id=2)
    _, watch = entry(db)
    db.execute("UPDATE watch_entries SET progress_episodes=0 WHERE id=?", (watch,))
    _partial(db, 2, other, 21)
    assert home_view(db, user_id=1)["continue_watching"] == []


def test_home_play_retains_more_info_and_episode_links():
    card = dict(
        id=7,
        title="Synthetic",
        anilist_id=123,
        progress_episodes=0,
        episode_total=12,
        next_episode=1,
        state="planned",
        play_url="/plus/watch/7/1?library_id=opaque",
        play_label="Play",
    )
    html = render(
        "home.html",
        continue_watching=[],
        ready_to_watch=[card],
        confirmation_needed=[],
        attention=[],
        recent_activity=[],
        new_episodes=[],
    )
    assert "/plus/watch/7/1?library_id=opaque" in html
    assert "/plus/library/7#overview" in html and "More info" in html


def test_home_resume_hero_shows_episode_and_watched_count_once():
    card = dict(
        id=7,
        title="Synthetic",
        anilist_id=123,
        progress_episodes=10,
        progress_percent=10 / 13 * 100,
        episode_total=13,
        next_episode=11,
        partial_playback=True,
        state="ready",
        required=13,
        usable=13,
        plex_visible=13,
        play_url="/plus/watch/7/11?library_id=opaque",
        play_label="Resume",
        play_episode=11,
        episode_resume={"percent": 40, "minutes_left": 14},
    )
    html = render(
        "home.html",
        continue_watching=[card],
        ready_to_watch=[{**card, "progress_episodes": 0, "partial_playback": False}],
        confirmation_needed=[],
        attention=[],
        recent_activity=[],
        new_episodes=[card],
    )
    hero = html.split('<section class="cinema-hero"', 1)[1].split("</section>", 1)[0]
    assert "Resume episode 11" in hero
    assert "Watched <b>10/13</b>" in hero
    assert "episodes watched through" not in hero
    rail = html.split('aria-label="Continue watching">', 1)[1].split("</section>", 1)[0]
    assert "Episode 11 · 14 min left" in rail
    assert rail.count("<progress ") == 1
    assert f'<progress max="100" value="{10 / 13 * 100}"' in rail
    assert "Watched <b>10/13</b>" in rail


@pytest.mark.parametrize(
    "identity,expected_episode,label", [("b" * 64, 2, "Resume"), ("c" * 64, 1, "Play")]
)
def test_resume_uses_actual_unfinished_episode_and_durable_source_identity(
    db, monkeypatch, identity, expected_episode, label
):
    _enable(monkeypatch)
    library_id = households.ensure_personal_library(db, user_id=1)
    monitor, watch = entry(db)
    db.execute("UPDATE watch_entries SET progress_episodes=0 WHERE id=?", (watch,))
    _partial(db, 1, library_id, 21)
    db.execute("UPDATE web_playback_watch SET episode_number=2")
    from omakase.plus import web_playback_store as store

    store.create_session(
        db,
        target=store.ResolvedTarget(1, library_id, "21", 1, 2, "a" * 64, 1400),
        binding_digest="f" * 64,
        snapshot={
            "membership": {},
            "policy": {},
            "source": {"identity": identity, "media_version": "a" * 64},
        },
        now=100,
    )
    monkeypatch.setattr(nav, "_candidates", lambda *a: {1: "b" * 64, 2: "b" * 64})
    changes = db.total_changes
    card = home_view(db, user_id=1)["continue_watching"][0]
    assert (
        card["play_url"]
        == f"/plus/watch/{monitor}/{expected_episode}?library_id={library_id}&play=1"
    )
    assert card["play_label"] == label
    assert card["progress_episodes"] == 0
    assert db.total_changes == changes


def test_tv_checkpoint_surfaces_on_home_without_counting_as_watched(db, monkeypatch):
    from omakase.plus import web_playback_store as store

    _enable(monkeypatch)
    library_id = households.ensure_personal_library(db, user_id=1)
    monitor, watch = entry(db)
    db.execute("UPDATE watch_entries SET progress_episodes=0 WHERE id=?", (watch,))
    target = store.ResolvedTarget(1, library_id, "21", 1, 2, "a" * 64, 1400)
    store.save_native_resume(db, target=target, position=710, publication="b" * 64, expected=None)
    monkeypatch.setattr(nav, "_candidates", lambda *args: {1: "c" * 64, 2: "b" * 64})
    card = home_view(db, user_id=1)["continue_watching"][0]
    assert card["play_url"] == f"/plus/watch/{monitor}/2?library_id={library_id}&play=1"
    assert card["play_label"] == "Resume"
    assert card["episode_resume"] == {"percent": 51, "minutes_left": 12}
    assert card["progress_episodes"] == 0
    assert db.execute("SELECT count(*) FROM web_playback_watch").fetchone()[0] == 0
    monkeypatch.setattr(nav, "_candidates", lambda *args: {1: "c" * 64, 2: "d" * 64})
    assert all(
        item["play_label"] != "Resume" for item in home_view(db, user_id=1)["continue_watching"]
    )
    monkeypatch.setattr(nav, "_candidates", lambda *args: {1: "c" * 64, 2: "b" * 64})
    db.execute(
        """INSERT INTO watch_events (watch_entry_id,user_id,source,event_key,kind,disposition,observed_at)
           VALUES(?,1,'manual','correction','correction','accepted','2026-11-10T00:00:00Z')""",
        (watch,),
    )
    assert all(
        item["play_label"] != "Resume" for item in home_view(db, user_id=1)["continue_watching"]
    )
