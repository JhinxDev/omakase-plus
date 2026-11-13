"""qBittorrent category/root scope and WebUI contract tests."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from omakase.plus.providers.contracts import ProviderBlock
from omakase.plus.providers.qbittorrent import QBittorrentError, QBittorrentProvider

CONTENT_HASH = "A" * 40
MAGNET = f"magnet:?xt=urn:btih:{CONTENT_HASH}&dn=Fixture"


class FakeQBittorrent:
    def __init__(self):
        self.categories: dict[str, dict] = {}
        self.rows: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.delete_files_values: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Referer"] == "http://qbittorrent.local:8080/"
        assert request.headers["Origin"] == "http://qbittorrent.local:8080"
        path = request.url.path
        if path == "/api/v2/auth/login":
            form = _form(request)
            assert form == {"username": ["omakase"], "password": ["super-secret"]}
            return httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=session; path=/"})
        assert request.headers.get("Cookie") == "SID=session"
        if path == "/api/v2/app/version":
            return httpx.Response(200, text="v5.1.2")
        if path == "/api/v2/app/webapiVersion":
            return httpx.Response(200, text="2.11.4")
        if path == "/api/v2/torrents/categories":
            return httpx.Response(200, json=self.categories)
        if path == "/api/v2/torrents/createCategory":
            form = _form(request)
            self.categories[form["category"][0]] = {
                "name": form["category"][0],
                "savePath": form["savePath"][0],
            }
            return httpx.Response(200)
        if path == "/api/v2/torrents/info":
            rows = list(self.rows.values())
            hashes = request.url.params.get("hashes")
            category = request.url.params.get("category")
            if hashes:
                rows = [row for row in rows if row["hash"] == hashes]
            if category is not None:
                rows = [row for row in rows if row["category"] == category]
            return httpx.Response(200, json=rows)
        if path == "/api/v2/torrents/add":
            form = _form(request)
            assert form["category"] == ["omakase"]
            assert form["savepath"] == ["/downloads/omakase"]
            self.rows[CONTENT_HASH] = _row()
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "index": 0,
                        "name": "Fixture Show/Fixture Show S01E01.mkv",
                        "size": 1024,
                        "priority": 1,
                    },
                    {
                        "index": 1,
                        "name": "Fixture Show/readme.txt",
                        "size": 10,
                        "priority": 0,
                    },
                ],
            )
        if path == "/api/v2/torrents/delete":
            form = _form(request)
            self.delete_files_values.append(form["deleteFiles"][0])
            self.rows.pop(form["hashes"][0], None)
            return httpx.Response(200)
        return httpx.Response(404)


def _form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode("utf-8"), keep_blank_values=True)


def _row(**overrides):
    row = {
        "hash": CONTENT_HASH,
        "name": "Fixture Show",
        "state": "uploading",
        "progress": 1.0,
        "size": 1034,
        "completed": 1034,
        "category": "omakase",
        "save_path": "/downloads/omakase",
        "content_path": "/downloads/omakase/Fixture Show",
    }
    row.update(overrides)
    return row


def _provider(fake, *, allow_delete=False):
    return QBittorrentProvider(
        "http://qbittorrent.local:8080",
        "omakase",
        "super-secret",
        category="omakase",
        save_root="/downloads/omakase",
        allow_delete=allow_delete,
        transport=httpx.MockTransport(fake),
    )


def test_health_category_creation_and_secret_redaction():
    fake = FakeQBittorrent()
    provider = _provider(fake)

    degraded = asyncio.run(provider.health(checked_at="2026-07-26T08:00:00Z"))
    created = asyncio.run(provider.ensure_category())
    healthy = asyncio.run(provider.health(checked_at="2026-07-26T08:01:00Z"))

    assert degraded.reason_code == "CATEGORY_MISSING"
    assert created is True
    assert healthy.status == "healthy"
    assert healthy.reason_code == "READY"
    assert "super-secret" not in repr(provider)
    assert "super-secret" not in repr(healthy)
    asyncio.run(provider.close())


def test_acquire_and_observe_are_confined_to_category_and_root():
    fake = FakeQBittorrent()
    provider = _provider(fake)

    external_id = asyncio.run(provider.acquire(MAGNET))
    item = asyncio.run(provider.get_item(str(external_id)))
    inventory = asyncio.run(provider.list_items())

    assert external_id == CONTENT_HASH
    assert item.content_hash == CONTENT_HASH
    assert item.storage_locator == "Fixture Show"
    assert item.files[0].selected is True
    assert item.files[1].selected is False
    assert inventory[0].files == ()
    asyncio.run(provider.close())


def test_existing_item_outside_omakase_scope_is_refused():
    fake = FakeQBittorrent()
    fake.rows[CONTENT_HASH] = _row(
        category="personal",
        save_path="/downloads/personal",
        content_path="/downloads/personal/Fixture Show",
    )
    provider = _provider(fake)

    with pytest.raises(ProviderBlock) as raised:
        asyncio.run(provider.acquire(MAGNET))

    assert raised.value.error_code == "OUTSIDE_OMAKASE_CATEGORY"
    assert all(request.url.path != "/api/v2/torrents/add" for request in fake.requests)
    asyncio.run(provider.close())


def test_delete_is_disabled_by_default_and_never_deletes_files():
    default_fake = FakeQBittorrent()
    default_fake.categories["omakase"] = {
        "name": "omakase",
        "savePath": "/downloads/omakase",
    }
    default_fake.rows[CONTENT_HASH] = _row()
    default_provider = _provider(default_fake)

    assert asyncio.run(default_provider.delete_item(CONTENT_HASH)) is False
    assert default_fake.delete_files_values == []
    asyncio.run(default_provider.close())

    enabled_fake = FakeQBittorrent()
    enabled_fake.categories.update(default_fake.categories)
    enabled_fake.rows[CONTENT_HASH] = _row()
    enabled_provider = _provider(enabled_fake, allow_delete=True)

    assert asyncio.run(enabled_provider.delete_item(CONTENT_HASH)) is True
    assert enabled_fake.delete_files_values == ["false"]
    asyncio.run(enabled_provider.close())


def test_category_path_mismatch_is_fail_closed():
    fake = FakeQBittorrent()
    fake.categories["omakase"] = {
        "name": "omakase",
        "savePath": "/downloads/wrong",
    }
    provider = _provider(fake)

    health = asyncio.run(provider.health(checked_at="2026-07-26T08:00:00Z"))
    assert health.reason_code == "CATEGORY_PATH_MISMATCH"
    with pytest.raises(QBittorrentError, match="save root"):
        asyncio.run(provider.ensure_category())
    asyncio.run(provider.close())
