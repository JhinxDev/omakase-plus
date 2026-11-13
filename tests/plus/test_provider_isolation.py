"""Provider isolation: one household plays one provider, and no other provider is touched.

The Real-Debrid test is the owner requirement in executable form: a household with
a connected Real-Debrid account and no Plex link must reach catalog, details, play,
subtitles and progress without a single Plex client entry point. The Plex tripwire
fixture patches every Plex seam to raise, so any silent fallback fails the test.

The registry tests pin selection order, the capability matrix and backwards
compatibility of saved playback_provider values.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.testclient import TestClient

from omakase.plus import (
    connections,
    debrid_routes,
    households,
    movies,
    native_tv_library,
    playback_proof,
    plex,
    web_playback_catalog,
    web_playback_navigation,
    web_playback_source,
)
from omakase.plus.db import run_migrations
from omakase.plus.media_store import upsert_asset
from omakase.plus.media_sync import _inventory_fingerprint
from omakase.plus.native_tv_test import COOKIE
from omakase.plus.providers import playback as playback_providers
from omakase.plus.providers.contracts import ProviderFile, ProviderItem
from omakase.plus.secrets import store_secret
from omakase.plus.web_security import csrf_token
from tests.plus.test_debrid_library import Provider as DebridProvider
from tests.plus.test_native_tv_test import (  # noqa: F401 - pytest fixtures
    API,
    LIBRARY_API,
    LIBRARY_PAGE,
    browser,
)
from tests.plus.test_native_tv_test import (
    system as system,
)
from tests.plus.test_web_playback_routes import client as playback_client
from tests.plus.test_web_playback_routes import setup as setup

ORIGIN = "https://testserver"
_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src/omakase/plus"


class PlexNotAllowed(RuntimeError):
    """Raised whenever a Plex client entry point is reached on another provider path."""


@pytest.fixture
def plex_tripwire(monkeypatch):
    """Make every Plex client entry point raise, and record each attempt."""
    calls: list[str] = []

    def deny(label):
        def trip(*args, **kwargs):
            calls.append(label)
            raise PlexNotAllowed(f"Plex was called on a non-Plex path: {label}")

        return trip

    original_read = connections._read

    def guarded_read(db, user_id, key):
        # Reading whether a Plex link exists is selection input, not a Plex client
        # call. Anything a Plex client needs beyond link presence is a violation.
        if key in {"plex_url", "plex_token"}:
            return original_read(db, user_id, key)
        if key.startswith("plex"):
            calls.append(f"_read:{key}")
            raise PlexNotAllowed(f"Plex credential read on a non-Plex path: {key}")
        return original_read(db, user_id, key)

    monkeypatch.setattr(connections, "_read", guarded_read)
    monkeypatch.setattr(connections, "_values", deny("connections._values"))
    monkeypatch.setattr(playback_proof, "_get", deny("playback_proof._get"))
    monkeypatch.setattr(playback_proof, "_account", deny("playback_proof._account"))
    monkeypatch.setattr(playback_proof, "_xml", deny("playback_proof._xml"))
    monkeypatch.setattr(
        web_playback_source,
        "resolve_anime_source",
        deny("web_playback_source.resolve_anime_source"),
    )
    monkeypatch.setattr(web_playback_source, "_snapshot", deny("web_playback_source._snapshot"))
    monkeypatch.setattr(
        web_playback_catalog, "resolve_catalog_source", deny("catalog.resolve_catalog_source")
    )
    monkeypatch.setattr(web_playback_catalog, "catalog_target", deny("catalog.catalog_target"))
    monkeypatch.setattr(web_playback_navigation, "_candidates", deny("navigation._candidates"))
    monkeypatch.setattr(native_tv_library, "personal_plex_movies", deny("personal_plex_movies"))
    monkeypatch.setattr(native_tv_library, "MovieService", deny("native_tv_library.MovieService"))
    monkeypatch.setattr(movies, "MovieService", deny("movies.MovieService"))
    monkeypatch.setattr(plex, "PlexClient", deny("plex.PlexClient"))
    yield calls


@pytest.fixture
def provider_db(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    run_migrations(db)
    for uid in (1, 2):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (uid, f"{uid}@example.invalid", "fixture"),
        )
        households.ensure_personal_library(db, user_id=uid)
    db.commit()
    yield db
    db.close()


def _save(db, user_id, name, value):
    store_secret(db, user_id, name, value)
    db.commit()


def _link_realdebrid(db, user_id=1):
    _save(db, user_id, "realdebrid_api_key", "owner-token")


def _link_plex(db, user_id=1):
    _save(db, user_id, "plex_url", "https://plex.fixture.invalid")
    _save(db, user_id, "plex_token", "fixture-plex-token")


def _link_local(db, user_id=1):
    db.execute(
        "INSERT OR IGNORE INTO media_providers(user_id,kind,label) "
        "VALUES(?,'local_import','Local: fixture')",
        (user_id,),
    )
    db.commit()


def _save_choice(db, user_id, provider_id):
    _save(db, user_id, "preferred_playback_provider", provider_id)


def test_auto_prefers_realdebrid_when_realdebrid_and_plex_are_linked(provider_db):
    """Owner requirement: a connected Real-Debrid account wins the automatic default."""
    _link_realdebrid(provider_db)
    _link_plex(provider_db)

    selection = playback_providers.select(provider_db, 1)

    assert selection.source == "auto"
    assert selection.id == "realdebrid"
    assert selection.cloud_route is True
    assert playback_providers.playback_provider(provider_db, 1) == "realdebrid"


def test_explicit_plex_choice_keeps_a_plex_household_on_plex(provider_db):
    """An explicit saved choice is honoured even when Real-Debrid is linked too."""
    _link_realdebrid(provider_db)
    _link_plex(provider_db)
    _save_choice(provider_db, 1, "plex")

    selection = playback_providers.select(provider_db, 1)

    assert selection.source == "saved"
    assert selection.id == "plex"
    assert selection.publication_route is True
    assert selection.cloud_route is False


def test_auto_uses_plex_only_when_plex_is_linked(provider_db):
    _link_plex(provider_db)

    assert playback_providers.select(provider_db, 1).id == "plex"


def test_no_linked_provider_resolves_to_the_legacy_auto_value(provider_db):
    selection = playback_providers.select(provider_db, 1)

    assert selection.provider is None
    assert playback_providers.playback_provider(provider_db, 1) == "auto"
    assert playback_providers.linked_providers(provider_db, 1) == ()


def test_saved_choice_without_its_link_falls_back_and_reports_the_reason(provider_db):
    _link_realdebrid(provider_db)
    _save_choice(provider_db, 1, "plex")

    selection = playback_providers.select(provider_db, 1)

    assert selection.id == "realdebrid"
    assert selection.saved == "plex"
    assert selection.source == "unavailable"
    assert selection.reason == "Plex is not connected."


def test_legacy_saved_values_stay_valid(provider_db):
    _link_realdebrid(provider_db)
    _link_plex(provider_db)
    _link_local(provider_db)
    for saved in ("auto", "realdebrid", "plex", "local"):
        _save_choice(provider_db, 1, saved)
        assert playback_providers.saved_choice(provider_db, 1) == saved


def test_unknown_or_stale_saved_value_is_treated_as_automatic(provider_db):
    _link_realdebrid(provider_db)
    _save_choice(provider_db, 1, "kodi")

    assert playback_providers.saved_choice(provider_db, 1) == "auto"
    assert playback_providers.select(provider_db, 1).id == "realdebrid"


def test_local_storage_is_a_first_class_choice(provider_db):
    _link_local(provider_db)
    _save_choice(provider_db, 1, "local")

    selection = playback_providers.select(provider_db, 1)

    assert selection.id == "local"
    assert selection.source == "saved"
    assert selection.supports("direct_file") is True
    assert selection.supports("play") is False
    assert playback_providers.linked_providers(provider_db, 1) == (playback_providers.get("local"),)


def test_auto_order_is_realdebrid_then_plex_then_local(provider_db):
    _link_realdebrid(provider_db)
    _link_plex(provider_db)
    _link_local(provider_db)

    assert tuple(provider.id for provider in playback_providers.AUTO_ORDER) == (
        "realdebrid",
        "plex",
        "local",
    )
    assert playback_providers.select(provider_db, 1).id == "realdebrid"


def test_capability_matrix_is_the_documented_one():
    matrix = {
        "realdebrid": (True, True, True, True, False, True, False),
        "plex": (True, True, True, True, False, True, True),
        "local": (True, False, False, False, True, False, True),
    }
    names = (
        "catalog",
        "play",
        "subtitles",
        "seek",
        "direct_file",
        "requires_token",
        "requires_link",
    )
    for provider_id, expected in matrix.items():
        capabilities = playback_providers.PROVIDERS[provider_id].capabilities
        assert tuple(getattr(capabilities, name) for name in names) == expected


def test_provider_ids_are_not_compared_outside_the_registry():
    """Policy modules ask capabilities, never provider ids."""
    comparison = re.compile(
        r"""(?:==|!=)\s*["'](?:realdebrid|plex|local)["']"""
        r"""|["'](?:realdebrid|plex|local)["']\s*(?:==|!=)"""
    )
    policy_modules = (
        "debrid_library.py",
        "debrid_routes.py",
        "connections_routes.py",
        "native_tv_library.py",
        "web_playback_routes.py",
        "web_playback_navigation.py",
    )
    offenders = {
        name: match.group(0)
        for name in policy_modules
        for match in [comparison.search((_SOURCE_ROOT / name).read_text(encoding="utf-8"))]
        if match
    }
    assert offenders == {}


