"""Offline exact episode artwork: ownership, mappings, transport limits and lazy cache."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from omakase.plus import history_artwork as art
from omakase.plus import history_artwork_routes as routes
from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db

STAMP = "2026-10-28T10:00:00Z"


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setattr(art, "poster_cache_root", lambda: tmp_path / "posters")
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    conn.executemany(
        "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
        [(1, "one@fixture.invalid", "x"), (2, "two@fixture.invalid", "x")],
    )
    conn.execute(
        "INSERT INTO media_monitors(id,user_id,anilist_id,title,season_number) VALUES(1,1,21,'Exact Anime',1)"
    )
    conn.execute(
        "INSERT INTO watch_entries(id,user_id,monitor_id,anilist_id,season_number) VALUES(1,1,1,21,1)"
    )
    for n in (1, 2):
        conn.execute(
            "INSERT INTO watch_events(id,watch_entry_id,user_id,source,event_key,kind,episode,confidence,disposition,observed_at) VALUES(?,1,1,'plex',?,'progress',?,'exact','accepted',?)",
            (n, f"plex:snapshot:100:s1:e{n}:None", n, STAMP),
        )
    conn.execute(
        "INSERT INTO plex_library_snapshots(id,schema_version,generated_at,received_at,payload_hash,item_count,title_count) VALUES(1,'plex-library-snapshot-v1',?,?,?,1,1)",
        (STAMP, STAMP, "a" * 64),
    )
    conn.execute(
        "INSERT INTO plex_library_items(snapshot_id,rating_key,section,media_type,title,season_number,episode_total,watched_through,canonical_title,managed_paths_json,path_identity_complete) VALUES(1,'100','Anime','show','Display name',1,2,0,'Exact Anime',?,1)",
        (json.dumps([["Exact Anime", 1]]),),
    )
    conn.execute(
        "INSERT INTO tv_catalog VALUES(1,42,?,?)", (json.dumps({"title": "Exact TV"}), STAMP)
    )
    conn.execute(
        "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,watched,watch_source,updated_at) VALUES(1,42,1,2,?,1,'manual',?)",
        (json.dumps({"episode_number": 2}), STAMP),
    )
    conn.commit()
    art._CACHE.clear()
    yield conn
    conn.close()
    art._CACHE.clear()


def source(db, identity):
    return art._Source(
        "https://plex.fixture" if identity.media_type == "anime" else "https://seerr.fixture",
        "fixture-secret",
        "plex" if identity.media_type == "anime" else "seerr",
        "machine-one",
    )


def plex_transport(calls, *, thumb=None, season=1, episode=1, machine="machine-one", path=None):
    def handler(request):
        calls.append(request)
        p = request.url.path
        if p == "/identity":
            return httpx.Response(200, text=f'<MediaContainer machineIdentifier="{machine}"/>')
        if p == "/library/metadata/100/children":
            return httpx.Response(
                200,
                text='<MediaContainer size="1"><Directory type="season" ratingKey="101" parentRatingKey="100" index="1"/></MediaContainer>',
            )
        if p == "/library/metadata/101/children":
            rows = ""
            for n in (1, 2):
                image = thumb or f"/library/metadata/{101 + n}/thumb/123"
                part = (
                    path or f"/published/Anime/Exact Anime/Season 01/Exact Anime - S01E{n:02}.mkv"
                )
                rows += f'<Video type="episode" ratingKey="{101 + n}" grandparentRatingKey="100" parentRatingKey="101" parentIndex="{season}" index="{n if episode == 1 else episode}" thumb="{image}"><Media><Part file="{part}"/></Media></Video>'
            return httpx.Response(200, text=f'<MediaContainer size="2">{rows}</MediaContainer>')
        if p.startswith("/library/metadata/") and "/thumb/" in p:
            return httpx.Response(
                200,
                content=png("red" if "/102/" in p else "blue"),
                headers={"content-type": "image/png"},
            )
        raise AssertionError(f"Unexpected offline request {p}")

    return httpx.MockTransport(handler)


def test_exact_anime_episodes_have_different_sanitized_stills_and_cache(db, monkeypatch):
    monkeypatch.setattr(art, "_source", source)
    calls = []
    transport = plex_transport(calls)
    first = art.anime_identity(db, 1, 1)
    second = art.anime_identity(db, 1, 2)
    before = db.total_changes

    async def run():
        a = await art.episode_artwork(db, first, transport=transport)
        b = await art.episode_artwork(db, second, transport=transport)
        again = await art.episode_artwork(db, first, transport=transport)
        assert a == again and a[1] == "image/webp"
        assert hashlib.sha256(a[0]).digest() != hashlib.sha256(b[0]).digest()

    asyncio.run(run())
    assert len(calls) == 8
    assert all(r.headers["X-Plex-Token"] == "fixture-secret" for r in calls)
    assert db.total_changes == before


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE plex_library_items SET path_identity_complete=0",
        "UPDATE plex_library_items SET canonical_title='Different'",
        "UPDATE plex_library_items SET season_number=2",
        "UPDATE watch_entries SET monitor_id=NULL WHERE id=1",
        "DELETE FROM watch_events WHERE id=1",
    ],
)
def test_missing_stale_or_contradictory_anime_identity_fails_closed(db, change):
    db.execute(change)
    with pytest.raises(art.EpisodeArtworkUnavailable):
        art.anime_identity(db, 1, 1)


@pytest.mark.parametrize(
    "options",
    [
        {"machine": "other"},
        {"season": 2},
        {"episode": 2},
        {"thumb": "https://attacker.invalid/thumb"},
        {"thumb": "/library/metadata/999/thumb/123"},
        {"path": "/published/Anime/Other/Season 01/S01E01.mkv"},
        {"path": "/published/Anime/Exact Anime/Season 01/S01E64.mkv"},
    ],
)
def test_anime_mismatched_machine_season_episode_path_thumb_never_returns_image(
    db, monkeypatch, options
):
    monkeypatch.setattr(art, "_source", source)
    calls = []

    async def run():
        with pytest.raises(art.EpisodeArtworkUnavailable):
            await art.episode_artwork(
                db, art.anime_identity(db, 1, 1), transport=plex_transport(calls, **options)
            )

    asyncio.run(run())
    assert not any("/thumb/" in r.url.path for r in calls)


def test_tv_exact_season_episode_still_has_no_db_writes_or_credentials_on_cdn(db, monkeypatch):
    monkeypatch.setattr(art, "_source", source)
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/api/v1/tv/42":
            return httpx.Response(200, json={"id": 42})
        if request.url.path == "/api/v1/tv/42/season/1":
            return httpx.Response(
                200,
                json={
                    "seasonNumber": 1,
                    "episodes": [
                        {"seasonNumber": 1, "episodeNumber": 2, "stillPath": "/exact.png"}
                    ],
                },
            )
        assert request.url.host == "image.tmdb.org" and request.url.path == "/t/p/w780/exact.png"
        assert "X-Api-Key" not in request.headers and "X-Plex-Token" not in request.headers
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    before = [tuple(r) for r in db.execute("SELECT * FROM tv_episodes")]
    result = asyncio.run(
        art.episode_artwork(
            db, art.tv_identity(db, 1, 42, 1, 2), transport=httpx.MockTransport(handler)
        )
    )
    assert result[1] == "image/webp" and len(calls) == 3
    assert [tuple(r) for r in db.execute("SELECT * FROM tv_episodes")] == before


@pytest.mark.parametrize(
    "row",
    [
        {"seasonNumber": 2, "episodeNumber": 2, "stillPath": "/a.jpg"},
        {"seasonNumber": 1, "episodeNumber": 2, "stillPath": "https://evil.invalid/a.jpg"},
        {"seasonNumber": 1, "episodeNumber": 2, "stillPath": "/../a.jpg"},
        {"seasonNumber": 1, "episodeNumber": 3, "stillPath": "/a.jpg"},
    ],
)
def test_tv_does_not_guess_numbering_or_use_untrusted_still(db, monkeypatch, row):
    monkeypatch.setattr(art, "_source", source)

    def handler(request):
        if request.url.path.endswith("/season/1"):
            return httpx.Response(200, json={"seasonNumber": 1, "episodes": [row]})
        assert request.url.path == "/api/v1/tv/42"
        return httpx.Response(200, json={"id": 42})

    with pytest.raises(art.EpisodeArtworkUnavailable):
        asyncio.run(
            art.episode_artwork(
                db, art.tv_identity(db, 1, 42, 1, 2), transport=httpx.MockTransport(handler)
            )
        )


def test_only_needed_tested_connection_is_required(db, monkeypatch):
    called = []
    monkeypatch.setattr(
        art,
        "_values",
        lambda db, user, spec: (
            called.append(spec.id) or (f"https://{spec.id}.fixture", "fixture-secret")
        ),
    )
    monkeypatch.setattr(art, "_saved_health", lambda *args: {"state": "connected"})
    monkeypatch.setattr(art, "_read", lambda *args: "machine-one")
    assert art._source(db, art.anime_identity(db, 1, 1)).service == "plex"
    assert art._source(db, art.tv_identity(db, 1, 42, 1, 2)).service == "seerr"
    assert called == ["plex", "seerr"]
    monkeypatch.setattr(art, "_saved_health", lambda *args: {"state": "configured"})
    with pytest.raises(art.EpisodeArtworkUnavailable):
        art._source(db, art.tv_identity(db, 1, 42, 1, 2))


def test_router_checks_current_ownership_before_even_cached_image(db, monkeypatch):
    monkeypatch.setattr(art, "_source", source)
    calls = []

    async def resolve(conn, identity):
        return await art.episode_artwork(conn, identity, transport=plex_transport(calls))

    monkeypatch.setattr(routes, "episode_artwork", resolve)
    app = FastAPI()
    app.include_router(routes.create_history_artwork_router(), prefix="/plus")
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        client.cookies.set("omakase_session", create_session(db, 2))
        assert client.get("/plus/history/artwork/anime/1").status_code == 404
        assert client.get("/plus/history/artwork/tv/42/1/2").status_code == 404
        assert calls == []
        client.cookies.set("omakase_session", create_session(db, 1))
        response = client.get("/plus/history/artwork/anime/1")
        assert (
            response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
        )
        assert response.headers["content-type"] == "image/webp"
        assert "fixture-secret" not in str(response.headers)
        count = len(calls)
        db.execute("DELETE FROM watch_events WHERE id=1")
        db.commit()
        assert (
            client.get("/plus/history/artwork/anime/1").status_code == 404 and len(calls) == count
        )
        client.cookies.clear()
        assert client.get("/plus/history/artwork/anime/2").status_code == 401


def test_parallel_misses_coalesce_and_negative_results_cool_down(db, monkeypatch):
    monkeypatch.setattr(art, "_source", source)
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(404)

    async def run():
        identity = art.tv_identity(db, 1, 42, 1, 2)

        async def attempt():
            with pytest.raises(art.EpisodeArtworkUnavailable):
                await art.episode_artwork(db, identity, transport=httpx.MockTransport(handler))

        await asyncio.gather(attempt(), attempt())
        await attempt()

    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,headers,body",
    [
        (302, {"location": "http://metadata.google.internal"}, b""),
        (200, {"content-length": str(art._IMAGE_LIMIT + 1)}, b""),
        (200, {"content-type": "image/svg+xml"}, b"<svg/>"),
    ],
)
def test_reader_rejects_redirect_oversize_and_nonimage(status, headers, body):
    reader = art._Reader(
        art._Source("https://plex.fixture", "secret", "plex"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, headers=headers, content=body)
        ),
    )
    with pytest.raises(art.EpisodeArtworkUnavailable):
        asyncio.run(reader.read("/library/metadata/1/thumb/1", image=True))


def test_real_home_and_history_routes_render_episode_identity_without_external_reads(
    db, monkeypatch
):
    from omakase.plus import routes as main_routes

    def forbidden(*args, **kwargs):
        raise AssertionError("History page render must never fetch episode metadata or images")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    app = FastAPI()
    app.include_router(main_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        client.cookies.set("omakase_session", create_session(db, 1))
        for url in ("/plus/home", "/plus/activity?view=watch"):
            response = client.get(url)
            assert response.status_code == 200
            assert "data-history-artwork" in response.text
            assert "/plus/history/artwork/anime/" in response.text
            assert "data-history-placeholder" in response.text
            assert "Image unavailable" in response.text
            assert "Observed" in response.text


def test_public_cdn_private_dns_rebinding_and_path_injection_are_rejected(monkeypatch):
    import ipaddress

    async def private_resolver(*args):
        return (ipaddress.ip_address("192.168.50.10"),)

    monkeypatch.setenv("OMAKASE_ALLOW_PRIVATE_CATALOG_URLS", "true")

    async def run():
        reader = art._Reader(
            art._Source("https://seerr.fixture", "secret", "seerr"),
            transport=httpx.MockTransport(
                lambda _: pytest.fail("Blocked request must not reach transport")
            ),
            resolver=private_resolver,
        )
        with pytest.raises(art.EpisodeArtworkUnavailable):
            await reader.read("/t/p/w780/a.jpg", image=True, public=True)
        for path in (
            "//evil.invalid/image",
            "/../secret",
            "/library/metadata/1/thumb/1?X-Plex-Token=leak",
        ):
            with pytest.raises(art.EpisodeArtworkUnavailable):
                await reader.read(path, image=True)

    asyncio.run(run())


def test_persisted_exact_tv_still_requires_no_unrelated_connection_or_metadata_fetch(
    db, monkeypatch
):
    db.execute(
        "UPDATE tv_episodes SET metadata_json=?",
        (
            json.dumps(
                {
                    "episode_number": 2,
                    "season_number": 1,
                    "still_url": "https://image.tmdb.org/t/p/w780/exact.jpg",
                }
            ),
        ),
    )
    monkeypatch.setattr(
        art, "_values", lambda *args: pytest.fail("Known public still needs no service credentials")
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "image.tmdb.org"
        assert "X-Api-Key" not in request.headers and "X-Plex-Token" not in request.headers
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    result = asyncio.run(
        art.episode_artwork(
            db, art.tv_identity(db, 1, 42, 1, 2), transport=httpx.MockTransport(handler)
        )
    )
    assert result[1] == "image/webp" and len(calls) == 1


def test_tv_image_route_cannot_serve_cached_bytes_after_episode_record_deleted(db, monkeypatch):
    db.execute(
        "UPDATE tv_episodes SET metadata_json=?",
        (
            json.dumps(
                {"episode_number": 2, "still_url": "https://image.tmdb.org/t/p/w780/exact.jpg"}
            ),
        ),
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    async def resolve(conn, identity):
        return await art.episode_artwork(conn, identity, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(routes, "episode_artwork", resolve)
    app = FastAPI()
    app.include_router(routes.create_history_artwork_router(), prefix="/plus")
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        client.cookies.set("omakase_session", create_session(db, 1))
        assert client.get("/plus/history/artwork/tv/42/1/2").status_code == 200
        db.execute("DELETE FROM tv_episodes")
        db.commit()
        assert client.get("/plus/history/artwork/tv/42/1/2").status_code == 404
        assert len(calls) == 1
        client.cookies.clear()
        response = client.get("/plus/history/artwork/tv/42/1/2")
        assert (
            response.status_code == 401 and response.headers["cache-control"] == "private, no-store"
        )


def test_disabled_private_mode_hides_image_router_before_account_lookup(db, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    app = FastAPI()
    app.include_router(routes.create_history_artwork_router(), prefix="/plus")
    app.dependency_overrides[get_db] = lambda: pytest.fail(
        "Disabled router must not read account database"
    )
    with TestClient(app) as client:
        response = client.get("/plus/history/artwork/anime/1")
        assert (
            response.status_code == 404 and response.headers["cache-control"] == "private, no-store"
        )


def test_history_without_exact_episode_identity_offers_labeled_series_art(db):
    from omakase.plus.viewing_history import viewing_history

    db.execute("UPDATE watch_entries SET monitor_id=NULL WHERE id=1")
    db.execute(
        "INSERT INTO tv_history(user_id,tmdb_id,status,status_source,updated_at) VALUES(1,42,'completed','manual',?)",
        (STAMP,),
    )
    cards = viewing_history(db, 1)["items"]
    anime = [card for card in cards if card["media_type"] == "anime"]
    completed = next(card for card in cards if card["id"] == "tv-42-completed")
    assert all(card["artwork_url"] is None and card["episode_artwork"] for card in anime)
    assert completed["artwork_url"] is None and completed["episode_artwork"]
    assert all(card["series_artwork_url"] == "/plus/media/21/poster" for card in anime)
    assert completed["series_artwork_url"] == "/plus/tv/42/artwork"


@pytest.mark.parametrize(
    "name,mime",
    [("history-artwork.js", "application/javascript"), ("history-artwork.css", "text/css")],
)
def test_root_wired_episode_assets_are_served_from_actual_static_allowlist(monkeypatch, name, mime):
    from pathlib import Path

    from omakase.plus import routes as main_routes

    app = FastAPI()
    app.include_router(main_routes.router)
    with TestClient(app) as client:
        response = client.get("/plus/static/" + name)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(mime)
        assert (
            response.content == (Path(main_routes.__file__).parent / "static" / name).read_bytes()
        )


def test_actual_snapshot_writer_stored_pairs_resolve_owned_episode(db):
    from omakase.plus.watch.plex_sync import store_plex_library_snapshot

    payload = {
        "schema_version": "plex-library-snapshot-v1",
        "generated_at": "2026-10-28T11:00:00Z",
        "items": [
            {
                "rating_key": "100",
                "section": "Anime",
                "media_type": "show",
                "title": "Display name",
                "season_number": 1,
                "episode_total": 2,
                "watched_through": 0,
                "last_viewed_at": None,
                "canonical_title": "Exact Anime",
                "managed_paths": [{"canonical_title": "Exact Anime", "season_number": 1}],
                "path_identity_complete": True,
            }
        ],
    }
    store_plex_library_snapshot(db, payload, received_at="2026-10-28T11:00:00Z")
    stored = db.execute(
        "SELECT managed_paths_json FROM plex_library_items ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert json.loads(stored) == [["Exact Anime", 1]]
    assert art.anime_identity(db, 1, 1).show_key == "100"
    with pytest.raises(art.EpisodeArtworkUnavailable):
        art.anime_identity(db, 2, 1)


def test_episode_disk_cache_survives_memory_reset_but_checks_current_binding(db, monkeypatch):
    monkeypatch.setattr(art, "_source", source)
    calls = []
    identity = art.anime_identity(db, 1, 1)
    first = asyncio.run(art.episode_artwork(db, identity, transport=plex_transport(calls)))
    assert len(calls) == 4
    art._CACHE.clear()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Persistent cache must not refetch the source")

    monkeypatch.setattr(art, "_plex_still", forbidden)
    assert asyncio.run(art.episode_artwork(db, art.anime_identity(db, 1, 1))) == first
    files = list((art.poster_cache_root() / "history-stills" / "1").glob("*.webp"))
    assert len(files) == 1 and "fixture-secret" not in files[0].name
    db.execute("DELETE FROM watch_events WHERE id=1")
    with pytest.raises(art.EpisodeArtworkUnavailable):
        art.anime_identity(db, 1, 1)


def test_noncanonical_reference_shapes_do_not_grant_episode_images(db):
    db.execute(
        "UPDATE plex_library_items SET managed_paths_json=?",
        (json.dumps([{"canonical_title": "Exact Anime", "season_number": 1}]),),
    )
    with pytest.raises(art.EpisodeArtworkUnavailable):
        art.anime_identity(db, 1, 1)


def test_persisted_still_is_not_reused_after_connection_machine_changes(db, monkeypatch):
    monkeypatch.setattr(art, "_source", source)
    identity = art.anime_identity(db, 1, 1)
    asyncio.run(art.episode_artwork(db, identity, transport=plex_transport([])))
    art._CACHE.clear()
    monkeypatch.setattr(
        art,
        "_source",
        lambda db, identity: art._Source(
            "https://plex.fixture", "fixture-secret", "plex", "different-machine"
        ),
    )
    calls = []
    with pytest.raises(art.EpisodeArtworkUnavailable):
        asyncio.run(art.episode_artwork(db, identity, transport=plex_transport(calls)))
    assert len(calls) == 1 and calls[0].url.path == "/identity"


def test_persistent_cache_rejects_corrupt_webp_and_contains_no_source_locator(db, monkeypatch):
    monkeypatch.setattr(art, "_source", source)
    identity = art.anime_identity(db, 1, 1)
    asyncio.run(art.episode_artwork(db, identity, transport=plex_transport([])))
    file = next((art.poster_cache_root() / "history-stills" / "1").glob("*.webp"))
    assert b"fixture-secret" not in file.read_bytes() and b"plex.fixture" not in file.read_bytes()
    file.write_bytes(b"RIFF0000WEBPnot-a-real-image")
    art._CACHE.clear()
    calls = []
    result = asyncio.run(art.episode_artwork(db, identity, transport=plex_transport(calls)))
    assert result[1] == "image/webp" and len(calls) == 4


def test_distinct_history_cards_queue_with_at_most_four_active_resolutions(db, monkeypatch):
    monkeypatch.setattr(art, "_source", source)
    for number in range(3, 9):
        db.execute(
            "INSERT INTO watch_events(id,watch_entry_id,user_id,source,event_key,kind,episode,confidence,disposition,observed_at) VALUES(?,1,1,'manual',?,'progress',?,'manual','accepted',?)",
            (number, "manual-concurrency-" + str(number), number, STAMP),
        )
    active = 0
    peak = 0
    resolved = []

    async def exact_still(reader, identity):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        resolved.append(identity.episode)
        active -= 1
        return png()

    monkeypatch.setattr(art, "_plex_still", exact_still)

    async def resolve_all():
        return await asyncio.gather(
            *(art.episode_artwork(db, art.anime_identity(db, 1, number)) for number in range(1, 9))
        )

    results = asyncio.run(resolve_all())
    assert len(results) == 8 and all(kind == "image/webp" for _, kind in results)
    assert sorted(resolved) == list(range(1, 9)) and peak == 4
