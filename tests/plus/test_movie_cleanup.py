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
            lambda key, timeout: SimpleNamespace(get_torrent=get_torrent),
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