def _prime_realdebrid_household(db):
    """Catalog and inventory data for one Real-Debrid household, no Plex anywhere."""
    _link_realdebrid(db)
    item = ProviderItem(
        "anime01",
        "Fixture Episode",
        "downloaded",
        100,
        0,
        0,
        (ProviderFile("1", "/Fixture Episode S01E01.mkv", 60, True),),
        "a" * 40,
    )
    db.execute("UPDATE media_monitors SET expected_episodes=1 WHERE id=1")
    upsert_asset(
        db,
        monitor_id=1,
        torrent_hash="a" * 40,
        source_title="Fixture Episode",
        rd_torrent_id="anime01",
        status="downloaded",
        usable_coverage=[1],
        inventory_fingerprint=_inventory_fingerprint(item),
        inventory_verified_at="2026-01-01T00:00:00Z",
    )
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,title,year,identity_verified,updated_at) "
        "VALUES(1,'tt0000001','Fixture Movie',2025,1,'fixture')"
    )
    db.execute(
        "INSERT INTO tv_catalog VALUES(1,1,?,'fixture')",
        (json.dumps({"title": "Fixture Series", "in_collection": True}),),
    )
    db.execute(
        "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,"
        "watched,watch_source,plex_present,updated_at) VALUES(1,1,1,2,'{}',0,'none',0,'fixture')"
    )
    db.commit()


