"""Offline bounded-job security regressions; these do not qualify Linux playback."""

import asyncio
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omakase.plus import web_playback_remux as remux
from omakase.plus.web_playback_exec import build_args
from omakase.plus.web_playback_source import PlexStream, ResolvedAnimeSource


@pytest.fixture
def setup(tmp_path):
    db = sqlite3.connect(":memory:")
    db.executescript(
        (Path(remux.__file__).parent / "migrations/040-web-playback-remux.sql").read_text()
    )
    source = ResolvedAnimeSource(
        actor_user_id=1,
        library_id="library",
        anilist_id=1,
        season_number=1,
        episode_number=1,
        server_id="server",
        account_id="personal",
        metadata_key="1",
        part_key="/library/parts/1/1/file.mkv",
        part_size=4,
        duration_ms=10000,
        container="mkv",
        video_codec="hevc",
        audio_codec="opus",
        streams=(
            PlexStream("77", "video", "hevc", "", True, False, True),
            PlexStream("88", "audio", "opus", "ja", True, False, True),
        ),
        media_version="a" * 64,
        source_identity="identity",
        base_url="https://plex.example",
        token="dummy-mux",
        _snapshot=(),
        _scope=None,
    )
    return db, source, tmp_path


def output(folder):
    (
        folder / "manifest.mpd"
    ).write_text("""<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S"><Period><AdaptationSet>
    <Representation id="0" codecs="hev1.1.6.L120" mimeType="video/mp4"><SegmentTemplate timescale="1000" initialization="init-$RepresentationID$.mp4" media="chunk-$RepresentationID$-$Number%05d$.m4s" startNumber="1"><SegmentTimeline><S t="0" d="5000" r="1"/></SegmentTimeline></SegmentTemplate></Representation>
    </AdaptationSet><AdaptationSet lang="jpn"><Representation id="1" codecs="opus"><SegmentTemplate timescale="1000" initialization="init-$RepresentationID$.mp4" media="chunk-$RepresentationID$-$Number%05d$.m4s" startNumber="1"><SegmentTimeline><S t="0" d="5000" r="1"/></SegmentTimeline></SegmentTemplate></Representation>
    </AdaptationSet></Period></MPD>""")
    for rep in (0, 1):
        (folder / f"init-{rep}.mp4").write_bytes(hevc_init() if rep == 0 else b"init")
        for number in (1, 2):
            (folder / f"chunk-{rep}-{number:05d}.m4s").write_bytes(b"chunk")
    packet_file(folder)


def hevc_init():
    """Tiny structural hvcC fixture matching the declared Main/L120 codec."""

    def box(kind, payload):
        return (8 + len(payload)).to_bytes(4, "big") + kind + payload

    config = bytes.fromhex("01016000000000000000000078f000fcfdf8f800000f03")
    config += b"".join(bytes([kind]) + b"\0\1\0\2" + bytes([kind << 1, 1]) for kind in (32, 33, 34))
    visual = box(b"hev1", bytes(78) + box(b"hvcC", config))
    stsd = box(b"stsd", bytes(4) + (1).to_bytes(4, "big") + visual)
    hdlr = box(b"hdlr", bytes(8) + b"vide" + bytes(12))
    return box(b"ftyp", b"iso5\0\0\0\1iso5") + box(
        b"moov", box(b"trak", box(b"mdia", hdlr + box(b"minf", box(b"stbl", stsd))))
    )


def packet_file(folder, *, audio_end=10, priming=0):
    summary = {
        "kind": "same-source-audio-packet-observer-v1",
        "media_version": "a" * 64,
        "part_size": 4,
        "input_start_time": 0,
        "output_bytes": 20,
        "lines": 500,
        "streams": [
            {
                "representation_id": "1",
                "container_index": 1,
                "plex_stream_id": "88",
                "codec": "opus",
                "timebase": [1, 1000],
                "packets": 500,
                "start_seconds": priming / 1000,
                "end_seconds": audio_end,
            }
        ],
    }
    (folder / "packet-observer.json").write_text(json.dumps(summary))


class Body(httpx.AsyncByteStream):
    def __init__(self, value=b"data"):
        self.value = value

    async def __aiter__(self):
        yield self.value


def transport(value=b"data", status=200, headers=None):
    def respond(request):
        assert request.headers["X-Plex-Token"] == "dummy-mux"
        assert "dummy-mux" not in str(request.url)
        if request.headers.get("range"):
            assert request.headers["range"] == "bytes=0-3"
            return httpx.Response(
                206, headers={"Content-Length": "4", "Content-Range": "bytes 0-3/4"}, stream=Body()
            )
        return httpx.Response(
            status, headers=headers or {"Content-Length": "4"}, stream=Body(value)
        )

    return httpx.MockTransport(respond)


def probe_document():
    return {
        "format": {"start_time": "0.000000"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 1920,
                "height": 1080,
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "opus",
                "channels": 2,
                "time_base": "1/1000",
                "tags": {"language": "jpn"},
            },
        ],
    }


def test_revised_eligibility_and_helper_deadline_preserve_cpu_bound(setup):
    _, source, _ = setup
    remux._inventory(replace(source, part_size=256 * 1024**2))
    with pytest.raises(remux.RemuxError, match="unsupported_source"):
        remux._inventory(replace(source, part_size=256 * 1024**2 + 1))
    assert build_args({"mode": "probe", "seconds": 45})[0] == "/usr/bin/ffprobe"
    with pytest.raises(ValueError, match="deadline"):
        build_args({"mode": "probe", "seconds": 45.01})


@pytest.mark.parametrize("deadline,kept", [(145, False), (146, True)])
def test_revised_restart_deadline_is_bounded_and_requires_absence(
    setup, monkeypatch, deadline, kept
):
    db, source, root = setup
    job = "c" * 32
    folder = root / job
    folder.mkdir()
    (folder / ".omakase-remux").write_text(job)
    db.execute(
        "INSERT INTO web_playback_remux(job_id,media_version,actor_user_id,state,created,expires,deadline,coordinator_pid,coordinator_start_ticks,coordinator_boot_id,coordinator_token) VALUES(?,?,1,'RUNNING',100,3700,?,321,'old-owner','boot',?)",
        (job, source.media_version, deadline, "a" * 64),
    )
    monkeypatch.setattr(remux.sys, "platform", "linux")
    monkeypatch.setattr(remux, "_identity", lambda pid: None)
    monkeypatch.setattr(remux, "_cwd_scan_absent", lambda folder: True)
    remux.cleanup(db, root, now=151)
    assert bool(db.execute("SELECT 1 FROM web_playback_remux").fetchone()) is kept


