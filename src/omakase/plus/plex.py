"""Token-safe Plex API client and immutable response contracts."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx

_MAX_XML_BYTES = 2 * 1024 * 1024


class PlexError(RuntimeError):
    """A Plex request or response failed without exposing credentials."""


@dataclass(frozen=True, slots=True)
class PlexIdentity:
    machine_identifier: str
    version: str


@dataclass(frozen=True, slots=True)
class PlexSection:
    key: int
    title: str
    section_type: str
    agent: str
    scanner: str
    language: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlexActivity:
    title: str
    subtitle: str
    progress: str


@dataclass(frozen=True, slots=True)
class PlexPlaybackReceipt:
    http_status: int
    bytes_received: int
    content_range: str
    range_supported: bool


class PlexClient:
    """Small Plex surface with credentials sent only in a request header."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
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
            raise PlexError("Plex base URL is invalid")
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 512
            or any(character.isspace() for character in token)
        ):
            raise PlexError("Plex token is invalid")
        if timeout <= 0 or timeout > 120:
            raise PlexError("Plex timeout is invalid")
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Accept": "application/xml",
                "X-Plex-Token": token,
                "X-Plex-Product": "Omakase Plus",
                "X-Plex-Client-Identifier": "omakase-plus",
            },
            timeout=timeout,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"PlexClient(base_url={self._base_url!r}, token=<redacted>)"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PlexClient:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def identity(self) -> PlexIdentity:
        root = self._xml("GET", "/identity")
        machine = root.attrib.get("machineIdentifier", "").strip()
        version = root.attrib.get("version", "").strip()
        if not machine or not version:
            raise PlexError("Plex identity response is incomplete")
        return PlexIdentity(machine_identifier=machine, version=version)

    def list_sections(self) -> tuple[PlexSection, ...]:
        root = self._xml("GET", "/library/sections")
        sections: list[PlexSection] = []
        for directory in root.findall("Directory"):
            try:
                key = int(directory.attrib.get("key", ""))
            except ValueError as exc:
                raise PlexError("Plex section identity is invalid") from exc
            title = directory.attrib.get("title", "").strip()
            section_type = directory.attrib.get("type", "").strip()
            if key <= 0 or not title or section_type not in {"movie", "show"}:
                raise PlexError("Plex section response is incomplete")
            sections.append(
                PlexSection(
                    key=key,
                    title=title,
                    section_type=section_type,
                    agent=directory.attrib.get("agent", "").strip(),
                    scanner=directory.attrib.get("scanner", "").strip(),
                    language=directory.attrib.get("language", "").strip(),
                    paths=tuple(
                        location.attrib["path"]
                        for location in directory.findall("Location")
                        if location.attrib.get("path")
                    ),
                )
            )
        return tuple(sections)

    def active_scans(self) -> tuple[PlexActivity, ...]:
        root = self._xml("GET", "/activities")
        return tuple(
            PlexActivity(
                title=activity.attrib.get("title", "Plex library scan"),
                subtitle=activity.attrib.get("subtitle", ""),
                progress=activity.attrib.get("progress", ""),
            )
            for activity in root
            if activity.attrib.get("type") == "library.update.section"
        )

    def create_section(
        self,
        *,
        title: str,
        section_type: str,
        path: str,
    ) -> None:
        path = _absolute_plex_path(path)
        if section_type == "show":
            agent = "tv.plex.agents.series"
            scanner = "Plex TV Series"
        elif section_type == "movie":
            agent = "tv.plex.agents.movie"
            scanner = "Plex Movie"
        else:
            raise PlexError("Plex section type is unsupported")
        self._request(
            "POST",
            "/library/sections",
            params=[
                ("name", _section_title(title)),
                ("type", section_type),
                ("agent", agent),
                ("scanner", scanner),
                ("language", "en-US"),
                ("location", path),
            ],
        )

    def update_section_path(self, section: PlexSection, *, path: str) -> None:
        path = _absolute_plex_path(path)
        if not section.agent or not section.scanner or not section.language:
            raise PlexError("Plex section lacks safe edit metadata")
        self._request(
            "PUT",
            f"/library/sections/{section.key}",
            params=[
                ("name", section.title),
                ("agent", section.agent),
                ("scanner", section.scanner),
                ("language", section.language),
                ("location", path),
            ],
        )

    def scan_section(self, section_key: int, *, path: str | None = None) -> None:
        if isinstance(section_key, bool) or section_key <= 0:
            raise PlexError("Plex section key is invalid")
        params = {"force": "1"}
        if path is not None:
            params["path"] = _absolute_plex_path(path)
        self._request(
            "POST",
            f"/library/sections/{section_key}/refresh",
            params=params,
        )

    def playback_probe(
        self,
        media_path: str,
        *,
        range_end: int = 65_535,
    ) -> PlexPlaybackReceipt:
        if (
            not isinstance(media_path, str)
            or not media_path.startswith("/")
            or media_path.startswith("//")
            or "://" in media_path
        ):
            raise PlexError("Plex playback path is invalid")
        if range_end < 0 or range_end > 1024 * 1024 - 1:
            raise PlexError("Plex playback range is invalid")
        response = self._request(
            "GET",
            media_path,
            headers={"Range": f"bytes=0-{range_end}"},
        )
        status = response.status_code
        content_range = response.headers.get("Content-Range", "")
        return PlexPlaybackReceipt(
            http_status=status,
            bytes_received=len(response.content),
            content_range=content_range,
            range_supported=status == 206 and content_range.startswith("bytes 0-"),
        )

    def _xml(self, method: str, path: str, **kwargs) -> ElementTree.Element:
        response = self._request(method, path, **kwargs)
        if len(response.content) > _MAX_XML_BYTES:
            raise PlexError("Plex XML response exceeded the safety limit")
        try:
            return ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise PlexError("Plex returned invalid XML") from exc

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PlexError("Plex request failed") from exc
        return response


def _section_title(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise PlexError("Plex section title is invalid")
    return value.strip()


def _absolute_plex_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.endswith("/")
        or "/../" in f"{value}/"
        or "/./" in f"{value}/"
    ):
        raise PlexError("Plex library path is invalid")
    return value
