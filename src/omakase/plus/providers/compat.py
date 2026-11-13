"""Compatibility helpers while the legacy Real-Debrid surface is retired.

The orchestration layer calls only these provider-neutral helpers. Older tests
and private callers may still supply the pre-contract Real-Debrid client; its
duck-typed payloads are normalized here without coupling the orchestrator back
to that concrete implementation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from omakase.plus.providers.contracts import (
    AcquisitionProvider,
    ProviderBlock,
    ProviderCapabilities,
    ProviderFile,
    ProviderItem,
)

_LEGACY_REALDEBRID_CAPABILITIES = ProviderCapabilities(
    kind="realdebrid",
    acquisition=True,
    adoption=True,
    instant_check=True,
    file_selection=True,
    deletion=True,
    local_storage=False,
)


def capabilities(provider: object) -> ProviderCapabilities:
    """Return declared capabilities or the bounded legacy RD compatibility set."""
    declared = getattr(provider, "capabilities", None)
    if isinstance(declared, ProviderCapabilities):
        return declared
    return _LEGACY_REALDEBRID_CAPABILITIES


def normalize_item(value: object) -> ProviderItem:
    """Convert a contract item or legacy provider object into one stable shape."""
    if isinstance(value, ProviderItem):
        return value
    external_id = str(getattr(value, "external_id", "") or getattr(value, "id", "")).strip()
    name = str(getattr(value, "name", "") or getattr(value, "filename", "")).strip()
    raw_files = getattr(value, "files", ())
    files = tuple(
        item
        if isinstance(item, ProviderFile)
        else ProviderFile(
            id=str(getattr(item, "id", "")).strip(),
            path=str(getattr(item, "path", "")).strip(),
            bytes=int(getattr(item, "bytes", 0) or 0),
            selected=bool(getattr(item, "selected", False)),
        )
        for item in raw_files
    )
    return ProviderItem(
        external_id=external_id,
        name=name,
        status=str(getattr(value, "status", "")).strip(),
        progress=float(getattr(value, "progress", 0) or 0),
        bytes_total=int(getattr(value, "bytes_total", 0) or 0),
        bytes_done=int(getattr(value, "bytes_done", 0) or 0),
        files=files,
        content_hash=str(getattr(value, "content_hash", "") or getattr(value, "torrent_hash", "")),
        storage_locator=str(getattr(value, "storage_locator", "") or ""),
    )


async def instant_available(provider: object, content_hash: str) -> bool | None:
    if not capabilities(provider).instant_check:
        return None
    method = _method(provider, "instant_available")
    return await method(content_hash)


async def acquire(provider: object, magnet: str) -> str | None:
    method = _method(provider, "acquire", legacy="add_magnet")
    try:
        result = await method(magnet)
    except ProviderBlock:
        raise
    except Exception as exc:
        if all(hasattr(exc, field) for field in ("http_status", "error_code", "detail")):
            raise ProviderBlock(
                http_status=int(getattr(exc, "http_status")),
                error_code=str(getattr(exc, "error_code")),
                detail=str(getattr(exc, "detail")),
            ) from exc
        raise
    return None if result is None else str(result)


async def select_files(provider: object, external_id: str, files: str = "all") -> bool:
    if not capabilities(provider).file_selection:
        return True
    method = _method(provider, "select_files")
    return bool(await method(external_id, files))


async def get_item(provider: object, external_id: str) -> ProviderItem | None:
    method = _method(provider, "get_item", legacy="get_torrent")
    value = await method(external_id)
    return None if value is None else normalize_item(value)


async def list_items(provider: object) -> list[ProviderItem]:
    method = _method(provider, "list_items", legacy="list_torrents")
    return [normalize_item(item) for item in await method()]


async def delete_item(provider: object, external_id: str) -> bool:
    if not capabilities(provider).deletion:
        return False
    method = _method(provider, "delete_item", legacy="delete_torrent")
    return bool(await method(external_id))


def _method(
    provider: object,
    name: str,
    *,
    legacy: str | None = None,
) -> Callable[..., Awaitable[Any]]:
    method = getattr(provider, name, None)
    if method is None and legacy is not None:
        method = getattr(provider, legacy, None)
    if method is None or not callable(method):
        provider_type = type(provider).__name__
        raise TypeError(f"{provider_type} does not implement provider operation {name}")
    return method


def is_provider(value: object) -> bool:
    """Return whether a value implements the new runtime-checkable protocol."""
    return isinstance(value, AcquisitionProvider)
