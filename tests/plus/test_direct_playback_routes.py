"""Original-file playback through the real Apple TV library routes.

The unit checks in ``test_direct_stream`` prove the transport against a hostile
provider. These prove the wiring: a client that asks for the original file gets a
same-origin range URL and exact bytes, a client that does not ask keeps the reply
it already understands, and nothing silently falls back to a generated timeline.
"""

import pathlib

import httpx
import pytest
from fastapi.testclient import TestClient

from omakase.plus import connections
from tests.plus.test_native_tv_test import API, COOKIE, DEVICES_PAGE, LIBRARY_API, ORIGIN, browser
from tests.plus.test_native_tv_test import system as _native_system  # noqa: F401


@pytest.fixture
def system(_native_system):  # noqa: F811 - pytest resolves the aliased fixture
    return _native_system


SIZE = 4096
BODY = bytes((index * 11 + 3) % 251 for index in range(SIZE))
MEDIA_HOST = "https://files.real-debrid.com/d/provider-secret"


def manifest(seconds=1440.0, length=6.0):
    """A provider transcode playlist whose declared length matches the duration."""
    count = int(seconds / length)
    rows = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:7",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for index in range(count):
        rows.extend((f"#EXTINF:{length:.3f},", f"s{index:05d}.ts?t=1"))
    rows.append("#EXT-X-ENDLIST")
    return "\n".join(rows) + "\n"


class Stream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


class Provider:
    """Real-Debrid stand-in that also serves the original file with ranges."""

    def __init__(self, *, direct=MEDIA_HOST, size=SIZE):
        self.direct = direct
        self.size = size
        self.calls = []
        self.file_requests = []

    def __call__(self, request):
        host = request.url.host
        self.calls.append((host, request.url.path))
        if host.endswith(".real-debrid.com") and host != "api.real-debrid.com":
            self.file_requests.append(request)
            return self._file(request)
        assert host == "api.real-debrid.com", host
        path = request.url.path.removeprefix("/rest/1.0/")
        if path == "torrents":
            return httpx.Response(200, json=[])
        if path == "downloads":
            row = {
                "id": "download1",
                "filename": "Original.mkv",
                "filesize": self.size,
                "streamable": 1,
                "download": self.direct,
            }
            return httpx.Response(200, json=[row], headers={"X-Total-Count": "1"})
        if path.startswith("streaming/mediaInfos/"):
            return httpx.Response(200, json={"duration": 1440, "size": self.size})
        if path.startswith("streaming/transcode/"):
            return httpx.Response(
                200, json={"apple": {"high": "https://3.stream.real-debrid.com/v.m3u8"}}
            )
        raise AssertionError(path)

    def _file(self, request):
        if request.url.path.endswith(".m3u8"):
            return httpx.Response(200, stream=Stream(manifest().encode()))
        requested = request.headers.get("range")
        if not requested:
            return httpx.Response(200, stream=Stream(BODY), headers={"content-length": str(SIZE)})
        left, right = requested.removeprefix("bytes=").split("-")
        left = int(left)
        right = min(int(right) if right else SIZE - 1, SIZE - 1)
        piece = BODY[left : right + 1]
        return httpx.Response(
            206,
            stream=Stream(piece),
            headers={
                "content-range": f"bytes {left}-{right}/{SIZE}",
                "content-length": str(len(piece)),
            },
        )


def paired(system, monkeypatch, provider):
    app, db, sessions, options = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    monkeypatch.setenv("OMAKASE_PLUS_REVIEW_MODE", "true")
    monkeypatch.setattr(
        connections,
        "_read",
        lambda _, actor, key: (
            {
                "realdebrid_api_key": "owner-token",
                "preferred_playback_provider": "realdebrid",
            }.get(key, "")
            if actor == 1
            else ""
        ),
    )
    options.debrid = provider
    owner, csrf = browser(app, sessions[1])
    native = TestClient(app, base_url=ORIGIN)
    pairing = native.post(LIBRARY_API + "/device").json()
    owner.post(
        DEVICES_PAGE,
        data={"csrf_token": csrf, "action": "approve", "device_code": pairing["user_code"]},
    )
    native.headers["Authorization"] = "Bearer " + pairing["device_token"]
    return native