class Process:
    pid = 12345

    def __init__(self, folder, *, stall=False, probe=False, observe=False):
        self.folder = folder
        self.returncode = None
        self.stall = stall
        self.probe = probe
        self.observe = observe
        self.probe_bytes = (
            "".join(f"duration=20|stream_index=1|pts={t}\n" for t in range(0, 10000, 20)).encode()
            if observe
            else json.dumps(probe_document()).encode()
        )
        self.stdin = SimpleNamespace(
            write=lambda value: None, drain=self.drain, close=lambda: None, wait_closed=self.drain
        )
        self.stderr = SimpleNamespace(read=self.read)
        self.stdout = SimpleNamespace(read=self.read_probe)

    async def read_probe(self, count):
        value, self.probe_bytes = self.probe_bytes, b""
        return value

    async def drain(self):
        await asyncio.sleep(0)

    async def read(self, count):
        return b""

    async def wait(self):
        if self.stall:
            await asyncio.Event().wait()
        if not self.probe and not self.observe:
            output(self.folder)
        self.returncode = 0
        return 0


def options(monkeypatch, *, stall=False):
    processes = []

    async def launch(folder, recipe):
        probe = recipe["mode"] == "probe"
        process = Process(
            folder, stall=stall and not probe, probe=probe, observe=recipe["mode"] == "observe"
        )
        process.pid = 12345 + len(processes)
        processes.append(process)
        return process

    async def stop(process):
        process.returncode = -9

    monkeypatch.setattr(remux, "_stop", stop)
    monkeypatch.setattr(remux, "_identity", lambda pid: ("ticks", "boot"))
    return dict(
        cache_admission=lambda root, size: None, launcher=launch, cpu_reader=lambda: 0
    ), processes


def test_complete_copy_inventory_and_whitelist(setup, monkeypatch):
    db, source, root = setup
    opts, _ = options(monkeypatch)
    artifact = asyncio.run(
        remux.prepare_remux(
            db,
            source=source,
            authorize=lambda: True,
            cache_root=root,
            transport=transport(),
            **opts,
        )
    )
    assert artifact["input_sha256"] == __import__("hashlib").sha256(b"data").hexdigest()
    assert artifact["track_map"][0]["plex_stream_id"] == "77"
    assert artifact["duration"] == 10
    assert len(artifact["resources"]) == 7
    assert remux.get_resource(db, artifact["job_id"], "manifest.mpd", cache_root=root).is_file()
    for name in ("../manifest.mpd", "init-99.mp4", "https://x", "packet-observer.json"):
        with pytest.raises(remux.RemuxError):
            remux.get_resource(db, artifact["job_id"], name, cache_root=root)


@pytest.mark.parametrize("truncated", [False, True])
def test_bare_hevc_is_normalized_before_complete_timeline_validation(setup, monkeypatch, truncated):
    import sys

    db, source, root = setup
    opts, _ = options(monkeypatch)
    original_output, original_validate = output, remux.validate_artifact
    checked = []

    def bare(folder):
        original_output(folder)
        path = folder / "manifest.mpd"
        text = path.read_text().replace('codecs="hev1.1.6.L120"', 'codecs="hev1"')
        if truncated:
            text = text.replace('d="5000" r="1"', 'd="2500" r="1"')
        path.write_text(text)

    def validate(folder, *args, **kwargs):
        assert 'codecs="hev1.1.6.L120"' in (folder / "manifest.mpd").read_text()
        checked.append(1)
        return original_validate(folder, *args, **kwargs)

    monkeypatch.setattr(sys.modules[__name__], "output", bare)
    monkeypatch.setattr(remux, "validate_artifact", validate)

    async def prepare():
        return await remux.prepare_remux(
            db,
            source=source,
            authorize=lambda: True,
            cache_root=root,
            transport=transport(),
            **opts,
        )

    if truncated:
        with pytest.raises(remux.RemuxError, match="invalid_manifest"):
            asyncio.run(prepare())
        assert db.execute("SELECT 1 FROM web_playback_remux WHERE state='READY'").fetchone() is None
    else:
        artifact = asyncio.run(prepare())
        assert (root / artifact["job_id"] / "init-0.mp4").read_bytes() == hevc_init()
        assert (root / artifact["job_id"] / "chunk-0-00001.m4s").read_bytes() == b"chunk"
    assert checked == [1]


@pytest.mark.parametrize(
    "value,headers",
    [
        (b"abc", None),
        (b"abcde", None),
        (b"data", {"Content-Length": "4", "Content-Encoding": "gzip"}),
    ],
)
def test_input_bounds_never_publish(setup, monkeypatch, value, headers):
    db, source, root = setup
    opts, processes = options(monkeypatch)
    with pytest.raises(remux.RemuxError):
        asyncio.run(
            remux.prepare_remux(
                db,
                source=source,
                authorize=lambda: True,
                cache_root=root,
                transport=transport(value, headers=headers),
                **opts,
            )
        )
    assert processes[-1].returncode == -9
    assert not db.execute("SELECT 1 FROM web_playback_remux WHERE state='READY'").fetchone()
    assert list(root.iterdir()) == []


def test_source_inventory_fails_before_job(setup):
    db, source, root = setup
    with pytest.raises(remux.RemuxError, match="ambiguous_track_inventory"):
        asyncio.run(
            remux.prepare_remux(
                db,
                source=replace(source, streams=source.streams + (source.streams[1],)),
                authorize=lambda: True,
                cache_root=root,
            )
        )
    assert not db.execute("SELECT 1 FROM web_playback_remux").fetchone()


