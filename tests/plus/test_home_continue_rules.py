"""Continue Watching fold rules, banner choice and per-profile Home layout.

These tests drive the same `omakase.plus.home_shell` projection the web page and
the Apple TV client read, so a rule proved here is the rule both surfaces render.
Fixtures are synthetic rows in the real tables; no provider or Plex is contacted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omakase.plus import home_shell, households

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def db():
    """One migrated in-memory database per test."""
    import sqlite3

    from omakase.plus.db import run_migrations

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    run_migrations(connection)
    yield connection
    connection.close()


def signal(**overrides):
    base = dict(
        domain="tv",
        key="1",
        title="Synthetic Show",
        title_id="1",
        last_watch_at="2026-11-10T20:00:00Z",
        season=1,
        episode=3,
        next_episode=None,
        next_playable=False,
        play_url="/plus/watch/title/tv/1/1/3?play=1",
        detail_url="/plus/tv/1",
        subtitle="S1 E3",
    )
    base.update(overrides)
    return home_shell.ContinueSignal(**base)


def fold(*signals):
    return home_shell.fold_continue_watching(list(signals))


# --------------------------------------------------------------- one per show


def test_one_card_per_show_even_with_many_episode_observations():
    result = fold(
        signal(episode=1, last_watch_at="2026-11-01T20:00:00Z"),
        signal(episode=2, last_watch_at="2026-11-05T20:00:00Z"),
        signal(episode=3, last_watch_at="2026-11-09T20:00:00Z"),
    )
    assert len(result["cards"]) == 1
    card = result["cards"][0]
    assert card["episode"]["episode"] == 3
    assert card["last_watch_at"] == "2026-11-09T20:00:00Z"


def test_partially_watched_movie_keeps_its_saved_position():
    result = fold(
        signal(
            domain="movie",
            key="tt1160419",
            title="Dune",
            episode=None,
            season=None,
            subtitle="Movie · 2021",
            position_seconds=4020,
            duration_seconds=9360,
            play_url="/plus/watch/title/movie/tt1160419?play=1",
        )
    )
    card = result["cards"][0]
    assert card["play_label"] == "Resume"
    assert card["play_action"] == "resume"
    assert card["play_url"] == "/plus/watch/title/movie/tt1160419?play=1"
    assert card["progress"]["percent"] == 43
    assert card["progress"]["minutes_left"] == 89
    assert card["detail_label"] == "89 min left"


def test_completed_episode_with_a_playable_next_episode_offers_play_episode_n():
    result = fold(
        signal(
            episode=4,
            next_episode=5,
            next_playable=True,
            completed=True,
            play_url="/plus/watch/title/tv/1/1/5?play=1",
        )
    )
    card = result["cards"][0]
    assert card["play_label"] == "Play episode 5"
    assert card["play_action"] == "play-next"
    assert card["play_url"].endswith("/1/1/5?play=1")


def test_tv_resume_and_next_episode_target_stay_coherent_across_seasons(db):
    viewer = db.execute(
        "INSERT INTO users(email,password_hash) VALUES('seasons@example.test','fixture')"
    ).lastrowid
    library = households.ensure_personal_library(db, user_id=viewer)
    db.execute(
        "INSERT INTO tv_catalog VALUES(?,1,?,?)",
        (viewer, '{"title":"Synthetic seasons"}', "2026-11-01T00:00:00Z"),
    )
    db.executemany(
        """INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,
                                   watched,watch_source,plex_present,updated_at)
           VALUES(?,1,?,?,?,?,'manual',1,?)""",
        [
            (viewer, 1, 12, '{"air_date":"2026-09-01"}', 1, "2026-11-03T00:00:00Z"),
            (viewer, 2, 1, '{"air_date":"2026-10-01"}', 0, "2026-11-04T00:00:00Z"),
            (viewer, 2, 2, '{"air_date":"2026-10-08"}', 0, "2026-11-04T00:00:00Z"),
        ],
    )

    def card():
        return fold(*home_shell.tv_signals(db, user_id=viewer, now=NOW, library_id=library))[
            "cards"
        ][0]

    assert card()["play_url"].endswith("/1/2/1?play=1")
    assert card()["play_label"] == "Play episode 1"

    db.execute(
        """INSERT INTO web_playback_resumes(target_key,user_id,library_id,domain,canonical_id,
             season_number,episode_number,media_version,duration_seconds,position_seconds,updated_at)
           VALUES('resume',?,?,'tv','1',2,1,?,3600,600,?)""",
        (viewer, library, "a" * 64, datetime(2026, 11, 5, tzinfo=UTC).timestamp()),
    )
    resume = card()
    assert resume["play_url"].endswith("/1/2/1?play=1")
    assert resume["play_label"] == "Resume episode 1"
    assert resume["episode"]["season"] == 2 and resume["episode"]["episode"] == 1
    assert resume["progress"]["position_seconds"] == 600

    db.execute("UPDATE tv_episodes SET watched=1 WHERE season_number=2 AND episode_number=1")
    db.execute("UPDATE web_playback_resumes SET position_seconds=3600")
    next_card = card()
    assert next_card["play_url"].endswith("/1/2/2?play=1")
    assert next_card["play_label"] == "Play episode 2"
    assert next_card["progress"]["percent"] is None


# ------------------------------------------------------- caught up then returns


def test_caught_up_show_is_hidden_until_its_next_episode_is_playable():
    caught_up = fold(signal(episode=12, caught_up=True, completed=True, next_playable=False))
    assert caught_up["cards"] == []
    assert [card["hidden_reason"] for card in caught_up["hidden"]] == ["awaiting-next-episode"]

    returned = fold(
        signal(
            episode=12,
            next_episode=13,
            next_playable=True,
            completed=True,
            play_url="/plus/watch/title/tv/1/2/13?play=1",
            last_watch_at="2026-11-12T20:00:00Z",
        )
    )
    assert [card["play_label"] for card in returned["cards"]] == ["Play episode 13"]


def test_returning_show_is_ranked_by_last_watch_time_not_by_arrival():
    # The returning show was watched earlier than the steady one, so it slots
    # behind it even though its next episode only just became playable.
    returning = signal(
        key="old",
        title="Older show",
        episode=12,
        next_episode=13,
        next_playable=True,
        completed=True,
        play_url="/plus/watch/title/tv/2/2/13?play=1",
        last_watch_at="2026-11-02T20:00:00Z",
    )
    steady = signal(
        key="new",
        title="Newer show",
        episode=4,
        play_url="/plus/watch/title/tv/3/1/4?play=1",
        last_watch_at="2026-11-13T20:00:00Z",
    )
    result = fold(returning, steady)
    assert [card["key"] for card in result["cards"]] == ["new", "old"]


def test_a_resume_inside_the_last_episode_survives_a_catch_up():
    # The viewer is part way through the last episode: nothing is hidden.
    result = fold(
        signal(
            episode=12,
            caught_up=True,
            completed=False,
            next_playable=False,
            position_seconds=600,
            duration_seconds=1440,
        )
    )
    assert [card["play_label"] for card in result["cards"]] == ["Resume episode 12"]


def test_announced_but_unavailable_episode_stays_hidden():
    result = fold(
        signal(
            episode=8,
            next_episode=9,
            next_playable=False,
            completed=True,
            caught_up=True,
        )
    )
    assert result["cards"] == []
    assert result["hidden"][0]["hidden_reason"] == "awaiting-next-episode"


def test_same_rules_apply_to_anime_and_tv():
    anime = fold(
        signal(
            domain="anime",
            key="7",
            title="Anime show",
            episode=12,
            next_episode=13,
            next_playable=True,
            completed=True,
            play_url="/plus/watch/7/13?play=1",
        )
    )
    tv = fold(
        signal(
            domain="tv",
            key="9",
            title="TV show",
            episode=12,
            next_episode=13,
            next_playable=True,
            completed=True,
            play_url="/plus/watch/title/tv/9/2/13?play=1",
        )
    )
    assert anime["cards"][0]["play_label"] == tv["cards"][0]["play_label"] == "Play episode 13"


# ------------------------------------------------- watch time is the only clock


def test_release_date_and_sync_stamps_never_reach_the_sort_key():
    """A title whose metadata/sync rows moved ranks by its own watch time."""
    before = fold(
        signal(key="a", title="Alpha", last_watch_at="2026-11-04T20:00:00Z"),
        signal(key="b", title="Beta", last_watch_at="2026-11-09T20:00:00Z"),
    )
    # Same watch evidence, but the "sync" and "release" fields churned.
    after = fold(
        signal(
            key="a",
            title="Alpha",
            last_watch_at="2026-11-04T20:00:00Z",
            meta={"sync": "2026-11-15T11:00:00Z", "next_release_at": "2026-11-20T00:00:00Z"},
        ),
        signal(
            key="b",
            title="Beta",
            last_watch_at="2026-11-09T20:00:00Z",
            meta={"sync": "2026-11-15T11:00:00Z", "next_release_at": "2026-11-21T00:00:00Z"},
        ),
    )
    assert [c["key"] for c in before["cards"]] == [c["key"] for c in after["cards"]] == ["b", "a"]


def test_a_signal_without_watch_evidence_is_ignored():
    result = fold(home_shell.ContinueSignal(domain="tv", key="1", title="Ghost"))
    assert result["cards"] == [] and result["hidden"] == []


# ------------------------------------------------------------------- banner


def test_banner_takes_the_most_recently_watched_playable_card():
    rows = [
        {
            "id": "continue-anime",
            "label": "Continue Watching Anime",
            "cards": [
                {
                    "id": "anime-1",
                    "domain": "anime",
                    "key": "1",
                    "title": "Anime older",
                    "title_id": "1",
                    "subtitle": "S1 E3",
                    "detail_label": "Up next",
                    "progress": {"percent": None, "remaining_label": ""},
                    "episode": {
                        "season": 1,
                        "episode": 3,
                        "next_episode": 4,
                        "next_playable": True,
                    },
                    "play_url": "/plus/watch/1/4?play=1",
                    "play_label": "Play episode 4",
                    "play_action": "play-next",
                    "detail_url": "/plus/library/1#overview",
                    "artwork": {"landscape": "/a.jpg", "poster": "/a-p.jpg"},
                    "last_watch_at": "2026-11-05T20:00:00Z",
                }
            ],
        },
        {
            "id": "continue-tv",
            "label": "Continue Watching TV Shows",
            "cards": [
                {
                    "id": "tv-9",
                    "domain": "tv",
                    "key": "9",
                    "title": "TV newer",
                    "title_id": "9",
                    "subtitle": "S1 E2",
                    "detail_label": "14 min left",
                    "progress": {"percent": 40, "remaining_label": "14 min left"},
                    "episode": {
                        "season": 1,
                        "episode": 2,
                        "next_episode": 2,
                        "next_playable": True,
                    },
                    "play_url": "/plus/watch/title/tv/9/1/2?play=1",
                    "play_label": "Resume episode 2",
                    "play_action": "resume",
                    "detail_url": "/plus/tv/9",
                    "artwork": {"landscape": "/b.jpg", "poster": "/b-p.jpg"},
                    "last_watch_at": "2026-11-12T20:00:00Z",
                }
            ],
        },
    ]
    banner = home_shell.select_banner(rows)
    assert banner["title"] == "TV newer"
    assert banner["domain"] == "tv"
    assert banner["play_url"] == "/plus/watch/title/tv/9/1/2?play=1"
    assert banner["progress"]["remaining_label"] == "14 min left"

    anime = rows[0]["cards"][0]
    movie = {
        **anime,
        "domain": "movie",
        "title": "Movie midweek",
        "play_url": "/plus/watch/title/movie/tt1234567?play=1",
        "last_watch_at": "2026-11-10T20:00:00Z",
    }
    rows.append(
        {"cards": [movie, {**anime, "title": "Older anime", "last_watch_at": "2026-11-01"}]}
    )
    rows.append({"cards": [{**movie, "title": "Unavailable movie", "play_url": None}]})
    choices = home_shell.select_banner_choices(rows)
    assert [choice["title"] for choice in choices] == ["TV newer", "Movie midweek", "Anime older"]
    assert choices[1]["play_url"] == movie["play_url"]


def test_banner_is_neutral_when_a_viewer_has_no_active_titles():
    banner = home_shell.select_banner([])
    assert banner is None
    neutral = home_shell.neutral_banner()
    assert neutral["neutral"] is True
    assert neutral["detail_url"] == "/plus/library"
    assert neutral["artwork"]["landscape"] is None


def test_banner_prefers_a_playable_card_and_falls_back_to_details():
    detail_only = [
        {
            "id": "continue-anime",
            "cards": [
                {
                    "id": "anime-1",
                    "domain": "anime",
                    "key": "1",
                    "title": "Overview only",
                    "title_id": "1",
                    "subtitle": "Episode 2",
                    "detail_label": "Up next",
                    "progress": {"percent": None, "remaining_label": ""},
                    "episode": {
                        "season": 1,
                        "episode": 2,
                        "next_episode": 2,
                        "next_playable": False,
                    },
                    "play_url": None,
                    "play_label": "View episodes",
                    "play_action": "detail",
                    "detail_url": "/plus/library/1#overview",
                    "artwork": {"landscape": "/a.jpg", "poster": None},
                    "last_watch_at": "2026-11-05T20:00:00Z",
                }
            ],
        }
    ]
    banner = home_shell.select_banner(detail_only)
    assert banner["play_url"] == "/plus/library/1#overview"
    assert banner["play_label"] == "View episodes"


# ------------------------------------------------------------ layout storage


@pytest.fixture
def seeded(db):
    user_ids = []
    for email in ("first@example.test", "second@example.test"):
        user_id = db.execute(
            "INSERT INTO users(email,password_hash) VALUES(?, 'fixture')", (email,)
        ).lastrowid
        households.ensure_personal_library(db, user_id=user_id)
        user_ids.append(user_id)
    db.commit()
    return user_ids


def test_defaults_are_the_three_continue_rows_and_no_ready_shelf(db):
    layout = home_shell.load_layout(db, user_id=1)["rows"]
    assert [row["id"] for row in layout if not row["hidden"]][:3] == [
        "continue-anime",
        "continue-movies",
        "continue-tv",
    ]
    visible = {row["id"] for row in layout if not row["hidden"]}
    assert "ready-to-watch" not in visible
    assert "new-episodes" not in {row["id"] for row in layout}


def test_layout_is_stored_per_profile_and_survives_a_reload(db, seeded):
    first, second = seeded
    reordered = [
        {"id": "continue-tv", "hidden": False},
        {"id": "continue-anime", "hidden": True},
        {"id": "continue-movies", "hidden": False},
    ]
    home_shell.save_layout(db, user_id=first, rows=reordered)
    stored = home_shell.load_layout(db, user_id=first)["rows"]
    assert [row["id"] for row in stored if not row["hidden"]][:2] == [
        "continue-tv",
        "continue-movies",
    ]
    assert next(row for row in stored if row["id"] == "continue-anime")["hidden"] is True
    # The other profile keeps the shipped default, untouched.
    other = home_shell.load_layout(db, user_id=second)["rows"]
    assert [row["id"] for row in other if not row["hidden"]][:3] == [
        "continue-anime",
        "continue-movies",
        "continue-tv",
    ]
    assert next(row for row in other if row["id"] == "continue-movies")["hidden"] is False


def test_move_and_hide_controls_apply_to_the_stored_order(db):
    rows = home_shell.load_layout(db, user_id=1)["rows"]
    moved = home_shell.apply_layout_action(rows, "move-up", "continue-tv")
    assert [row["id"] for row in moved][:3] == [
        "continue-anime",
        "continue-tv",
        "continue-movies",
    ]
    hidden = home_shell.apply_layout_action(moved, "remove", "continue-anime")
    assert next(row for row in hidden if row["id"] == "continue-anime")["hidden"] is True
    back = home_shell.apply_layout_action(hidden, "add", "continue-anime")
    assert next(row for row in back if row["id"] == "continue-anime")["hidden"] is False


def test_hiding_every_row_is_rejected(db):
    rows = [{"id": row["id"], "hidden": True} for row in home_shell.default_layout()]
    with pytest.raises(home_shell.HomeLayoutError):
        home_shell.save_layout(db, user_id=1, rows=rows)


def test_unknown_and_retired_row_ids_are_dropped_not_fatal():
    normalized = home_shell.normalize_layout(
        [
            {"id": "continue-tv", "hidden": False},
            {"id": "new-episodes", "hidden": False},
            {"id": "made-up-shelf", "hidden": False},
        ]
    )
    ids = [row["id"] for row in normalized]
    assert ids[0] == "continue-tv"
    assert "new-episodes" not in ids and "made-up-shelf" not in ids
    assert set(home_shell.DEFAULT_LAYOUT_IDS) <= set(ids)


def test_projection_is_json_serialisable_for_the_native_client(db, seeded):
    import json

    first, _ = seeded
    payload = home_shell.projection(db, user_id=first, now=NOW)
    encoded = json.dumps(payload)
    assert '"banner"' in encoded and '"rows"' in encoded
    assert [row["id"] for row in payload["rows"]][:3] == [
        "continue-anime",
        "continue-movies",
        "continue-tv",
    ]
    assert payload["layout"]["rows"][0]["label"] == "Continue Watching Anime"
    assert payload["addable"], "hidden shelves must stay addable"
