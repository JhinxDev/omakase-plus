"""Real-Debrid REST API client — add magnet links and manage torrents.

API docs: https://api.real-debrid.com/
Token: https://real-debrid.com/apitoken
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath

import httpx

RD_API = "https://api.real-debrid.com/rest/1.0"


class RealDebridProviderBlock(Exception):
    """Real-Debrid rejected a torrent because the provider blocked the hash."""

    def __init__(self, *, http_status: int, error_code: str, detail: str):
        self.http_status = http_status
        self.error_code = error_code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class RDFile:
    """One file record from RD's selected inventory."""

    id: int
    path: str
    bytes: int
    selected: bool


@dataclass(frozen=True, slots=True)
class RDTorrent:
    """A torrent in the Real-Debrid queue."""

    id: str
    filename: str
    status: str  # magnet_error, waiting_files_selection, downloading, downloaded, error
    progress: float  # 0.0–100.0
    bytes_total: int
    bytes_done: int
    links: tuple[str, ...]
    files: tuple[RDFile, ...] = ()
    torrent_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "links", tuple(self.links))
        object.__setattr__(self, "files", tuple(self.files))


class RealDebridClient:
    """Async client for the Real-Debrid REST API."""

    def __init__(self, api_key: str, timeout: float = 15.0):
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def add_magnet(self, magnet: str) -> str | None:
        """Add a magnet link to Real-Debrid.

        Returns the torrent ID on success, or ``None`` on failure.
        Raises ``RealDebridProviderBlock`` for known provider-blocked hashes.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{RD_API}/torrents/addMagnet",
                headers=self._headers(),
                data={"magnet": magnet},
            )
            if resp.status_code in (201, 200):
                data = resp.json()
                return data.get("id")
            block = _provider_block_from_response(resp)
            if block is not None:
                raise block
            return None

    async def select_files(self, torrent_id: str, files: str = "all") -> bool:
        """Select files to download from a torrent. Default: all files."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{RD_API}/torrents/selectFiles/{torrent_id}",
                headers=self._headers(),
                data={"files": files},
            )
            return resp.status_code in (200, 201, 202, 204)

    async def verify_download(self, torrent_id: str) -> bool:
        """Check selected media links, not just the misleading downloaded flag.

        Generated URLs stay in memory and are never returned or persisted.
        This proves provider access only; Plex must still validate the media.
        """
        torrent = await self.get_torrent(torrent_id)
        if torrent is None or torrent.status != "downloaded":
            return False
        selected = [file for file in torrent.files if file.selected]
        if not selected or len(selected) != len(torrent.links):
            return False
        links = [
            link
            for file, link in zip(selected, torrent.links, strict=True)
            if PurePosixPath(file.path).suffix.lower()
            in {".mkv", ".mp4", ".avi", ".m4v", ".m2ts", ".ts"}
        ]
        if not links:
            return False
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for link in links:
                # Leave API-rate headroom for inventory calls and other consumers.
                await asyncio.sleep(0.4)
                resp = await client.post(
                    f"{RD_API}/unrestrict/link", headers=self._headers(), data={"link": link}
                )
                block = _provider_block_from_response(resp)
                if block is not None:
                    raise RealDebridProviderBlock(
                        http_status=block.http_status,
                        error_code=block.error_code,
                        detail="Real-Debrid restricted access to this media file",
                    )
                if resp.status_code != 200:
                    return False
                try:
                    payload = resp.json()
                except ValueError:
                    return False
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("download"), str)
                    or not payload["download"].startswith("https://")
                ):
                    return False
        return True

    async def get_torrent(self, torrent_id: str) -> RDTorrent | None:
        """Get info for a specific torrent."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{RD_API}/torrents/info/{torrent_id}",
                headers=self._headers(),
            )
            if resp.status_code != 200:
                return None
            return _torrent_from_payload(resp.json())

    async def instant_available(self, torrent_hash: str) -> bool | None:
        """Check RD cache state without creating or selecting a torrent."""
        normalized_hash = (torrent_hash or "").strip().upper()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{RD_API}/torrents/instantAvailability/{normalized_hash}",
                headers=self._headers(),
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
        if not isinstance(data, dict):
            return None
        entry = data.get(normalized_hash) or data.get(normalized_hash.lower())
        if not isinstance(entry, dict):
            return False
        variants = entry.get("rd")
        return isinstance(variants, list) and bool(variants)

    async def list_torrents(self) -> list[RDTorrent]:
        """List all active torrents."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{RD_API}/torrents",
                headers=self._headers(),
                params={"limit": 5000},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            torrents: list[RDTorrent] = []
            for t in data:
                torrents.append(_torrent_from_payload(t))
            return torrents

    async def delete_torrent(self, torrent_id: str) -> bool:
        """Delete a torrent from Real-Debrid. Returns True on success."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.delete(
                f"{RD_API}/torrents/delete/{torrent_id}",
                headers=self._headers(),
            )
            return resp.status_code in (200, 204)


def _torrent_from_payload(data: object) -> RDTorrent:
    if not isinstance(data, dict):
        data = {}
    progress = float(data.get("progress", 0) or 0)
    bytes_total = max(0, int(data.get("bytes", 0) or 0))
    raw_bytes_done = data.get("bytes_done")
    bytes_done = (
        max(0, int(raw_bytes_done))
        if raw_bytes_done is not None
        else max(0, int(bytes_total * progress / 100))
    )
    files: list[RDFile] = []
    raw_files = data.get("files") or []
    if isinstance(raw_files, list):
        for raw_file in raw_files:
            if not isinstance(raw_file, dict):
                continue
            files.append(
                RDFile(
                    id=int(raw_file.get("id", 0) or 0),
                    path=str(raw_file.get("path") or ""),
                    bytes=max(0, int(raw_file.get("bytes", 0) or 0)),
                    selected=raw_file.get("selected") in (1, True, "1"),
                )
            )
    links = data.get("links") or []
    if not isinstance(links, list):
        links = []
    return RDTorrent(
        id=str(data.get("id") or ""),
        filename=str(data.get("filename") or ""),
        status=str(data.get("status") or ""),
        progress=progress,
        bytes_total=bytes_total,
        bytes_done=bytes_done,
        links=tuple(str(link) for link in links),
        files=tuple(files),
        torrent_hash=str(data.get("hash") or "").upper(),
    )


def _provider_block_from_response(resp: httpx.Response) -> RealDebridProviderBlock | None:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    error = str(data.get("error") or "")
    error_code = error or str(data.get("error_code") or "")
    detail = str(
        data.get("error_details")
        or data.get("error_description")
        or data.get("error")
        or getattr(resp, "text", "")
        or f"Real-Debrid request failed with HTTP {resp.status_code}"
    )

    if (
        resp.status_code == 451
        or str(data.get("error_code")) == "35"
        or error == "infringing_file"
        or "infringing_file" in detail
    ):
        return RealDebridProviderBlock(
            http_status=resp.status_code,
            error_code=error_code or "provider_block",
            detail=detail,
        )
    return None
