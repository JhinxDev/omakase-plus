"""Actual global header and scoped search markup preserve truthful mixed results."""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


def render_search(**context):
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"),
        autoescape=select_autoescape(),
    )
    defaults = {
        "email": "sample@example.test",
        "active_page": "home",
        "query": "breaking bad",
        "kind": "all",
        "cards": [],
        "search_error": "",
        "search_warnings": [],
    }
    defaults.update(context)
    return env.get_template("search.html").render(**defaults)


@pytest.mark.parametrize("page", ["home", "library", "movies", "tv", "search"])
def test_header_scope_is_global_even_when_the_page_search_is_explicitly_anime(page):
    html = render_search(active_page=page, kind="anime")
    header = html.split('<form class="header-search"', 1)[1].split("</form>", 1)[0]
    assert 'name="kind" value="all"' in header
    assert '<option value="anime" selected>' in html
    assert '<option value="all"' in html


def test_unavailable_catalogs_are_not_rendered_as_no_matches():
    html = render_search(
        search_warnings=[{"label": "TV", "detail": "TV search is unavailable.", "retry_after": 60}]
    )
    assert "Search is incomplete" in html and "TV search is unavailable" in html
    assert "No matching titles" not in html
    assert "Try again in 60 seconds" in html


def test_mixed_result_kind_is_visible_and_full_title_is_escaped():
    html = render_search(
        cards=[
            {
                "title": "Breaking Bad <script>bad()</script>",
                "media_type": "tv",
                "id": 1396,
                "url": "/plus/tv/1396",
                "format": "TV series",
            }
        ]
    )
    assert "<span>TV</span>" in html
    assert 'href="/plus/tv/1396"' in html
    assert "<script>bad()</script>" not in html
    assert "&lt;script&gt;bad()&lt;/script&gt;" in html