def test_deadline_and_authorization_cleanup(setup, monkeypatch):
    db, source, root = setup
    opts, processes = options(monkeypatch, stall=True)
    with pytest.raises(remux.RemuxError, match="deadline_exceeded"):
        asyncio.run(
            remux.prepare_remux(
                db,
                source=source,
                authorize=lambda: True,
                cache_root=root,
                transport=transport(),
                deadline_seconds=0.03,
                **opts,
            )
        )
    assert processes[-1].returncode == -9
    assert not db.execute("SELECT 1 FROM web_playback_remux WHERE state='RUNNING'").fetchone()


def test_revoked_before_publication(setup, monkeypatch):
    db, source, root = setup
    opts, _ = options(monkeypatch)
    checks = []

    def authorize():
        checks.append(1)
        return len(checks) < 3

    with pytest.raises(remux.RemuxError, match="authorization_changed"):
        asyncio.run(
            remux.prepare_remux(
                db,
                source=source,
                authorize=authorize,
                cache_root=root,
                transport=transport(),
                **opts,
            )
        )
    assert not db.execute("SELECT 1 FROM web_playback_remux WHERE state='READY'").fetchone()


def test_single_running_lease_and_cancellation(setup, monkeypatch):
    db, source, root = setup
    opts, processes = options(monkeypatch, stall=True)

    async def scenario():
        first = asyncio.create_task(
            remux.prepare_remux(
                db,
                source=source,
                authorize=lambda: True,
                cache_root=root,
                transport=transport(),
                **opts,
            )
        )
        while not processes:
            await asyncio.sleep(0)
        with pytest.raises(remux.RemuxError, match="worker_busy"):
            await remux.prepare_remux(
                db,
                source=source,
                authorize=lambda: True,
                cache_root=root,
                transport=transport(),
                **opts,
            )
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

    asyncio.run(scenario())
    assert not db.execute("SELECT 1 FROM web_playback_remux WHERE state='RUNNING'").fetchone()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("boundary", ["handoff", "validation"])
def test_cleanup_never_releases_active_coordinator_after_child_exit(setup, monkeypatch, boundary):
    db, source, root = setup
    opts, processes = options(monkeypatch)
    original_launch = opts["launcher"]
    monkeypatch.setattr(remux.sys, "platform", "linux")
    monkeypatch.setattr(
        remux,
        "_identity",
        lambda pid: (
            ("coordinator", "boot")
            if pid == os.getpid()
            else (("child", "boot") if processes and processes[-1].returncode is None else None)
        ),
    )

    async def scenario():
        paused = asyncio.Event()
        release = asyncio.Event()
        checks = 0

        async def launch(folder, recipe):
            if boundary == "handoff" and recipe["mode"] == "remux":
                paused.set()
                await release.wait()
            return await original_launch(folder, recipe)

        async def authorize():
            nonlocal checks
            checks += 1
            if boundary == "validation" and checks == 5:
                paused.set()
                await release.wait()
            return True

        opts["launcher"] = launch
        task = asyncio.create_task(
            remux.prepare_remux(
                db,
                source=source,
                authorize=authorize,
                cache_root=root,
                transport=transport(),
                held_versions=lambda: [source.media_version],
                **opts,
            )
        )
        try:
            await asyncio.wait_for(paused.wait(), 2)
            assert processes[-1].returncode == 0
            created = db.execute("SELECT created FROM web_playback_remux").fetchone()[0]
            remux.cleanup(db, root, held_versions=lambda: [source.media_version], now=created + 100)
            assert db.execute("SELECT state FROM web_playback_remux").fetchone()[0] == "RUNNING"
            assert remux.held_media_versions(db) == {source.media_version}
            with pytest.raises(remux.RemuxError, match="worker_busy"):
                await remux.prepare_remux(
                    db,
                    source=source,
                    authorize=lambda: True,
                    cache_root=root,
                    transport=transport(),
                    held_versions=lambda: [source.media_version],
                    **opts,
                )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_lost_running_lease_cannot_register_or_publish(setup, monkeypatch):
    db, source, root = setup
    opts, processes = options(monkeypatch)
    original_launch = opts["launcher"]

    async def scenario():
        paused = asyncio.Event()
        release = asyncio.Event()

        async def launch(folder, recipe):
            if recipe["mode"] == "remux":
                paused.set()
                await release.wait()
            return await original_launch(folder, recipe)

        opts["launcher"] = launch
        task = asyncio.create_task(
            remux.prepare_remux(
                db,
                source=source,
                authorize=lambda: True,
                cache_root=root,
                transport=transport(),
                held_versions=lambda: [source.media_version],
                **opts,
            )
        )
        await asyncio.wait_for(paused.wait(), 2)
        db.execute("UPDATE web_playback_remux SET state='ERROR'")
        db.commit()
        release.set()
        with pytest.raises(remux.RemuxError, match="lease_lost"):
            await task
        assert not db.execute("SELECT 1 FROM web_playback_remux WHERE state='READY'").fetchone()
        assert all(process.returncode is not None for process in processes)

    asyncio.run(scenario())


def test_lost_owner_token_cannot_publish_after_validation(setup, monkeypatch):
    db, source, root = setup
    opts, _ = options(monkeypatch)
    original = remux.validate_artifact

    def validate(folder, selected, recipe, **kwargs):
        artifact = original(folder, selected, recipe, **kwargs)
        db.execute("UPDATE web_playback_remux SET coordinator_token=?", ("b" * 64,))
        db.commit()
        return artifact

    monkeypatch.setattr(remux, "validate_artifact", validate)
    with pytest.raises(remux.RemuxError, match="lease_lost"):
        asyncio.run(
            remux.prepare_remux(
                db,
                source=source,
                authorize=lambda: True,
                cache_root=root,
                transport=transport(),
                held_versions=lambda: [source.media_version],
                **opts,
            )
        )
    assert db.execute("SELECT state,artifact_json FROM web_playback_remux").fetchone() == (
        "RUNNING",
        None,
    )


@pytest.mark.parametrize(
    "replacement",
    [
        "<!DOCTYPE MPD><MPD/>",
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><BaseURL>https://external</BaseURL></MPD>',
    ],
)
def test_manifest_external_features(setup, replacement):
    _, source, folder = setup
    output(folder)
    (folder / "manifest.mpd").write_text(replacement)
    with pytest.raises(remux.RemuxError):
        remux.validate_artifact(folder, source)


