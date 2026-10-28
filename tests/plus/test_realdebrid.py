"""Tests for Real-Debrid API client.

Network-dependent tests are skipped; pure logic is tested here.
For integration tests, set REALDEBRID_API_KEY in env.
"""

from __future__ import annotations

import asyncio

import pytest

from omakase.plus.realdebrid import RDFile, RDTorrent, RealDebridClient


class TestRDTorrent:
    def test_from_dict(self):
        t = RDTorrent(
            id="ABC123",
            filename="[SubsPlease] Dungeon Meshi - 01",
            status="downloading",
            progress=50.0,
            bytes_total=1_500_000_000,
            bytes_done=750_000_000,
            links=[],
        )
        assert t.id == "ABC123"
        assert t.status == "downloading"
        assert t.progress == 50.0

    def test_statuses(self):
        for status in [
            "magnet_error",
            "waiting_files_selection",
            "downloading",
            "downloaded",
            "error",
        ]:
            t = RDTorrent(
                id="x",
                filename="f",
                status=status,
                progress=0,
                bytes_total=0,
                bytes_done=0,
                links=[],
            )
            assert t.status == status


class TestRealDebridClient:
    def test_headers(self):
        client = RealDebridClient("test-api-key")
        headers = client._headers()
        assert headers["Authorization"] == "Bearer test-api-key"

    def test_instantiation(self):
        client = RealDebridClient("api-key-123", timeout=30.0)
        assert client.api_key == "api-key-123"
        assert client.timeout == 30.0

    def test_add_magnet_raises_provider_block_for_rd_451(self, monkeypatch):
        from omakase.plus.realdebrid import RealDebridProviderBlock

        class FakeResponse:
            status_code = 451

            def json(self):
                return {
                    "error": "infringing_file",
                    "error_code": 31,
                    "error_details": "This file is unavailable for copyright reasons.",
                }

        class FakeAsyncClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers, data):
                assert data["magnet"] == "magnet:?xt=urn:btih:BLOCKED"
                return FakeResponse()

        monkeypatch.setattr("omakase.plus.realdebrid.httpx.AsyncClient", FakeAsyncClient)

        client = RealDebridClient("api-key")
        with pytest.raises(RealDebridProviderBlock) as exc:
            asyncio.run(client.add_magnet("magnet:?xt=urn:btih:BLOCKED"))

        assert exc.value.http_status == 451
        assert exc.value.error_code == "infringing_file"
        assert "copyright" in exc.value.detail

    def test_add_magnet_handles_non_object_json_error_body(self, monkeypatch):
        class FakeResponse:
            status_code = 500
            text = '["upstream error"]'

            def json(self):
                return ["upstream error"]

        class FakeAsyncClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers, data):
                return FakeResponse()

        monkeypatch.setattr("omakase.plus.realdebrid.httpx.AsyncClient", FakeAsyncClient)

        client = RealDebridClient("api-key")

        assert asyncio.run(client.add_magnet("magnet:?xt=urn:btih:GENERIC")) is None

    def test_get_torrent_parses_selected_file_inventory(self, monkeypatch):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "id": "rd-1",
                    "hash": "A" * 40,
                    "filename": "Show Season 1",
                    "status": "downloaded",
                    "progress": 100,
                    "bytes": 300,
                    "links": ["https://example.invalid/media"],
                    "files": [
                        {"id": 1, "path": "/Show S01E01.mkv", "bytes": 100, "selected": 1},
                        {"id": 2, "path": "/Show S01E02.mkv", "bytes": 200, "selected": 0},
                    ],
                }

        class FakeAsyncClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url, headers):
                assert url.endswith("/torrents/info/rd-1")
                return FakeResponse()

        monkeypatch.setattr("omakase.plus.realdebrid.httpx.AsyncClient", FakeAsyncClient)

        torrent = asyncio.run(RealDebridClient("api-key").get_torrent("rd-1"))

        assert torrent is not None
        assert torrent.torrent_hash == "A" * 40
        assert torrent.files == (
            RDFile(id=1, path="/Show S01E01.mkv", bytes=100, selected=True),
            RDFile(id=2, path="/Show S01E02.mkv", bytes=200, selected=False),
        )
        assert torrent.bytes_done == 300

    def test_instant_availability_lookup_does_not_mutate_rd(self, monkeypatch):
        torrent_hash = "A" * 40

        class FakeResponse:
            status_code = 200

            def json(self):
                return {torrent_hash.lower(): {"rd": [{"1": {"filename": "Show.mkv"}}]}}

        class FakeAsyncClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url, headers):
                assert url.endswith(f"/torrents/instantAvailability/{torrent_hash}")
                return FakeResponse()

            async def post(self, *args, **kwargs):
                raise AssertionError("instant availability must not mutate RD")

        monkeypatch.setattr("omakase.plus.realdebrid.httpx.AsyncClient", FakeAsyncClient)

        assert asyncio.run(RealDebridClient("api-key").instant_available(torrent_hash)) is True

    def test_list_torrents_requests_the_documented_full_inventory_window(self, monkeypatch):
        class FakeResponse:
            status_code = 200

            def json(self):
                return []

        class FakeAsyncClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url, headers, params):
                assert url.endswith("/torrents")
                assert params == {"limit": 5000}
                return FakeResponse()

        monkeypatch.setattr("omakase.plus.realdebrid.httpx.AsyncClient", FakeAsyncClient)

        assert asyncio.run(RealDebridClient("api-key").list_torrents()) == []
