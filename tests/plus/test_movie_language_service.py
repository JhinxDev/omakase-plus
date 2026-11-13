"""Actual movie service uses the same actor/file/language proof as persistence."""

import asyncio
import sqlite3
from dataclasses import replace

import pytest

from omakase.plus import connections, households
from omakase.plus.movies import MovieService
from tests.plus import test_playback_proof as proof_fixtures
from tests.plus.test_playback_proof import request, transport


@pytest.fixture
def data(tmp_path):
    yield from proof_fixtures.data.__wrapped__(tmp_path)


@pytest.fixture(autouse=True)
def private_mode(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")


def service(data, **options):
    mock, calls = transport(**options)
    cfg = replace(
        connections.movie_connections(data[0], 1),
        enabled=True,
        riven_url="https://riven.fixture.invalid",
        riven_api_key="fixture-riven",
        seerr_url="https://seerr.fixture.invalid",
        seerr_api_key="fixture-seerr",
    )
    return MovieService(data[0], cfg, transport=mock), calls


def test_shared_french_movie_verifies_actual_viewer_without_copying_owner_catalog(data):
    snap = request(data)
    data[0].commit()
    backend, calls = service(data)
    result = asyncio.run(backend.verify_language(2, request_id=snap.request_id, rating_key="10"))
    assert result["state"] == "ready" and result["selected_audio"] == "French"
    assert result["bytes_read"] == 65536 and result["proof_schema"] == 2
    assert len(calls) == 7
    assert data[0].execute("SELECT count(*) FROM movie_catalog WHERE user_id=2").fetchone()[0] == 0
    assert (
        data[0]
        .execute("SELECT proof_schema FROM movie_verifications WHERE user_id=2")
        .fetchone()[0]
        == 2
    )
    before = len(calls)
    cached = backend.language_status(2, request_id=snap.request_id)
    assert cached["state"] == "ready" and len(calls) == before
    households.connection_generation_changed(data[0], owner_user_id=1, integration="plex")
    assert backend.language_status(2, request_id=snap.request_id)["state"] != "ready"
    assert len(calls) == before


def test_required_subtitles_unselected_does_not_write_ready_or_replace_media(data):
    snap = request(data)
    data[0].commit()
    backend, calls = service(data, subtitle="0")
    result = asyncio.run(backend.verify_language(2, request_id=snap.request_id, rating_key="10"))
    assert result["state"] != "ready" and result["language_state"] == "selection_required"
    assert result["physical_compatible"] is True
    assert (
        data[0].execute("SELECT count(*) FROM movie_verifications WHERE user_id=2").fetchone()[0]
        == 0
    )
    assert all(call[0] == "GET" for call in calls)


def test_revoked_viewer_never_calls_sources_or_reuses_ready_receipt(data):
    snap = request(data)
    data[0].commit()
    backend, calls = service(data)
    owner = households.resolve_scope(data[0], viewer_id=1, library_id=data[1])
    households.leave_or_revoke(data[0], owner, member_id=2)
    result = asyncio.run(backend.verify_language(2, request_id=snap.request_id, rating_key="10"))
    assert result["state"] != "ready" and calls == []


def test_personal_playback_verification_does_not_require_members_acquisition_connections(data):
    from omakase.plus.movies import MovieConnections

    snap = request(data)
    data[0].commit()
    mock, calls = transport()
    backend = MovieService(data[0], MovieConnections(), transport=mock)
    result = asyncio.run(backend.verify_language(2, request_id=snap.request_id, rating_key="10"))
    assert result["state"] == "ready" and len(calls) == 7


def acquisition(data):
    import json

    from omakase.plus import language_store
    from omakase.plus.secrets import store_secret

    db, lib, item, _ = data
    db.execute("UPDATE libraries SET acquisition_requests=1 WHERE id=?", (lib,))
    db.commit()
    snap = request(data, name="preparation")
    scope = households.resolve_scope(db, viewer_id=2, library_id=lib)
    event = households.LibraryRepository(db, scope).queue_request(
        item, idempotency_key="same-movie"
    )
    snap = replace(snap, request_id="shared-acquisition")
    language_store.record_request(db, snap, intent="acquire", household_event_id=event)
    for name, value in [
        ("riven_url", "https://riven.fixture.invalid"),
        ("riven_api_key", "fixture-riven"),
        ("seerr_url", "https://seerr.fixture.invalid"),
        ("seerr_api_key", "fixture-seerr"),
    ]:
        store_secret(db, 1, name, value)
    for integration in ["riven", "seerr", "plex"]:
        url, key = connections._values(db, 1, connections._spec(integration))
        db.execute(
            "INSERT OR REPLACE INTO user_connection_health(user_id,integration,fingerprint,health_json,checked_at) VALUES(?,?,?,?,?)",
            (
                1,
                integration,
                connections._fingerprint(url, key),
                json.dumps(connections._health("connected", "connected", connected=True)),
                "2026-10-28T00:00:00Z",
            ),
        )
    for integration in ("riven", "seerr"):
        db.execute(
            "INSERT INTO library_settings(library_id,integration,owner_user_id) VALUES(?,?,1)",
            (lib, integration),
        )
    db.commit()
    return snap, event


def acquisition_backend(data, before_inventory=None):
    import httpx

    calls = []
    items = []

    def handle(req):
        calls.append(req)
        if req.url.host == "seerr.fixture.invalid":
            assert req.method == "GET" and req.url.path == "/api/v1/movie/1"
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "title": "Synthetic",
                    "releaseDate": "2026-01-01",
                    "imdbId": "tt0000001",
                    "externalIds": {"imdbId": "tt0000001"},
                },
            )
        assert req.url.host == "riven.fixture.invalid"
        if req.url.path == "/api/v1/items/add":
            assert req.method == "POST" and dict(req.url.params) == {"imdb_ids": "tt0000001"}
            items.append(
                {
                    "id": 1,
                    "type": "movie",
                    "imdb_id": "tt0000001",
                    "tmdb_id": 1,
                    "title": "Synthetic",
                    "year": 2026,
                    "state": "Completed",
                }
            )
            return httpx.Response(200, json={"success": True})
        if before_inventory:
            before_inventory()
        if req.url.path == "/api/v1/items":
            return httpx.Response(200, json={"items": items})
        assert req.url.path == "/api/v1/items/1"
        return httpx.Response(200, json=items[0])

    return MovieService(
        data[0], connections.movie_connections(data[0], 1), transport=httpx.MockTransport(handle)
    ), calls