def test_hold_and_unknown_directory_preservation(setup, monkeypatch):
    db, source, root = setup
    opts, _ = options(monkeypatch)
    artifact = asyncio.run(
        remux.prepare_remux(
            db,
            source=source,
            authorize=lambda: True,
            cache_root=root,
            transport=transport(),
            **opts,
        )
    )
    unknown = root / "unknown"
    unknown.mkdir()
    (unknown / "unrelated").write_text("preserve")
    db.execute("UPDATE web_playback_remux SET expires=0")
    with pytest.raises(remux.RemuxError, match="expired_resource"):
        remux.get_resource(db, artifact["job_id"], "manifest.mpd", cache_root=root)
    held = [source.media_version]
    before = db.total_changes
    assert remux.get_resource(
        db, artifact["job_id"], "manifest.mpd", cache_root=root, held_versions=lambda: held
    ).is_file()
    assert db.total_changes == before
    held.clear()
    with pytest.raises(remux.RemuxError, match="expired_resource"):
        remux.get_resource(
            db, artifact["job_id"], "manifest.mpd", cache_root=root, held_versions=lambda: held
        )
    remux.cleanup(db, root, held_versions=lambda: [source.media_version])
    assert (root / artifact["job_id"]).exists()
    # chmod only for Windows fake-artifact deletion; actual Linux owner can unlink 0400.
    for path in (root / artifact["job_id"]).iterdir():
        path.chmod(0o600)
    remux.cleanup(db, root)
    assert unknown.is_dir()
    assert not (root / artifact["job_id"]).exists()


def test_explicit_prepare_reuses_expired_held_exact_actor_cache(setup, monkeypatch):
    db, source, root = setup
    opts, processes = options(monkeypatch)

    async def prepare():
        return await remux.prepare_remux(
            db,
            source=source,
            authorize=lambda: True,
            cache_root=root,
            transport=transport(),
            held_versions=lambda: [source.media_version],
            **opts,
        )

    original = asyncio.run(prepare())
    db.execute("UPDATE web_playback_remux SET expires=0")
    source = replace(
        source, streams=(source.streams[0], replace(source.streams[1], selected=False))
    )
    snapshot = {
        name: (root / original["job_id"] / name).read_bytes() for name in original["resources"]
    }
    second = asyncio.run(prepare())
    assert second["job_id"] == original["job_id"]
    assert second["track_map"][1]["selected"] is False
    assert len(processes) == 3  # Prefix, mux, observer; no extra job on explicit Play.
    assert db.execute("SELECT expires FROM web_playback_remux").fetchone()[0] > 0
    assert {
        name: (root / second["job_id"] / name).read_bytes() for name in second["resources"]
    } == snapshot


def test_orphan_pid_reuse_is_not_authority(setup, monkeypatch):
    db, _, root = setup
    job = "b" * 32
    folder = root / job
    folder.mkdir()
    (folder / ".omakase-remux").write_text(job)
    db.execute(
        "INSERT INTO web_playback_remux(job_id,media_version,actor_user_id,state,created,expires,pid,start_ticks,boot_id,artifact_json) VALUES(?,?,1,'RUNNING',0,0,123,'old','boot',NULL)",
        (job, "a" * 64),
    )
    db.execute(
        "UPDATE web_playback_remux SET deadline=30,coordinator_pid=321,coordinator_start_ticks='old-owner',coordinator_boot_id='boot',coordinator_token=?",
        ("a" * 64,),
    )
    monkeypatch.setattr(remux.sys, "platform", "linux")
    monkeypatch.setattr(remux, "_identity", lambda pid: None if pid == 321 else ("reused", "boot"))
    monkeypatch.setattr(remux, "_cwd_scan_absent", lambda folder: True)
    remux.cleanup(db, root, now=9999999999)
    assert db.execute("SELECT state FROM web_playback_remux").fetchone()[0] == "RUNNING"
    monkeypatch.setattr(remux, "_identity", lambda pid: None)
    remux.cleanup(db, root)
    assert db.execute("SELECT state FROM web_playback_remux").fetchone() is None


@pytest.mark.parametrize(
    "proof,now,expected", [(True, 136, False), (False, 136, True), (True, 134, True)]
)
def test_missing_pid_recovery_needs_deadline_and_complete_absence(
    setup, monkeypatch, proof, now, expected
):
    db, source, root = setup
    job = "c" * 32
    folder = root / job
    folder.mkdir()
    (folder / ".omakase-remux").write_text(job)
    (folder / "partial").write_bytes(b"partial")
    db.execute(
        "INSERT INTO web_playback_remux(job_id,media_version,actor_user_id,state,created,expires,deadline) VALUES(?,?,1,'RUNNING',100,3700,130)",
        (job, source.media_version),
    )
    db.execute(
        "UPDATE web_playback_remux SET coordinator_pid=321,coordinator_start_ticks='old-owner',coordinator_boot_id='boot',coordinator_token=?",
        ("a" * 64,),
    )
    monkeypatch.setattr(remux.sys, "platform", "linux")
    monkeypatch.setattr(remux, "_identity", lambda pid: None)
    scans = []

    def absent(path):
        scans.append(path)
        return proof

    monkeypatch.setattr(remux, "_cwd_scan_absent", absent)
    assert remux.held_media_versions(db) == {source.media_version}
    remux.cleanup(db, root, now=now)
    assert bool(db.execute("SELECT 1 FROM web_playback_remux").fetchone()) is expected
    assert folder.exists() is expected
    assert len(scans) == (0 if now == 134 else 1)
    assert remux.held_media_versions(db) == ({source.media_version} if expected else set())


def test_cwd_scan_complete_absence_matching_unknown_and_overflow(setup, monkeypatch):
    _, _, root = setup
    proc = root / "proc"
    proc.mkdir()
    for pid in ("1", "22"):
        (proc / pid).mkdir()
    target = root / "owned"
    target.mkdir()
    monkeypatch.setattr(remux, "_read_cwd", lambda path: "/unrelated")
    assert remux._cwd_scan_absent(target, proc_root=proc)
    monkeypatch.setattr(remux, "_read_cwd", lambda path: str(target.resolve()))
    assert not remux._cwd_scan_absent(target, proc_root=proc)

    def inaccessible(path):
        raise PermissionError

    monkeypatch.setattr(remux, "_read_cwd", inaccessible)
    assert not remux._cwd_scan_absent(target, proc_root=proc)
    monkeypatch.setattr(remux, "_read_cwd", lambda path: "/unrelated")
    assert not remux._cwd_scan_absent(target, proc_root=proc, pid_limit=1)

    def new_process(path):
        (proc / "33").mkdir(exist_ok=True)
        return "/unrelated"

    monkeypatch.setattr(remux, "_read_cwd", new_process)
    assert not remux._cwd_scan_absent(target, proc_root=proc)


