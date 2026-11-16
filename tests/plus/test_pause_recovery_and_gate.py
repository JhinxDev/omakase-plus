"""Paused-stream recovery, subtitle-gate ownership and startup phase receipts.

Two defects were reproduced against release server 1335bf9 with the audit
probes: a valid viewer who paused for longer than the native abandon window got
25 successful access checks and then HTTP 410 for video, and a close for a
replaced pass cleared the shared subtitle flag while the replacement was still
streaming. These tests pin the repaired contract.

The gate is no longer a shared flag. It follows the passes that are actually
being served, so idle reclamation, revocation, replacement and a failed start
stay separate outcomes.

The fixture callables are imported so pytest can use them in this module; Ruff
sees those imports as unused and as redefinitions, hence the file-level noqa.
"""

# ruff: noqa: F401, F811

import asyncio
import time

from omakase.plus.debrid_subs import SubtitleCache
from omakase.plus.native_hls import NativeStreams
from tests.plus.test_native_hls_routes import _open_native, native_plan, system
from tests.plus.test_native_tv_test import API, LIBRARY_API

CLOUD_PATH = "/plus/watch/cloud/d-fixture"


def _capture_gate(monkeypatch):
    """Record the SubtitleCache the test server builds, to read its gate.

    The router is built with the fixture, so the instance is captured from a
    request-time method rather than from construction.
    """
    caches = []
    original = SubtitleCache.begin_start

    def track(self, attempt):
        if self not in caches:
            caches.append(self)
        return original(self, attempt)

    monkeypatch.setattr(SubtitleCache, "begin_start", track)
    return caches


def _fixture_clock(system, monkeypatch):
    """Make the plan's idle stamp use the fixture clock, not wall time."""
    start = NativeStreams.start

    def start_with_fixture_clock(self, *args, **kwargs):
        plan = start(self, *args, **kwargs)
        plan.touched = self.clock()
        return plan

    monkeypatch.setattr(NativeStreams, "start", start_with_fixture_clock)
    return system[3]


def _open(client_hook):
    client, opened = client_hook
    assert opened.status_code == 200
    grant = opened.json()
    media = f"{API}/media/{grant['media_id']}"
    access = f"{LIBRARY_API}/media/{grant['media_id']}/access"
    cookie = {"omakase_tv_test_media": grant["media_token"]}
    return client, grant, media, access, cookie


def test_long_pause_reports_a_recoverable_plan_expiry(system, monkeypatch, native_plan):
    """A 250 second pause keeps access valid but names the reclaimed plan."""
    caches = _capture_gate(monkeypatch)
    options = _fixture_clock(system, monkeypatch)
    client, grant, media, access, cookie = _open(_open_native(system, monkeypatch))
    assert client.get(media + "/index.m3u8", cookies=cookie).status_code == 200
    assert client.get(media + "/v00000.m4s", cookies=cookie).status_code == 200

    replies = []
    for _ in range(25):
        options.now += 10
        reply = client.get(access)
        assert reply.status_code == 200, reply.text
        replies.append(reply.json())
        assert replies[-1]["allowed"] is True

    # Inside the abandon window the stream is still owned by this pass.
    assert "stream" not in replies[-2]
    assert "recover" not in replies[-2]
    # After it, the viewer is told to reopen instead of learning from a failure.
    assert replies[-1]["stream"] == "expired"
    assert replies[-1]["recover"] == "reopen"
    # Idle reclamation is kept: the reclaimed plan is still gone.
    assert client.get(media + "/v00000.m4s", cookies=cookie).status_code == 410
    # The grant itself was still valid, which is why the old reply was silent.
    assert replies[-1]["allowed"] is True
    assert caches[-1].playing is True


def test_reopen_after_idle_sweep_restores_a_playable_timeline(system, monkeypatch, native_plan):
    """The recovery signal leads to one fresh pass that serves media again."""
    caches = _capture_gate(monkeypatch)
    options = _fixture_clock(system, monkeypatch)
    client, grant, media, access, cookie = _open(_open_native(system, monkeypatch))
    assert client.get(media + "/v00000.m4s", cookies=cookie).status_code == 200
    for _ in range(25):
        options.now += 10
        assert client.get(access).status_code == 200
    assert client.get(f"{LIBRARY_API}/media/{grant['media_id']}/access").json()["stream"] == (
        "expired"
    )

    reopened = client.post(
        LIBRARY_API + "/play", json={"watch_path": CLOUD_PATH, "engine": "native"}
    )
    assert reopened.status_code == 200, reopened.text
    fresh = reopened.json()
    assert fresh["media_id"] != grant["media_id"]
    assert fresh["duration_seconds"] == grant["duration_seconds"]
    fresh_media = f"{API}/media/{fresh['media_id']}"
    fresh_access = f"{LIBRARY_API}/media/{fresh['media_id']}/access"
    fresh_cookie = {"omakase_tv_test_media": fresh["media_token"]}
    assert client.get(fresh_media + "/index.m3u8", cookies=fresh_cookie).status_code == 200
    assert client.get(fresh_media + "/v00000.m4s", cookies=fresh_cookie).status_code == 200
    live = client.get(fresh_access)
    assert live.status_code == 200
    assert live.json() == {"allowed": True}
    assert caches[-1].playing is True


