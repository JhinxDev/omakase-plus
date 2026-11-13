"""Recommendation detail keeps planning, history and intake actions distinct."""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


def render(**overrides):
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"),
        autoescape=select_autoescape(),
    )
    pick = {
        "title": "Food Wars! The Second Plate <script>bad()</script>",
        "source": "anilist",
        "url": "https://anilist.co/anime/42/",
        "reasoning": "A sample reason.",
        "description": "A saved synopsis.",
        "availability_state": "unknown",
        "availability": "Not in your library",
        **overrides,
    }
    return env.get_template("recommendation.html").render(
        pick=pick,
        run={"id": 7, "source": "anilist"},
        media_id=42,
        csrf_token="fixture-csrf",
        active_page="discover",
        email="sample@example.test",
    )


def test_watchlist_history_and_intake_preserve_exact_existing_contracts():
    html = render()
    assert "Your watchlist &amp; history" in html
    assert 'action="/plus/add/preview"' in html
    assert 'name="csrf_token" value="fixture-csrf"' in html
    assert 'name="query" value="https://anilist.co/anime/42/"' in html
    assert 'class="button button-secondary btn-plan"' in html
    assert 'data-media-id="42"' in html and 'data-run-id="7"' in html
    assert 'data-feedback="already_watched"' in html
    assert 'data-feedback="not_for_me"' in html
    assert "data-feedback-score" in html
    assert "Your rating" in html and "(optional)" in html
    assert 'class="watched-control"' not in html
    assert 'class="pick-availability' not in html
    assert "<script>bad()</script>" not in html
    assert "&lt;script&gt;bad()&lt;/script&gt;" in html


@pytest.mark.parametrize("library_id", [None, 9])
def test_intake_and_existing_library_action_are_mutually_exclusive(library_id):
    html = render(library_id=library_id)
    if library_id:
        assert 'href="/plus/library/9"' in html
        assert 'action="/plus/add/preview"' not in html
    else:
        assert 'action="/plus/add/preview"' in html
    assert "Save to watchlist" in html and "Mark as watched" in html


def test_missing_fit_score_is_not_presented_as_an_empty_rating():
    assert "Predicted fit" not in render()
    assert "Predicted fit 8.4/10" in render(predicted_score=8.4)