def test_launcher_has_no_source_or_preexec():
    code = Path(remux.__file__).with_name("web_playback_exec.py").read_text()
    assert '"-c", "copy"' in code
    assert "preexec_fn" not in Path(remux.__file__).read_text()
    assert "X-Plex" not in code and "http://" not in code


def test_revocation_during_stalled_transfer(setup, monkeypatch):
    db, source, root = setup
    opts, processes = options(monkeypatch, stall=True)
    checks = []

    def authorize():
        checks.append(1)
        return len(checks) < 5

    with pytest.raises(remux.RemuxError, match="authorization_changed"):
        asyncio.run(
            remux.prepare_remux(
                db,
                source=source,
                authorize=authorize,
                cache_root=root,
                transport=transport(),
                deadline_seconds=3,
                **opts,
            )
        )
    assert processes[-1].returncode == -9
    assert len(checks) == 5


def test_resource_monitor_and_manifest_timeline(setup, monkeypatch):
    _, source, root = setup
    output(root)
    monkeypatch.setattr(remux, "JOB_LIMIT", 4)
    with pytest.raises(remux.RemuxError, match="output_bound"):
        remux._usage(root)
    monkeypatch.setattr(remux, "JOB_LIMIT", 768 * 1024**2)
    manifest = root / "manifest.mpd"
    manifest.write_text(manifest.read_text().replace('t="0"', 't="9000"'))
    with pytest.raises(remux.RemuxError, match="invalid_manifest"):
        remux.validate_artifact(root, source)


@pytest.mark.parametrize(
    "tail", ["both", "audio", "period", "presentation_offset", "cross_end", "start_offset"]
)
def test_publication_rejects_incomplete_original_timeline(setup, tail):
    _, source, root = setup
    output(root)
    path = root / "manifest.mpd"
    xml = path.read_text()
    if tail == "both":
        xml = xml.replace("PT10S", "PT5S").replace(' r="1"', ' r="0"')
        for rep in (0, 1):
            (root / f"chunk-{rep}-00002.m4s").unlink()
    elif tail == "audio":
        xml = xml.replace('<S t="0" d="5000" r="1"/>', '<S t="0" d="10000"/>', 1)
        xml = xml.replace('<S t="0" d="5000" r="1"/>', '<S t="0" d="8000"/>', 1)
        for rep in (0, 1):
            (root / f"chunk-{rep}-00002.m4s").unlink()
    elif tail == "period":
        xml = xml.replace("<Period>", '<Period duration="PT5S">')
    elif tail == "presentation_offset":
        xml = xml.replace('timescale="1000"', 'timescale="1000" presentationTimeOffset="5000"')
    else:
        replacements = (
            ('<S t="0" d="9800"/>', '<S t="0" d="10200"/>')
            if tail == "cross_end"
            else ('<S t="300" d="9700"/>', '<S t="0" d="10000"/>')
        )
        for value in replacements:
            xml = xml.replace('<S t="0" d="5000" r="1"/>', value, 1)
        for rep in (0, 1):
            (root / f"chunk-{rep}-00002.m4s").unlink()
    path.write_text(xml)
    with pytest.raises(remux.RemuxError):
        remux.validate_artifact(root, source)


def test_long_gop_copy_is_complete_without_fixed_segment_arithmetic(setup):
    _, source, root = setup
    output(root)
    path = root / "manifest.mpd"
    path.write_text(path.read_text().replace('<S t="0" d="5000" r="1"/>', '<S t="0" d="10000"/>'))
    for rep in (0, 1):
        (root / f"chunk-{rep}-00002.m4s").unlink()
    assert remux.validate_artifact(root, source)["duration"] == 10


def test_missing_boot_identity_is_not_process_absence(monkeypatch):
    def read(path):
        if str(path).replace("\\", "/") == "/proc/123/stat":
            return "123 (worker) " + " ".join(["S"] + ["0"] * 18 + ["99"])
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(remux.RemuxError, match="process_identity_unknown"):
        remux._identity(123)


def observed(setup, *, data=None):
    _, source, _ = setup
    probe = probe_document()
    probe["format"]["start_time"] = "-0.007000"
    recipe = remux.qualify_tracks(source, probe)

    async def parse():
        reader = asyncio.StreamReader()
        value = (
            data
            if data is not None
            else "".join(
                f"duration=20|stream_index=1|pts={t}|\n" for t in range(-7, 9193, 20)
            ).encode()
        )
        reader.feed_data(value)
        reader.feed_eof()
        return await remux._observe_packets(reader, source, recipe)

    return asyncio.run(parse()), recipe


def test_observer_shorter_opus_and_negative_priming_fixed_basis(setup):
    _, source, root = setup
    summary, recipe = observed(setup)
    output(root)
    (root / remux.PACKET_FILE).write_text(json.dumps(summary))
    manifest = root / "manifest.mpd"
    first = '<S t="0" d="5000" r="1"/>'
    head, tail = manifest.read_text().split(first, 1)
    manifest.write_text(head + first + tail.replace(first, '<S t="0" d="5000"/><S d="4200"/>', 1))
    result = remux.validate_artifact(root, source, recipe, require_packet_evidence=True)
    assert result["track_map"][1]["packet_evidence"]["start_seconds"] == 0
    assert result["track_map"][1]["packet_evidence"]["end_seconds"] == pytest.approx(9.2)
    assert remux.PACKET_FILE not in result["resources"]
    # Independently truncate DASH while retaining complete observation.
    manifest.chmod(0o600)
    manifest.write_text(manifest.read_text().replace('d="4200"', 'd="3000"'))
    with pytest.raises(remux.RemuxError):
        remux.validate_artifact(root, source, recipe, require_packet_evidence=True)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"stream_index=1|pts=N/A|duration=20\n",
        b"stream_index=7|pts=0|duration=20\n",
        b"stream_index=1|pts=0|duration=0\n",
        b"stream_index=1|pts=0|duration=121\n",
        b"stream_index=1|pts=0|duration=20",
        b"stream_index=1|pts=0|data=payload\n",
        b"x" * 129 + b"\n",
        b"stream_index=1|stream_index=1|pts=0\n",
    ],
)
def test_observer_rejects_missing_malformed_wrong_packets(setup, data):
    with pytest.raises(remux.RemuxError):
        observed(setup, data=data)


