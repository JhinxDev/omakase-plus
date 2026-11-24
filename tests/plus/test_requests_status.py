"""Shared request status model and the web Requests dashboard.

The Chouchou fixture is a synthetic projection shaped from the recorded live
evidence: Riven received the request, rejected two candidates, then reported an
acquisition failure whose exact provider reason was never supplied. No provider
is contacted by these tests.
"""

from __future__ import annotations

import pytest

from omakase.plus import for_you, movies_store, request_status
from tests.plus.test_dashboard import (
    _connect_client_db,
    _csrf_token,
    _signup_and_login,
    _user_id,
)
from tests.plus.test_dashboard import client as client
from tests.plus.test_movies import run

CHOUCHOU = "tt0328955"
CHOUCHOU_TMDB = 56780
T0 = "2026-11-10T18:00:00Z"
T1 = "2026-11-10T18:00:05Z"
T2 = "2026-11-10T18:02:00Z"
T3 = "2026-11-10T18:05:00Z"
T4 = "2026-11-10T18:09:00Z"
T5 = "2026-11-10T18:12:00Z"
FAILED_DETAIL = "Riven reported that this request failed. No automatic resubmission was sent."


@pytest.fixture(autouse=True)
def _private_app(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    yield


@pytest.fixture(autouse=True)
def _clear_observation_ledger():
    request_status.clear_observations()
    yield
    request_status.clear_observations()


class NoProvider:
    """Movie service stand-in: records reads and proves no submission happens."""

    def __init__(self):
        self.reads = []
        self.posts = []
        self.inventory = {}

    async def indexed_requests(self, user_id, imdb_ids):
        self.reads.append(("indexed_requests", tuple(imdb_ids)))
        return {
            imdb_id: self.inventory[imdb_id] for imdb_id in imdb_ids if imdb_id in self.inventory
        }

    async def refresh(self, user_id, imdb_id):
        self.reads.append(("refresh", imdb_id))
        return {"imdb_id": imdb_id, "code": "searching", "request_check": {"performed": True}}

    async def verify(self, user_id, imdb_id):
        self.reads.append(("verify", imdb_id))
        return {"imdb_id": imdb_id, "state": "ready"}


def _seed_movie(db, user_id, *, code, state, detail, created_at=T0, updated_at=T5):
    movies_store.upsert_movie(
        db,
        user_id,
        {
            "imdb_id": CHOUCHOU,
            "tmdb_id": CHOUCHOU_TMDB,
            "title": "Chouchou",
            "year": 2003,
        },
        now=created_at,
    )
    movies_store.ensure_request(db, user_id, CHOUCHOU, now=created_at, edition=None)
    movies_store.update_request(
        db, user_id, CHOUCHOU, now=updated_at, code=code, state=state, detail=detail
    )


def _record_chouchou_evidence(user_id):
    """Synthetic milestones: received, searched, two rejected candidates, failure."""
    for code, at in [
        ("requested", T1),
        ("searching", T2),
        ("search_complete", T3),
        ("candidate_rejected", "2026-11-10T18:06:00Z"),
        ("candidate_rejected", "2026-11-10T18:08:00Z"),
        ("backend_failed", T5),
    ]:
        request_status.record_observation(
            user_id, "movie", CHOUCHOU, lifecycle=T0, code=code, occurred_at=at
        )


def _stage(model, name):
    return next(entry for entry in model["stages"] if entry["stage"] == name)


def test_chouchou_fixture_reports_received_searched_and_failed_acquisition(client):
    _signup_and_login(client)
    user_id = _user_id(client)
    with _connect_client_db(client) as db:
        _seed_movie(
            db,
            user_id,
            code="backend_failed",
            state="unavailable",
            detail=FAILED_DETAIL,
        )
        db.commit()
        _record_chouchou_evidence(user_id)

        model = for_you.request_status(db, user_id, "movie", CHOUCHOU)
        assert model["supported_stages"] == ["saved", "received", "searched", "acquired", "library"]
        assert _stage(model, "saved")["state"] == "done"
        assert _stage(model, "received")["state"] == "done"
        assert _stage(model, "received")["occurred_at"] == "2026-11-10T18:00:05Z"
        assert _stage(model, "searched")["state"] == "done"
        assert _stage(model, "searched")["occurred_at"] == T3
        assert _stage(model, "acquired")["state"] == "stopped"
        assert _stage(model, "acquired")["occurred_at"] == T5
        assert _stage(model, "acquired")["label"] == "Acquisition failed"
        assert _stage(model, "library")["state"] == "unknown"
        assert model["stage"] == "acquired"
        assert model["stage_label"] == "Acquisition failed"
        assert model["stopped_at"] == "acquired"
        assert model["category"] == "attention"
        assert model["unknown_reason"] is True
        assert model["reason"] is None
        assert model["candidates_rejected"] == 2
        assert model["href"] == f"/plus/movies/{CHOUCHOU}"
        assert model["detail_href"] == f"/plus/requests/movie/{CHOUCHOU}"
        assert "exact provider reason for this stop was not supplied" in model["reason_text"]
        assert "Riven reported that this request failed" in model["reason_text"]

        detail = client.get(f"/plus/requests/movie/{CHOUCHOU}")
        assert detail.status_code == 200
        html = detail.text
        assert "Chouchou" in html
        assert 'data-stopped-at="acquired"' in html
        assert "Received by the acquisition service" in html
        assert "Searched" in html
        assert "Acquisition failed" in html
        assert "data-unknown-reason" in html
        assert "exact provider reason for this stop was not supplied" in html
        assert "rejected 2 candidates" in html
        assert T5 in html

        dashboard = client.get("/plus/requests")
        assert dashboard.status_code == 200
        assert 'data-request-category="active"' in dashboard.text
        assert 'data-request-category="attention"' in dashboard.text
        assert 'data-request-category="history"' in dashboard.text
        assert "Chouchou" in dashboard.text
        assert f'href="/plus/requests/movie/{CHOUCHOU}"' in dashboard.text
        assert "Needs attention" in dashboard.text
        assert "Last checked" in dashboard.text
        assert T5 in dashboard.text


def test_verified_library_match_resolves_a_stale_pending_request(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    fake = NoProvider()
    monkeypatch.setattr(for_you, "MovieService", lambda db, _connections: fake)
    with _connect_client_db(client) as db:
        _seed_movie(
            db,
            user_id,
            code="requested",
            state="requested",
            detail="Requested; delivery has not been verified.",
            updated_at=T0,
        )
        db.commit()
        pending = for_you.request_status(db, user_id, "movie", CHOUCHOU)
        assert pending["state"] == "requested" and pending["category"] == "active"

        fake.inventory[CHOUCHOU] = ("f" * 64, T4)
        page = run(for_you.dashboard(db, user_id))
        history = [model for model in page["categories"] if model["key"] == "history"][0]
        resolved = next(model for model in history["requests"] if model["title_id"] == CHOUCHOU)
        assert resolved["state"] == "in_library"
        assert resolved["resolved"] is True
        assert resolved["category"] == "history"
        assert _stage(resolved, "library")["state"] == "done"
        assert _stage(resolved, "library")["occurred_at"] == T4
        assert resolved["last_confirmed_at"] == T4
        active = [model for model in page["categories"] if model["key"] == "active"][0]
        assert CHOUCHOU not in {model["title_id"] for model in active["requests"]}

    dashboard = client.get("/plus/requests")
    assert "In your library" in dashboard.text
    assert 'data-request-category="history"' in dashboard.text


def test_indexed_movie_shows_receipt_without_inventing_a_search(client):
    _signup_and_login(client)
    user_id = _user_id(client)
    with _connect_client_db(client) as db:
        _seed_movie(db, user_id, code="requested", state="requested", detail="Indexed")
        saved = for_you.request_status(db, user_id, "movie", CHOUCHOU)
        assert saved["status_label"] == "Request saved"
        assert _stage(saved, "received")["state"] == "pending"

        movies_store.update_request(db, user_id, CHOUCHOU, now=T1, backend_id="verified-indexed-id")
        received = for_you.request_status(db, user_id, "movie", CHOUCHOU)
        assert received["status_label"] == "Received by Riven"
        assert _stage(received, "received")["state"] == "done"
        assert _stage(received, "searched")["state"] == "pending"
        assert _stage(received, "acquired")["state"] == "pending"
        assert received["timeline"][-1]["occurred_at"] == T1


def test_confirmed_removal_lands_in_history_and_not_in_active(client):
    _signup_and_login(client)
    user_id = _user_id(client)
    with _connect_client_db(client) as db:
        _seed_movie(
            db,
            user_id,
            code="copy_removed",
            state="unavailable",
            detail=movies_store.COPY_REMOVED_DETAIL,
        )
        db.commit()
        page = run(for_you.dashboard(db, user_id))
        history = [entry for entry in page["categories"] if entry["key"] == "history"][0]
        model = next(item for item in history["requests"] if item["title_id"] == CHOUCHOU)
        assert model["state"] == "removed"
        assert model["removed"] is True
        assert model["resolved"] is True
        assert model["category"] == "history"
        assert model["next_action"] is None
        assert model["can_check"] is False and model["can_retry"] is False

        page_html = client.get("/plus/requests")
        assert page_html.status_code == 200
        assert "Removed" in page_html.text
        assert movies_store.COPY_REMOVED_DETAIL in page_html.text
        # A removal is history only: it is never offered as an active request.
        assert 'data-request-row="movie:' + CHOUCHOU + '"' in page_html.text
        active = page_html.text.split('data-request-category="active"', 1)[1]
        active = active.split("</section>", 1)[0]
        assert "Chouchou" not in active
        detail = client.get(f"/plus/requests/movie/{CHOUCHOU}")
        assert detail.status_code == 200
        assert "Removed" in detail.text
        assert f'data-request-refresh="/plus/movies/{CHOUCHOU}/request-status"' not in detail.text


def test_check_status_reads_only_and_retry_cannot_duplicate(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    fake = NoProvider()
    monkeypatch.setattr(for_you, "MovieService", lambda db, _connections: fake)
    with _connect_client_db(client) as db:
        _seed_movie(
            db,
            user_id,
            code="backend_failed",
            state="unavailable",
            detail=FAILED_DETAIL,
        )
        db.commit()
        before = movies_store.get_request(db, user_id, CHOUCHOU)
        token = _csrf_token(client)

        blocked = client.post(f"/plus/requests/movie/{CHOUCHOU}/check", data={})
        assert blocked.status_code == 403
        assert fake.reads == []

        checked = client.post(
            f"/plus/requests/movie/{CHOUCHOU}/check",
            data={"csrf_token": token},
            headers={"accept": "application/json"},
        )
        assert checked.status_code == 200
        assert checked.json()["identity"] == f"movie:{CHOUCHOU}"
        assert fake.posts == []
        noticed = client.get(f"/plus/requests/movie/{CHOUCHOU}?checked=1")
        assert noticed.status_code == 200
        assert "Status refreshed." in noticed.text
        assert (
            f'data-request-identity="movie:{CHOUCHOU}" data-request-refresh="/plus/movies/{CHOUCHOU}/request-status"'
            in noticed.text
        )
        with _connect_client_db(client) as db:
            after = movies_store.get_request(db, user_id, CHOUCHOU)
            assert after["code"] == before["code"]
            assert db.execute("SELECT count(*) FROM movie_requests").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM movie_submissions").fetchone()[0] == 0

        first = client.post(
            f"/plus/requests/movie/{CHOUCHOU}/retry",
            data={"csrf_token": token},
            headers={"accept": "application/json"},
        )
        assert first.status_code == 200
        second = client.post(
            f"/plus/requests/movie/{CHOUCHOU}/retry",
            data={"csrf_token": token},
            headers={"accept": "application/json"},
        )
        assert second.status_code == 200
        assert first.json()["identity"] == second.json()["identity"] == f"movie:{CHOUCHOU}"
        assert fake.posts == []
        with _connect_client_db(client) as db:
            assert db.execute("SELECT count(*) FROM movie_requests").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM movie_submissions").fetchone()[0] == 0
            request = movies_store.get_request(db, user_id, CHOUCHOU)
            assert request is not None


@pytest.mark.parametrize(
    ("provider_code", "performed"),
    [
        ("no_eligible_results", True),
        ("no_eligible_results", False),
        ("backend_failed", False),
        ("searching", True),
        ("acquiring", True),
    ],
)
def test_request_checks_riven_before_searching_configured_sources(
    client, monkeypatch, provider_code, performed
):
    from types import SimpleNamespace

    from omakase.plus import catalog_config
    from tests.plus.test_request_candidates import Candidate

    _signup_and_login(client)
    user_id = _user_id(client)
    events = []

    class RivenStatus(NoProvider):
        async def refresh(self, user_id, imdb_id):
            events.append(("riven", imdb_id))
            return {"code": provider_code, "request_check": {"performed": performed}}

    class Fallback:
        async def search(self, query):
            events.append(("indexer", query))
            return [Candidate("Chouchou 2003 1080p"), Candidate("Chouchou 1998 1080p")]

    monkeypatch.setenv("OMAKASE_REQUEST_SEARCH", "1")
    monkeypatch.setattr(for_you, "MovieService", lambda *_: RivenStatus())
    monkeypatch.setattr(
        catalog_config,
        "configured_catalogs",
        lambda *a, **k: SimpleNamespace(sources=(Fallback(),)),
    )
    with _connect_client_db(client) as db:
        _seed_movie(db, user_id, code="requested", state="requested", detail="Request saved")
        db.commit()

    url = f"/plus/requests/movie/{CHOUCHOU}"
    opened = client.get(url)
    assert opened.status_code == 200
    assert not events
    result = client.post(
        url + "/check",
        data={"csrf_token": _csrf_token(client)},
        follow_redirects=False,
    )
    assert result.status_code == 303
    expected = [("riven", CHOUCHOU)]
    if provider_code in {"no_eligible_results", "backend_failed"}:
        expected.append(("indexer", "Chouchou"))
    assert events == expected
    checked = client.get(result.headers["location"])
    assert ("the release year does not match the title" in checked.text) == (len(expected) == 2)
    assert events == expected


def test_saved_fallback_evidence_is_available_in_shared_request_status(client, monkeypatch):
    from omakase.plus import movies_routes, native_tv_test, request_candidates
    from tests.plus.test_request_candidates import Candidate

    monkeypatch.setenv("OMAKASE_REQUEST_SEARCH", "1")
    monkeypatch.setattr(movies_routes, "monotonic", lambda: 100.0)
    _signup_and_login(client)
    user_id = _user_id(client)
    monkeypatch.setattr(for_you, "MovieService", lambda *_: NoProvider())
    with _connect_client_db(client) as db:
        _seed_movie(db, user_id, code="backend_failed", state="unavailable", detail=FAILED_DETAIL)
        request_candidates.record(
            db,
            user_id + 1000,
            "movie",
            CHOUCHOU,
            [
                (
                    Candidate("Other owner's release"),
                    request_candidates.Verdict(True, "accepted", ""),
                )
            ],
        )
        request_candidates.record(
            db,
            user_id,
            "movie",
            CHOUCHOU,
            [(Candidate("Fixture release"), request_candidates.Verdict(True, "accepted", ""))],
            marker=request_candidates.PARTIAL_CODE,
        )
        status = run(for_you.dashboard(db, user_id))

    expected = {
        "outcome": "partial",
        "found": 1,
        "eligible": 1,
    }
    model = next(item for group in status["categories"] for item in group["requests"])
    native = native_tv_test.native_request_status(model)["fallback_search"]
    assert {key: native[key] for key in expected} == expected
    assert native["checked_at"].endswith("Z")
    assert "Fixture release" not in str(native)
    web = client.get("/plus/requests/status")
    assert web.status_code == 200
    model = next(item for group in web.json()["categories"] for item in group["requests"])
    assert model["fallback_search"] == native


def test_terminal_riven_check_can_advance_an_eligible_cloud_fallback(client, monkeypatch):
    from types import SimpleNamespace

    from omakase.plus import catalog_config, movie_fallback
    from tests.plus.test_request_candidates import Candidate

    _signup_and_login(client)
    user_id = _user_id(client)
    events = []

    class RivenStatus(NoProvider):
        async def refresh(self, user_id, imdb_id):
            events.append("riven")
            return {"code": "backend_failed"}

    class Fallback:
        async def search(self, query):
            events.append("configured sources")
            return [Candidate("Chouchou 2003 1080p"), Candidate("Chouchou 1998 1080p")]

    async def advance(db, owner, title_id, candidates=()):
        assert (owner, title_id) == (user_id, CHOUCHOU)
        if candidates:
            events.extend(item.title for item in candidates)

    async def library(*args, **kwargs):
        return {}

    monkeypatch.setenv("OMAKASE_REQUEST_SEARCH", "1")
    monkeypatch.setenv("OMAKASE_REQUEST_ACQUIRE", "1")
    monkeypatch.setattr(for_you, "MovieService", lambda *_: RivenStatus())
    monkeypatch.setattr(for_you, "_movie_library", library)
    monkeypatch.setattr(
        for_you.playback_providers, "select", lambda *_: SimpleNamespace(cloud_route=True)
    )
    monkeypatch.setattr(movie_fallback, "advance", advance)
    monkeypatch.setattr(
        catalog_config,
        "configured_catalogs",
        lambda *a, **k: SimpleNamespace(sources=(Fallback(),)),
    )
    with _connect_client_db(client) as db:
        _seed_movie(db, user_id, code="backend_failed", state="unavailable", detail=FAILED_DETAIL)
        status = run(for_you.refresh_request(db, user_id, "movie", CHOUCHOU))

    assert events == ["riven", "configured sources", "Chouchou 2003 1080p"]
    assert status["fallback_search"]["eligible"] == 1


def test_automatic_refresh_is_bounded_per_viewer(client, monkeypatch):
    from omakase.plus import movies_routes

    _signup_and_login(client)
    user_id = _user_id(client)
    fake = NoProvider()
    monkeypatch.setattr(for_you, "MovieService", lambda db, _connections: fake)
    clock = [500.0]
    monkeypatch.setattr(movies_routes, "monotonic", lambda: clock[0])
    with _connect_client_db(client) as db:
        _seed_movie(
            db,
            user_id,
            code="requested",
            state="requested",
            detail="Requested; delivery has not been verified.",
            updated_at=T0,
        )
        db.commit()
        page = client.get("/plus/requests")
        assert page.status_code == 200
        assert 'data-requests-status="/plus/requests/status"' in page.text
        assert "/plus/static/requests.js" in page.text
        after_page = len(fake.reads)
        assert after_page == 1

        first = client.get("/plus/requests/status")
        assert first.status_code == 200
        assert first.headers["cache-control"] == "private, no-store"
        assert sorted(first.json()["counts"]) == ["active", "attention", "history"]
        assert first.json() == client.get("/plus/requests/status").json()
        assert len(fake.reads) == after_page + 1

        clock[0] += 60
        again = client.get("/plus/requests/status")
        assert again.status_code == 200
        assert len(fake.reads) == after_page + 2


def test_tv_requests_stay_manual_and_resolve_on_a_verified_copy(client):
    _signup_and_login(client)
    user_id = _user_id(client)
    with _connect_client_db(client) as db:
        db.execute(
            "INSERT INTO for_you_requests (user_id,kind,title_id,title) VALUES (?,'tv',?,?)",
            (user_id, 123, "A manual show"),
        )
        db.commit()
        manual = for_you.request_status(db, user_id, "tv", 123)
        assert manual["manual"] is True
        assert manual["supported_stages"] == ["saved", "library"]
        assert [entry["stage"] for entry in manual["stages"]] == ["saved", "library"]
        assert manual["detail"] == "Saved for review."
        assert manual["manual_note"] is None
        assert manual["next_action"] is None
        assert manual["can_retry"] is False
        assert manual["category"] == "active"

        db.execute(
            "INSERT INTO tv_catalog (user_id,tmdb_id,metadata_json,updated_at) VALUES (?,?,?,?)",
            (user_id, 123, "{}", T1),
        )
        db.execute(
            """INSERT INTO tv_plex_snapshots
               (user_id,tmdb_id,source_scope,plex_key,complete,observed_at)
               VALUES (?,?,?,?,1,?)""",
            (user_id, 123, "s" * 64, "12345", T4),
        )
        db.commit()
        resolved = for_you.request_status(db, user_id, "tv", 123)
        assert resolved["manual"] is True and resolved["manual_note"] is None
        assert resolved["state"] == "in_library"
        assert resolved["resolved"] is True
        assert resolved["category"] == "history"
        assert _stage(resolved, "library")["occurred_at"] == T4


def test_anime_never_shows_a_stage_its_workflow_cannot_perform(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    monkeypatch.setattr(for_you, "anime_episodes", lambda *args: [])
    monkeypatch.setattr(for_you, "_candidates", lambda *args: [])
    with _connect_client_db(client) as db:
        from omakase.plus.media_store import upsert_monitor

        db.execute(
            "INSERT INTO for_you_requests (user_id,kind,title_id,title) VALUES (?,'anime',?,?)",
            (user_id, 81001, "A sourced anime"),
        )
        upsert_monitor(
            db,
            user_id=user_id,
            anilist_id=81001,
            title="A sourced anime",
            search_titles=("A sourced anime",),
            genres=(),
            anilist_planning_id=None,
            format="TV",
            release_status="FINISHED",
            season_number=1,
            expected_episodes=12,
            aired_episodes=12,
            next_airing_at=None,
            active=True,
        )
        db.commit()
        model = for_you.request_status(db, user_id, "anime", 81001)
        assert model["supported_stages"] == ["saved", "searched", "acquired", "library"]
        assert "received" not in [entry["stage"] for entry in model["stages"]]
        assert all(entry["state"] != "done" for entry in model["stages"][1:])
        assert model["can_retry"] is False


def test_status_projection_validates_identity_and_bounds_inputs():
    assert request_status.identity("movie", "tt0328955") == "movie:tt0328955"
    assert request_status.normalize_time("2026-11-10 18:12:00") == "2026-11-10T18:12:00Z"
    assert request_status.normalize_time("not a time") is None
    with pytest.raises(request_status.RequestStatusError):
        request_status.identity("movie", "../../etc/passwd")
    with pytest.raises(request_status.RequestStatusError):
        request_status.record_observation(
            1, "movie", "tt0328955", lifecycle=T0, code="invented_reason", occurred_at=T1
        )
    request_status.record_observation(
        1, "movie", "tt0328955", lifecycle=T0, code="searching", occurred_at=T1
    )
    request_status.record_observation(
        1, "movie", "tt0328955", lifecycle=T0, code="searching", occurred_at=T2
    )
    assert request_status.observations(1, "movie", "tt0328955", lifecycle=T0) == (
        ("searching", T2),
    )
    assert request_status.observations(1, "movie", "tt0328955", lifecycle="other") == ()


def test_dashboard_rejects_a_request_owned_by_another_viewer(client):
    _signup_and_login(client)
    user_id = _user_id(client)
    with _connect_client_db(client) as db:
        _seed_movie(
            db,
            user_id,
            code="backend_failed",
            state="unavailable",
            detail=FAILED_DETAIL,
        )
        other = db.execute(
            "INSERT INTO users(email,password_hash) VALUES('other@example.test','test')"
        ).lastrowid
        db.commit()
        assert for_you.request_status(db, other, "movie", CHOUCHOU) is None
    assert client.get("/plus/requests/movie/tt0000001").status_code == 404


def test_requests_dashboard_renders_progress_bar_clean_copy_and_diagnostics_disclosure(
    client, monkeypatch
):
    _signup_and_login(client)
    user_id = _user_id(client)
    monkeypatch.setenv("OMAKASE_REQUEST_SEARCH", "1")
    monkeypatch.setenv("OMAKASE_TV_REQUEST_RIVEN", "1")
    with _connect_client_db(client) as db:
        _seed_movie(
            db,
            user_id,
            code="requested",
            state="requested",
            detail="Requested; delivery has not been verified.",
        )
        db.execute(
            "UPDATE movie_requests SET backend_id='verified-riven-id' WHERE user_id=? AND imdb_id=?",
            (user_id, CHOUCHOU),
        )
        db.execute(
            "INSERT INTO for_you_requests (user_id,kind,title_id,title) VALUES (?,'tv',909,'Ray Fixture')",
            (user_id,),
        )
        from omakase.plus import tv_provider_requests

        tv_provider_requests.remember(db, user_id, 909, [1])
        tv_provider_requests._write_snapshot(
            db,
            user_id,
            909,
            {
                "status": "Some episodes sent to the library",
                "expected": 10,
                "library": {
                    "provider": "Real-Debrid",
                    "binding": "b" * 64,
                    "episodes": [[1, 1], [1, 2]],
                },
                "seasons": [
                    {
                        "season": 1,
                        "episodes": [
                            {"episode": n, "status": "Sent to the library"} for n in range(1, 11)
                        ],
                    }
                ],
            },
        )
        db.execute(
            "UPDATE tv_provider_requests SET snapshot_json=json_set(snapshot_json,'$.library.binding',?) WHERE user_id=? AND title_id=909",
            ("", user_id),
        )
        db.commit()

    page = client.get("/plus/requests")
    assert page.status_code == 200
    assert "Received by Riven" not in page.text
    assert "This page cannot say what exists yet" not in page.text
    assert "10 episodes" in page.text or "episodes" in page.text

    detail = client.get(f"/plus/requests/movie/{CHOUCHOU}")
    assert detail.status_code == 200
    assert "requests-diagnostics" in detail.text
    assert "Source &amp; Release Diagnostics" in detail.text
