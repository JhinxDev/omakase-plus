"""Immutable provider contracts used by the media orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Stable feature declaration for one configured provider."""

    kind: str
    acquisition: bool
    adoption: bool
    instant_check: bool
    file_selection: bool
    deletion: bool
    local_storage: bool

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("provider kind is required")


@dataclass(frozen=True, slots=True)
class ProviderFile:
    """One provider inventory file with a provider-stable locator."""

    id: str
    path: str
    bytes: int
    selected: bool

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("provider file id is required")
        if not self.path:
            raise ValueError("provider file path is required")
        if self.bytes < 0:
            raise ValueError("provider file bytes must be nonnegative")


@dataclass(frozen=True, slots=True)
class ProviderItem:
    """Provider-neutral acquisition and inventory state."""

    external_id: str
    name: str
    status: str
    progress: float
    bytes_total: int
    bytes_done: int
    files: tuple[ProviderFile, ...] = ()
    content_hash: str = ""
    storage_locator: str = ""

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("provider external id is required")
        if not self.name:
            raise ValueError("provider item name is required")
        if not 0 <= self.progress <= 100:
            raise ValueError("provider progress must be between 0 and 100")
        if self.bytes_total < 0 or self.bytes_done < 0:
            raise ValueError("provider item byte counts must be nonnegative")
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "content_hash", self.content_hash.strip().upper())


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Secret-free readiness receipt for a configured provider."""

    status: str
    reason_code: str
    detail: str
    checked_at: str
    capabilities: ProviderCapabilities

    def __post_init__(self) -> None:
        if self.status not in {"healthy", "degraded", "error"}:
            raise ValueError("provider health status is invalid")
        if len(self.reason_code) > 100 or len(self.detail) > 500:
            raise ValueError("provider health detail is too long")


class ProviderBlock(Exception):
    """A provider refused a candidate for a stable, actionable reason."""

    def __init__(self, *, http_status: int, error_code: str, detail: str):
        self.http_status = int(http_status)
        self.error_code = str(error_code)
        self.detail = str(detail)
        super().__init__(self.detail)


@runtime_checkable
class AcquisitionProvider(Protocol):
    """Operations the orchestrator may request from an acquisition provider."""

    capabilities: ProviderCapabilities

    async def health(self, *, checked_at: str) -> ProviderHealth: ...

    async def instant_available(self, content_hash: str) -> bool | None: ...

    async def acquire(self, magnet: str) -> str | None: ...

    async def select_files(self, external_id: str, files: str = "all") -> bool: ...

    async def get_item(self, external_id: str) -> ProviderItem | None: ...

    async def list_items(self) -> list[ProviderItem]: ...

    async def delete_item(self, external_id: str) -> bool: ...