def test_observer_output_and_summary_bounds(setup, monkeypatch):
    monkeypatch.setattr(remux, "OBSERVER_BYTES", 10)
    with pytest.raises(remux.RemuxError, match="packet_evidence_bound"):
        observed(setup, data=b"stream_index=1|pts=0|duration=20\n")
    monkeypatch.setattr(remux, "OBSERVER_BYTES", 16 * 1024**2)
    monkeypatch.setattr(remux, "PACKET_LINES", 1)
    with pytest.raises(remux.RemuxError, match="packet_evidence_bound"):
        observed(setup)


@pytest.mark.parametrize("failure", ["invalid", "exit", "hang", "disconnect"])
def test_observer_failure_reaps_both_and_never_publishes(setup, monkeypatch, failure):
    db, source, root = setup
    opts, processes = options(monkeypatch)
    launch = opts["launcher"]

    async def fail_launch(folder, recipe):
        child = await launch(folder, recipe)
        if recipe["mode"] == "observe":
            if failure == "invalid":
                child.probe_bytes = b"invalid\n"
            if failure == "exit":

                async def exit():
                    child.returncode = 1
                    return 1

                child.wait = exit
            if failure == "hang":

                async def read(count):
                    await asyncio.Event().wait()

                child.stdout.read = read
            if failure == "disconnect":

                async def drain():
                    raise BrokenPipeError

                child.stdin.drain = drain
        return child

    opts["launcher"] = fail_launch
    with pytest.raises(remux.RemuxError):
        asyncio.run(
            remux.prepare_remux(
                db,
                source=source,
                authorize=lambda: True,
                cache_root=root,
                transport=transport(),
                deadline_seconds=0.1,
                **opts,
            )
        )
    assert len(processes) == 3 and all(child.returncode is not None for child in processes)
    assert not db.execute(
        "SELECT 1 FROM web_playback_remux WHERE state IN ('READY','RUNNING')"
    ).fetchone()


def test_observer_cancel_keeps_primary_identity_and_reaps_both(setup, monkeypatch):
    db, source, root = setup
    opts, processes = options(monkeypatch, stall=True)

    async def scenario():
        task = asyncio.create_task(
            remux.prepare_remux(
                db,
                source=source,
                authorize=lambda: True,
                cache_root=root,
                transport=transport(),
                **opts,
            )
        )
        while len(processes) < 3:
            await asyncio.sleep(0)
        row = db.execute("SELECT pid FROM web_playback_remux").fetchone()
        assert row[0] == processes[1].pid and row[0] != processes[2].pid
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert all(child.returncode is not None for child in processes)


def test_sampled_final_cpu_counts_reaped_observer(setup, monkeypatch):
    db, source, root = setup
    opts, processes = options(monkeypatch)
    usage = [0]
    launch = opts["launcher"]

    async def counted(folder, recipe):
        child = await launch(folder, recipe)
        if recipe["mode"] == "observe":
            wait = child.wait

            async def exit():
                result = await wait()
                usage[0] = 30_000_001
                return result

            child.wait = exit
        return child

    opts.update(launcher=counted, cpu_reader=lambda: usage[0])
    with pytest.raises(remux.RemuxError, match="sampled_cpu_budget_exceeded"):
        asyncio.run(
            remux.prepare_remux(
                db,
                source=source,
                authorize=lambda: True,
                cache_root=root,
                transport=transport(),
                **opts,
            )
        )
    assert all(child.returncode is not None for child in processes)
    assert not db.execute("SELECT 1 FROM web_playback_remux WHERE state='READY'").fetchone()


@pytest.mark.parametrize(
    "quota,accepted", [("100000 100000", True), ("200000 100000", False), ("max 100000", False)]
)
def test_sampled_cpu_requires_qualified_one_core_quota(monkeypatch, quota, accepted):
    read = Path.read_text

    def fake(path, *args, **kwargs):
        if str(path).replace("\\", "/").endswith("/cpu.max"):
            return quota
        if str(path).replace("\\", "/").endswith("/cpu.stat"):
            return "usage_usec 123456\nuser_usec 123\n"
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake)
    if accepted:
        assert remux._cpu_usage() == 123456
    else:
        with pytest.raises(remux.RemuxError, match="cpu_budget_unqualified"):
            remux._cpu_usage()


@pytest.mark.parametrize("protection", ["none", "held", "unknown", "all_held"])
def test_idle_ready_lru_turnover_preserves_active_and_unknown(setup, monkeypatch, protection):
    db, source, root = setup
    opts, processes = options(monkeypatch)
    held = set()

    def prepare(version):
        return asyncio.run(
            remux.prepare_remux(
                db,
                source=replace(source, media_version=version),
                authorize=lambda: True,
                cache_root=root,
                transport=transport(),
                held_versions=lambda: held,
                **opts,
            )
        )

    a = prepare("a" * 64)
    b = prepare("b" * 64)
    if protection in {"held", "all_held"}:
        held.add("a" * 64)
    if protection == "all_held":
        held.add("b" * 64)
    if protection == "unknown":
        (root / a["job_id"] / "unrelated").write_text("preserve")
    if protection == "all_held":
        with pytest.raises(remux.RemuxError, match="cache_busy"):
            prepare("c" * 64)
        assert len(processes) == 6
    else:
        c = prepare("c" * 64)
        assert (root / c["job_id"]).is_dir()
        survivor = b if protection == "none" else a
        evicted = a if protection == "none" else b
        assert (root / survivor["job_id"]).is_dir() and not (root / evicted["job_id"]).exists()
        assert (
            db.execute("SELECT COUNT(*) FROM web_playback_remux WHERE state='READY'").fetchone()[0]
            == 2
        )


