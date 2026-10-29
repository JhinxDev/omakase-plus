"""Actual history/library templates for bounded synthetic browser regression."""

from __future__ import annotations

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[2]


def fixtures():
    env = Environment(
        loader=FileSystemLoader(ROOT / "src/omakase/plus/templates"),
        autoescape=select_autoescape(["html"]),
    )
    base = {
        "csrf_token": "synthetic-csrf",
        "email": "review@example.invalid",
        "display_name": "Alex",
        "asset_version": "history-review",
        "synthetic_review": True,
        "movies_enabled": True,
    }

    def card(identity, episode, primary, fallback, **extra):
        return dict(
            id="anime-" + str(identity),
            media_type="anime",
            title="BLEACH: A long historical anime title",
            episode=episode,
            episode_title=None,
            status_label="Completed" if episode is None else "Progress saved",
            artwork_url=primary,
            series_artwork_url=fallback,
            episode_artwork=True,
            detail_url="/plus/catalog/anime/21",
            recorded_at="2026-10-28T10:00:00Z",
            watched_at=None,
            watch_time_known=False,
            source_detail="Added manually",
            event_label="Series completed" if episode is None else None,
            **extra,
        )

    cards = [
        card(1, 10, "/plus/history/artwork/anime/1", "/plus/media/21/poster"),
        card(2, 9, "/plus/history/artwork/anime/2", "/plus/media/21/poster"),
        card(3, None, None, "/plus/media/99/poster"),
        card(4, 7, "/plus/history/artwork/anime/3", "/plus/media/404/poster"),
        dict(
            card(5, 2, "/plus/history/artwork/tv/42/1/2", "/plus/tv/42/artwork"),
            media_type="tv",
            season_number=1,
        ),
        dict(
            id="movie",
            media_type="movie",
            title="The Thing",
            episode=None,
            episode_title=None,
            status_label="Completed",
            artwork_url="/plus/movies/tt0084787/artwork",
            episode_artwork=False,
            detail_url="/plus/movies/tt0084787",
            rating=8,
            recorded_at="2026-10-28T10:00:00Z",
            watched_at=None,
            watch_time_known=False,
            source_detail="Added manually",
        ),
    ]
    history = env.get_template("activity.html").render(
        **base,
        active_page="activity",
        watch_history=cards,
        activity_view="watch",
        history_page=1,
        history_has_more=False,
    )
    library = env.get_template("library.html").render(
        **base,
        active_page="library",
        lifecycle_filter="active",
        query="",
        state_filter="all",
        provider_filter="all",
        content_type_filter="all",
        title_suggestions=[],
        titles=[],
        saved_picks=[],
        plex_library={"connected": False},
    )
    return {"history": history, "library": library}


def test_actual_history_template_distinguishes_series_completion_and_cover_fallback():
    pages = fixtures()
    assert "Series completed" in pages["history"] and "Viewing progress" not in pages["history"]
    assert 'data-history-fallback="/plus/media/21/poster"' in pages["history"]
    assert "Series artwork" in pages["history"]
    assert pages["library"].count('aria-label="Library views"') == 1


def test_movie_identity_is_a_heading_and_episode_event_semantics_are_retained():
    history = fixtures()["history"]
    assert re.search(r"<h3\b[^>]*>The Thing</h3>", history)
    assert not re.search(r"<h3\b[^>]*>Movie</h3>", history)
    assert "E10" in history and "S1 · E2" in history and "Series completed" in history
    assert 'datetime="2026-10-28T10:00:00Z"' in history
    assert "Your rating" in history and "8/10" in history


if __name__ == "__main__":
    print(json.dumps(fixtures()))
