"""Home resumes accepted viewing activity, independently of catalog ordering."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from omakase.plus.product_views import home_view, library_titles
from tests.plus.test_streaming_templates import render
from tests.plus.test_viewing_history import db as db
from tests.plus.test_viewing_history import entry, event


def test_home_sorts_all_in_progress_titles_before_ten_card_limit(db):
    expected = []
    for number in range(12):
        title = f"Alpha {number:02d}" if number < 11 else "Zed most recently watched"
        monitor, watch = entry(db, anilist_id=1000 + number, title=title)
        stamp = (datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=number)).isoformat()
        event(db, watch, stamp=stamp, key=f"watch:{watch}")
        expected.insert(0, monitor)
    completed, _ = entry(db, anilist_id=2000, title="Already finished")
    db.execute(
        "UPDATE watch_entries SET status='completed',completed_at='2026-10-28T00:00:00Z' WHERE monitor_id=?",
        (completed,),
    )
    entry(db, user_id=2, anilist_id=3000, title="Other account's private series")
    changes = db.total_changes
    cards = home_view(db, user_id=1)["continue_watching"]
    assert [card["id"] for card in cards] == expected[:10]
    assert db.total_changes == changes
    assert library_titles(db, user_id=1)[0]["title"] == "Alpha 00"


def test_home_uses_normalized_watch_time_not_newer_snapshot_import_time(db):
    older, watch = entry(db, anilist_id=21, title="Alpha old viewing imported later")
    event(
        db,
        watch,
        episode=3,
        stamp="2026-11-10T10:00:00Z",
        key="plex:snapshot:701:s1:e3:2026-10-28T23:00:00Z",
    )
    newer, watch = entry(db, anilist_id=22, title="Zed latest actual viewing")
    event(
        db,
        watch,
        source="apple_tv",
        stamp="2026-10-29T05:31:00Z",
        payload={"watched_at": "2026-10-29T01:30:00-04:00"},
        key="apple:newer",
    )
    assert [card["id"] for card in home_view(db, user_id=1)["continue_watching"]] == [newer, older]


@pytest.mark.parametrize(
    "irrelevant",
    [
        {"disposition": "ignored"},
        {"disposition": "confirmation_required"},
        {"source": "manual", "kind": "completed", "reason": "completion_score_updated"},
        {"source": "manual", "key": "manual:bulk:fixture"},
        {"source": "migration"},
        {"source": "manual", "kind": "correction"},
        {"source": "manual", "kind": "reopened"},
        {"stamp": "2026-99-99T99:99:99Z"},
        {"key": "plex:snapshot:701:s1:e3:None", "episode": 3},
        {"key": "plex:snapshot:701:s2:e3:2099-01-01T00:00:00Z", "episode": 3},
        {"key": "plex:snapshot:701:s1:e2:2099-01-01T00:00:00Z", "episode": 3},
        pytest.param(
            {"key": "plex:snapshot:bad-key:s1:e3:2099-01-01T00:00:00Z", "episode": 3},
            id="snapshot-bad-rating-key",
        ),
        pytest.param(
            {"key": "plex:snapshot:0:s1:e3:2099-01-01T00:00:00Z", "episode": 3},
            id="snapshot-zero-rating-key",
        ),
        pytest.param(
            {
                "key": "plex:snapshot:123456789012345678901234567890123:s1:e3:2099-01-01T00:00:00Z",
                "episode": 3,
            },
            id="snapshot-long-rating-key",
        ),
    ],
)
def test_non_viewing_or_unqualified_events_never_raise_resume_priority(db, irrelevant):
    older, old_watch = entry(db, anilist_id=21, title="Alpha older viewing")
    event(db, old_watch, stamp="2026-10-20T00:00:00Z", key="valid:older")
    newer, new_watch = entry(db, anilist_id=22, title="Zed newer viewing")
    event(db, new_watch, stamp="2026-10-29T00:00:00Z", key="valid:newer")
    arguments = {"stamp": "2099-01-01T00:00:00Z", "key": "later:non-viewing", **irrelevant}
    event(db, old_watch, **arguments)
    db.execute(
        "UPDATE watch_entries SET last_observed_at='2099-01-01T00:00:00Z' WHERE id=?", (old_watch,)
    )
    assert [card["id"] for card in home_view(db, user_id=1)["continue_watching"]] == [newer, older]


def test_unknown_dates_follow_dated_titles_with_stable_catalog_ties(db):
    alpha, _ = entry(db, anilist_id=21, title="Alpha unknown date")
    beta, watch = entry(db, anilist_id=22, title="Beta unknown snapshot date")
    event(db, watch, episode=3, stamp="2099-01-01T00:00:00Z", key="plex:snapshot:702:s1:e3:None")
    zed, watch = entry(db, anilist_id=23, title="Zed dated viewing")
    event(db, watch, stamp="2026-10-29T10:00:00+09:00", key="actual:dated")
    assert [card["id"] for card in home_view(db, user_id=1)["continue_watching"]] == [
        zed,
        alpha,
        beta,
    ]
    assert [card["id"] for card in home_view(db, user_id=1)["continue_watching"]] == [
        zed,
        alpha,
        beta,
    ]


def test_equivalent_timezone_dates_have_a_stable_title_identity_tie(db):
    alpha, watch = entry(db, anilist_id=21, title="Alpha same actual time")
    event(db, watch, stamp="2026-10-29T10:00:00Z", key="actual:alpha")
    zed, watch = entry(db, anilist_id=22, title="Zed same actual time")
    event(db, watch, stamp="2026-10-29T06:00:00-04:00", key="actual:zed")
    assert [card["id"] for card in home_view(db, user_id=1)["continue_watching"]] == [alpha, zed]


@pytest.mark.parametrize("has_continue", [True, False])
def test_hero_uses_same_first_resume_card_even_when_partial_ready(db, has_continue):
    _, watch = entry(db, anilist_id=21, title="Alpha older ready title")
    event(db, watch, stamp="2026-10-20T00:00:00Z", key="older")
    _, watch = entry(db, anilist_id=22, title="Zed newer partial title")
    event(db, watch, stamp="2026-10-29T00:00:00Z", key="newer")
    context = home_view(db, user_id=1)
    cards = {card["anilist_id"]: card for card in context["continue_watching"]}
    older = {**cards[21], "state": "ready", "usable": 12, "plex_visible": 12}
    newer = {**cards[22], "state": "acquiring", "usable": 3, "plex_visible": 3}
    context["continue_watching"] = [newer, older] if has_continue else []
    context["ready_to_watch"] = [older]
    html = render("home.html", **context)
    hero = html.split('<section class="cinema-hero"', 1)[1].split("</section>", 1)[0]
    expected = newer if has_continue else older
    assert re.search(r'<h1 id="featured-title">([^<]+)</h1>', hero).group(1) == expected["title"]
    assert f'href="/plus/library/{expected["id"]}#overview"' in hero
    if has_continue:
        hero_text = " ".join(re.sub(r"<[^>]*>", " ", hero).split())
        assert "Availability unknown" in hero_text
        assert "Watched 3/12" in hero_text
        assert "In Plex 12/12" not in hero_text


def test_shared_home_ranks_only_this_viewers_visible_scoped_titles(db, monkeypatch):
    from omakase.plus import household_media as media
    from omakase.plus import households as hh
    from omakase.plus.watch.models import WatchEvent
    from tests.plus.test_household_media import scopes, shared

    library = shared(db)
    owner, viewer = scopes(db, library)
    for number, title in [(21, "Alpha shared title"), (22, "Zed shared title")]:
        monitor, watch = entry(db, anilist_id=number, title=title)
        event(db, watch, stamp="2099-01-01T00:00:00Z", key=f"owner:{number}")
        item = hh.LibraryRepository(db, owner).add(
            domain="anime", canonical_id=str(number), season=1, title=title, legacy_id=str(monitor)
        )
        stamp = f"2026-10-{20 if number == 21 else 29}T00:00:00Z"
        monkeypatch.setattr(hh, "_stamp", lambda stamp=stamp: stamp)
        media.save_anime_watch(
            db,
            media.binding(db, viewer, item_id=item),
            event=WatchEvent(
                source="manual",
                kind="progress",
                confidence="manual",
                episode=3,
                event_key=f"viewer:{number}",
            ),
        )
    _, watch = entry(db, user_id=2, anilist_id=99, title="Private personal title outside scope")
    event(db, watch, user_id=2, stamp="2099-01-02T00:00:00Z", key="private:personal")
    changes = db.total_changes
    assert [
        card["title"]
        for card in home_view(db, user_id=2, library_scope=viewer)["continue_watching"]
    ] == ["Zed shared title", "Alpha shared title"]
    assert db.total_changes == changes
