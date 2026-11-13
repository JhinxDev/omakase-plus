"""Read-only local import provider."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from omakase.plus.providers.contracts import (
    ProviderCapabilities,
    ProviderFile,
    ProviderHealth,
    ProviderItem,
)
from omakase.plus.storage import (
    MEDIA_SUFFIXES,
    ConfiguredRoot,
    StorageSafetyError,
    file_receipt,
    locator_for_path,
    normalize_locator,
    require_media_file,
)


class LocalImportProvider:
    """Expose configured local files without acquiring, selecting, or deleting."""

    capabilities = ProviderCapabilities(
        kind="local_import",
        acquisition=False,
        adoption=True,
        instant_check=False,
        file_selection=False,
        deletion=False,
        local_storage=True,
    )

    def __init__(self, root: ConfiguredRoot, *, max_inventory_files: int = 10_000):
        if max_inventory_files <= 0:
            raise ValueError("max_inventory_files must be positive")
        self.root = root
        self.max_inventory_files = max_inventory_files

    async def health(self, *, checked_at: str) -> ProviderHealth:
        try:
            next(self.root.path.iterdir(), None)
        except OSError:
            return ProviderHealth(
                status="error",
                reason_code="IMPORT_ROOT_UNREADABLE",
                detail="Configured import root is not readable",
                checked_at=checked_at,
                capabilities=self.capabilities,
            )
        return ProviderHealth(
            status="healthy",
            reason_code="READY",
            detail="Configured import root is readable",
            checked_at=checked_at,
            capabilities=self.capabilities,
        )

    async def instant_available(self, content_hash: str) -> bool | None:
        return None

    async def acquire(self, magnet: str) -> str | None:
        return None

    async def select_files(self, external_id: str, files: str = "all") -> bool:
        return True

    async def get_item(self, external_id: str) -> ProviderItem | None:
        try:
            return self._item(normalize_locator(external_id))
        except StorageSafetyError:
            return None

    async def list_items(self) -> list[ProviderItem]:
        items: list[ProviderItem] = []
        for path in sorted(self.root.path.rglob("*"), key=lambda value: value.as_posix()):
            if len(items) >= self.max_inventory_files:
                break
            if path.is_file() and path.suffix.casefold() in MEDIA_SUFFIXES:
                try:
                    items.append(self._item(locator_for_path(self.root, path)))
                except StorageSafetyError:
                    continue
        return items

    async def delete_item(self, external_id: str) -> bool:
        return False

    def _item(self, locator: str) -> ProviderItem:
        path = require_media_file(self.root, locator)
        receipt = file_receipt(self.root, locator)
        return ProviderItem(
            external_id=receipt.locator,
            name=path.name,
            status="downloaded",
            progress=100,
            bytes_total=receipt.bytes,
            bytes_done=receipt.bytes,
            files=(
                ProviderFile(
                    id=receipt.locator,
                    path=receipt.locator,
                    bytes=receipt.bytes,
                    selected=True,
                ),
            ),
            content_hash=receipt.sha256,
            storage_locator=receipt.locator,
        )


def utc_now() -> str:
    """Return a canonical timestamp for small provider readiness tools."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def root_label(path: Path) -> str:
    """Return a non-secret display label without exposing the absolute root."""
    return path.name or "Local imports"
