"""Read the viewer's Real-Debrid inventory and resolve exact playable files."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import httpx

from omakase.plus import connections
from omakase.plus.realdebrid import RD_API

_ID = r"[a-zA-Z0-9]{3,64}"
_ENTRY = re.compile(rf"(?:t-{_ID}|d-{_ID})")
_FILE = re.compile(rf"(?:t-{_ID}-[1-9][0-9]{{0,9}}|d-{_ID})")
_VIDEO = frozenset(
    {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpg", ".mpeg", ".ts", ".m2ts", ".wmv"}
)
_PAGE = 100
_MAX_ACCOUNT_ENTRIES = 10000
_RESPONSE_BYTES = 2 * 1024 * 1024
_TTL = 25
_STALE = 900
PLAYBACK_PROVIDERS = {"auto", "realdebrid", "plex"}
PLAYBACK_PROVIDER_KEY = "preferred_playback_provider"


class DebridLibraryError(ValueError):
    def __init__(self, message="Real-Debrid is temporarily unavailable.", status=502):
        super().__init__(message)
        self.status = status


def account_token(db, user_id):
    if not connections.private_connections_enabled():
        return ""
    return connections._read(db, user_id, "realdebrid_api_key")


def account_binding(token):
    return hashlib.sha256(token.encode()).hexdigest()


def playback_provider(db, user_id):
    saved = connections._read(db, user_id, PLAYBACK_PROVIDER_KEY)
    if saved in PLAYBACK_PROVIDERS:
        return saved
    if account_token(db, user_id) and not connections._read(db, user_id, "plex_token"):
        return "realdebrid"
    return "auto"


def _text(text, limit=300):
    if not isinstance(text, str):
        return ""
    return "".join(char for char in text if char.isprintable()).strip()[:limit]


def _size(value):
    return value if type(value) is int and 0 < value < 2**63 else 0


def _video(name):
    return PurePosixPath(name).suffix.lower() in _VIDEO


@dataclass(frozen=True)
class CloudEntry:
    id: str
    title: str
    note: str
    folder: bool = False
    playable: bool = False

    def payload(self):
        return {
            "id": self.id,
            "title": self.title,
            "note": self.note,
            "watch_path": f"/plus/watch/cloud/{self.id}" if self.playable else None,
            "is_folder": self.folder,
        }


@dataclass(frozen=True)
class CloudSource:
    title: str
    identity: str
    media_version: str
    duration: float
    account: str
    playlist: str = field(repr=False)
    playlist_alt: str = field(default="", repr=False)
    direct: str = field(default="", repr=False)


class DebridLibrary:
    def __init__(self, transport=None, clock=time.monotonic):
        self.transport = transport
        self.clock = clock
        self.cache = OrderedDict()
        self.last_good = OrderedDict()
        self.lock = asyncio.Lock()
        self.paused = {}

    async def _api(self, token, path, *, params=None, data=None):
        if not token:
            raise DebridLibraryError("Connect Real-Debrid in Settings to browse your files.", 403)
        # Real-Debrid reports a hint of transient 5xx load. One bounded retry keeps
        # ordinary playback from surfacing a provider hiccup as an app failure.
        for attempt in range(2):
            try:
                return await self._api_once(token, path, params=params, data=data)
            except DebridLibraryError as error:
                if attempt or error.status not in {502, 503, 504}:
                    raise
                await asyncio.sleep(0.4)

    async def _api_once(self, token, path, *, params=None, data=None):
        now = self.clock()
        self.paused = {key: deadline for key, deadline in self.paused.items() if deadline > now}
        binding = account_binding(token)
        if binding in self.paused:
            raise DebridLibraryError("Real-Debrid is rate limited. Try again shortly.", 429)
        try:
            async with (
                httpx.AsyncClient(
                    transport=self.transport, timeout=8, follow_redirects=False, trust_env=False
                ) as client,
                client.stream(
                    "POST" if data is not None else "GET",
                    RD_API + "/" + path,
                    params=params,
                    data=data,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                ) as response,
            ):
                if response.status_code == 429:
                    delay = response.headers.get("retry-after", "")
                    self.paused[binding] = (
                        now + max(60, min(int(delay), 86400))
                        if delay.isdigit() and len(delay) <= 5
                        else now + 60
                    )
                    raise DebridLibraryError("Real-Debrid is rate limited. Try again shortly.", 429)
                if response.status_code in {401, 403}:
                    raise DebridLibraryError(
                        "Real-Debrid denied access to this account or file.", 403
                    )
                if response.status_code in {404, 451}:
                    raise DebridLibraryError(
                        "This file is unavailable from Real-Debrid.", response.status_code
                    )
                if response.status_code == 204 and path in {"torrents", "downloads"}:
                    return [], response.headers.get("x-total-count")
                if response.status_code != 200:
                    raise DebridLibraryError(status=503 if response.status_code == 503 else 502)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _RESPONSE_BYTES:
                        raise DebridLibraryError()
                return json.loads(body), response.headers.get("x-total-count")
        except (httpx.HTTPError, UnicodeError, json.JSONDecodeError):
            raise DebridLibraryError() from None

    async def _all(self, token, kind):
        entries = {}
        expected = None
        for offset in range(0, _MAX_ACCOUNT_ENTRIES, _PAGE):
            rows, total = await self._api(
                token, kind, params={"limit": _PAGE, "page": offset // _PAGE + 1}
            )
            if not isinstance(rows, list) or len(rows) > _PAGE:
                raise DebridLibraryError()
            if total is not None:
                if not re.fullmatch(r"[0-9]{1,10}", total):
                    raise DebridLibraryError()
                total = int(total)
                if total > _MAX_ACCOUNT_ENTRIES:
                    raise DebridLibraryError(
                        "This account inventory exceeds the current browse limit.", 422
                    )
                if expected is None:
                    expected = total
                if total != expected or len(rows) != min(_PAGE, max(0, total - offset)):
                    raise DebridLibraryError(
                        "The account changed while loading. Refresh to try again.", 409
                    )
            elif expected is not None and len(rows) != min(_PAGE, max(0, expected - offset)):
                raise DebridLibraryError(
                    "The account changed while loading. Refresh to try again.", 409
                )
            for row in rows:
                if not isinstance(row, dict):
                    raise DebridLibraryError()
                key = row.get("id")
                if not isinstance(key, str) or not re.fullmatch(_ID, key) or key in entries:
                    raise DebridLibraryError(
                        "The account changed while loading. Refresh to try again.", 409
                    )
                entries[key] = row
            if len(rows) < _PAGE or len(entries) == expected:
                return entries
        raise DebridLibraryError("This account inventory exceeds the current browse limit.", 422)

    async def inventory(self, token):
        binding = account_binding(token)
        async with self.lock:
            cached = self.cache.get(binding)
            if cached is not None and cached[0] > self.clock():
                self.cache.move_to_end(binding)
                return cached[1:]
            # Opening a stream can add a download-history row. A browse that races
            # that change restarts from a clean snapshot instead of failing.
            failure = None
            for attempt in range(3):
                try:
                    async with asyncio.timeout(12):
                        result = await asyncio.gather(
                            self._all(token, "torrents"),
                            self._all(token, "downloads"),
                            return_exceptions=True,
                        )
                except TimeoutError:
                    failure = DebridLibraryError()
                    result = ()
                for entries in result:
                    if isinstance(entries, BaseException):
                        if getattr(entries, "status", None) != 409:
                            raise entries
                        failure = entries
                        break
                else:
                    result = tuple(result)
                    self.cache[binding] = (self.clock() + _TTL, *result)
                    self.cache.move_to_end(binding)
                    if len(self.cache) > 16:
                        self.cache.popitem(last=False)
                    self.last_good[binding] = (self.clock(), *result)
                    self.last_good.move_to_end(binding)
                    if len(self.last_good) > 16:
                        self.last_good.popitem(last=False)
                    return result
                if attempt < 2:
                    await asyncio.sleep(0.25 * (attempt + 1))
            # A recently read inventory is safer than blanking a section during a
            # transient provider hiccup. Keep the fallback bounded.
            if cached is not None and self.clock() - (cached[0] - _TTL) <= _STALE:
                self.cache.move_to_end(binding)
                return cached[1:]
            last_good = self.last_good.get(binding)
            if last_good is not None and self.clock() - last_good[0] <= _STALE:
                self.last_good.move_to_end(binding)
                return last_good[1:]
            raise failure or DebridLibraryError(
                "The account changed while loading. Refresh to try again.", 409
            )

    async def entries(self, token, *, query=""):
        torrents, downloads = await self.inventory(token)
        rows = []
        for key, data in torrents.items():
            state = data.get("status")
            note = (
                {
                    "downloaded": "Files",
                    "downloading": "Downloading",
                    "queued": "Queued",
                    "waiting_files_selection": "Waiting for file selection",
                }.get(state, "Unavailable")
                if isinstance(state, str)
                else "Unavailable"
            )
            rows.append(
                (
                    _text(data.get("added")),
                    CloudEntry(
                        "t-" + key, _text(data.get("filename")) or "Torrent", note, folder=True
                    ),
                )
            )
        for key, data in downloads.items():
            title = _text(data.get("filename")) or "Download"
            rows.append(
                (
                    _text(data.get("generated")),
                    CloudEntry(
                        "d-" + key, title, "Download history", playable=data.get("streamable") == 1
                    ),
                )
            )
        rows.sort(key=lambda pair: (pair[0], pair[1].id), reverse=True)
        folded = query.casefold()
        return [entry for _, entry in rows if folded in entry.title.casefold()]

    async def _torrent(self, token, torrent_id):
        result, _ = await self._api(token, "torrents/info/" + torrent_id)
        if not isinstance(result, dict) or result.get("id") != torrent_id:
            raise DebridLibraryError()
        files, links = result.get("files"), result.get("links")
        if not isinstance(files, list) or not isinstance(links, list) or len(files) > 3000:
            raise DebridLibraryError()
        by_id = {}
        for file in files:
            if not isinstance(file, dict):
                raise DebridLibraryError()
            number = file.get("id")
            if type(number) is not int or not 1 <= number < 10**10 or number in by_id:
                raise DebridLibraryError()
            by_id[number] = file
        selected = [number for number, file in by_id.items() if file.get("selected") == 1]
        ready = result.get("status") == "downloaded" and len(selected) == len(links)
        sources = dict(zip(selected, links, strict=True)) if ready else {}
        return by_id, sources, _text(result.get("hash"), 64)

    async def files(self, token, entry_id):
        if not _ENTRY.fullmatch(entry_id) or not entry_id.startswith("t-"):
            raise DebridLibraryError("This folder is unavailable.", 404)
        files, sources, _ = await self._torrent(token, entry_id[2:])
        rows = []
        for number, file in files.items():
            title = _text(PurePosixPath(_text(file.get("path"), 1000)).name) or "File"
            playable = number in sources and _video(title) and bool(_size(file.get("bytes")))
            note = "Video" if playable else "Unavailable" if _video(title) else "File"
            rows.append(CloudEntry(f"{entry_id}-{number}", title, note, playable=playable))
        return sorted(rows, key=lambda row: (row.title.casefold(), row.id))

    async def source(self, token, key):
        if not _FILE.fullmatch(key):
            raise DebridLibraryError("This file is unavailable.", 404)
        if key.startswith("t-"):
            torrent_id, _, number = key[2:].rpartition("-")
            return await self.torrent_source(token, torrent_id, int(number))
        else:
            _, downloads = await self.inventory(token)
            item = downloads.get(key[2:], {})
            name = _text(item.get("filename"))
            size = _size(item.get("filesize"))
            content_hash = _text(item.get("generated"), 64)
        return await self._source(token, key, item, name, size, content_hash)

    @staticmethod
    def _download_match(rows, link, name, size):
        if not isinstance(rows, list):
            return None
        return next(
            (
                row
                for row in rows
                if isinstance(row, dict)
                and row.get("link") == link
                and row.get("filename") == name
                and row.get("filesize") == size
                and row.get("streamable") == 1
            ),
            None,
        )

    async def _recent_downloads(self, token, *, pages=2):
        rows = []
        for page in range(1, pages + 1):
            batch, _ = await self._api(token, "downloads", params={"limit": 100, "page": page})
            if not isinstance(batch, list):
                break
            rows.extend(batch)
            if len(batch) < 100:
                break
        return rows

    async def _registered_download(self, token, link, name, size, *, attempts=4):
        # /unrestrict/link can answer with a composite id that the /streaming
        # endpoints reject. The canonical /downloads id appears within a moment.
        for attempt in range(attempts):
            item = self._download_match(await self._recent_downloads(token), link, name, size)
            if item is not None:
                return item
            if attempt + 1 < attempts:
                await asyncio.sleep(0.35 * (attempt + 1))
        return None

    async def torrent_source(self, token, torrent_id, number, *, inventory=None):
        if not re.fullmatch(_ID, torrent_id) or type(number) is not int or not 1 <= number < 10**10:
            raise DebridLibraryError("This file is unavailable.", 404)
        files, links, content_hash = inventory or await self._torrent(token, torrent_id)
        file = files.get(number, {})
        name = PurePosixPath(_text(file.get("path"), 1000).replace("\\", "/")).name
        size = _size(file.get("bytes"))
        link = links.get(number)
        if not size or not _video(name) or not isinstance(link, str) or len(link) > 4096:
            raise DebridLibraryError("This file is unavailable.", 404)
        item = self._download_match(await self._recent_downloads(token), link, name, size)
        if item is not None:
            try:
                return await self._source(
                    token, f"t-{torrent_id}-{number}", item, name, size, content_hash
                )
            except DebridLibraryError as error:
                if error.status != 404:
                    raise
        item, _ = await self._api(token, "unrestrict/link", data={"link": link})
        if (
            not isinstance(item, dict)
            or item.get("filename") != name
            or _size(item.get("filesize")) != size
        ):
            raise DebridLibraryError("Real-Debrid returned a different file.", 409)
        registered = await self._registered_download(token, link, name, size)
        if registered is None:
            raise DebridLibraryError(
                "Real-Debrid is still preparing this file. Try again shortly.", 503
            )
        return await self._source(
            token, f"t-{torrent_id}-{number}", registered, name, size, content_hash
        )

    async def _source(self, token, key, item, name, size, content_hash):
        download_id = item.get("id")
        if (
            item.get("streamable") != 1
            or not name
            or not size
            or not isinstance(download_id, str)
            or not re.fullmatch(_ID, download_id)
        ):
            raise DebridLibraryError("Real-Debrid cannot stream this file.", 415)
        async with asyncio.TaskGroup() as requests:
            # A wrapper keeps expected provider errors as plain exceptions for route callers.
            info_task = requests.create_task(
                self._api_result(token, "streaming/mediaInfos/" + download_id)
            )
            stream_task = requests.create_task(
                self._api_result(token, "streaming/transcode/" + download_id)
            )
        info, streams = info_task.result(), stream_task.result()
        for result in (info, streams):
            if isinstance(result, DebridLibraryError):
                raise result
        duration = info.get("duration") if isinstance(info, dict) else None
        apple = streams.get("apple") if isinstance(streams, dict) else None
        original = apple.get("original") if isinstance(apple, dict) else None
        playlist = (
            (apple.get("high") or apple.get("medium") or original)
            if isinstance(apple, dict)
            else None
        )
        playlist_alt = (
            original
            if isinstance(original, str)
            and isinstance(playlist, str)
            and original != playlist
            and len(original) <= 4096
            else ""
        )
        if (
            type(duration) not in (float, int)
            or not math.isfinite(duration)
            or not 1 <= duration <= 43200
            or info.get("size") not in (None, 0, size)
            or not isinstance(playlist, str)
        ):
            raise DebridLibraryError("This file has no compatible Real-Debrid stream.", 415)
        account = account_binding(token)
        identity = hashlib.sha256(f"{account}\0{key}".encode()).hexdigest()
        version = hashlib.sha256(f"{identity}\0{content_hash}\0{name}\0{size}".encode()).hexdigest()
        direct = item.get("download")
        if not isinstance(direct, str) or not re.fullmatch(
            r"https://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.real-debrid\.com/[^\s]{1,2000}",
            direct,
        ):
            direct = ""
        return CloudSource(
            _text(name, 120), identity, version, duration, account, playlist, playlist_alt, direct
        )

    async def _api_result(self, token, path):
        try:
            for delay in (0.75, 1.5):
                try:
                    value, _ = await self._api(token, path)
                    return value
                except DebridLibraryError as error:
                    if error.status != 503:
                        raise
                    await asyncio.sleep(delay)
            value, _ = await self._api(token, path)
            return value
        except DebridLibraryError as error:
            return error
