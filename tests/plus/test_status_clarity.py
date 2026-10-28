"""Status cards keep saved facts separate from provenance and timestamps."""

from pathlib import Path

import pytest
from jinja2 import DictLoader, Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[2] / "src" / "omakase" / "plus"


def history_card(**overrides):
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
    item = {
        "title": "A saved movie",
        "detail_url": "/plus/movies/tt0084787",
        "media_type": "movie",
        "status_label": "Completed",
        "rating": None,
        "source_detail": "Added manually",
        "recorded_at": "2026-10-27T18:00:00Z",
        "watched_at": None,
        "watch_time_known": False,
        **overrides,
    }
    return str(env.get_template("_media_components.html").module.history_card(item))


@pytest.mark.parametrize("rating, label", [(None, "Not rated"), (8.0, "8/10"), (8.5, "8.5/10")])
def test_completed_movie_renders_current_rating_separately(rating, label):
    html = history_card(rating=rating)
    assert ">Completed</span>" in html
    assert label in html
    assert "Added manually" in html
    assert 'Observed <time datetime="2026-10-27T18:00:00Z"' in html
    assert "Watched <time" not in html
    assert "Marked watched" not in html


def test_only_a_known_watch_timestamp_is_labelled_watched():
    observed = history_card(watched_at="2026-10-20T10:00:00Z")
    assert "Observed <time" in observed
    assert "Watched <time" not in observed
    watched = history_card(watched_at="2026-10-20T10:00:00Z", watch_time_known=True)
    assert 'Watched <time datetime="2026-10-20T10:00:00Z"' in watched


def test_health_keeps_technical_receipts_in_disclosures():
    source = (ROOT / "templates" / "health.html").read_text(encoding="utf-8")
    env = Environment(
        loader=DictLoader(
            {"base.html": "{% block content %}{% endblock %}", "health.html": source}
        ),
        autoescape=select_autoescape(),
    )
    html = env.get_template("health.html").render(
        overall="attention",
        revision="sample-revision",
        monitor_failures=1,
        apple_tv_integration=None,
        apple_tv_watch_enabled=False,
        catalogs=[
            {
                "label": "Source search",
                "status": "degraded",
                "status_label": "Needs attention",
                "reason_label": "Some searches failed",
                "reason_code": "PARTIAL_CATALOG_ERROR",
                "detail": "Technical detail",
                "source": "Saved search",
                "checked_at": None,
            }
        ],
        providers=[],
        plex_targets=[],
        scheduler={"state": "idle"},
    )
    assert 'class="health-orb"' in html
    assert "<i>" not in html
    assert 'aria-label="Status colors"' in html
    assert "1 monitored title needs a retry." in html
    before_details, details = html.split('<details class="health-check-details">', 1)
    assert "Source search" in before_details and "Some searches failed" in before_details
    assert "PARTIAL_CATALOG_ERROR" not in before_details
    assert "PARTIAL_CATALOG_ERROR" in details and "No checks yet" in details
    assert "sample-revision" in html.split("<summary>Advanced diagnostics</summary>", 1)[1]
