"""Category- and root-scoped qBittorrent WebUI provider."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlsplit

import httpx

from omakase.plus.providers.contracts import (
    ProviderBlock,
    ProviderCapabilities,
    ProviderFile,
    ProviderHealth,
    ProviderItem,
)

_CONTENT_HASH_RE = re.compile(r"(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})")
_DOWNLOADED_STATES = frozenset(
    {
        "uploading",
        "stalledup",
        "queuedup",
        "pausedup",
        "forcedup",
        "checkingup",
    }
)
_QUEUED_STATES = frozenset({"pauseddl", "queueddl", "metadl", "checkingdl"})
_ERROR_STATES = frozenset({"error", "missingfiles", "unknown"})


class QBittorrentError(RuntimeError):
    """A secret-free qBittorrent contract failure."""


class QBittorrentProvider:
    """Use only one exact category and save root in a shared qBittorrent."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        category: str = "omakase",
        save_root: str = "/downloads/omakase",
        allow_delete: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise QBittorrentError("qBittorrent base URL is invalid")
        if not username or not password or any(character.isspace() for character in username):
            raise QBittorrentError("qBittorrent credentials are invalid")
        if (
            not category
            or len(category) > 100
            or any(character in category for character in "\r\n|")
        ):
            raise QBittorrentError("qBittorrent category is invalid")
        self.save_root = _absolute_posix_path(save_root, "save root")
        self.category = category
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._logged_in = False
        self.capabilities = ProviderCapabilities(
            kind="qbittorrent",
            acquisition=True,
            adoption=True,
            instant_check=False,
            file_selection=False,
            deletion=bool(allow_delete),
            local_storage=True,
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Referer": f"{self._base_url}/",
                "Origin": self._base_url,
                "User-Agent": "Omakase-Plus/1",
            },
            timeout=timeout,
            transport=transport,
        )

    def __repr__(self) -> str:
        return (
            f"QBittorrentProvider(base_url={self._base_url!r}, "
            f"category={self.category!r}, password=<redacted>)"
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self, *, checked_at: str) -> ProviderHealth:
        try:
            version = (await self._request("GET", "/api/v2/app/version")).text.strip()
            api_version = (await self._request("GET", "/api/v2/app/webapiVersion")).text.strip()
            categories = await self._categories()
        except QBittorrentError:
            return ProviderHealth(
                status="error",
                reason_code="PROVIDER_UNREACHABLE",
                detail="qBittorrent authentication or readiness check failed",
                checked_at=checked_at,
                capabilities=self.capabilities,
            )
        category = categories.get(self.category)
        if category is None:
            return ProviderHealth(
                status="degraded",
                reason_code="CATEGORY_MISSING",
                detail="The Omakase qBittorrent category is not configured",
                checked_at=checked_at,
                capabilities=self.capabilities,
            )
        if _normalized_path(str(category.get("savePath", ""))) != self.save_root:
            return ProviderHealth(
                status="error",
                reason_code="CATEGORY_PATH_MISMATCH",
                detail="The Omakase qBittorrent category uses a different save root",
                checked_at=checked_at,
                capabilities=self.capabilities,
            )
        return ProviderHealth(
            status="healthy",
            reason_code="READY",
            detail=f"qBittorrent {version} WebAPI {api_version} is ready",
            checked_at=checked_at,
            capabilities=self.capabilities,
        )

    async def ensure_category(self) -> bool:
        """Create the exact category if absent; refuse to edit an existing path."""
        categories = await self._categories()
        current = categories.get(self.category)
        if current is not None:
            if _normalized_path(str(current.get("savePath", ""))) != self.save_root:
                raise QBittorrentError("qBittorrent category save root does not match")
            return False
        await self._request(
            "POST",
            "/api/v2/torrents/createCategory",
            data={"category": self.category, "savePath": self.save_root},
        )
        created = (await self._categories()).get(self.category)
        if created is None or _normalized_path(str(created.get("savePath", ""))) != self.save_root:
            raise QBittorrentError("qBittorrent category did not converge")
        return True

    async def instant_available(self, content_hash: str) -> bool | None:
        return None

    async def acquire(self, magnet: str) -> str | None:
        content_hash = _magnet_hash(magnet)
        existing = await self._torrent_rows(content_hash=content_hash, category=None)
        if existing:
            self._require_scoped(existing[0])
            return content_hash
        await self.ensure_category()
        response = await self._request(
            "POST",
            "/api/v2/torrents/add",
            data={
                "urls": magnet,
                "savepath": self.save_root,
                "category": self.category,
                "paused": "false",
                "root_folder": "true",
                "autoTMM": "false",
            },
        )
        if response.text.strip() not in {"", "Ok."}:
            raise QBittorrentError("qBittorrent rejected the acquisition request")
        observed = await self._torrent_rows(content_hash=content_hash, category=None)
        if not observed:
            raise QBittorrentError("qBittorrent did not expose the acquired item")
        self._require_scoped(observed[0])
        return content_hash

    async def select_files(self, external_id: str, files: str = "all") -> bool:
        row = await self._scoped_row(external_id)
        return row is not None and files == "all"

    async def get_item(self, external_id: str) -> ProviderItem | None:
        content_hash = _content_hash(external_id)
        rows = await self._torrent_rows(content_hash=content_hash, category=None)
        if not rows:
            return None
        row = rows[0]
        self._require_scoped(row)
        response = await self._request(
            "GET",
            "/api/v2/torrents/files",
            params={"hash": content_hash},
        )
        payload = _json(response)
        if not isinstance(payload, list):
            raise QBittorrentError("qBittorrent file inventory is invalid")
        return self._provider_item(row, files=payload)

    async def list_items(self) -> list[ProviderItem]:
        rows = await self._torrent_rows(content_hash=None, category=self.category)
        return [self._provider_item(row, files=[]) for row in rows]

    async def delete_item(self, external_id: str) -> bool:
        if not self.capabilities.deletion:
            return False
        content_hash = _content_hash(external_id)
        row = await self._scoped_row(content_hash)
        if row is None:
            return True
        await self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={"hashes": content_hash, "deleteFiles": "false"},
        )
        return True

    async def _scoped_row(self, external_id: str) -> dict | None:
        content_hash = _content_hash(external_id)
        rows = await self._torrent_rows(content_hash=content_hash, category=None)
        if not rows:
            return None
        self._require_scoped(rows[0])
        return rows[0]

    async def _torrent_rows(
        self,
        *,
        content_hash: str | None,
        category: str | None,
    ) -> list[dict]:
        params: dict[str, str] = {}
        if content_hash is not None:
            params["hashes"] = _content_hash(content_hash)
        if category is not None:
            params["category"] = category
        payload = _json(
            await self._request(
                "GET",
                "/api/v2/torrents/info",
                params=params,
            )
        )
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise QBittorrentError("qBittorrent inventory is invalid")
        if len(payload) > 10_000:
            raise QBittorrentError("qBittorrent inventory exceeded the safety limit")
        if category is not None:
            for row in payload:
                self._require_scoped(row)
        return payload

    async def _categories(self) -> dict:
        payload = _json(await self._request("GET", "/api/v2/torrents/categories"))
        if not isinstance(payload, dict):
            raise QBittorrentError("qBittorrent category inventory is invalid")
        return payload

    def _provider_item(self, row: dict, *, files: list[dict]) -> ProviderItem:
        self._require_scoped(row)
        content_hash = _content_hash(str(row.get("hash", "")))
        size = _nonnegative_int(row.get("size"))
        completed = _nonnegative_int(row.get("completed"))
        progress = max(0.0, min(100.0, float(row.get("progress", 0) or 0) * 100))
        return ProviderItem(
            external_id=content_hash,
            name=str(row.get("name", "")).strip() or content_hash,
            status=_status(str(row.get("state", "")), progress),
            progress=progress,
            bytes_total=size,
            bytes_done=min(size, completed) if size else completed,
            files=tuple(_provider_file(item) for item in files),
            content_hash=content_hash,
            storage_locator=_relative_content_locator(
                str(row.get("content_path", "")),
                save_root=self.save_root,
            ),
        )

    def _require_scoped(self, row: dict) -> None:
        if str(row.get("category", "")) != self.category:
            raise ProviderBlock(
                http_status=409,
                error_code="OUTSIDE_OMAKASE_CATEGORY",
                detail="Existing qBittorrent item is outside the Omakase category",
            )
        save_path = _normalized_path(str(row.get("save_path", "")))
        if save_path != self.save_root:
            raise ProviderBlock(
                http_status=409,
                error_code="OUTSIDE_OMAKASE_ROOT",
                detail="Existing qBittorrent item is outside the Omakase save root",
            )
        _relative_content_locator(
            str(row.get("content_path", "")),
            save_root=self.save_root,
        )

    async def _login(self) -> None:
        try:
            response = await self._client.post(
                "/api/v2/auth/login",
                data={"username": self._username, "password": self._password},
            )
        except httpx.HTTPError as exc:
            raise QBittorrentError("qBittorrent authentication failed") from exc
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise QBittorrentError("qBittorrent authentication failed")
        self._logged_in = True

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._logged_in:
            await self._login()
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QBittorrentError("qBittorrent request failed") from exc
        return response


