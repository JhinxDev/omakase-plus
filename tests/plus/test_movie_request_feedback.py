"""Saved request reports never impersonate an active search or viewing record."""

from copy import deepcopy
from datetime import timedelta

import pytest

from omakase.plus import movies, movies_store
from tests.plus.test_movies import IMDB, NOW, TMDB, NativeMovies, run, service
from tests.plus.test_movies import db as db


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("Unknown", ("deferred", "backend_state_unknown")),
        ("Indexed", ("requested", "requested")),
        ("Scraped", ("requested", "search_complete")),
        ("Failed", ("unavailable", "backend_failed")),
        ("Searching", ("searching", "searching")),
    ],
)
def test_riven_report_does_not_invent_search_activity(reported, expected):
    assert movies._riven_state(reported) == expected


def requested(db, *, state="Searching", clock=None):
    class ReportedMovies(NativeMovies):
        def movie(self):
            return {**super().movie(), "state": state}

    fake = ReportedMovies()
    fake.items = [fake.movie()]
    svc = service(db, fake, now=(lambda: clock[0]) if clock else None)
    card = run(svc.request(1, imdb_id=IMDB, tmdb_id=TMDB))
    assert fake.posts == []
    return svc, fake, card


@pytest.mark.parametrize("owned", [True, False])
def test_current_inventory_observation_is_separate_from_an_old_or_absent_saved_request(db, owned):
    from omakase.plus.movie_presentation import present_movie

    clock = [NOW]
    svc, fake, _ = requested(db, clock=clock)
    if not owned:
        db.execute("DELETE FROM movie_requests")
        db.commit()
    clock[0] += timedelta(hours=3)
    fake.items = [{**fake.movie(), "state": "Downloading"}]
    fake.xml_override = b'<MediaContainer librarySectionID="6" size="0" totalSize="0"/>'
    before_history = db.execute("SELECT COUNT(*) FROM movie_history").fetchone()[0]
    card = next(card for card in run(svc.list(1)) if card["imdb_id"] == IMDB)
    display = present_movie(card, now=clock[0])["display"]
    assert card["request_observation"] == {
        "state": "acquiring",
        "code": "acquiring",
        "observed_at": "2026-10-27T18:00:00Z",
    }
    assert display["status_label"] == "Transfer reported"
    assert display["request_observed_at"] == "2026-10-27T18:00:00Z"
    assert display["request_recorded_at"] == ("2026-10-27T15:00:00Z" if owned else None)
    assert (card["request_report"] or {}).get("code") == ("searching" if owned else None)
    assert db.execute("SELECT COUNT(*) FROM movie_history").fetchone()[0] == before_history
    assert fake.posts == []


def test_failed_inventory_read_keeps_saved_report_without_claiming_a_current_observation(db):
    from omakase.plus.movie_presentation import present_movie

    clock = [NOW]
    svc, fake, _ = requested(db, clock=clock)
    clock[0] += timedelta(hours=3)
    fake.inventory_status = 503
    fake.xml_override = b'<MediaContainer librarySectionID="6" size="0" totalSize="0"/>'
    card = next(card for card in run(svc.list(1)) if card["imdb_id"] == IMDB)
    display = present_movie(card, now=clock[0])["display"]
    assert card.get("request_observation") is None
    assert display["status_label"] == "Service unavailable"
    assert display["request_observed_at"] is None
    assert display["request_label"] == "Searching" and display["request_stale"] is True
    assert card["request_report"]["updated_at"] == "2026-10-27T15:00:00Z"
    assert fake.posts == []


def test_request_record_dates_are_projected_separately_from_plex_verification(db):
    _, _, card = requested(db)
    report = card["request_report"]
    assert report["created_at"] == report["updated_at"] == "2026-10-27T15:00:00Z"
    assert report["code"] == "searching" and report["investigations"] == 0
    assert card["verified_at"] is None and "last_successful_check" not in report


def test_repeated_request_check_reports_same_result_without_duplicate_submission(db):
    clock = [NOW]
    svc, fake, _ = requested(db, clock=clock)
    clock[0] += timedelta(minutes=10)
    first = run(svc.refresh(1, IMDB))
    assert first["request_check"] == {
        "performed": True,
        "completed": True,
        "outcome": "unchanged",
        "reason": "searching",
        "attempted_at": "2026-10-27T15:10:00Z",
    }
    clock[0] += timedelta(minutes=1)
    second = run(svc.refresh(1, IMDB))
    assert second["request_check"]["outcome"] == "unchanged" and fake.posts == []
    assert second["request_report"]["updated_at"] == "2026-10-27T15:11:00Z"


@pytest.mark.parametrize(
    "code",
    ["provider_restricted", "no_eligible_results", "investigation_exhausted", "rate_limited"],
)
def test_suppressed_request_check_is_not_reported_as_a_new_service_check(db, code):
    svc, fake, _ = requested(db)
    movies_store.update_request(
        db,
        1,
        IMDB,
        now="2026-10-27T14:00:00Z",
        state="deferred",
        code=code,
        detail="Saved fixture result",
    )
    before = len(fake.calls)
    card = run(svc.refresh(1, IMDB))
    assert card["request_check"] == {
        "performed": False,
        "completed": False,
        "outcome": "saved",
        "reason": code,
        "attempted_at": None,
    }
    assert card["request_report"]["updated_at"] == "2026-10-27T14:00:00Z"
    assert len(fake.calls) == before and fake.posts == []


def test_failed_request_check_has_attempt_provenance_without_a_success_claim(db):
    svc, fake, _ = requested(db)
    fake.inventory_status = 503
    card = run(svc.refresh(1, IMDB))
    assert card["request_check"]["performed"] is True
    assert card["request_check"]["completed"] is False
    assert card["request_check"]["outcome"] == "failed"
    assert card["request_check"]["reason"] == "backend_unavailable" and fake.posts == []


def test_request_report_stays_account_scoped(db):
    svc, fake, _ = requested(db)
    other = run(svc.detail(2, IMDB))
    assert other["request_report"] is None and other["requested"] is False
    assert fake.posts == []


def test_old_request_report_is_stale_and_missing_viewing_uses_concise_display_only(db):
    from omakase.plus.movie_presentation import present_movie

    _, _, card = requested(db)
    before = deepcopy(card)
    changes = db.total_changes
    displayed = present_movie(card, now=NOW + timedelta(hours=2))
    assert displayed["display"]["status_label"] == "Status out of date"
    assert displayed["display"]["watch_label"] == "Not watched"
    assert displayed["display"]["request_label"] == "Searching"
    assert displayed["display"]["request_recorded_at"] == "2026-10-27T15:00:00Z"
    assert "is searching" not in displayed["display"]["summary"]
    assert card == before and db.total_changes == changes
    assert db.execute("SELECT COUNT(*) FROM movie_history").fetchone()[0] == 0


@pytest.mark.parametrize("stamp", [None, "not-a-time", "2099-01-01T00:00:00Z"])
def test_untrusted_request_time_does_not_create_a_fresh_activity_claim(db, stamp):
    from omakase.plus.movie_presentation import present_movie

    _, _, card = requested(db)
    card["request_report"]["updated_at"] = stamp
    displayed = present_movie(card, now=NOW)
    assert displayed["display"]["status_label"] == "Status unknown"
    assert displayed["display"]["request_recorded_at"] is None