def test_shared_acquisition_retains_actor_event_and_backend_global_reservation(data):
    snap, event = acquisition(data)
    svc, calls = acquisition_backend(data)
    first = asyncio.run(svc.request_language(2, request_id=snap.request_id))
    assert first["requested"] and first["request_id"] == snap.request_id
    assert len([r for r in calls if r.method == "POST"]) == 1
    assert (
        data[0]
        .execute("SELECT actor_user_id,state FROM library_request_events WHERE id=?", (event,))
        .fetchone()[0]
        == 2
    )
    asyncio.run(svc.request_language(2, request_id=snap.request_id))
    assert len([r for r in calls if r.method == "POST"]) == 1
    assert data[0].execute("SELECT count(*) FROM movie_requests WHERE user_id=2").fetchone()[0] == 0


def test_membership_revoked_during_backend_read_never_dispatches_movie(data):
    snap, event = acquisition(data)
    changed = False

    def revoke():
        nonlocal changed
        if not changed:
            changed = True
            households.leave_or_revoke(
                data[0],
                households.resolve_scope(data[0], viewer_id=1, library_id=data[1]),
                member_id=2,
            )

    svc, calls = acquisition_backend(data, before_inventory=revoke)
    result = asyncio.run(svc.request_language(2, request_id=snap.request_id))
    assert result["state"] != "ready" and all(r.method == "GET" for r in calls)


def test_owner_personal_connectors_are_not_implicit_shared_acquisition_authority(data):
    snap, event = acquisition(data)
    data[0].execute(
        "DELETE FROM library_settings WHERE library_id=? AND integration='riven'", (data[1],)
    )
    data[0].commit()
    svc, calls = acquisition_backend(data)
    result = asyncio.run(svc.request_language(2, request_id=snap.request_id))
    assert result["state"] != "ready" and calls == []


@pytest.mark.parametrize("consumer", ["cache", "proof", "rewrite"])
def test_fresh_unselected_subtitle_must_supersede_prior_ready_cache(data, consumer):
    snap = proof_fixtures.request(data)
    data[0].commit()
    first, _ = service(data)
    assert (
        asyncio.run(first.verify_language(2, request_id=snap.request_id, rating_key="10"))["state"]
        == "ready"
    )
    old = dict(data[0].execute("SELECT * FROM movie_verifications WHERE user_id=2").fetchone())
    second, calls = service(data, subtitle="0")
    changed = asyncio.run(second.verify_language(2, request_id=snap.request_id, rating_key="10"))
    assert changed["language_state"] == "selection_required" and changed["state"] != "ready"
    before = len(calls)
    if consumer == "cache":
        cached = second.language_status(2, request_id=snap.request_id)
        assert cached["state"] != "ready", (
            "Current cache resurrected prior selected-subtitle proof after fresh contrary readback"
        )
    elif consumer == "proof":
        from omakase.plus.playback_proof import receipt_kwargs

        with pytest.raises(ValueError):
            receipt_kwargs(
                data[0],
                actor_user_id=2,
                request_id=snap.request_id,
                evaluation_id=old["language_evaluation_id"],
            )
    else:
        from omakase.plus.movies_store import save_verification

        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            save_verification(data[0], 2, "tt0000001", old)
    assert len(calls) == before