def _provider_file(value: dict) -> ProviderFile:
    if not isinstance(value, dict):
        raise QBittorrentError("qBittorrent file inventory is invalid")
    return ProviderFile(
        id=str(_nonnegative_int(value.get("index"))),
        path=str(value.get("name", "")).replace("\\", "/"),
        bytes=_nonnegative_int(value.get("size")),
        selected=_nonnegative_int(value.get("priority")) > 0,
    )


def _status(state: str, progress: float) -> str:
    normalized = state.casefold()
    if progress >= 100 or normalized in _DOWNLOADED_STATES:
        return "downloaded"
    if normalized in _ERROR_STATES:
        return "error"
    if normalized in _QUEUED_STATES:
        return "queued"
    return "downloading"


def _relative_content_locator(content_path: str, *, save_root: str) -> str:
    content = PurePosixPath(_absolute_posix_path(content_path, "content path"))
    root = PurePosixPath(save_root)
    try:
        relative = content.relative_to(root)
    except ValueError as exc:
        raise ProviderBlock(
            http_status=409,
            error_code="OUTSIDE_OMAKASE_ROOT",
            detail="qBittorrent content path is outside the Omakase save root",
        ) from exc
    if not relative.parts:
        raise QBittorrentError("qBittorrent content locator is invalid")
    return relative.as_posix()


