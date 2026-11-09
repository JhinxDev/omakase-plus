"""Availability is independent from a saved viewing record and an active check."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from omakase.plus.movie_presentation import present_movie


def test_movie_plex_review_script_is_served():
    from omakase.plus.routes import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/plus/static/movie-plex-review.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")


@pytest.mark.parametrize(
    "history,label",
    [
        (None, "No viewing record"),
        ({"status": "unwatched", "status_source": "none"}, "No viewing record"),
        ({"status": "unwatched", "status_source": "manual"}, "Not watched"),
        ({"status": "completed", "status_source": "manual", "rating": 8}, "Watched"),
    ],
)
def test_missing_viewing_evidence_is_distinct_from_explicit_unwatched(history, label):
    card = {"imdb_id": "tt0084787", "title": "The Thing", "history": history}
    displayed = present_movie(card)
    assert displayed["display"]["watch_label"] == label
    assert displayed["history"] == history
    templates = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    env = Environment(loader=FileSystemLoader(templates), autoescape=True)
    html = str(env.get_template("_movie_components.html").module.movie_card(displayed))
    assert label in html
    if history and history.get("rating"):
        assert "Your rating 8/10" in html


@pytest.mark.parametrize("expired", [False, True])
def test_idle_availability_never_claims_an_active_check_or_changes_history(expired):
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"),
        autoescape=select_autoescape(),
    )
    card = dict(
        imdb_id="tt3289956",
        tmdb_id=397243,
        title="The Autopsy of Jane Doe",
        state="verifying",
        code="verification_expired" if expired else "verifying",
        detail="A check is needed.",
        requested=False,
        history=dict(status_source="manual", status="completed", rating=8.0),
        last_checked_at="2026-10-27T09:30:05Z" if expired else None,
    )
    html = env.get_template("movie_detail.html").render(
        card=present_movie(card), csrf_token="fixture-csrf", email="sample@example.test"
    )
    assert "Verifying</strong>" not in html
    assert ("Needs a Plex check" if expired else "Not checked in Plex") in html
    assert "Your rating 8.0/10" in html and ">Watched</span>" in html
    assert 'id="movie-rating-heading"' in html
    assert "Plex after watching" in html
    assert 'action="/plus/movies/tt3289956/plex-review"' in html
    assert 'src="/plus/static/movie-plex-review.js?' in html
    assert "Check in Plex" in html and "Open in Plex" not in html
    assert 'href="/plus/settings/connections"' in html
    assert 'href="/plus/health/overview"' in html
    assert 'href="https://www.justwatch.com/" target="_blank" rel="noopener noreferrer"' in html
    assert 'href="/plus/settings/downloads"' not in html
    assert 'href="/plus/settings/sources"' not in html
    if expired:
        assert "Previous Plex verification" in html and 'datetime="2026-10-27T09:30:05Z"' in html


@pytest.mark.parametrize("plex_present", [False, True])
def test_removed_copy_only_offers_review_if_a_replacement_exists(plex_present):
    templates = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    env = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape())
    card = present_movie(
        dict(
            imdb_id="tt3289956",
            tmdb_id=397243,
            title="Sample Film",
            code="copy_removed",
            plex_present=plex_present,
            history=dict(status_source="manual", status="completed", rating=8.0),
        )
    )
    html = env.get_template("movie_detail.html").render(
        card=card,
        csrf_token="fixture-csrf",
        email="sample@example.test",
        notice="saved",
        cleanup_enabled=True,
        cleanup_status=None,
    )
    assert ("Review removal" in html) is plex_present
    assert ("Removed from Plex and Real-Debrid." in html) is not plex_present
    assert "Your rating 8.0/10" in html and "Your viewing changes were saved." in html
    assert 'id="movie-after-watch"' in html


def test_specific_availability_failure_keeps_its_actionable_reason():
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"),
        autoescape=select_autoescape(),
    )
    card = dict(
        imdb_id="tt3289956",
        tmdb_id=397243,
        title="The Autopsy of Jane Doe",
        state="verifying",
        code="english_audio_not_selected",
        detail="An English audio track is not selected in Plex.",
        requested=False,
        history=dict(status_source="manual", status="completed", rating=8.0),
    )
    html = env.get_template("movie_detail.html").render(
        card=present_movie(card), csrf_token="fixture-csrf", email="sample@example.test"
    )
    assert "Check audio in Plex" in html and 'data-status-tone="attention"' in html
    assert "An English audio track is not selected in Plex." in html
    assert "playback check has not been saved" not in html


@pytest.mark.parametrize(
    "code,label",
    [
        ("backend_failed", "Request failed"),
        ("provider_restricted", "Provider restricted"),
        ("backend_state_unknown", "Status unknown"),
    ],
)
def test_collection_cards_use_same_freshness_aware_mapping_as_detail(code, label):
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"),
        autoescape=select_autoescape(),
    )
    card = present_movie(
        dict(
            imdb_id="tt1234567",
            title="Fixture",
            state="deferred",
            code=code,
            requested=True,
            history=dict(status_source="manual", status="completed", rating=8.5),
        )
    )
    html = env.from_string(
        '{% from "_movie_components.html" import movie_card %}{{ movie_card(card) }}'
    ).render(card=card)
    assert label in html
    assert "Watched" in html and "Your rating 8.5/10" in html
    assert "Check in Plex" not in html
    assert "On the way" not in html


def test_discovery_card_shows_actual_local_or_public_reason_without_fake_fit_score():
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"),
        autoescape=select_autoescape(),
    )
    card = present_movie(
        dict(
            imdb_id="tt1234567",
            title="Fixture",
            state="verifying",
            code="verifying",
            rank=1,
            ranking_basis="public_popularity",
            recommendation_reason="Popular on TMDB; save ratings to personalize this list.",
        )
    )
    html = env.from_string(
        '{% from "_movie_components.html" import movie_card %}{{ movie_card(card) }}'
    ).render(card=card)
    assert "Public discovery" in html and card["recommendation_reason"] in html
    assert "fit /10" not in html and "Predicted fit" not in html
    assert "Check in Plex" not in html
