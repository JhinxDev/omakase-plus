from types import SimpleNamespace
from uuid import uuid4

import httpx

from omakase.plus import households, native_tv_test
from omakase.plus.debrid_library import CloudSource, DebridLibrary, account_binding
from omakase.plus.debrid_stream import DebridStreams
from omakase.plus.web_playback_catalog import personal_scope
from tests.plus.test_native_tv_test import API, LIBRARY_API, _anime_pass
from tests.plus.test_native_tv_test import system as system


def test_native_direct_cloud_pass_is_personal_and_revoked_on_account_change(system, monkeypatch):
    native, db, options, _, _ = _anime_pass(system, monkeypatch)
    monkeypatch.setenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", "true")
    shared = households.create_household(db, user_id=1, name="Shared Cinema")
    personal = personal_scope(db, 1).library_id
    account = SimpleNamespace(token=uuid4().hex)
    reads = []

    monkeypatch.setattr(
        native_tv_test, "account_token", lambda _, actor: account.token if actor == 1 else ""
    )

    async def resolve(self, token, file_id):
        assert token == account.token and file_id == "d-fixture"
        return CloudSource(
            "Fixture.mkv",
            "a" * 64,
            "b" * 64,
            1800,
            account_binding(token),
            "",
            direct="https://download.real-debrid.com/synthetic",
            size=4,
        )

    async def unexpected_preparation(*args, **kwargs):
        raise AssertionError("Direct Cloud playback cannot prepare HLS")

    def original_file(request):
        reads.append(request)
        assert request.headers["range"] == "bytes=0-3"
        return httpx.Response(
            206, stream=httpx.ByteStream(b"test"), headers={"content-range": "bytes 0-3/4"}
        )

    monkeypatch.setattr(DebridLibrary, "source", resolve)
    monkeypatch.setattr(DebridStreams, "prepare", unexpected_preparation)
    options.debrid = original_file
    body = {"watch_path": "/plus/watch/cloud/d-fixture", "engine": "direct"}
    denied = native.post(LIBRARY_API + "/play", params={"library_id": shared}, json=body)
    assert denied.status_code == 403
    opened = native.post(LIBRARY_API + "/play", params={"library_id": personal}, json=body)
    assert opened.status_code == 200, opened.text
    data = opened.json()
    assert data["identity"]["library_id"] == personal
    assert data["identity"]["domain"] == "cloud"
    assert data["media_format"] == "direct"
    assert "resume_scope" not in data
    assert data["media_url"] == f"{API}/direct/{data['media_id']}/{data['media_token']}"
    assert account.token not in opened.text and "real-debrid.com" not in opened.text
    ranged = native.get(data["media_url"], headers={"Range": "bytes=0-3"})
    assert (ranged.status_code, ranged.content) == (206, b"test")
    assert len(reads) == 1

    account.token = uuid4().hex
    revoked = native.get(data["media_url"], headers={"Range": "bytes=0-3"})
    assert revoked.status_code == 404
    assert len(reads) == 1