def _absolute_posix_path(value: str, field: str) -> str:
    normalized = _normalized_path(value)
    path = PurePosixPath(normalized)
    if (
        not path.is_absolute()
        or path == PurePosixPath("/")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise QBittorrentError(f"qBittorrent {field} is invalid")
    return path.as_posix()


def _normalized_path(value: str) -> str:
    return value.strip().replace("\\", "/").rstrip("/")


def _content_hash(value: str) -> str:
    normalized = value.strip().upper()
    if not _CONTENT_HASH_RE.fullmatch(normalized):
        raise QBittorrentError("qBittorrent content hash is invalid")
    return normalized


def _magnet_hash(magnet: str) -> str:
    if not isinstance(magnet, str) or not magnet.startswith("magnet:?"):
        raise QBittorrentError("qBittorrent magnet is invalid")
    query = parse_qs(urlsplit(magnet).query)
    for value in query.get("xt", []):
        prefix = "urn:btih:"
        if value.casefold().startswith(prefix):
            return _content_hash(value[len(prefix) :])
    raise QBittorrentError("qBittorrent magnet has no supported content hash")


def _json(response: httpx.Response):
    try:
        return response.json()
    except ValueError as exc:
        raise QBittorrentError("qBittorrent returned invalid JSON") from exc


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