def _realdebrid_upstream(provider):
    """Real-Debrid fixture covering named anime, movie and episode copies."""
    names = {
        "anime01": "Fixture Episode S01E01.mkv",
        "movie01": "Fixture Movie 2025.mkv",
        "series01": "Fixture Series S01E02.mkv",
    }
    content_hash = "a" * 40
    registered: list[dict] = []

    def upstream(request):
        path = request.url.path.removeprefix("/rest/1.0/")
        if path == "torrents":
            return httpx.Response(
                200,
                json=[
                    {"id": key, "filename": name, "hash": content_hash, "status": "downloaded"}
                    for key, name in names.items()
                ],
            )
        if path == "downloads":
            base = json.loads(provider(request).content)
            rows = base + registered
            return httpx.Response(200, json=rows, headers={"X-Total-Count": str(len(rows))})
        if path.startswith("torrents/info/"):
            key = path.rsplit("/", 1)[-1]
            if key not in names:
                return provider(request)
            return httpx.Response(
                200,
                json={
                    "id": key,
                    "hash": content_hash,
                    "status": "downloaded",
                    "files": [{"id": 1, "path": "/" + names[key], "bytes": 60, "selected": 1}],
                    "links": ["https://real-debrid.com/d/" + key],
                },
            )
        if path == "unrestrict/link":
            key = next(key for key in names if key.encode() in request.content)
            registered.append(
                {
                    "id": "download" + key,
                    "filename": names[key],
                    "filesize": 60,
                    "streamable": 1,
                    "link": "https://real-debrid.com/d/" + key,
                }
            )
            return httpx.Response(
                200,
                json={
                    "id": "generated" + key,
                    "filename": names[key],
                    "filesize": 60,
                    "streamable": 1,
                },
            )
        return provider(request)

    return upstream


