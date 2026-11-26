"""An iPhone or Apple TV that keeps being used must stay paired.

These tests pin the server half of "the app keeps asking me to connect again":
a device credential slides forward with use, the approving browser session is kept
alive by that use, an expired credential is never revived, the phone registers as
an iPhone, and request posters reach the paired client only as fetchable targets.

The `system` fixture is imported from the module that defines it so pytest can
resolve it here; Ruff sees that as an unused redefinition, hence the file-level noqa.
"""

# ruff: noqa: F401, F811

import time
from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from omakase.plus import native_tv_devices as devices
from omakase.plus import native_tv_test
from tests.plus.test_native_tv_test import (
    DEVICES_PAGE,
    LIBRARY_API,
    ORIGIN,
    browser,
    system,
)


def _pair(system, monkeypatch, *, kind=None):
    app, db, sessions, _ = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    owner, csrf = browser(app, sessions[1])
    native = TestClient(app, base_url=ORIGIN)
    params = {"kind": kind} if kind else {}
    pairing = native.post(LIBRARY_API + "/device", params=params).json()
    approved = owner.post(
        DEVICES_PAGE,
        data={"csrf_token": csrf, "action": "approve", "device_code": pairing["user_code"]},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    native.headers["Authorization"] = "Bearer " + pairing["device_token"]
    return native, db, sessions


def test_the_phone_registers_as_an_iphone_when_it_asks_to(system, monkeypatch):
    _, db, _ = _pair(system, monkeypatch, kind="iphone")
    assert db.execute("SELECT device_kind FROM native_tv_devices").fetchone()[0] == "iphone"


def test_using_a_device_slides_its_expiry_forward(system, monkeypatch):
    native, db, _ = _pair(system, monkeypatch, kind="iphone")
    soon = int(time.time()) + 1000
    with db:
        db.execute("UPDATE native_tv_devices SET expires_at=?,last_seen_at=NULL", (soon,))
    assert native.get(LIBRARY_API + "/profile").status_code == 200
    renewed = db.execute("SELECT expires_at FROM native_tv_devices").fetchone()[0]
    assert renewed >= int(time.time()) + devices.DEVICE_TTL - 30
    reply = native.get(LIBRARY_API + "/device")
    assert reply.status_code == 200
    assert reply.json()["expires_in"] >= devices.DEVICE_TTL - 30


def test_using_a_device_keeps_its_approving_browser_session_alive(system, monkeypatch):
    native, db, sessions = _pair(system, monkeypatch)
    almost_over = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with db:
        db.execute("UPDATE sessions SET expires_at=? WHERE id=?", (almost_over, sessions[1]))
    assert native.get(LIBRARY_API + "/profile").status_code == 200
    extended = datetime.fromisoformat(
        db.execute("SELECT expires_at FROM sessions WHERE id=?", (sessions[1],)).fetchone()[0]
    )
    assert extended > datetime.now(timezone.utc) + timedelta(days=30)


def test_an_expired_device_is_not_revived_by_activity(system, monkeypatch):
    native, db, _ = _pair(system, monkeypatch)
    expired = int(time.time()) - 10
    with db:
        db.execute("UPDATE native_tv_devices SET expires_at=?,last_seen_at=NULL", (expired,))
    key = db.execute("SELECT token_digest FROM native_tv_devices").fetchone()[0]
    devices.record_activity(db, key, seen_at=int(time.time()), network=None)
    assert db.execute("SELECT expires_at FROM native_tv_devices").fetchone()[0] == expired
    assert native.get(LIBRARY_API + "/profile").status_code == 401


def test_an_expired_browser_session_still_ends_the_device_link(system, monkeypatch):
    native, db, sessions = _pair(system, monkeypatch)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with db:
        db.execute("UPDATE sessions SET expires_at=? WHERE id=?", (past, sessions[1]))
    assert native.get(LIBRARY_API + "/profile").status_code == 401


def test_request_posters_reach_the_client_only_as_fetchable_targets(system, monkeypatch):
    native, db, _ = _pair(system, monkeypatch)
    tmdb = "https://image.tmdb.org/t/p/w342/abc123.jpg"
    with db:
        db.execute(
            """INSERT INTO movie_catalog(user_id,imdb_id,title,poster_url,updated_at)
               VALUES(1,'tt0328955','Chouchou',?,'2026-11-26')""",
            (tmdb,),
        )
        db.execute(
            "INSERT INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) VALUES(1,42,?, '2026-11-26')",
            ('{"poster_url": "https://evil.example/p.jpg"}',),
        )

    def request(kind, title_id, href):
        return {
            "kind": kind,
            "media_kind": kind,
            "title_id": title_id,
            "title": "T",
            "category": "active",
            "poster_href": href,
        }

    async def dashboard(_db, _user):
        return {
            "categories": [
                {
                    "key": "active",
                    "title": "Active",
                    "requests": [
                        request(
                            "movie", "tt0328955", "/plus/movies/tt0328955/artwork?variant=poster"
                        ),
                        request("tv", "42", "/plus/tv/42/artwork?variant=poster"),
                        request("anime", "100", "/plus/media/100/poster"),
                        request("anime", "999", "/plus/media/999/poster"),
                    ],
                }
            ]
        }

    monkeypatch.setattr("omakase.plus.for_you.dashboard", dashboard)
    reply = native.get(LIBRARY_API + "/requests")
    assert reply.status_code == 200, reply.text
    posters = [item["poster"] for item in reply.json()["categories"][0]["requests"]]
    # Movie: TMDB image; TV: untrusted host dropped; anime: native artwork by monitor id;
    # an anime the viewer does not own gets no poster rather than a guessable path.
    assert posters == [tmdb, None, "artwork/1/poster", None]


def test_native_routes_reject_expired_tokens_and_personal_routes_refuse_household_scope(
    system, monkeypatch
):
    from omakase.plus import for_you, households

    native, db, sessions = _pair(system, monkeypatch, kind="iphone")
    monkeypatch.setenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", "true")
    household = households.create_household(db, user_id=1, name="Shared Home")

    async def fake_check(_db, _viewer_id, kind, title_id):
        return {
            "kind": kind,
            "media_kind": kind,
            "title_id": title_id,
            "title": "Test",
            "state": "requested",
            "label": "Requested",
            "detail": "",
            "can_check": True,
        }

    monkeypatch.setattr(for_you, "refresh_request", fake_check)
    checked = native.post(
        LIBRARY_API + "/for-you/request/check", json={"kind": "tv", "title_id": "42"}
    )
    assert checked.status_code == 200
    assert checked.json()["poster"] is None

    for path, extra in (
        ("/requests", {}),
        ("/for-you", {"category": "tv"}),
        ("/catalog-search", {"q": "test"}),
    ):
        assert (
            native.get(LIBRARY_API + path, params={"library_id": household, **extra}).status_code
            == 404
        )
    assert (
        native.post(
            LIBRARY_API + "/for-you/request/check",
            params={"library_id": household},
            json={"kind": "tv", "title_id": "42"},
        ).status_code
        == 404
    )

    with db:
        db.execute("DELETE FROM native_tv_devices")
    for method, path, body in (
        ("GET", "/home", None),
        ("POST", "/home/layout", {"action": "reset"}),
        ("GET", "/requests", None),
        ("GET", "/for-you?category=tv", None),
        ("GET", "/catalog-search?q=test", None),
        ("POST", "/for-you/request/check", {"kind": "tv", "title_id": "42"}),
    ):
        response = (
            native.get(LIBRARY_API + path)
            if method == "GET"
            else native.post(LIBRARY_API + path, json=body)
        )
        assert response.status_code == 401, (method, path, response.status_code)
