"""Offline gate-helper contracts; native production pipeline is a separate gate."""

import asyncio
import copy
import hashlib
import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def helper():
    path = Path(__file__).resolve().parents[2] / "scripts/qualify_web_player_pipeline.py"
    spec = importlib.util.spec_from_file_location("pipeline_qualification", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def descriptor():
    return {
        "schema": "omakase-player-pipeline-v1",
        "bytes": 249_000_000,
        "sha256": "a" * 64,
        "duration_ms": 1_800_233,
        "video": {"index": 0, "codec": "hevc", "language": "und", "width": 1920, "height": 1080},
        "audio": [
            {"index": 1, "codec": "opus", "language": "jpn", "default": True},
            {"index": 2, "codec": "opus", "language": "eng"},
        ],
        "subtitles": [
            {"index": 3, "codec": "ass", "language": "eng"},
            {"index": 4, "codec": "ass", "language": "jpn"},
        ],
        "fonts": [{"index": index, "bytes": 100_000, "sha256": "b" * 64} for index in range(5, 16)],
        "min_audio_packets": 176_000,
    }


def test_representative_descriptor_contract(helper, descriptor):
    assert helper.validate_fixture(descriptor) is descriptor


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(url="https://untrusted.invalid"),
        lambda d: d.update(bytes=256 * 1024**2 + 1),
        lambda d: d.update(bytes=214_999_999),
        lambda d: d.update(sha256="secret"),
        lambda d: d.update(min_audio_packets=175_999),
        lambda d: d["video"].update(width=1280),
        lambda d: d["video"].update(codec="h264"),
        lambda d: d["audio"][0].update(codec="aac"),
        lambda d: d["audio"][1].update(index=1),
        lambda d: d["subtitles"][0].update(codec="webvtt"),
        lambda d: d["fonts"][0].update(bytes=8 * 1024**2 + 1),
        lambda d: d["fonts"].pop(),
    ],
)
def test_descriptor_rejects_unrepresentative_or_unbounded_inputs(helper, descriptor, mutation):
    value = copy.deepcopy(descriptor)
    mutation(value)
    with pytest.raises(ValueError):
        helper.validate_fixture(value)


def test_adapter_validates_actual_target_before_mapping_bytes(helper, descriptor):
    from omakase.plus.language_source import digest

    calls = []
    counts = {"validated_publication_adapter": 0}

    def original(*args):
        calls.append(args)
        return "real/validated/path.mkv", 215_638_680, "original-identity"

    wrapped = helper.publication_adapter(original, descriptor, counts)
    assert wrapped("db", "scope", 100, 1, 2) == (
        "real/validated/path.mkv",
        descriptor["bytes"],
        digest(["synthetic-pipeline-v1", "original-identity", descriptor["sha256"]]),
    )
    assert calls == [("db", "scope", 100, 1, 2)]
    assert counts["validated_publication_adapter"] == 1

    def denied(*args):
        raise ValueError("revoked")

    with pytest.raises(ValueError):
        helper.publication_adapter(denied, descriptor, counts)("db", "scope", 100, 1, 2)
    assert counts["validated_publication_adapter"] == 1


def test_fixture_body_is_bounded_exact_and_stall_is_cancellable(helper, tmp_path):
    data = bytes(range(256)) * 1000
    path = tmp_path / "synthetic.mkv"
    path.write_bytes(data)

    async def scenario():
        counts = {"bytes": 0}
        chunks = [chunk async for chunk in helper.FixtureStream(path, len(data), counts).chunks()]
        assert max(map(len, chunks)) <= 65536
        assert counts["bytes"] == len(data)
        assert hashlib.sha256(b"".join(chunks)).digest() == hashlib.sha256(data).digest()
        stalled, counts = asyncio.Event(), {"bytes": 0}

        async def consume():
            async for _ in helper.FixtureStream(path, len(data), counts, stall=stalled).chunks():
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(stalled.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert counts["bytes"] == 65536

    asyncio.run(scenario())


def test_worker_absence_includes_watchdog_zombies(helper):
    worker = SimpleNamespace(pid=200, returncode=0)
    snapshots = [
        [{"pid": 201, "pgrp": 200, "state": "Z"}],
        [{"pid": 201, "pgrp": 200, "state": "S"}],
        [],
    ]
    asyncio.run(helper.workers_absent([worker], read_rows=lambda: snapshots.pop(0)))
    with pytest.raises(ValueError):
        asyncio.run(
            helper.workers_absent(
                [worker], read_rows=lambda: [{"pid": 201, "pgrp": 200, "state": "Z"}]
            )
        )


def test_request_boundary_clears_cross_request_evidence_only(helper):
    resolved = SimpleNamespace(_publication_cache={"old": "prior-request"})
    counts = {"http_request_boundaries": 0, "request_publication_cache_clears": 0}
    calls = []

    async def app(scope, receive, send):
        assert resolved._publication_cache == {}
        resolved._publication_cache["current"] = len(calls)
        calls.append(1)
        await asyncio.sleep(0)
        assert resolved._publication_cache == {"current": len(calls) - 1}

    boundary = helper.RequestPublicationBoundary(
        app, sources=[(resolved, b"synthetic-metadata")], counts=counts
    )

    async def scenario():
        await boundary({"type": "http"}, None, None)
        assert resolved._publication_cache == {"current": 0}
        await boundary({"type": "http"}, None, None)
        assert resolved._publication_cache == {"current": 1}

    asyncio.run(scenario())
    assert counts == {"http_request_boundaries": 2, "request_publication_cache_clears": 2}


def test_request_boundary_does_not_clear_inside_request_or_nonhttp_scope(helper):
    resolved = SimpleNamespace(_publication_cache={"valid": "evidence"})
    counts = {"http_request_boundaries": 0, "request_publication_cache_clears": 0}

    async def app(scope, receive, send):
        assert resolved._publication_cache == {"valid": "evidence"}

    boundary = helper.RequestPublicationBoundary(app, sources=[(resolved, b"")], counts=counts)
    asyncio.run(boundary({"type": "lifespan"}, None, None))
    assert counts == {"http_request_boundaries": 0, "request_publication_cache_clears": 0}


def test_private_metadata_clone_enforces_foreign_keys_after_backup(helper):
    original = sqlite3.connect(":memory:")
    original.executescript(
        "CREATE TABLE parents(id PRIMARY KEY); "
        "CREATE TABLE children(parent REFERENCES parents(id)); "
        "INSERT INTO parents VALUES(1);"
    )
    clone = helper.metadata_clone(original)
    try:
        assert clone.execute("SELECT id FROM parents").fetchone()[0] == 1
        assert clone.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert original.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            clone.execute("INSERT INTO children VALUES(2)")
    finally:
        clone.close()
        original.close()