def test_direct_engine_serves_the_original_file_with_exact_ranges(system, monkeypatch):
    provider = Provider()
    native = paired(system, monkeypatch, provider)
    reply = native.post(
        LIBRARY_API + "/play",
        json={"watch_path": "/plus/watch/cloud/d-download1", "engine": "direct"},
    )
    assert reply.status_code == 200, reply.text
    media = reply.json()
    assert media["media_format"] == "direct"
    assert "media_playlist" not in media
    assert media["direct"] == {"bytes": SIZE, "container": "mkv", "etag": media["direct"]["etag"]}
    assert media["duration_seconds"] == 1440
    url = media["media_url"]
    assert url == f"{API}/direct/{media['media_id']}/{media['media_token']}"

    first = native.get(
        url, headers={"Cookie": f"{COOKIE}={media['media_token']}", "Range": "bytes=0-99"}
    )
    assert first.status_code == 206, first.text
    assert first.headers["content-range"] == f"bytes 0-99/{SIZE}"
    assert first.headers["accept-ranges"] == "bytes"
    assert first.headers["content-type"] == "video/x-matroska"
    assert first.content == BODY[:100]

    # A distant seek asks the provider for a different range instead of walking
    # the file, and returns exactly those bytes.
    late = native.get(
        url,
        headers={
            "Cookie": f"{COOKIE}={media['media_token']}",
            "Range": f"bytes={SIZE - 50}-{SIZE - 1}",
        },
    )
    assert late.status_code == 206, late.text
    assert late.content == BODY[-50:]
    assert len(provider.file_requests) == 2
    assert len(provider.file_requests) == 2

    # HEAD costs no provider traffic: the size is already pinned.
    head = native.head(url, headers={"Cookie": f"{COOKIE}={media['media_token']}"})
    assert head.status_code == 200
    assert head.headers["content-length"] == str(SIZE)
    assert len(provider.file_requests) == 2


def test_an_existing_client_still_gets_the_reply_it_understands(system, monkeypatch):
    provider = Provider()
    native = paired(system, monkeypatch, provider)
    reply = native.post(LIBRARY_API + "/play", json={"watch_path": "/plus/watch/cloud/d-download1"})
    assert reply.status_code == 200, reply.text
    media = reply.json()
    assert media["media_format"] == "hls"
    assert media["media_playlist"] == "vod"
    assert "media_url" not in media


def test_a_copy_without_an_original_file_fails_instead_of_changing_modes(system, monkeypatch):
    provider = Provider(direct="")
    native = paired(system, monkeypatch, provider)
    reply = native.post(
        LIBRARY_API + "/play",
        json={"watch_path": "/plus/watch/cloud/d-download1", "engine": "direct"},
    )
    assert reply.status_code == 415, reply.text
    assert "media_format" not in reply.text


def test_revoking_the_pass_stops_the_range_endpoint(system, monkeypatch):
    provider = Provider()
    native = paired(system, monkeypatch, provider)
    media = native.post(
        LIBRARY_API + "/play",
        json={"watch_path": "/plus/watch/cloud/d-download1", "engine": "direct"},
    ).json()
    cookie = {"Cookie": f"{COOKIE}={media['media_token']}"}
    assert native.get(media["media_url"], headers=cookie).status_code == 200
    assert native.post(f"{LIBRARY_API}/media/{media['media_id']}/close").status_code == 200
    assert native.get(media["media_url"], headers=cookie).status_code == 404
    # Revocation is answered from the pass table: the provider is not contacted.
    assert len(provider.file_requests) == 1


def test_the_signed_url_is_the_only_capability_and_it_is_checked(system, monkeypatch):
    """A header-less player holds the pass token in the URL, so the URL is checked."""
    provider = Provider()
    native = paired(system, monkeypatch, provider)
    media = native.post(
        LIBRARY_API + "/play",
        json={"watch_path": "/plus/watch/cloud/d-download1", "engine": "direct"},
    ).json()
    good = media["media_url"]
    assert native.get(good, headers={"Range": "bytes=0-9"}).status_code == 206

    wrong = good[: -len(media["media_token"])] + "z" * 43
    assert native.get(wrong, headers={"Range": "bytes=0-9"}).status_code == 404

    other = native.post(
        LIBRARY_API + "/play",
        json={"watch_path": "/plus/watch/cloud/d-download1", "engine": "direct"},
    ).json()
    # Issuing a new pass supersedes the previous one for the same device.
    assert native.get(good, headers={"Range": "bytes=0-9"}).status_code == 404
    assert native.get(other["media_url"], headers={"Range": "bytes=0-9"}).status_code == 206


def data_dir(db):
    for row in db.execute("PRAGMA database_list"):
        if row[1] == "main":
            return pathlib.Path(row[2]).parent
    raise AssertionError("no main database")


def test_a_full_playthrough_adds_no_media_files(system, monkeypatch):
    """The storage contract: playback leaves no local video behind."""
    app, db, sessions, options = system
    provider = Provider()
    native = paired(system, monkeypatch, provider)
    root = data_dir(db)

    def media_files():
        return sorted(
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file() and path.suffix not in {".db", ".db-wal", ".db-shm", ""}
        )

    before = media_files()
    media = native.post(
        LIBRARY_API + "/play",
        json={"watch_path": "/plus/watch/cloud/d-download1", "engine": "direct"},
    ).json()
    cookie = {"Cookie": f"{COOKIE}={media['media_token']}"}
    # Walk the title the way a player does, then seek far ahead and back.
    for left in range(0, SIZE, 512):
        right = min(left + 511, SIZE - 1)
        response = native.get(
            media["media_url"], headers=cookie | {"Range": f"bytes={left}-{right}"}
        )
        assert response.status_code == 206
        assert response.content == BODY[left : right + 1]
    assert media_files() == before
    assert not any((root / "prepared").exists() for _ in [0])
    # Every byte came from the provider's own range support, in order.
    assert len(provider.file_requests) == SIZE // 512
