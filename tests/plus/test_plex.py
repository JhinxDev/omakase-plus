"""Token-safe Plex client and setup contract tests."""

from __future__ import annotations

from xml.etree import ElementTree

import httpx
import pytest

from omakase.plus.plex import PlexClient, PlexError, PlexSection
from omakase.plus.plex_setup import (
    PlexLibrarySpec,
    PlexSetupError,
    apply_plex_setup_plan,
    plan_plex_setup,
)


class FakePlex:
    def __init__(self):
        self.sections = [
            {
                "key": 1,
                "title": "Anime",
                "type": "show",
                "agent": "tv.plex.agents.series",
                "scanner": "Plex TV Series",
                "language": "en-US",
                "paths": ["/old/anime"],
            }
        ]
        self.requests: list[httpx.Request] = []
        self.scans: list[tuple[int, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["X-Plex-Token"] == "super-secret-token"
        assert "super-secret-token" not in str(request.url)
        path = request.url.path
        if path == "/identity":
            return httpx.Response(
                200,
                content=b'<MediaContainer machineIdentifier="machine-1" version="1.41.0"/>',
            )
        if path == "/library/sections" and request.method == "GET":
            root = ElementTree.Element("MediaContainer")
            for section in self.sections:
                directory = ElementTree.SubElement(
                    root,
                    "Directory",
                    {
                        "key": str(section["key"]),
                        "title": str(section["title"]),
                        "type": str(section["type"]),
                        "agent": str(section["agent"]),
                        "scanner": str(section["scanner"]),
                        "language": str(section["language"]),
                    },
                )
                for location in section["paths"]:
                    ElementTree.SubElement(directory, "Location", {"path": str(location)})
            return httpx.Response(200, content=ElementTree.tostring(root))
        if path == "/library/sections" and request.method == "POST":
            params = request.url.params
            self.sections.append(
                {
                    "key": max(section["key"] for section in self.sections) + 1,
                    "title": params["name"],
                    "type": params["type"],
                    "agent": params["agent"],
                    "scanner": params["scanner"],
                    "language": params["language"],
                    "paths": [params["location"]],
                }
            )
            return httpx.Response(201)
        if path.startswith("/library/sections/") and request.method == "PUT":
            key = int(path.rsplit("/", 1)[-1])
            section = next(item for item in self.sections if item["key"] == key)
            section["paths"] = [request.url.params["location"]]
            return httpx.Response(200)
        if path.endswith("/refresh") and request.method == "POST":
            key = int(path.split("/")[3])
            self.scans.append((key, request.url.params["path"]))
            return httpx.Response(200)
        if path == "/activities":
            return httpx.Response(
                200,
                content=(
                    b'<MediaContainer><Activity type="library.update.section" '
                    b'title="Scanning Anime" subtitle="Fixture" progress="42"/></MediaContainer>'
                ),
            )
        if path == "/library/parts/1/file.mkv":
            assert request.headers["Range"] == "bytes=0-65535"
            return httpx.Response(
                206,
                headers={"Content-Range": "bytes 0-9/100"},
                content=b"0123456789",
            )
        return httpx.Response(404)


@pytest.fixture
def plex():
    fake = FakePlex()
    client = PlexClient(
        "http://plex.local:32400",
        "super-secret-token",
        transport=httpx.MockTransport(fake),
    )
    yield client, fake
    client.close()


def test_client_identity_sections_activity_playback_and_redaction(plex):
    client, fake = plex

    identity = client.identity()
    sections = client.list_sections()
    scans = client.active_scans()
    playback = client.playback_probe("/library/parts/1/file.mkv")

    assert identity.machine_identifier == "machine-1"
    assert sections[0].paths == ("/old/anime",)
    assert scans[0].progress == "42"
    assert playback.http_status == 206
    assert playback.range_supported is True
    assert playback.bytes_received == 10
    assert "super-secret-token" not in repr(client)
    assert all("super-secret-token" not in str(request.url) for request in fake.requests)


def test_playback_probe_refuses_external_url_to_prevent_token_leak(plex):
    client, _fake = plex

    with pytest.raises(PlexError, match="playback path"):
        client.playback_probe("https://attacker.invalid/file.mkv")


def test_plan_and_apply_converges_existing_and_missing_libraries(plex):
    client, fake = plex
    libraries = (
        PlexLibrarySpec("Anime", "show", "/library/Anime"),
        PlexLibrarySpec("Anime Movies", "movie", "/library/Anime Movies"),
    )
    plan = plan_plex_setup(client.list_sections(), libraries=libraries)

    assert [action["kind"] for action in plan["actions"]] == ["update_path", "create"]
    receipt = apply_plex_setup_plan(
        client,
        plan=plan,
        expected_plan_hash=plan["plan_hash"],
    )

    assert receipt == {
        "plan_hash": plan["plan_hash"],
        "actions_applied": 2,
        "scans_requested": 2,
    }
    assert {item["title"]: item["paths"] for item in fake.sections} == {
        "Anime": ["/library/Anime"],
        "Anime Movies": ["/library/Anime Movies"],
    }
    assert len(fake.scans) == 2


def test_setup_plan_refuses_wrong_existing_media_type():
    sections = (
        PlexSection(
            key=1,
            title="Anime",
            section_type="movie",
            agent="agent",
            scanner="scanner",
            language="en-US",
            paths=("/library/Anime",),
        ),
    )

    with pytest.raises(PlexSetupError, match="wrong media type"):
        plan_plex_setup(
            sections,
            libraries=(PlexLibrarySpec("Anime", "show", "/library/Anime"),),
        )
