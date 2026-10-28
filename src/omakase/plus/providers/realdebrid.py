"""Real-Debrid adapter for the provider-neutral media contracts."""

from __future__ import annotations

from omakase.plus.provider_limits import ProviderRateLimit
from omakase.plus.providers.contracts import (
    ProviderBlock,
    ProviderCapabilities,
    ProviderFile,
    ProviderHealth,
    ProviderItem,
)
from omakase.plus.realdebrid import RDTorrent, RealDebridClient, RealDebridProviderBlock


class RealDebridProvider:
    """Translate the existing Real-Debrid client into generic provider state."""

    capabilities = ProviderCapabilities(
        kind="realdebrid",
        acquisition=True,
        adoption=True,
        instant_check=True,
        file_selection=True,
        deletion=True,
        local_storage=False,
    )

    def __init__(
        self, api_key: str, *, timeout: float = 15.0, request_guard=None, rate_limit_observer=None
    ):
        self._client = RealDebridClient(
            api_key,
            timeout=timeout,
            request_guard=request_guard,
            rate_limit_observer=rate_limit_observer,
        )

    async def health(self, *, checked_at: str) -> ProviderHealth:
        try:
            await self._client.list_torrents()
        except ProviderRateLimit:
            raise
        except Exception:
            return ProviderHealth(
                status="error",
                reason_code="PROVIDER_UNREACHABLE",
                detail="Real-Debrid inventory request failed",
                checked_at=checked_at,
                capabilities=self.capabilities,
            )
        return ProviderHealth(
            status="healthy",
            reason_code="READY",
            detail="Real-Debrid inventory is reachable",
            checked_at=checked_at,
            capabilities=self.capabilities,
        )

    async def instant_available(self, content_hash: str) -> bool | None:
        return await self._client.instant_available(content_hash)

    async def acquire(self, magnet: str) -> str | None:
        try:
            return await self._client.add_magnet(magnet)
        except RealDebridProviderBlock as exc:
            raise ProviderBlock(
                http_status=exc.http_status,
                error_code=exc.error_code,
                detail=exc.detail,
            ) from exc

    async def select_files(self, external_id: str, files: str = "all") -> bool:
        return await self._client.select_files(external_id, files)

    async def verify_download(self, external_id: str) -> bool:
        try:
            return await self._client.verify_download(external_id)
        except RealDebridProviderBlock as exc:
            raise ProviderBlock(
                http_status=exc.http_status, error_code=exc.error_code, detail=exc.detail
            ) from exc

    async def get_item(self, external_id: str) -> ProviderItem | None:
        torrent = await self._client.get_torrent(external_id)
        return None if torrent is None else _provider_item(torrent)

    async def list_items(self) -> list[ProviderItem]:
        return [_provider_item(item) for item in await self._client.list_torrents()]

    async def delete_item(self, external_id: str) -> bool:
        return await self._client.delete_torrent(external_id)


def _provider_item(torrent: RDTorrent) -> ProviderItem:
    return ProviderItem(
        external_id=torrent.id,
        name=torrent.filename,
        status=torrent.status,
        progress=torrent.progress,
        bytes_total=torrent.bytes_total,
        bytes_done=torrent.bytes_done,
        files=tuple(
            ProviderFile(
                id=str(item.id),
                path=item.path,
                bytes=item.bytes,
                selected=item.selected,
            )
            for item in torrent.files
        ),
        content_hash=torrent.torrent_hash,
    )
