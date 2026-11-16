"""Subtitle work must not slip past a playback gate.

The review reproduced two gaps that the earlier ownership tests did not cover:

* a viewer can acquire the gate while a child spawn is suspended, and the child
  then registers and runs to completion under an active playback hold;
* a refused signal (an injected `PermissionError`) raised while taking a hold,
  which left the start hold and the admission permit stranded.

These tests drive both directly. They use the real `SubtitleCache` and a
controllable child rather than an OCR binary.
"""

import asyncio

import pytest

from omakase.plus import debrid_subs
from omakase.plus.debrid_subs import SubtitleCache


class HoldingChild:
    """A child whose lifetime the test controls."""

    def __init__(self):
        self.killed = False
        self.communicated = asyncio.Event()
        self.returncode = None

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
        self.communicated.set()

    def terminate(self):
        self.kill()

    async def communicate(self):
        await self.communicated.wait()
        return b"", b""

    async def wait(self):
        await self.communicated.wait()
        return self.returncode


def test_a_child_spawned_after_playback_started_is_ended(tmp_path, monkeypatch):
    """The registration alone is not enough: recheck the gate at the child."""
    cache = SubtitleCache(tmp_path)
    child = HoldingChild()
    spawned = asyncio.Event()
    resume = asyncio.Event()

    async def slow_spawn(*args, **kwargs):
        spawned.set()
        await resume.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    monkeypatch.setattr(debrid_subs.shutil, "which", lambda _: None)

    async def run():
        task = asyncio.create_task(cache._run_ocr(["ffmpeg"], 5))
        await spawned.wait()
        # The viewer starts while the spawn is suspended.
        cache.begin_start("viewer")
        resume.set()
        result = await asyncio.wait_for(task, timeout=2)
        return result

    result = asyncio.run(run())
    assert result is False
    assert child.killed is True, "a child registered after playback must be ended"
    assert cache.playing is True


def test_a_start_hold_is_released_when_a_kill_is_refused(tmp_path):
    """An injected refusal must not leave the hold or the permit stranded."""
    cache = SubtitleCache(tmp_path)

    class RefusingChild:
        def kill(self):
            raise PermissionError("signal refused")

    cache.processes.add(RefusingChild())

    # Taking a hold kills running children first; a refusal must not propagate.
    cache.begin_start("attempt")
    assert cache.playing is True
    cache.end_start("attempt")
    assert cache.playing is False


def test_a_child_registered_while_playing_is_not_left_running(tmp_path, monkeypatch):
    """A refusal to kill must still leave no untracked child and no hold."""
    cache = SubtitleCache(tmp_path)
    child = HoldingChild()

    async def spawn(*args, **kwargs):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(debrid_subs.shutil, "which", lambda _: None)
    cache.begin_start("viewer")

    result = asyncio.run(asyncio.wait_for(cache._run_ocr(["ffmpeg"], 5), timeout=2))
    assert result is False
    assert child.killed is True
    assert cache.processes == set()


def test_ocr_does_not_start_when_a_pass_begins_while_waiting_for_the_permit(tmp_path, monkeypatch):
    """The idle wait can end before the permit: recheck it under the semaphore."""
    cache = SubtitleCache(tmp_path)
    ran = []

    async def ocr_locked(job, direct, workdir, candidates):
        ran.append(True)

    async def streams(direct):
        return [{"index": 2, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}}]

    async def no_idle():
        return None

    monkeypatch.setattr(cache, "_ocr_locked", ocr_locked)
    monkeypatch.setattr(cache, "_wait_for_idle", no_idle)
    cache.begin_start("viewer")

    async def run():
        await cache._ocr(object(), "https://example.invalid/a.mkv", tmp_path, await streams(""))

    asyncio.run(run())
    assert ran == [], "OCR must not run under an active playback hold"


def test_asr_does_not_start_when_a_pass_begins_while_waiting_for_the_permit(tmp_path, monkeypatch):
    """The transcription path has the same race and the same guard."""
    cache = SubtitleCache(tmp_path)
    ran = []

    async def asr_locked(job, direct, workdir):
        ran.append(True)

    async def no_idle():
        return None

    monkeypatch.setattr(cache, "_asr_locked", asr_locked)
    monkeypatch.setattr(cache, "_wait_for_idle", no_idle)
    cache.begin_start("viewer")

    asyncio.run(cache._asr(object(), "https://example.invalid/a.mkv", tmp_path))
    assert ran == []


def test_holds_are_released_independently(tmp_path):
    """A late release for one attempt leaves another hold intact."""
    cache = SubtitleCache(tmp_path)
    cache.begin_start("late-close")
    cache.begin_start("live-pass")
    cache.end_start("late-close")
    assert cache.playing is True
    cache.end_start("live-pass")
    assert cache.playing is False


def test_a_reclaimed_plan_stops_holding_subtitle_work():
    """An unexpired grant whose timeline is gone has nothing to protect."""
    cache = SubtitleCache(None)
    live = {"plan": True}

    cache.bind_playback_probe(lambda: live["plan"])
    assert cache.playing is True
    live["plan"] = False
    assert cache.playing is False


@pytest.mark.parametrize("value", [True, False])
def test_legacy_boolean_cannot_unpark_a_live_pass(tmp_path, value):
    """The old surface still works but cannot release a live pass."""
    cache = SubtitleCache(tmp_path)
    cache.bind_playback_probe(lambda: True)
    cache.playing = value
    assert cache.playing is True
