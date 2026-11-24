"""Shared loop-scoped HTTPX connection pools with HTTP/2 and SNI isolation."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

_ORIGINAL_ASYNC_CLIENT = httpx.AsyncClient
_DEFAULT_LIMITS = httpx.Limits(
    max_connections=32,
    max_keepalive_connections=16,
    keepalive_expiry=45.0,
)
_MAX_POOLS_PER_LOOP = 64

_PoolKey = tuple[str, str | None, bool, tuple[object, ...]]
_POOLS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[_PoolKey, httpx.AsyncClient]] = (
    weakref.WeakKeyDictionary()
)


def _timeout_key(timeout: httpx.Timeout | float | int | None) -> tuple[object, ...]:
    if isinstance(timeout, httpx.Timeout):
        return (timeout.connect, timeout.read, timeout.write, timeout.pool)
    return (timeout,)


def acquire_client(
    *,
    key: str = "default",
    transport: httpx.AsyncBaseTransport | None = None,
    sni_hostname: str | None = None,
    timeout: httpx.Timeout | float = 15.0,
    follow_redirects: bool = False,
    trust_env: bool = False,
    http2: bool = True,
    limits: httpx.Limits | None = None,
    client_cls: type[httpx.AsyncClient] | None = None,
    client_kwargs: dict[str, object] | None = None,
) -> tuple[httpx.AsyncClient, bool]:
    """Return `(client, owned)` reusing an HTTP/2 pool when unmocked and transport-free.

    Callers MUST close `client` when and only when `owned` is `True`.
    """
    factory = client_cls or httpx.AsyncClient
    if transport is not None:
        client = factory(
            transport=transport,
            timeout=timeout,
            follow_redirects=follow_redirects,
            trust_env=trust_env,
        )
        return client, True
    if factory is not _ORIGINAL_ASYNC_CLIENT or httpx.AsyncClient is not _ORIGINAL_ASYNC_CLIENT:
        kwargs: dict[str, object] = {"timeout": timeout}
        if client_kwargs:
            kwargs.update(client_kwargs)
        return factory(**kwargs), True

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=follow_redirects,
            trust_env=trust_env,
            http2=http2,
            limits=limits or _DEFAULT_LIMITS,
        )
        return client, True

    pool_map = _POOLS.setdefault(loop, {})
    pool_key: _PoolKey = (
        key,
        sni_hostname,
        bool(follow_redirects),
        _timeout_key(timeout),
    )
    client = pool_map.get(pool_key)
    if client is not None and not client.is_closed:
        return client, False
    if client is not None:
        pool_map.pop(pool_key, None)

    while len(pool_map) >= _MAX_POOLS_PER_LOOP:
        oldest_key = next(iter(pool_map))
        evicted = pool_map.pop(oldest_key)
        if not evicted.is_closed:
            loop.create_task(evicted.aclose())

    client = httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=follow_redirects,
        trust_env=trust_env,
        http2=http2,
        limits=limits or _DEFAULT_LIMITS,
    )
    pool_map[pool_key] = client
    return client, False


@asynccontextmanager
async def client_scope(
    *,
    key: str = "default",
    transport: httpx.AsyncBaseTransport | None = None,
    sni_hostname: str | None = None,
    timeout: httpx.Timeout | float = 15.0,
    follow_redirects: bool = False,
    trust_env: bool = False,
    http2: bool = True,
    limits: httpx.Limits | None = None,
    client_cls: type[httpx.AsyncClient] | None = None,
    client_kwargs: dict[str, object] | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async context manager yielding a pooled or test-owned `httpx.AsyncClient`."""
    client, owned = acquire_client(
        key=key,
        transport=transport,
        sni_hostname=sni_hostname,
        timeout=timeout,
        follow_redirects=follow_redirects,
        trust_env=trust_env,
        http2=http2,
        limits=limits,
        client_cls=client_cls,
        client_kwargs=client_kwargs,
    )
    if owned:
        async with client as entered:
            yield entered
    else:
        yield client


async def close_pools() -> None:
    """Close all pooled HTTPX clients bound to the running event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    pool_map = _POOLS.pop(loop, None)
    if not pool_map:
        return
    clients = [client for client in pool_map.values() if not client.is_closed]
    pool_map.clear()
    for client in clients:
        await client.aclose()