def test_unheld_ready_can_evict_before_idle_ttl_for_reservation(setup, monkeypatch):
    db, source, root = setup
    opts, _ = options(monkeypatch)
    first = asyncio.run(
        remux.prepare_remux(
            db,
            source=source,
            authorize=lambda: True,
            cache_root=root,
            transport=transport(),
            **opts,
        )
    )

    def capacity(path, size):
        if db.execute("SELECT 1 FROM web_playback_remux WHERE state='READY'").fetchone():
            raise remux.RemuxError("cache_capacity")

    opts["cache_admission"] = capacity
    second = asyncio.run(
        remux.prepare_remux(
            db,
            source=replace(source, media_version="b" * 64),
            authorize=lambda: True,
            cache_root=root,
            transport=transport(),
            **opts,
        )
    )
    assert not (root / first["job_id"]).exists() and (root / second["job_id"]).is_dir()


def test_verified_multi_audio_indexes_and_no_id_guess(setup):
    _, source, _ = setup
    video, audio = source.streams
    en = replace(audio, stream_id="99", language="en", container_index=3, selected=False)
    audio = replace(audio, container_index=2)
    source = replace(source, streams=(replace(video, container_index=0), en, audio))
    probe = probe_document()
    probe["streams"][1]["index"] = 2
    probe["streams"].append(
        {
            "index": 3,
            "codec_type": "audio",
            "codec_name": "opus",
            "channels": 2,
            "time_base": "1/1000",
            "tags": {"language": "eng"},
        }
    )
    recipe = remux.qualify_tracks(source, probe)
    assert [a["id"] for a in recipe["audio"]] == ["88", "99"]
    args = build_args(recipe)
    assert [args[i + 1] for i, a in enumerate(args) if a == "-map"] == ["0:0", "0:2", "0:3"]
    assert "framecrc" not in args
    assert all(
        args[i + 1] == "copy" for i, arg in enumerate(args) if arg in {"-c", "-c:v", "-c:a", "-c:s"}
    )
    assert "0:88" not in args and "0:99" not in args
    with pytest.raises(remux.RemuxError, match="ambiguous_track_inventory"):
        remux.qualify_tracks(
            replace(source, streams=(source.streams[0], replace(en, container_index=None), audio)),
            probe,
        )
    probe["streams"][2]["tags"]["language"] = "jpn"
    with pytest.raises(remux.RemuxError, match="track_inventory"):
        remux.qualify_tracks(source, probe)


def test_video_uses_container_language_but_audio_subtitle_language_stays_strict(setup):
    _, source, root = setup
    source = replace(
        source,
        streams=(
            replace(source.streams[0], language="eng", container_index=0),
            replace(source.streams[1], container_index=1),
        ),
    )
    probe = probe_document()
    probe["streams"][0]["tags"] = {}
    recipe = remux.qualify_tracks(source, probe)
    assert recipe["video"]["language"] == "und"
    output(root)
    artifact = remux.validate_artifact(root, source, recipe)
    assert artifact["track_map"][0]["language"] == "und"
    probe["streams"][1]["tags"]["language"] = "eng"
    with pytest.raises(remux.RemuxError, match="track_inventory"):
        remux.qualify_tracks(source, probe)
    probe["streams"][1]["tags"]["language"] = "jpn"
    with pytest.raises(remux.RemuxError, match="ambiguous_track_inventory"):
        remux.qualify_tracks(
            replace(
                source, streams=(replace(source.streams[0], container_index=9), source.streams[1])
            ),
            probe,
        )
    subtitle = PlexStream("99", "subtitle", "ass", "eng", False, False, True, container_index=2)
    probe["streams"].append(
        {"index": 2, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "jpn"}}
    )
    with pytest.raises(remux.RemuxError, match="track_inventory"):
        remux.qualify_tracks(replace(source, streams=source.streams + (subtitle,)), probe)


@pytest.mark.parametrize("codec,mime", [("png", "image/png"), ("ttf", "application/octet-stream")])
def test_non_font_attachments_rejected(setup, codec, mime):
    _, source, _ = setup
    probe = probe_document()
    probe["streams"].append(
        {"index": 2, "codec_type": "attachment", "codec_name": codec, "tags": {"mimetype": mime}}
    )
    with pytest.raises(remux.RemuxError, match="unsupported_attachment"):
        remux.qualify_tracks(source, probe)


def test_safe_recipe_rejects_output_injection(setup):
    _, source, _ = setup
    recipe = remux.qualify_tracks(source, probe_document())
    recipe["audio"][0]["id"] = "../escape"
    with pytest.raises(ValueError):
        build_args(recipe)


def test_unsupported_subtitles_are_explicit(setup):
    db, source, root = setup
    source = replace(
        source,
        streams=source.streams + (PlexStream("99", "subtitle", "pgs", "en", False, False, True),),
    )
    with pytest.raises(remux.RemuxError, match="unsupported_subtitle_codec"):
        asyncio.run(remux.prepare_remux(db, source=source, authorize=lambda: True, cache_root=root))
    assert not db.execute("SELECT 1 FROM web_playback_remux").fetchone()


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": "4"},
        {"Content-Length": "4", "Content-Range": "bytes 0-2/4"},
        {"Content-Length": "4", "Content-Range": "bytes 0-3/4", "Content-Encoding": "gzip"},
    ],
)
def test_probe_range_identity_guard(setup, monkeypatch, headers):
    db, source, root = setup
    opts, processes = options(monkeypatch)
    mock = httpx.MockTransport(lambda request: httpx.Response(206, headers=headers, stream=Body()))
    with pytest.raises(remux.RemuxError, match="invalid_probe_response"):
        asyncio.run(
            remux.prepare_remux(
                db, source=source, authorize=lambda: True, cache_root=root, transport=mock, **opts
            )
        )
    assert len(processes) == 1 and processes[0].returncode == -9
    assert not db.execute("SELECT 1 FROM web_playback_remux WHERE state='READY'").fetchone()


