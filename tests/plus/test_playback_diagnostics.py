"""Playback diagnostics expose classified failures and keep source data private."""

import asyncio
import logging

from omakase.plus.debrid_subs import SubtitleCache, SubtitleJob

SYNTHETIC_FAILURE_MESSAGE = "https://fixture.invalid/signed/private-token"


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
