"""Availability is independent from a saved viewing record and an active check."""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from omakase.plus.movie_presentation import present_movie


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
    assert "Your watched status and rating are saved separately." in html
    assert "Check in Plex" in html and "Open in Plex" not in html
    assert 'href="/plus/settings/connections"' in html
    assert 'href="/plus/health/overview"' in html
    assert 'href="https://www.justwatch.com/" target="_blank" rel="noopener noreferrer"' in html
    assert 'href="/plus/settings/downloads"' not in html
    assert 'href="/plus/settings/sources"' not in html
    if expired:
        assert "Previous Plex verification" in html and 'datetime="2026-10-27T09:30:05Z"' in html


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
