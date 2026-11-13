import asyncio
import sqlite3

import httpx
import pytest

from omakase.plus.anime_synopsis import cached_synopsis, ensure_anime_synopsis
from omakase.plus.catalog_search import AnimeCatalogSearch
from omakase.plus.db import run_migrations


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    conn.execute(
        "INSERT INTO users (id,email,password_hash) VALUES (1,'synopsis@example.test','hash')"
    )
    yield conn
    conn.close()


def response(description):
    return {"data": {"Media": {"id": 21, "title": {"english": "Test"}, "description": description}}}


def test_public_catalog_synopsis_persists_and_reuses_cache(db):
    calls = []

    def handler(request):
        calls.append(request)
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json=response(
                "<p>A &amp; B.</p><script>attack()</script><style>bad</style><p>Second paragraph.</p>"
            ),
        )

    transport = httpx.MockTransport(handler)
    first = asyncio.run(ensure_anime_synopsis(db, user_id=1, anilist_id=21, transport=transport))
    assert first["description"] == "A & B.\nSecond paragraph."
    assert first["description_preview"] == "A & B."
    assert first["description_more"] == "Second paragraph."
    assert first["description_source"] == "AniList"
    assert (
        asyncio.run(ensure_anime_synopsis(db, user_id=1, anilist_id=21, transport=transport))
        == first
    )
    assert len(calls) == 1
    assert cached_synopsis(db, 21) == first


@pytest.mark.parametrize("status", [200, 503])
def test_missing_or_failed_metadata_has_empty_state_and_cooldown(db, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=response(None))

    for _ in range(2):
        view = asyncio.run(
            ensure_anime_synopsis(
                db, user_id=1, anilist_id=21, transport=httpx.MockTransport(handler)
            )
        )
        assert view["description"] == ""
    assert len(calls) == 1


def test_existing_catalog_detail_populates_persistent_synopsis(db):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=response("A real catalog description."))
    )
    asyncio.run(AnimeCatalogSearch(db, 1, transport=transport).detail(21))
    assert cached_synopsis(db, 21)["description"] == "A real catalog description."


def test_long_first_paragraph_preserves_complete_remaining_text(db):
    text = "A complete sentence. " * 60
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response(text)))
    view = asyncio.run(ensure_anime_synopsis(db, user_id=1, anilist_id=21, transport=transport))
    assert len(view["description_preview"]) <= 420
    assert view["description_more"]
    assert view["description_preview"] + " " + view["description_more"] == text.strip()


def test_title_view_uses_persisted_text_and_template_escapes_markup(db):
    from pathlib import Path

    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

    from omakase.plus.anime_synopsis import store_synopsis
    from omakase.plus.media_store import upsert_monitor
    from omakase.plus.product_views import title_detail

    monitor = upsert_monitor(
        db,
        user_id=1,
        anilist_id=21,
        title="Test",
        format="TV",
        release_status="FINISHED",
        expected_episodes=12,
        aired_episodes=12,
    )
    store_synopsis(db, 21, "<p>Real &amp; safe overview.</p><p>Rest of the story.</p>")
    title = title_detail(db, user_id=1, monitor_id=monitor)
    assert title["description_preview"] == "Real & safe overview."
    assert title_detail(db, user_id=99, monitor_id=monitor) is None
    templates = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(templates),
            ]
        ),
        autoescape=True,
    )
    html = env.get_template("title.html").render(title=title)
    assert "Real &amp; safe overview." in html
    assert "<summary>Read more about Test</summary>" in html
    assert "Rest of the story." in html
    assert "Synopsis from" in html
    title.update(description="", description_preview="", description_more="")
    assert "No synopsis is available from AniList yet." in env.get_template("title.html").render(
        title=title
    )


def test_synopsis_cache_survives_connection_restart(tmp_path):
    from omakase.plus.anime_synopsis import store_synopsis

    path = tmp_path / "catalog.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    store_synopsis(conn, 21, "Persisted catalog description.")
    conn.close()
    with sqlite3.connect(path) as reopened:
        reopened.row_factory = sqlite3.Row
        assert cached_synopsis(reopened, 21)["description"] == "Persisted catalog description."