def test_another_viewers_subtitle_selection_does_not_supersede_member(data):
    snap = proof_fixtures.request(data)
    data[0].commit()
    member, _ = service(data)
    assert (
        asyncio.run(member.verify_language(2, request_id=snap.request_id, rating_key="10"))["state"]
        == "ready"
    )
    owner_snap = proof_fixtures.request(data, name="owner-view", user=1)
    data[0].commit()
    owner, calls = service(data, subtitle="0", expected_token="synthetic-owner")
    assert (
        asyncio.run(owner.verify_language(1, request_id=owner_snap.request_id, rating_key="10"))[
            "language_state"
        ]
        == "selection_required"
    )
    before = len(calls)
    assert member.language_status(2, request_id=snap.request_id)["state"] == "ready"
    assert len(calls) == before


def test_removed_shared_riven_authority_before_dispatch_never_posts(data):
    snap, _ = acquisition(data)
    changed = False

    def withdraw_binding():
        nonlocal changed
        if not changed:
            changed = True
            # Change the authoritative binding itself, without a revision hint.
            data[0].execute(
                "DELETE FROM library_settings WHERE library_id=? AND integration='riven'",
                (data[1],),
            )
            data[0].commit()

    backend, calls = acquisition_backend(data, before_inventory=withdraw_binding)
    result = asyncio.run(backend.request_language(2, request_id=snap.request_id))
    assert changed
    assert all(r.method != "POST" for r in calls), (
        "Final dispatch trusted the earlier shared-connector authorization"
    )
    assert result["state"] != "ready"


@pytest.mark.parametrize("consumer", ["cache", "proof", "rewrite"])
def test_failed_explicit_byte_check_invalidates_old_ready_without_erasing_history(data, consumer):
    snap = request(data)
    data[0].commit()
    first, _ = service(data)
    assert (
        asyncio.run(first.verify_language(2, request_id=snap.request_id, rating_key="10"))["state"]
        == "ready"
    )
    old = dict(data[0].execute("SELECT * FROM movie_verifications WHERE user_id=2").fetchone())
    count = data[0].execute("SELECT count(*) FROM viewer_selection_observations").fetchone()[0]
    failed, calls = service(data, status=403)
    assert (
        asyncio.run(failed.verify_language(2, request_id=snap.request_id, rating_key="10"))["state"]
        != "ready"
    )
    assert (
        data[0].execute("SELECT count(*) FROM viewer_selection_observations").fetchone()[0] == count
    )
    before = len(calls)
    if consumer == "cache":
        assert failed.language_status(2, request_id=snap.request_id)["state"] != "ready"
    elif consumer == "proof":
        from omakase.plus.playback_proof import receipt_kwargs

        with pytest.raises(ValueError):
            receipt_kwargs(
                data[0],
                actor_user_id=2,
                request_id=snap.request_id,
                evaluation_id=old["language_evaluation_id"],
            )
    else:
        from omakase.plus.movies_store import save_verification

        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            save_verification(data[0], 2, "tt0000001", old)
    assert len(calls) == before


def test_older_inflight_success_cannot_overwrite_newer_failed_check(data):
    import httpx

    snap = request(data)
    data[0].commit()
    original, calls = transport()
    final_metadata = 0
    failed, _ = service(data, status=403)

    async def handle(req):
        nonlocal final_metadata
        response = original.handle_request(req)
        if req.url.path == "/library/metadata/10":
            final_metadata += 1
            if final_metadata == 2:
                assert (
                    await failed.verify_language(2, request_id=snap.request_id, rating_key="10")
                )["state"] != "ready"
        return response

    older = MovieService(
        data[0], connections.movie_connections(data[0], 1), transport=httpx.MockTransport(handle)
    )
    assert (
        asyncio.run(older.verify_language(2, request_id=snap.request_id, rating_key="10"))["state"]
        != "ready"
    )
    assert final_metadata == 2 and len(calls) == 7
    assert older.language_status(2, request_id=snap.request_id)["state"] != "ready"
    assert data[0].execute("SELECT count(*) FROM viewer_selection_observations").fetchone()[0] == 0


def test_failed_explicit_check_can_recover_on_a_later_confirmed_check(data):
    snap = request(data)
    data[0].commit()
    failed, _ = service(data, status=403)
    assert (
        asyncio.run(failed.verify_language(2, request_id=snap.request_id, rating_key="10"))["state"]
        != "ready"
    )
    recovered, calls = service(data)
    assert (
        asyncio.run(recovered.verify_language(2, request_id=snap.request_id, rating_key="10"))[
            "state"
        ]
        == "ready"
    )
    before = len(calls)
    assert recovered.language_status(2, request_id=snap.request_id)["state"] == "ready"
    assert len(calls) == before
