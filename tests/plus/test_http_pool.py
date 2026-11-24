"""Tests for shared loop-scoped HTTP/2 client pooling and mock isolation."""

from __future__ import annotations

import asyncio

import httpx

from omakase.plus import http_pool


def test_pooled_client_reuses_same_key_and_isolates_sni():
    async def exercise():
        try:
            first, first_owned = http_pool.acquire_client(
                key="media", sni_hostname="alpha.example.invalid"
            )
            second, second_owned = http_pool.acquire_client(
                key="media", sni_hostname="alpha.example.invalid"
            )
            other_sni, other_owned = http_pool.acquire_client(
                key="media", sni_hostname="beta.example.invalid"
            )
            assert first_owned is False
            assert second_owned is False
            assert other_owned is False
            assert first is second
            assert first is not other_sni
            async with http_pool.client_scope(
                key="media", sni_hostname="alpha.example.invalid"
            ) as scoped:
                assert scoped is first
            assert not first.is_closed
        finally:
            await http_pool.close_pools()
            assert first.is_closed
            assert other_sni.is_closed

    asyncio.run(exercise())


def test_close_pools_evicts_and_recreates_fresh_clients():
    async def exercise():
        first, _ = http_pool.acquire_client(key="probe")
        await http_pool.close_pools()
        assert first.is_closed
        second, owned = http_pool.acquire_client(key="probe")
        assert owned is False
        assert second is not first
        assert not second.is_closed
        await http_pool.close_pools()

    asyncio.run(exercise())


def test_mock_transport_bypasses_pool_and_closes_on_exit():
    async def exercise():
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
        async with http_pool.client_scope(key="media", transport=transport) as client:
            assert client._transport is transport
            resp = await client.get("https://origin.example.invalid/check")
            assert resp.status_code == 200
        assert client.is_closed

    asyncio.run(exercise())


def test_monkeypatched_client_class_keeps_minimal_constructor_and_context_exit(monkeypatch):
    entered = []
    exited = []

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            entered.append(self.timeout)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            exited.append(self.timeout)
            return False

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    async def exercise():
        async with http_pool.client_scope(key="provider", timeout=9.5) as client:
            assert isinstance(client, FakeAsyncClient)
            assert client.timeout == 9.5

    asyncio.run(exercise())
    assert entered == [9.5]
    assert exited == [9.5]