def _pair_library_device(app, owner, csrf):
    device = TestClient(app, base_url=ORIGIN)
    issue_page = owner.post(LIBRARY_PAGE, data={"csrf_token": csrf, "action": "issue"})
    link = re.search(r"https://testserver#library-([A-F0-9]{20})", issue_page.text)[1]
    paired = device.post(LIBRARY_API + "/redeem", json={"code": link})
    device.headers["Authorization"] = "Bearer " + paired.json()["token"]
    return device


def _cloud_session(owner, session):
    header = csrf_token(
        Request(
            {
                "type": "http",
                "headers": [(b"cookie", f"omakase_session={session}".encode())],
            }
        )
    )
    page = owner.get("/plus/watch/cloud/d-download1?play=1&title=Existing.mp4")
    assert page.status_code == 200 and "data-web-player" in page.text
    opened = owner.post(
        "/plus/api/playback/cloud-sessions",
        json={"file_key": "d-download1"},
        headers={"X-CSRF-Token": header},
    )
    assert opened.status_code == 201, opened.text
    return opened.json()


def test_realdebrid_household_reaches_every_flow_without_any_plex_entry_point(
    system, monkeypatch, plex_tripwire
):
    """Catalog, details, play, subtitles and progress on Real-Debrid, with no Plex link."""
    app, db, sessions, options = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    _prime_realdebrid_household(db)
    provider = DebridProvider()
    options.debrid = _realdebrid_upstream(provider)
    assert playback_providers.select(db, 1).id == "realdebrid"
    assert playback_providers.linked_providers(db, 1) == (playback_providers.get("realdebrid"),)
    templates = Jinja2Templates(directory=str(_SOURCE_ROOT / "templates"))
    app.include_router(
        debrid_routes.create_debrid_router(
            templates,
            lambda request, user, **context: {"email": user.email, **context},
            transport=httpx.MockTransport(options.debrid),
        ),
        prefix="/plus",
    )
    owner, csrf = browser(app, sessions[1])
    device = _pair_library_device(app, owner, csrf)

    catalog = device.get(LIBRARY_API + "/catalog?kind=anime")
    assert catalog.status_code == 200 and catalog.json()["items"]
    for kind in ("movie", "tv"):
        listing = device.get(LIBRARY_API + f"/catalog?kind={kind}")
        assert listing.status_code == 200

    details = device.get(LIBRARY_API + "/episodes?kind=tv&title_id=1")
    assert details.status_code == 200 and details.json()["items"][0]["watch_path"] == (
        "/plus/watch/title/tv/1/1/2"
    )

    paths = {
        "anime": "/plus/watch/1/1",
        "movie": "/plus/watch/title/movie/tt0000001",
        "tv": "/plus/watch/title/tv/1/1/2",
    }
    for domain, path in paths.items():
        played = device.post(LIBRARY_API + "/play", json={"watch_path": path})
        assert played.status_code == 200, played.text
        media = played.json()
        assert media["identity"]["domain"] == domain
        device.cookies.set(COOKIE, media["media_token"])
        prefix = API + "/media/" + media["media_id"]
        assert "00000.ts" in device.get(prefix + "/index.m3u8").text
        assert device.get(prefix + "/00000.ts").content == b"synthetic segment"
        if domain == "anime":
            # Progress is saved while this exact provider pass is the active one.
            resumed = device.post(
                LIBRARY_API + "/media/" + media["media_id"] + "/resume",
                json={"seq": 1, "position_seconds": 2},
            )
            assert resumed.status_code == 200, resumed.text
    assert db.execute("SELECT COUNT(*) FROM web_playback_resumes").fetchone()[0] >= 1

    saved = owner.post(
        "/plus/api/cloud/progress",
        data={
            "csrf_token": csrf,
            "file_key": "d-download1",
            "title": "Existing.mp4",
            "position": 42,
            "duration": 600,
        },
    )
    assert saved.status_code == 200
    opened = _cloud_session(owner, sessions[1])
    assert opened["subtitles"] == [] and opened["subtitle_url"] is None
    media_url = opened["media_url"]
    assert owner.get(media_url).status_code == 200
    assert owner.get(media_url.removesuffix("index.m3u8") + "00000.ts").content == (
        b"synthetic segment"
    )

    subtitles = owner.get(media_url.removesuffix("index.m3u8") + "subtitles0.m3u8")
    assert subtitles.status_code == 404
    assert subtitles.json()["detail"] == "This stream has no subtitles."
    assert plex_tripwire == []