def test_revoked_access_is_not_reported_as_recoverable(system, monkeypatch, native_plan):
    """Unlinking is a different outcome from a reclaimed plan."""
    _capture_gate(monkeypatch)
    options = _fixture_clock(system, monkeypatch)
    client, grant, media, access, cookie = _open(_open_native(system, monkeypatch))
    for _ in range(25):
        options.now += 10
        assert client.get(access).status_code == 200
    assert client.post(LIBRARY_API + "/unlink").status_code == 200
    revoked = client.get(access)
    assert revoked.status_code == 401, revoked.text
    assert "recover" not in revoked.text
    assert client.get(media + "/v00000.m4s", cookies=cookie).status_code in {401, 404}


def test_late_close_from_a_replaced_pass_leaves_the_gate_held(system, monkeypatch, native_plan):
    """The replacement pass keeps protecting subtitle work after the old close."""
    caches = _capture_gate(monkeypatch)
    _fixture_clock(system, monkeypatch)
    client, previous, _, _, _ = _open(_open_native(system, monkeypatch))
    replacement = client.post(
        LIBRARY_API + "/play", json={"watch_path": CLOUD_PATH, "engine": "native"}
    )
    assert replacement.status_code == 200
    active = replacement.json()["media_id"]
    assert active != previous["media_id"]
    gate = caches[-1]
    assert gate.playing is True

    late = client.post(f"{LIBRARY_API}/media/{previous['media_id']}/close")
    assert late.status_code == 200
    assert client.get(f"{LIBRARY_API}/media/{active}/access").status_code == 200
    assert gate.playing is True

    # A repeated close is still idempotent and still cannot unpark a live pass.
    assert client.post(f"{LIBRARY_API}/media/{previous['media_id']}/close").status_code == 200
    assert gate.playing is True

    # The explicit final teardown releases deferred subtitle work. The fixture
    # also holds one older library pass, so unlinking this device is the
    # deterministic end of service for it.
    assert client.post(LIBRARY_API + "/unlink").status_code == 200
    assert gate.playing is False


def test_finished_pass_expiry_releases_the_gate(system, monkeypatch, native_plan):
    """An expired pass stops holding subtitle work without any close."""
    caches = _capture_gate(monkeypatch)
    options = _fixture_clock(system, monkeypatch)
    client, grant, media, access, cookie = _open(_open_native(system, monkeypatch))
    assert caches[-1].playing is True
    options.now += 2500
    # The pass itself has expired by now, so the grant answers 401/404 rather
    # than a recovery invitation.
    assert client.get(access).status_code in {401, 404}
    assert caches[-1].playing is False


def test_saved_position_open_records_the_startup_phases(system, monkeypatch, native_plan):
    """The wait before a saved position is attributable to named, open-anchored phases."""
    plans = []
    start = NativeStreams.start

    def capture(self, *args, **kwargs):
        plan = start(self, *args, **kwargs)
        if plan is not None:
            plan.touched = self.clock()
            plans.append(plan)
        return plan

    monkeypatch.setattr(NativeStreams, "start", capture)
    client, grant, media, access, cookie = _open(_open_native(system, monkeypatch))
    assert client.get(media + "/index.m3u8?start_ms=66000", cookies=cookie).status_code == 200
    assert client.get(media + "/v00000.m4s", cookies=cookie).status_code == 200

    phases = plans[-1].phases
    assert {
        "open",
        "resolve",
        "plan",
        "index",
        "grant",
        "captions",
        "timeline",
        "opening",
        "first_media",
    } <= set(phases)
    assert all(isinstance(value, int) and value >= 0 for value in phases.values())
    # One clock: every phase is measured from the same open stamp.
    assert phases["open"] == 0
    assert phases["grant"] >= phases["plan"] >= phases["resolve"]
    assert phases["first_media"] >= phases["opening"]
    # Caption readiness and first media are separately attributed, which is the
    # split the audit asked for before any tuning.
    assert phases["captions"] >= 0


