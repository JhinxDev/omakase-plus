"""Route boundaries preserve auth, explicit mutations and metadata-only browsing."""

import asyncio
import io
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from jinja2 import DictLoader, FileSystemLoader
from PIL import Image

from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.movies_routes import create_movies_router
from omakase.plus.web_security import csrf_token


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    db = sqlite3.connect(tmp_path / "movie-routes.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    run_migrations(db)
    db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'one@example.test','hash')")
    db.commit()

    class Service:
        def __init__(self):
            self.db, self.calls = db, []

        async def list(self, user_id, **kwargs):
            self.calls.append("list")
            return [
                dict(
                    imdb_id="tt0084787",
                    tmdb_id=1091,
                    title="The Thing",
                    state="verifying",
                    detail="Not verified",
                    requested=False,
                )
            ]

        async def search(self, user_id, query):
            self.calls.append("search")
            return await self.list(user_id)

        async def detail(self, user_id, imdb_id):
            self.calls.append("detail")
            return dict(
                imdb_id=imdb_id, tmdb_id=1091, title="The Thing", state="verifying", requested=False
            )

        async def request(self, user_id, **kwargs):
            self.calls.append("request")
            return dict(imdb_id=kwargs["imdb_id"], state="requested")

        async def verify(self, user_id, imdb_id):
            self.calls.append("verify")
            return dict(imdb_id=imdb_id, state="ready")

        async def refresh(self, user_id, imdb_id):
            self.calls.append("refresh")
            return dict(imdb_id=imdb_id, state="searching")

    svc = Service()
    app = FastAPI()
    templates = Jinja2Templates(directory=str(Path(__file__).parent))
    templates.env.loader = DictLoader(
        {
            "movies.html": "{% for card in cards %}{{card.title}}{% endfor %}{{movies_error}}",
            "movie_detail.html": "{{card.title}} {{card.history.status}}",
        }
    )
    app.include_router(
        create_movies_router(templates, service_factory=lambda db, user: svc), prefix="/plus"
    )
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        session = create_session(db, 1)
        client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
        token = csrf_token(
            Request(
                {"type": "http", "headers": [(b"cookie", f"omakase_session={session}".encode())]}
            )
        )
        client.templates = templates
        yield client, svc, token
    db.close()


def test_browsing_and_detail_never_acquire_or_probe(browser):
    client, svc, _ = browser
    assert client.get("/plus/movies").status_code == 200
    detail = client.get("/plus/movies/tt0084787")
    assert detail.status_code == 200 and "The Thing" in detail.text
    assert "unwatched" not in detail.text
    assert svc.calls == ["list", "list"]


