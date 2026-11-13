from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from omakase.plus import torrent_metadata as metadata


def encode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    return b"d" + b"".join(encode(key) + encode(item) for key, item in sorted(value.items())) + b"e"


def packet(files=None):
    files = files if files is not None else [b"Show S01E01.mkv", b"Show S01E02.mkv"]
    info = {
        b"name": b"Show Season 1 [1080p]",
        b"files": [{b"path": [name], b"length": 100} for name in files],
    }
    return encode({b"info": info}), hashlib.sha1(encode(info)).hexdigest().upper()


def parse(data, content_hash):
    return metadata.planned_coverage(
        data,
        torrent_hash=content_hash,
        target_title="Show Season 1",
        target_aliases=("Show",),
        season=1,
        expected=2,
        anilist_id=999,
    )


def test_hash_bound_actual_file_list_proves_only_named_episodes_and_skips_extras():
    data, content_hash = packet(
        [b"Show S01E01.mkv", b"Show S01E02.mkv", b"Show NCOP.mkv", b"Show S01E03.srt"]
    )
    assert parse(data, content_hash) == frozenset({1, 2})


@pytest.mark.parametrize(
    "files",
    [
        [b"Show S02E01.mkv"],
        [b"Unrelated Series S01E01.mkv"],
        [b"Show S01E01.mkv", b"Show S01E01.mp4"],
        [b"Show Season 1.mkv"],
        [b"../Show S01E01.mkv"],
        [b"Show S01E03.mkv"],
    ],
)
def test_metadata_rejects_foreign_seasons_identity_duplicates_ambiguity_and_paths(files):
    data, content_hash = packet(files)
    with pytest.raises(metadata.MetadataRejected):
        parse(data, content_hash)


def test_metadata_rejects_changed_hash_and_empty_media():
    data, content_hash = packet()
    with pytest.raises(metadata.MetadataRejected, match="HASH"):
        parse(data, "A" * 40)
    info = {b"name": b"Show Season 1", b"files": [{b"path": [b"Show S01E01.mkv"], b"length": 0}]}
    with pytest.raises(metadata.MetadataRejected, match="EMPTY_MEDIA"):
        parse(encode({b"info": info}), hashlib.sha1(encode(info)).hexdigest())


@pytest.mark.parametrize(
    "data", [b"", b"d4:infoi1eejunk", b"d4:infoi1e4:infoi2ee", b"l" * 30 + b"e" * 30]
)
def test_metadata_decoder_rejects_malformed_duplicate_keys_and_depth(data):
    with pytest.raises(metadata.MetadataRejected):
        parse(data, "A" * 40)


def test_metadata_decoder_and_file_counts_are_bounded(monkeypatch):
    data, content_hash = packet()
    monkeypatch.setattr(metadata, "MAX_NODES", 4)
    with pytest.raises(metadata.MetadataRejected):
        parse(data, content_hash)
    monkeypatch.setattr(metadata, "MAX_NODES", 10000)
    monkeypatch.setattr(metadata, "MAX_FILES", 1)
    with pytest.raises(metadata.MetadataRejected):
        parse(data, content_hash)


@pytest.mark.parametrize(
    "url",
    [
        "http://nyaa.si/download/1.torrent",
        "https://nyaa.si.evil/download/1.torrent",
        "https://127.0.0.1/download/1.torrent",
        "https://nyaa.si/download/1.torrent?secret=private",
        "https://nyaa.si/view/1",
        "https://[bad",
    ],
)
def test_only_exact_public_nyaa_download_origin_is_allowed(url):
    assert not metadata.download_url_allowed(url)


@pytest.mark.parametrize("response_kind", ["success", "redirect", "oversize"])
def test_bounded_metadata_http_never_follows_redirects_or_promotes_failure(
    monkeypatch, response_kind
):
    data, content_hash = packet()
    calls = []

    def request(req):
        calls.append(req)
        if response_kind == "redirect":
            return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
        return httpx.Response(
            200, content=data if response_kind == "success" else b"x" * (metadata.MAX_BYTES + 1)
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        metadata.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(request), **kwargs),
    )
    result = asyncio.run(
        metadata.fetch_planned_coverage(
            "https://nyaa.si/download/1.torrent",
            torrent_hash=content_hash,
            target_title="Show Season 1",
            target_aliases=("Show",),
            season=1,
            expected=2,
            anilist_id=999,
        )
    )
    assert result == (frozenset({1, 2}) if response_kind == "success" else frozenset())
    assert len(calls) == 1


def test_nested_foreign_season_and_symlink_metadata_are_rejected():
    for file in [
        {b"path": [b"Season 2", b"Show S01E01.mkv"], b"length": 100},
        {b"path": [b"Show S01E01.mkv"], b"length": 100, b"attr": b"l"},
    ]:
        info = {b"name": b"Show Season 1", b"files": [file]}
        data = encode({b"info": info})
        with pytest.raises(metadata.MetadataRejected):
            parse(data, hashlib.sha1(encode(info)).hexdigest())