def test_a_head_request_is_not_first_playable_media(system, monkeypatch, native_plan, caplog):
    """Only a media GET that returns bytes completes the receipt."""
    _capture_gate(monkeypatch)
    _fixture_clock(system, monkeypatch)
    client, grant, media, access, cookie = _open(_open_native(system, monkeypatch))
    assert client.get(media + "/index.m3u8", cookies=cookie).status_code == 200
    caplog.clear()
    head = client.head(media + "/v00000.m4s", cookies=cookie)
    assert head.status_code == 200 and not head.content
    receipts = [r for r in caplog.records if "native phases" in r.getMessage()]
    assert receipts == [], "a HEAD with no body must not count as first media"

    assert client.get(media + "/v00000.m4s", cookies=cookie).status_code == 200
    receipts = [r for r in caplog.records if "native phases" in r.getMessage()]
    assert len(receipts) == 1
    assert "first_media=" in receipts[0].getMessage()


def test_named_anime_start_publishes_the_phase_receipt(system, monkeypatch, native_plan, caplog):
    """The named route records the same receipt as the Cloud route."""
    from omakase.plus import native_tv_test
    from omakase.plus.debrid_anime import DebridTitleSource

    client, opened = _open_native(system, monkeypatch)
    assert opened.status_code == 200
    monkeypatch.setattr(native_tv_test, "playback_provider", lambda *_: "realdebrid")
    monkeypatch.setattr(native_tv_test, "revalidate_anime", lambda *_: None)

    async def resolve(connection, scope, monitor, episode, library):
        cloud = await library.source(native_tv_test.account_token(connection, 1), "d-fixture")
        return DebridTitleSource(scope, "anime", "100", 1, episode, cloud, "b" * 64, ()), "Fixture"

    monkeypatch.setattr(native_tv_test, "resolve_debrid_anime", resolve)
    played = client.post(
        LIBRARY_API + "/play", json={"watch_path": "/plus/watch/1/1", "engine": "native"}
    )
    assert played.status_code == 200, played.text
    data = played.json()
    media = f"{API}/media/{data['media_id']}"
    cookie = {"omakase_tv_test_media": data["media_token"]}
    caplog.clear()
    assert client.get(media + "/index.m3u8", cookies=cookie).status_code == 200
    assert client.get(media + "/v00000.m4s", cookies=cookie).status_code == 200
    receipts = [r for r in caplog.records if "native phases" in r.getMessage()]
    assert len(receipts) == 1, "the named native route must publish a phase receipt"
    line = receipts[0].getMessage()
    for name in ("open=0ms", "resolve=", "index=", "grant=", "first_media="):
        assert name in line


def test_gate_follows_live_passes_not_a_shared_flag(tmp_path):
    """One close cannot unpark work for a pass that is still being served."""
    cache = SubtitleCache(tmp_path)
    live = {"a": 1000.0, "b": 1000.0}
    cache.bind_playback_probe(lambda: any(expires > 500.0 for expires in live.values()))

    cache.begin_start("attempt")
    cache.end_start("attempt")
    assert cache.playing is True

    del live["a"]  # the replaced pass closes
    assert cache.playing is True  # the replacement still holds the gate

    cache.playing = False  # the old clear is late and no longer global
    assert cache.playing is True

    live.clear()  # the final teardown
    assert cache.playing is False


def test_failed_start_releases_only_its_own_hold(tmp_path):
    """A failed open on one device must not release another device's pass."""
    cache = SubtitleCache(tmp_path)
    live = {"other-device": 1000.0}
    cache.bind_playback_probe(lambda: any(expires > 500.0 for expires in live.values()))

    cache.begin_start("failing")
    assert cache.playing is True
    cache.end_start("failing")
    assert cache.playing is True  # the live pass is untouched

    live.clear()
    assert cache.playing is False


def test_broken_probe_does_not_wedge_subtitle_work(tmp_path):
    """A registry read that raises must not stop deferred work forever."""

    def explode():
        raise RuntimeError("registry unavailable")

    cache = SubtitleCache(tmp_path)
    cache.bind_playback_probe(explode)
    assert cache.playing is False
    asyncio.run(asyncio.sleep(0))


def test_killing_running_jobs_happens_when_a_pass_starts(tmp_path):
    """Starting a pass still stops expensive subtitle children."""
    cache = SubtitleCache(tmp_path)

    class Child:
        def __init__(self):
            self.killed = False

        def kill(self):
            self.killed = True

    child = Child()
    cache.processes.add(child)
    cache.begin_start("attempt")
    assert child.killed is True


def test_idle_wait_ends_when_the_last_pass_closes(tmp_path):
    """Deferred work proceeds after the final teardown, not after a timeout."""
    cache = SubtitleCache(tmp_path)
    live = {"a": 1000.0}
    cache.bind_playback_probe(lambda: any(expires > 500.0 for expires in live.values()))

    async def wait_then_close():
        waiter = asyncio.create_task(cache._wait_for_idle())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        live.clear()
        await asyncio.wait_for(waiter, timeout=2.0)

    started = time.monotonic()
    asyncio.run(wait_then_close())
    assert time.monotonic() - started < 2.0