@pytest.mark.parametrize(
    "outcome,message",
    [
        ("updated", "Request checked. The reported state changed."),
        ("unchanged", "No change reported."),
        ("saved", "Request check skipped."),
        ("failed", "The request check did not complete."),
    ],
)
def test_html_request_check_preserves_its_outcome_through_redirect(browser, outcome, message):
    client, svc, token = browser
    client.templates.env.loader = FileSystemLoader(
        Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    )

    async def refresh(user_id, imdb_id):
        svc.calls.append("refresh")
        return {
            "imdb_id": imdb_id,
            "request_check": {"outcome": outcome, "performed": outcome != "saved"},
        }

    svc.refresh = refresh
    before = svc.db.total_changes
    response = client.post("/plus/movies/tt0084787/refresh", data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["location"] == f"/plus/movies/tt0084787?notice=request_{outcome}"
    page = client.get(response.headers["location"])
    assert page.status_code == 200 and message in page.text
    assert "Not watched" in page.text and "Watch status not recorded" not in page.text
    assert 'value="" selected' in page.text
    assert svc.calls == ["refresh", "list"] and svc.db.total_changes == before


def test_collection_get_reads_saved_import_status_without_starting_sync(browser, monkeypatch):
    from omakase.plus.movie_history import MovieHistoryService

    client, svc, _ = browser
    svc.connections = SimpleNamespace(
        enabled=True, plex_url="https://plex.example.test", plex_token="fixture-token"
    )
    history = MovieHistoryService(svc)
    svc.db.execute(
        "INSERT INTO movie_sync_cursors VALUES(1,?,12,?)",
        (history._scope(), "2026-10-27T07:59:09Z"),
    )
    svc.db.commit()

    async def forbidden(*args, **kwargs):
        raise AssertionError("GET must not start a Plex history import")

    monkeypatch.setattr(MovieHistoryService, "sync_plex", forbidden)
    client.templates.env.loader.mapping["movies.html"] = (
        "{{history_sync.mode}} {{history_sync.checked_at}} {{history_sync.has_more}}"
    )
    before = svc.db.total_changes
    response = client.get("/plus/movies")
    assert response.status_code == 200
    assert response.text == "manual 2026-10-27T07:59:09Z True"
    assert svc.calls == ["list"] and svc.db.total_changes == before


def test_catalog_detail_does_not_require_collection_membership(browser):
    client, svc, _ = browser

    async def empty(user_id, **kwargs):
        svc.calls.append("list")
        return []

    svc.list = empty
    response = client.get("/plus/movies/tt0084787")
    assert response.status_code == 200 and "The Thing" in response.text
    assert svc.calls == ["list", "detail"]


def test_authenticated_movie_artwork_uses_a_small_display_derivative(browser, monkeypatch):
    from omakase.plus.movies_store import upsert_movie

    client, svc, _ = browser
    upsert_movie(
        svc.db,
        1,
        dict(
            imdb_id="tt0084787",
            tmdb_id=1091,
            title="The Thing",
            year=1982,
            poster_url="https://image.tmdb.org/t/p/w342/poster.jpg",
        ),
        now="2026-10-27T00:00:00Z",
    )
    original = io.BytesIO()
    Image.new("RGB", (64, 96), "red").save(original, format="PNG")

    async def artwork(_url):
        return original.getvalue(), "image/png"

    monkeypatch.setattr("omakase.plus.movies_routes.movie_artwork", artwork)
    response = client.get("/plus/movies/tt0084787/artwork")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/webp")
    assert Image.open(io.BytesIO(response.content)).size == (64, 96)
    assert svc.calls == []
    client.cookies.clear()
    assert client.get("/plus/movies/tt0084787/artwork").status_code == 401


@pytest.mark.parametrize(
    "path,data",
    [
        ("/plus/movies/request", {"imdb_id": "tt0084787", "tmdb_id": 1091}),
        ("/plus/movies/tt0084787/verify", {}),
        ("/plus/movies/tt0084787/refresh", {}),
        ("/plus/movies/history", {"imdb_id": "tt0084787", "tmdb_id": 1091, "status": "completed"}),
        ("/plus/movies/sync", {}),
    ],
)
def test_all_mutations_require_explicit_csrf(browser, path, data):
    client, svc, _ = browser
    assert client.post(path, data=data).status_code == 403
    assert svc.calls == []


def test_explicit_verify_and_exact_detail_redirect(browser):
    client, svc, token = browser
    response = client.post("/plus/movies/tt0084787/verify", data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["location"] == "/plus/movies/tt0084787?notice=updated"
    assert svc.calls == ["verify"]


def test_api_verify_receipt_and_auth_boundary(browser):
    client, svc, token = browser
    response = client.post(
        "/plus/movies/tt0084787/verify",
        data={"csrf_token": token},
        headers={"Accept": "application/json"},
    )
    assert response.json() == {"imdb_id": "tt0084787", "state": "ready"}
    client.cookies.clear()
    assert client.get("/plus/movies").status_code == 401


def test_private_routes_unavailable_when_disabled(browser, monkeypatch):
    client, svc, _ = browser
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    assert client.get("/plus/movies").status_code == 404
    assert svc.calls == []


def test_movie_forms_are_stream_bounded_before_auth(browser):
    client, svc, _ = browser
    client.cookies.clear()
    consumed = []

    async def chunks():
        for index in range(20):
            consumed.append(index)
            yield b"x" * 4096

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="https://testserver"
        ) as request:
            return await request.post(
                "/plus/movies/request",
                content=chunks(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

    assert asyncio.run(run()).status_code == 413
    assert len(consumed) == 5 and svc.calls == []


@pytest.mark.parametrize(
    "fields,expected_status,expected_rating",
    [
        ({"rating": "8"}, "omitted", 8.0),
        ({"status": "completed"}, "completed", "omitted"),
        ({"rating": ""}, "omitted", None),
    ],
)
def test_history_form_preserves_omitted_fields(
    browser, monkeypatch, fields, expected_status, expected_rating
):
    from omakase.plus.movie_history import UNSET, MovieHistoryService

    client, _, token = browser
    captured = {}

    async def save(self, user_id, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(MovieHistoryService, "save", save)
    response = client.post(
        "/plus/movies/history",
        data={"csrf_token": token, "imdb_id": "tt0084787", "tmdb_id": 1091, **fields},
    )
    assert response.status_code == 303
    assert (
        captured["status"] is UNSET
        if expected_status == "omitted"
        else captured["status"] == expected_status
    )
    assert (
        captured["rating"] is UNSET
        if expected_rating == "omitted"
        else captured["rating"] == expected_rating
    )


@pytest.mark.parametrize("genre", ["", "27"])
@pytest.mark.parametrize("watched", [False, True])
def test_discovery_native_get_form_empty_or_selected_genre_is_html(
    browser, monkeypatch, genre, watched
):
    from html.parser import HTMLParser

    client, svc, _ = browser
    client.templates.env.loader = FileSystemLoader(
        Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    )
    calls = []

    class Discovery:
        def __init__(self, service):
            assert service is svc

        async def options(self, user_id):
            return {"genres": [{"id": 27, "name": "Horror"}]}

        async def discover(self, user_id, **filters):
            calls.append(filters)
            return []

    class Form(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = []

        def handle_starttag(self, tag, attrs):
            if tag == "form":
                self.forms.append(dict(attrs))

    monkeypatch.setattr("omakase.plus.movie_discovery.MovieDiscoveryService", Discovery)
    initial = client.get("/plus/discover/movies")
    assert initial.status_code == 200
    form = Form()
    form.feed(initial.text)
    native = next(f for f in form.forms if f.get("action") == "/plus/discover/movies")
    assert native["method"] == "get"
    fields = {"genre_id": genre, "sort": "personalized"}
    if watched:
        fields["include_completed"] = "true"
    response = client.get(native["action"], params=fields)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Explore movies" in response.text
    assert calls[-1] == {
        "genre_id": int(genre) if genre else None,
        "sort": "personalized",
        "include_completed": watched,
    }
    if genre:
        assert '<option value="27" selected>' in response.text
    if watched:
        assert 'name="include_completed" value="true" checked' in response.text


@pytest.mark.parametrize("genre", ["not-a-number", "-1", "0", "9" * 40])
def test_invalid_discovery_genre_returns_a_useful_html_state(browser, monkeypatch, genre):
    client, _, _ = browser
    client.templates.env.loader = FileSystemLoader(
        Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    )

    class Discovery:
        def __init__(self, service):
            pass

        async def options(self, user_id):
            return {"genres": []}

        async def discover(self, user_id, **filters):
            pytest.fail("Invalid genre must be rejected before discovery")

    monkeypatch.setattr("omakase.plus.movie_discovery.MovieDiscoveryService", Discovery)
    response = client.get("/plus/discover/movies", params={"genre_id": genre})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Choose a valid discovery filter." in response.text
