"""Playback diagnostics expose classified failures and keep source data private."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from omakase.plus.debrid_seek import SeekStreams
from omakase.plus.debrid_subs import SubtitleCache, SubtitleJob

SYNTHETIC_FAILURE_MESSAGE = "https://fixture.invalid/signed/private-token"


def test_seek_warm_failure_is_classified_and_sanitized(monkeypatch, caplog, tmp_path):
    async def fail(*args):
        raise OSError(SYNTHETIC_FAILURE_MESSAGE)

    streams = SeekStreams(tmp_path)
    monkeypatch.setattr(streams, "segment", fail)
    plan = SimpleNamespace(
        index=SimpleNamespace(container="ts"), warm_position=None, diagnostic_id="a" * 16
    )
    with caplog.at_level(logging.WARNING):
        asyncio.run(streams._warm(plan))
    record = caplog.records[-1]
    assert (record.stage, record.code, record.delivery_mode) == (
        "seek_warm",
        "resource_unavailable",
        "seek",
    )
    assert record.request_id == plan.diagnostic_id
    assert SYNTHETIC_FAILURE_MESSAGE not in caplog.text


def test_seek_ensure_propagates_cancellation(monkeypatch, tmp_path):
    async def case():
        started = asyncio.Event()

        async def pending(*args):
            started.set()
            await asyncio.Future()

        streams = SeekStreams(tmp_path)
        monkeypatch.setattr(streams, "segment", pending)
        plan = SimpleNamespace(root=tmp_path, index=SimpleNamespace(segments=[0], suffix="ts"))
        task = asyncio.create_task(streams.ensure(plan, 0, 20))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(case())


def test_subtitle_asr_failure_is_classified_and_sanitized(monkeypatch, caplog, tmp_path):
    cache = SubtitleCache(tmp_path)

    async def ready(*args):
        return True

    def fail(*args):
        raise RuntimeError(SYNTHETIC_FAILURE_MESSAGE)

    monkeypatch.setattr(cache, "_run_ocr", ready)
    monkeypatch.setattr(cache, "_asr_sync", fail)
    job = SubtitleJob("fixture")
    with caplog.at_level(logging.WARNING):
        asyncio.run(cache._asr_locked(job, SYNTHETIC_FAILURE_MESSAGE, tmp_path))
    record = caplog.records[-1]
    assert (record.stage, record.code, record.delivery_mode) == (
        "subtitle_asr",
        "asr_failed",
        "provider",
    )
    assert record.request_id == job.diagnostic_id
    assert SYNTHETIC_FAILURE_MESSAGE not in caplog.text
