"""Removed media retains owner-scoped history without claiming current availability."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.responses import HTMLResponse

from omakase.plus.catalog_search import AnimeCatalogSearch
from omakase.plus.db import run_migrations
from omakase.plus.product_views import library_titles, title_detail


def seeded_db(scope="plex_and_provider", status="complete", linked=True):
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    run_migrations(db)
    db.execute(
        "INSERT INTO users(id,email,password_hash) VALUES(1,'one@example.invalid','h'),(2,'two@example.invalid','h')"
    )
    db.execute(
        "INSERT INTO media_monitors(id,user_id,anilist_id,title,expected_episodes,active,release_status) VALUES(291,1,151807,'Solo Leveling',12,0,'FINISHED')"
    )
    db.execute(
        "INSERT INTO watch_entries(id,user_id,monitor_id,anilist_id,status,progress_episodes,episode_total,last_source,completed_at) VALUES(1,1,?,151807,'completed',12,12,'manual','2026-10-27T18:00:00Z')",
        (291 if linked else None,),
    )
    db.execute(
        "INSERT INTO watch_events(watch_entry_id,user_id,source,event_key,kind,episode,disposition,observed_at,score_ten) VALUES(1,1,'manual','completed','completed',12,'accepted','2026-10-27T18:00:00Z',9)"
    )
    stamp = "2026-10-27T18:00:00Z"
    db.execute(
        "INSERT INTO media_title_removals(user_id,monitor_id,anilist_id,title,requested_at,status,scope,curator_run_id,completed_at,plex_removed_at) VALUES(1,291,151807,'Solo Leveling',?,?,?,?,?,?)",
        (
            stamp,
            status,
            scope,
            "fixture" if status != "awaiting_plex" else "",
            stamp if status == "complete" else None,
            stamp if status != "awaiting_plex" else None,
        ),
    )
    db.commit()
    return db


def catalog_card(db, user_id=1):
    return AnimeCatalogSearch(db, user_id)._enrich(
        {
            "id": 151807,
            "anilist_id": 151807,
            "title": "Solo Leveling",
            "episode_total": 12,
            "poster_url": "/plus/media/151807/poster",
        }
    )


def render(name, **context):
    folder = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    env = Environment(loader=FileSystemLoader(folder), autoescape=select_autoescape())
    return env.get_template(name).render(
        email="sample@example.invalid", csrf_token="fixture-csrf", **context
    )


@pytest.mark.parametrize("scope", ["plex_and_provider", "plex_only"])
def test_removed_monitor_detail_retains_history_without_current_media(scope):
    db = seeded_db(scope)
    before = db.total_changes
    title = title_detail(db, user_id=1, monitor_id=291)
    assert title["state"] == "archived"
    assert title["status_label"] == (
        "Removed from Plex" if scope == "plex_only" else "Media removed"
    )
    assert title["progress_episodes"] == title["episode_total"] == 12
    assert title["watch_status"] == "completed" and title["score_ten"] == 9
    assert title["usable"] == title["plex_visible"] == 0
    assert title["current_assets"] == title["usable_episodes"] == []
    assert not title["publication_pending"]
    assert title["watch_timeline"][0]["episode"] == 12
    html = render("title.html", title=title)
    assert "View episode history" in html and "Media removed" in html
    assert "Removed media is not available for playback" in html
    assert "/retry" not in html and "Find missing episodes" not in html
    assert "Your rating: <strong>9 / 10</strong>" in html
    assert db.total_changes == before
    assert title_detail(db, user_id=2, monitor_id=291) is None
    assert title_detail(db, user_id=1, monitor_id=999) is None
    if scope == "plex_and_provider":
        assert library_titles(db, user_id=1, lifecycle="all") == []
    db.close()


@pytest.mark.parametrize(
    "status,expected",
    [("complete", "removed"), ("awaiting_plex", "removing"), ("provider_failed", "removing")],
)
def test_catalog_reports_saved_history_and_removal_state(status, expected):
    db = seeded_db(status=status)
    card = catalog_card(db)
    assert card["library_id"] == 291 and card["library_status"] == expected
    assert not card["library_present"]
    assert card["progress"] == 12 and card["rating"] == 9
    html = render("catalog_anime.html", card=card)
    assert "In your library" not in html and "View your episodes" not in html
    assert ("View saved history" if status == "complete" else "View removal status") in html
    other = catalog_card(db, 2)
    assert other["library_id"] is None and other["progress"] is None and other["rating"] is None
    db.close()


def test_missing_monitor_keeps_catalog_history_without_inventing_a_library_link():
    db = seeded_db(linked=False)
    db.execute("DELETE FROM media_title_removals")
    db.execute("DELETE FROM media_monitors")
    card = catalog_card(db)
    assert card["library_id"] is None and not card["library_present"]
    assert card["watch_status"] == "completed" and card["progress"] == 12 and card["rating"] == 9
    assert title_detail(db, user_id=1, monitor_id=291) is None
    html = render("catalog_anime.html", card=card)
    assert "Not in your library" in html and "/plus/library/291" not in html
    db.close()


def test_old_library_link_renders_archived_html_and_foreign_id_is_404(monkeypatch):
    from omakase.plus import routes
    from omakase.plus.deps import get_db
    from omakase.plus.middleware import require_user

    db = seeded_db()

    async def synopsis(*args, **kwargs):
        return {}

    class Templates:
        def TemplateResponse(self, request, name, context):
            return HTMLResponse(render(name, title=context["title"]))

    monkeypatch.setattr(routes, "ensure_anime_synopsis", synopsis)
    monkeypatch.setattr(routes, "templates", Templates())
    monkeypatch.setattr(routes, "_product_context", lambda *args, **kwargs: {})
    app = FastAPI()
    app.add_api_route("/plus/library/{monitor_id}", routes.title_page, methods=["GET"])
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as client:
        response = client.get("/plus/library/291")
        assert response.status_code == 200 and "View episode history" in response.text
        assert client.get("/plus/library/999").status_code == 404
        app.dependency_overrides[require_user] = lambda: SimpleNamespace(id=2)
        assert client.get("/plus/library/291").status_code == 404
    db.close()


def test_archived_detail_recovers_unlinked_same_season_history_only():
    db = seeded_db(linked=False)
    title = title_detail(db, user_id=1, monitor_id=291)
    assert title["watch_status"] == "completed" and title["progress_episodes"] == 12
    assert title["score_ten"] == 9 and len(title["watch_timeline"]) == 1
    db.execute("UPDATE watch_entries SET season_number=2")
    other_season = title_detail(db, user_id=1, monitor_id=291)
    assert other_season["progress_episodes"] == 0
    assert other_season["score_ten"] is None and not other_season["watch_timeline"]
    db.close()
