"""Exact movie cleanup proofs only use the viewer's connected copies."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
from xml.etree import ElementTree

import httpx
import pytest

from omakase.plus import movie_cleanup, realdebrid
from omakase.plus.db import run_migrations
from omakase.plus.movies import MovieService
from omakase.plus.movies_store import claim_submission, upsert_movie
from omakase.plus.realdebrid import RDFile, RealDebridClient

IMDB = "tt0123456"
TMDB = 9876
TICKET = "fixture012"
TORRENT_HASH = "a" * 40
PUBLICATION = ["Sample Film (2024) {imdb-tt0123456}", "Published.mkv"]
ACQUISITION_TAG = "c" * 32


@pytest.fixture
def copy(monkeypatch):
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        run_migrations(db)
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(1,'a@example.test','a'),"
            "(2,'b@example.test','b')"
        )
        upsert_movie(
            db,
            1,
            {"imdb_id": IMDB, "tmdb_id": TMDB, "title": "Sample Film"},
            now="2026-11-09T08:00:00Z",
        )
        binding = {
            "schema": "riven-movie-removal-binding-v1",
            "imdb_id": IMDB,
            "tmdb_id": TMDB,
            "riven_id": "movie_tmdb_9876",
            "torrent_id": TICKET,
            "torrent_hash": TORRENT_HASH,
            "file_name": "Original.mkv",
            "publication": list(PUBLICATION),
        }
        video = ElementTree.fromstring(
            '<Video type="movie" librarySectionID="3" ratingKey="7">'
            '<Guid id="imdb://tt0123456"/><Guid id="tmdb://9876"/>'
            '<Media><Part file="/media/Published.mkv" size="144"/></Media></Video>'
        )
        video.find(".//Part").set("file", "/media/movies/" + "/".join(PUBLICATION))
        responses = {
            "/:/prefs": ElementTree.fromstring(
                '<MediaContainer><Setting id="autoEmptyTrash" value="1"/></MediaContainer>'
            ),
            "/status/sessions": ElementTree.Element("MediaContainer"),
        }
        service = SimpleNamespace(
            db=db,
            _scope="d" * 64,
            _guard=lambda user_id: None,
            _json=AsyncMock(return_value=binding),
            _plex_movie=AsyncMock(return_value=(video, 3)),
            _guids=MovieService._guids,
            _xml=AsyncMock(side_effect=responses.__getitem__),
            responses=responses,
        )
        get_torrent = AsyncMock(
            return_value=SimpleNamespace(
                id=TICKET,
                torrent_hash=TORRENT_HASH.upper(),
                status="downloaded",
                files=[RDFile(id=1, path="/rd/Original.mkv", bytes=144, selected=True)],
            )
        )
        monkeypatch.setattr(movie_cleanup, "read_secret", lambda db, uid, key: "fixture-key")
        monkeypatch.setattr(
            movie_cleanup,
            "RealDebridClient",
            lambda key, timeout, **kwargs: SimpleNamespace(get_torrent=get_torrent),
        )
        yield service, binding, video, get_torrent
    db.close()


def test_movie_cleanup_proof_matches_a_single_connected_copy(copy):
    service, _, _, rd = copy
    proof = asyncio.run(movie_cleanup.verify_movie_cleanup_copy(service, 1, IMDB))
    assert proof.torrent_id == TICKET
    assert proof.publication == tuple(PUBLICATION)
    assert (proof.plex_key, proof.plex_section, proof.file_size) == ("7", 3, 144)
    assert proof.owned is False
    service._json.assert_awaited_once_with("riven", "/api/v1/items/movie-removal-binding/" + IMDB)
    service._xml.assert_has_awaits([call("/:/prefs"), call("/status/sessions")])
    rd.assert_awaited_once_with(TICKET, require_definite_absence=True)


@pytest.mark.parametrize(
    "owner, native_tag, shared, owned",
    [
        (1, ACQUISITION_TAG, False, True),
        (2, ACQUISITION_TAG, False, False),
        (1, "f" * 32, False, False),
        (1, None, False, False),
        (1, ACQUISITION_TAG, True, False),
    ],
)
def test_cleanup_ownership_needs_the_native_acquisition_for_this_user(
    copy, owner, native_tag, shared, owned
):
    service, binding, _, _ = copy
    binding["acquisition_tag"] = native_tag
    now = "2026-11-09T08:00:00Z"
    assert claim_submission(
        service.db, service._scope, IMDB, TMDB, user_id=owner, cleanup_tag=ACQUISITION_TAG, now=now
    )
    if shared:
        upsert_movie(
            service.db, 2, {"imdb_id": IMDB, "tmdb_id": TMDB, "title": "Sample Film"}, now=now
        )
        service.db.execute(
            """INSERT INTO movie_requests (user_id, imdb_id, backend_id, detail, created_at, updated_at)
               VALUES (2, ?, ?, 'Fixture', ?, ?)""",
            (IMDB, binding["riven_id"], now, now),
        )
    proof = asyncio.run(movie_cleanup.verify_movie_cleanup_copy(service, 1, IMDB))
    assert proof.owned is owned


def test_movie_cleanup_rejects_another_viewers_movie(copy):
    service, _, _, _ = copy
    with pytest.raises(movie_cleanup.MovieCleanupUnavailable, match="movie_identity_unverified"):
        asyncio.run(movie_cleanup.verify_movie_cleanup_copy(service, 2, IMDB))
    service._json.assert_not_awaited()


def test_movie_cleanup_honors_the_household_guard(copy, monkeypatch):
    service, _, _, _ = copy
    monkeypatch.setattr(
        movie_cleanup,
        "managed_removal_guard",
        lambda *args, **kwargs: {"blocked": False, "has_shared_references": True},
    )
    with pytest.raises(movie_cleanup.MovieCleanupUnavailable, match="movie_in_shared_library"):
        asyncio.run(movie_cleanup.verify_movie_cleanup_copy(service, 1, IMDB))
    service._json.assert_not_awaited()


@pytest.mark.parametrize(
    "change,reason",
    [
        ("wrong_riven_tmdb", "riven_binding_unverified"),
        ("wrong_publication", "plex_binding_unverified"),
        ("unsafe_scan", "plex_scan_unverified"),
        ("playing", "movie_is_playing"),
        ("wrong_torrent_hash", "real_debrid_binding_unverified"),
        ("wrong_size", "real_debrid_binding_unverified"),
    ],
)
def test_movie_cleanup_refuses_unmatched_or_playing_copy(copy, change, reason):
    service, binding, video, rd = copy
    if change == "wrong_riven_tmdb":
        binding["tmdb_id"] += 1
    elif change == "wrong_publication":
        video.find(".//Part").set("file", "/other/Published.mkv")
    elif change == "unsafe_scan":
        service.responses["/:/prefs"] = ElementTree.Element("MediaContainer")
    elif change == "playing":
        service.responses["/status/sessions"] = ElementTree.fromstring(
            '<MediaContainer><Video ratingKey="7"/></MediaContainer>'
        )
    elif change == "wrong_torrent_hash":
        rd.return_value.torrent_hash = "b" * 40
    else:
        video.find(".//Part").set("size", "145")
    with pytest.raises(movie_cleanup.MovieCleanupUnavailable, match=reason):
        asyncio.run(movie_cleanup.verify_movie_cleanup_copy(service, 1, IMDB))


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_definite_real_debrid_absence_requires_an_exact_404(monkeypatch, status):
    create = httpx.AsyncClient

    def response(request):
        assert request.method == "GET"
        assert request.url.path.endswith("/torrents/info/" + TICKET)
        return httpx.Response(status)

    monkeypatch.setattr(
        realdebrid.httpx,
        "AsyncClient",
        lambda **kwargs: create(transport=httpx.MockTransport(response), **kwargs),
    )
    client = RealDebridClient("fixture-key")
    if status == 404:
        assert asyncio.run(client.get_torrent(TICKET, require_definite_absence=True)) is None
    else:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(client.get_torrent(TICKET, require_definite_absence=True))


@pytest.fixture
def cleanup_job(copy, monkeypatch):
    from omakase.plus import movie_cleanup_worker
    from omakase.plus.movie_cleanup_worker import process_movie_cleanup_jobs
    from omakase.plus.movies import MovieConnections, MovieError

    service, binding, _, get_torrent = copy
    db = service.db
    monkeypatch.setenv("OMAKASE_PLUS_MOVIE_CLEANUP_ENABLED", "1")
    monkeypatch.setattr(movie_cleanup_worker, "read_secret", movie_cleanup.read_secret)
    binding["acquisition_tag"] = ACQUISITION_TAG
    now = "2026-11-09T08:00:00Z"
    assert claim_submission(
        db, service._scope, IMDB, TMDB, user_id=1, cleanup_tag=ACQUISITION_TAG, now=now
    )
    movie_cleanup.set_movie_cleanup_preference(db, 1, True)
    db.execute(
        """INSERT INTO movie_history
           (user_id, imdb_id, tmdb_id, status, rating, status_source, rating_source,
            created_at, updated_at)
           VALUES (1, ?, ?, 'completed', 8, 'manual', 'manual', ?, ?)""",
        (IMDB, TMDB, now, now),
    )
    history = db.execute("SELECT * FROM movie_history").fetchone()
    movie_cleanup.record_movie_cleanup_intent(
        db,
        user_id=1,
        imdb_id=IMDB,
        backend_scope=service._scope,
        before=None,
        after=history,
        now=now,
    )
    db.commit()
    service.connections = MovieConnections()
    state = SimpleNamespace(
        withdrawal="present",
        plex_present=True,
        lose_riven_response=False,
        interrupt_riven_withdrawal=False,
        lose_rd_response=False,
        receipt_overrides={},
        events=[],
    )
    path = "/api/v1/items/movie-removal-binding/" + IMDB

    def job():
        return db.execute("SELECT * FROM movie_cleanup_jobs").fetchone()

    def riven_json(provider, requested, *, params=None):
        assert provider == "riven"
        if requested == path:
            assert params is None
            return binding
        assert requested == path + "/withdrawal"
        assert params == {
            "omakase_acquisition_tag": ACQUISITION_TAG,
            "riven_id": binding["riven_id"],
            "tmdb_id": TMDB,
            "torrent_id": TICKET,
            "torrent_hash": TORRENT_HASH,
        }
        return {
            "schema": "riven-movie-withdrawal-v1",
            "acquisition_tag": ACQUISITION_TAG,
            "riven_id": binding["riven_id"],
            "tmdb_id": TMDB,
            "torrent_id": TICKET,
            "torrent_hash": TORRENT_HASH,
            "state": state.withdrawal,
            **state.receipt_overrides,
        }

    def riven_delete(provider, method, requested, *, params):
        assert (provider, method, requested) == ("riven", "DELETE", path)
        riven_json(provider, path + "/withdrawal", params=params)
        assert job()["stage"] == "withdrawal_intent"
        state.events.append("riven_delete")
        if state.interrupt_riven_withdrawal:
            state.interrupt_riven_withdrawal = False
            state.withdrawal = "withdrawing"
            state.plex_present = False
            raise MovieError("transport_timeout")
        state.withdrawal = "withdrawn"
        if state.lose_riven_response:
            raise MovieError("transport_timeout")

    def plex_xml(requested):
        if requested == "/identity":
            return ElementTree.fromstring('<MediaContainer machineIdentifier="fixture-machine"/>')
        if requested == "/library/metadata/7":
            state.events.append("plex_check")
            if state.plex_present:
                return ElementTree.Element("MediaContainer")
            raise MovieError("backend_unavailable", http_status=404)
        return service.responses[requested]

    def rd_delete(torrent_id, *, require_exact_id):
        assert torrent_id == TICKET and require_exact_id
        assert job()["stage"] == "provider_intent"
        state.events.append("rd_delete")
        get_torrent.return_value = None
        if state.lose_rd_response:
            raise httpx.ReadTimeout("Lost response after provider committed")
        return True

    service._json.side_effect = riven_json
    service._bytes = AsyncMock(side_effect=riven_delete)
    service._xml.side_effect = plex_xml
    service._sections = AsyncMock(return_value=(3, None))
    service._videos = AsyncMock(return_value=[])
    state.rd_delete = AsyncMock(side_effect=rd_delete)
    monkeypatch.setattr(
        movie_cleanup,
        "RealDebridClient",
        lambda key, timeout, **kwargs: SimpleNamespace(
            get_torrent=get_torrent, delete_torrent=state.rd_delete
        ),
    )

    def tick():
        # Move the scheduler past its retry delay without sleeping or contacting services.
        db.execute("UPDATE movie_cleanup_jobs SET retry_at=NULL")
        db.commit()
        asyncio.run(
            process_movie_cleanup_jobs(
                db, heartbeat=lambda: None, service_factory=lambda *_: service
            )
        )
        return job()

    return service, state, tick


def test_worker_waits_for_plex_and_verifies_both_provider_removals(cleanup_job):
    service, state, tick = cleanup_job
    assert tick()["stage"] == "withdrawal_intent"
    assert tick()["stage"] == "awaiting_plex"
    job = tick()
    assert (job["stage"], job["reason"]) == ("awaiting_plex", "waiting_for_plex")
    state.rd_delete.assert_not_awaited()
    state.plex_present = False
    assert tick()["stage"] == "provider_intent"
    assert tick()["stage"] == "complete"
    assert tick()["stage"] == "complete"
    assert state.events == ["riven_delete", "plex_check", "plex_check", "plex_check", "rd_delete"]
    assert tuple(service.db.execute("SELECT status, rating FROM movie_history").fetchone()) == (
        "completed",
        8,
    )


def test_worker_recovers_after_lost_delete_responses(cleanup_job):
    _, state, tick = cleanup_job
    state.lose_riven_response = state.lose_rd_response = True
    assert tick()["stage"] == "withdrawal_intent"
    assert tick()["stage"] == "withdrawal_intent"
    assert tick()["stage"] == "awaiting_plex"
    state.plex_present = False
    assert tick()["stage"] == "provider_intent"
    assert tick()["stage"] == "provider_intent"
    assert tick()["stage"] == "complete"
    assert [event for event in state.events if event.endswith("_delete")] == [
        "riven_delete",
        "rd_delete",
    ]


def test_worker_resumes_a_native_intent_when_the_plex_link_is_already_unpublished(cleanup_job):
    service, state, tick = cleanup_job
    assert tick()["stage"] == "withdrawal_intent"
    state.interrupt_riven_withdrawal = True
    assert tick()["stage"] == "withdrawal_intent"
    assert state.withdrawal == "withdrawing"
    service._plex_movie.side_effect = AssertionError("The original copy is no longer indexed")
    assert tick()["stage"] == "awaiting_plex"
    assert tick()["stage"] == "provider_intent"
    assert tick()["stage"] == "complete"
    assert state.events.count("riven_delete") == 2
    assert state.events.count("rd_delete") == 1


def test_worker_protects_a_new_plex_copy_after_partial_native_withdrawal(cleanup_job):
    service, state, tick = cleanup_job
    assert tick()["stage"] == "withdrawal_intent"
    state.interrupt_riven_withdrawal = True
    assert tick()["stage"] == "withdrawal_intent"
    service._videos.return_value = [
        ElementTree.fromstring('<Video ratingKey="9"><Guid id="imdb://tt0123456"/></Video>')
    ]
    job = tick()
    assert (job["stage"], job["reason"]) == ("withdrawal_intent", "waiting_for_plex")
    service._bytes.assert_awaited_once()
    state.rd_delete.assert_not_awaited()


def test_worker_waits_for_an_active_plex_session_after_partial_native_withdrawal(cleanup_job):
    service, state, tick = cleanup_job
    assert tick()["stage"] == "withdrawal_intent"
    state.interrupt_riven_withdrawal = True
    assert tick()["stage"] == "withdrawal_intent"
    service.responses["/status/sessions"] = ElementTree.fromstring(
        '<MediaContainer><Video ratingKey="7"/></MediaContainer>'
    )
    job = tick()
    assert (job["stage"], job["reason"]) == ("withdrawal_intent", "movie_is_playing")
    service._bytes.assert_awaited_once()
    state.rd_delete.assert_not_awaited()


def test_worker_refuses_withdrawal_receipts_for_a_different_copy(cleanup_job):
    service, state, tick = cleanup_job
    assert tick()["stage"] == "withdrawal_intent"
    state.receipt_overrides["torrent_id"] = "anothercopy"
    job = tick()
    assert (job["stage"], job["reason"]) == ("blocked", "guarded_removal_receipt_invalid")
    service._bytes.assert_not_awaited()
    state.rd_delete.assert_not_awaited()


def test_worker_stops_between_providers_when_viewer_turns_cleanup_off(cleanup_job):
    service, state, tick = cleanup_job
    assert tick()["stage"] == "withdrawal_intent"
    assert tick()["stage"] == "awaiting_plex"
    state.plex_present = False
    assert tick()["stage"] == "provider_intent"
    with service.db:
        movie_cleanup.set_movie_cleanup_preference(service.db, 1, False)
    assert tick()["state"] == "cancelled"
    state.rd_delete.assert_not_awaited()


def test_worker_refuses_new_plex_copy_even_when_old_key_is_gone(cleanup_job):
    service, state, tick = cleanup_job
    assert tick()["stage"] == "withdrawal_intent"
    assert tick()["stage"] == "awaiting_plex"
    state.plex_present = False
    service._videos.return_value = [
        ElementTree.fromstring('<Video ratingKey="8"><Guid id="tmdb://9876"/></Video>')
    ]
    job = tick()
    assert (job["stage"], job["reason"]) == ("awaiting_plex", "waiting_for_plex")
    state.rd_delete.assert_not_awaited()


def test_worker_rechecks_connection_before_withdrawing(cleanup_job):
    from dataclasses import replace

    service, state, tick = cleanup_job
    assert tick()["stage"] == "withdrawal_intent"
    service.connections = replace(service.connections, plex_machine_id="changed")
    job = tick()
    assert (job["stage"], job["reason"]) == ("blocked", "connections_or_identity_changed")
    service._bytes.assert_not_awaited()
    state.rd_delete.assert_not_awaited()


def test_worker_cannot_mutate_without_guarded_riven_endpoint(cleanup_job):
    from omakase.plus.movies import MovieError

    service, state, tick = cleanup_job
    assert tick()["stage"] == "withdrawal_intent"
    service._json.side_effect = MovieError("backend_unavailable", http_status=404)
    job = tick()
    assert (job["stage"], job["reason"]) == ("blocked", "guarded_removal_unavailable")
    service._bytes.assert_not_awaited()
    state.rd_delete.assert_not_awaited()