@pytest.mark.parametrize(
    "value", [b"not-a-font", b"OTTO" + b"x" * (8 * 1024**2)], ids=["bad-magic", "oversize"]
)
def test_font_magic_and_size_guard(setup, value):
    _, source, root = setup
    output(root)
    recipe = remux.qualify_tracks(source, probe_document())
    recipe["fonts"] = [2]
    (root / "font-2.bin").write_bytes(value)
    with pytest.raises(remux.RemuxError):
        remux.validate_artifact(root, source, recipe)


def test_personal_selection_does_not_reorder_audio(setup):
    _, source, _ = setup
    video, audio = source.streams
    source = replace(
        source,
        streams=(
            replace(video, container_index=0),
            replace(audio, container_index=1, selected=False),
            replace(audio, stream_id="99", language="en", container_index=2, selected=True),
        ),
    )
    probe = probe_document()
    probe["streams"].append(
        {
            "index": 2,
            "codec_type": "audio",
            "codec_name": "opus",
            "channels": 2,
            "time_base": "1/1000",
            "tags": {"language": "eng"},
        }
    )
    assert [t["id"] for t in remux.qualify_tracks(source, probe)["audio"]] == ["88", "99"]


def test_local_ffmpeg_two_audio_ass_font_copy(setup, tmp_path, monkeypatch):
    """Actual Windows muxer semantics only; Linux resource enforcement is separate."""
    import subprocess

    ffmpeg = Path("C:/ffmpeg/ffmpeg.exe")
    ffprobe = Path("C:/ffmpeg/ffprobe.exe")
    font = Path("C:/Windows/Fonts/arial.ttf")
    if not all(p.is_file() for p in (ffmpeg, ffprobe, font)):
        pytest.skip("Local synthetic muxer fixture tools unavailable")
    db, source, _ = setup
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    ass = input_dir / "source.ass"
    ass.write_text("""[Script Info]
ScriptType: v4.00+
PlayResX: 160
PlayResY: 90
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,12,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,1,1,1,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,{\\i1}Synthetic caption{\\i0}
""")
    mkv = input_dir / "synthetic.mkv"
    made = subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=10:d=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=4.2",
            "-i",
            str(ass),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-map",
            "3:s",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "20",
            "-c:a",
            "libopus",
            "-c:s",
            "copy",
            "-metadata:s:a:0",
            "language=jpn",
            "-metadata:s:a:1",
            "language=eng",
            "-metadata:s:s:0",
            "language=eng",
            "-attach",
            str(font),
            "-metadata:s:t",
            "mimetype=application/x-truetype-font",
            str(mkv),
        ],
        capture_output=True,
        timeout=15,
    )
    assert made.returncode == 0, made.stderr.decode(errors="replace")[:4000]
    payload = mkv.read_bytes()
    args = build_args({"mode": "probe"})
    probed = subprocess.run(
        [str(ffprobe), *args[1:]], input=payload[: 16 * 1024**2], capture_output=True, timeout=10
    )
    assert probed.returncode == 0, probed.stderr.decode(errors="replace")[:4000]
    document = json.loads(probed.stdout)
    priming = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-f",
            "matroska",
            "-protocol_whitelist",
            "pipe,file",
            "-read_intervals",
            "%+#1",
            "-show_entries",
            "stream=index,start_time",
            "-of",
            "json",
            "-i",
            "pipe:0",
        ],
        input=payload,
        capture_output=True,
        timeout=10,
    )
    assert priming.returncode == 0
    starts = {
        s["index"]: float(s["start_time"])
        for s in json.loads(priming.stdout)["streams"]
        if "start_time" in s
    }
    assert starts[1] < 0 and starts[2] < 0
    source = replace(
        source,
        part_size=len(payload),
        duration_ms=5008,
        video_codec="h264",
        streams=(
            replace(source.streams[0], codec="h264", container_index=0),
            replace(source.streams[1], container_index=1),
            replace(
                source.streams[1], stream_id="99", language="en", container_index=2, selected=False
            ),
            PlexStream("100", "subtitle", "ass", "en", False, False, True, container_index=3),
        ),
    )
    recipe = remux.qualify_tracks(source, document)
    assert recipe["fonts"] == [4]
    cache = tmp_path / "cache"
    cache.mkdir()
    requests = []

    def upstream(request):
        requests.append(request.headers.get("range"))
        if request.headers.get("range"):
            size = min(len(payload), 16 * 1024**2)
            assert request.headers["range"] == f"bytes=0-{size - 1}"
            return httpx.Response(
                206,
                headers={
                    "Content-Length": str(size),
                    "Content-Range": f"bytes 0-{size - 1}/{len(payload)}",
                },
                stream=Body(payload[:size]),
            )
        return httpx.Response(
            200, headers={"Content-Length": str(len(payload))}, stream=Body(payload)
        )

    async def local_launch(folder, recipe):
        args = build_args(recipe)
        binary = ffprobe if recipe["mode"] in {"probe", "observe"} else ffmpeg
        return await asyncio.create_subprocess_exec(
            str(binary),
            *args[1:],
            cwd=folder,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE
            if recipe["mode"] in {"probe", "observe"}
            else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    async def local_stop(process):
        if process.returncode is None:
            process.kill()
        await process.wait()

    monkeypatch.setattr(remux, "_stop", local_stop)
    prepared = asyncio.run(
        remux.prepare_remux(
            db,
            source=source,
            authorize=lambda: True,
            cache_root=cache,
            transport=httpx.MockTransport(upstream),
            launcher=local_launch,
            cache_admission=lambda root, size: None,
            deadline_seconds=10,
            cpu_reader=lambda: 0,
        )
    )
    assert requests == [f"bytes=0-{len(payload) - 1}", None]
    assert len(prepared["track_map"]) == 3
    assert (
        prepared["fonts"][0]["sha256"]
        == __import__("hashlib").sha256(font.read_bytes()).hexdigest()
    )
    assert remux.get_resource(
        db, prepared["job_id"], "subtitle-100.ass", cache_root=cache
    ).is_file()
    out = cache / prepared["job_id"]
    assert b"{\\i1}Synthetic caption{\\i0}" in (out / "subtitle-100.ass").read_bytes()
    assert (out / "font-4.bin").read_bytes() == font.read_bytes()
    assert prepared["packet_evidence"]["streams"][1]["end_seconds"] < 4.5
    assert prepared["packet_evidence"]["streams"][0]["start_seconds"] == pytest.approx(0)
