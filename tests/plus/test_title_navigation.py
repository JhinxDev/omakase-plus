"""Owned titles have one read-only overview/episode surface, with exact scope."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from omakase.plus import households, viewing_history
from omakase.plus.product_views import home_view
from tests.plus.test_search_routes import browser as browser
from tests.plus.test_search_routes import login
from tests.plus.test_viewing_history import db as db
from tests.plus.test_viewing_history import entry, event, feed


@pytest.mark.parametrize("active", [0, 1])
def test_owned_catalog_bookmark_resolves_locally_to_canonical_overview(browser, active):
    client, conn, calls = browser
    monitor = conn.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,active,season_number) VALUES(1,21,'One Piece',?,1)",
        (active,),
    ).lastrowid
    conn.commit()
    login(client, conn)
    before = conn.total_changes
    response = client.get("/plus/catalog/anime/21", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == f"/plus/library/{monitor}#overview"
    assert response.headers["cache-control"] == "private, no-store"
    assert calls == [] and conn.total_changes == before


def test_another_accounts_title_keeps_catalog_discovery(browser):
    client, conn, calls = browser
    conn.execute("INSERT INTO users(id,email,password_hash) VALUES(2,'other@example.invalid','h')")
    conn.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number) VALUES(2,21,'Private other title',1)"
    )
    conn.commit()
    login(client, conn)
    before = conn.total_changes
    response = client.get("/plus/catalog/anime/21", follow_redirects=False)
    assert response.status_code == 200 and "location" not in response.headers
    assert calls == [("detail", 21)] and conn.total_changes == before


@pytest.mark.parametrize("shared", [False, True])
def test_history_uses_direct_owned_overview_only_in_personal_context(db, monkeypatch, shared):
    monitor, watch = entry(db)
    event(db, watch, episode=3)
    monkeypatch.setattr(viewing_history, "feature_enabled", lambda: True)
    monkeypatch.setattr(
        households,
        "selected_scope",
        lambda *args, **kwargs: SimpleNamespace(kind="household" if shared else "personal"),
    )
    before = db.total_changes
    card = feed(db)["items"][0]
    assert card["detail_url"] == (
        "/plus/catalog/anime/21" if shared else f"/plus/library/{monitor}#overview"
    )
    assert db.total_changes == before


def test_history_does_not_alias_a_monitor_from_another_season(db, monkeypatch):
    monitor, watch = entry(db)
    event(db, watch, episode=3)
    db.execute("UPDATE media_monitors SET season_number=2 WHERE id=?", (monitor,))
    monkeypatch.setattr(viewing_history, "feature_enabled", lambda: False)
    card = feed(db)["items"][0]
    assert card["monitor_id"] is None
    assert not card["detail_url"].startswith("/plus/library/")


def test_home_episode_link_and_overview_share_the_most_recent_title_without_writes(db, monkeypatch):
    monkeypatch.setattr(households, "feature_enabled", lambda: False)
    monitor, watch = entry(db)
    event(db, watch, episode=3, source="manual")
    before = db.total_changes
    context = home_view(db, user_id=1)
    templates = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    env = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape())
    html = env.get_template("home.html").render(
        **context, email="fixture@example.test", active_page="home"
    )
    actions = html.split('class="cinema-actions"', 1)[1].split("</div>", 1)[0]
    assert f'href="/plus/library/{monitor}#episode-4"' in actions
    assert f'href="/plus/library/{monitor}#overview"' in actions
    assert "View episodes" in actions and "More info" in actions and "▶" not in actions
    assert db.total_changes == before