def test_plex_household_still_plays_through_the_web_player(setup):
    app, db, sessions, calls, _ = setup
    _link_plex(db)
    _save_choice(db, 1, "plex")
    assert playback_providers.select(db, 1).id == "plex"

    viewer = playback_client(app, sessions[1])
    page = viewer.get("/plus/watch/1/1")

    assert page.status_code == 200
    assert "data-web-player" in page.text
    assert calls == []


def test_local_household_catalog_and_details_never_borrow_another_provider(
    provider_db, monkeypatch, plex_tripwire
):
    def deny(label):
        def trip(*args, **kwargs):
            raise AssertionError(f"another provider was reached: {label}")

        return trip

    monkeypatch.setattr(native_tv_library, "anime_episodes", deny("anime_episodes"))
    monkeypatch.setattr(native_tv_library, "debrid_catalog_title", deny("debrid_catalog_title"))
    _link_local(provider_db)
    _save_choice(provider_db, 1, "local")
    provider_db.execute(
        "INSERT INTO media_monitors(id,user_id,anilist_id,title,season_number) "
        "VALUES(1,1,100,'Fixture episode',1)"
    )
    provider_db.commit()
    scope = web_playback_catalog.personal_scope(provider_db, 1)

    cards = native_tv_library.collection(provider_db, scope, "anime", 0)

    assert [card["title"] for card in cards["items"]] == ["Fixture episode"]
    assert cards["items"][0]["featured_path"] is None
    details = native_tv_library.episodes(
        provider_db, scope, "anime", "1", 0, debrid_episodes=(), debrid_only=False
    )
    assert details["items"] == []
    assert plex_tripwire == []


def test_local_household_is_refused_by_the_plex_web_player(setup):
    app, db, sessions, calls, _ = setup
    _link_local(db)
    _save_choice(db, 1, "local")
    assert playback_providers.select(db, 1).id == "local"

    viewer = playback_client(app, sessions[1])
    page = viewer.get("/plus/watch/1/1")

    assert page.status_code == 409
    assert "not available in the web player yet" in page.json()["detail"]
    assert calls == []
