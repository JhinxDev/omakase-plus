"""Finite synthetic player API gate over a private, isolated metadata clone.

Linux/native execution is owned by the operator. No production endpoint or
original media is contacted. The publication-to-synthetic-byte seam is explicit.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

TARGET_KEYS = {
    "actor_user_id",
    "library_id",
    "anilist_id",
    "season_number",
    "episode_number",
    "duration_ms",
}


def require(ok):
    if not ok:
        raise ValueError("pipeline qualification invariant")


def validate_fixture(value):
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "schema",
            "bytes",
            "sha256",
            "duration_ms",
            "video",
            "audio",
            "subtitles",
            "fonts",
            "min_audio_packets",
        }
    )
    require(value["schema"] == "omakase-player-pipeline-v1")
    require(type(value["bytes"]) is int and 215_000_000 <= value["bytes"] <= 256 * 1024**2)
    require(type(value["duration_ms"]) is int and 1_700_000 <= value["duration_ms"] <= 2_400_000)
    require(isinstance(value["sha256"], str) and re.fullmatch("[0-9a-f]{64}", value["sha256"]))
    require(
        type(value["min_audio_packets"]) is int
        and 176_000 <= value["min_audio_packets"] <= 1_000_000
    )
    require(
        isinstance(value["video"], dict)
        and set(value["video"]) == {"index", "codec", "language", "width", "height"}
    )
    require(
        value["video"]["codec"] == "hevc"
        and value["video"]["width"] == 1920
        and value["video"]["height"] == 1080
    )
    require(isinstance(value["audio"], list) and len(value["audio"]) == 2)
    require(isinstance(value["subtitles"], list) and len(value["subtitles"]) == 2)
    require(isinstance(value["fonts"], list) and len(value["fonts"]) == 11)
    indexes = []
    for kind, tracks in (
        ("video", [value["video"]]),
        ("audio", value["audio"]),
        ("subtitle", value["subtitles"]),
        ("font", value["fonts"]),
    ):
        for track in tracks:
            require(isinstance(track, dict))
            require(type(track.get("index")) is int and 0 <= track["index"] <= 127)
            indexes.append(track["index"])
            if kind == "font":
                require(set(track) == {"index", "bytes", "sha256"})
                require(type(track["bytes"]) is int and 0 < track["bytes"] <= 8 * 1024**2)
                require(
                    isinstance(track["sha256"], str)
                    and re.fullmatch("[0-9a-f]{64}", track["sha256"])
                )
            else:
                allowed = {"index", "codec", "language", "default", "forced"}
                require(kind == "video" or set(track) <= allowed)
                require(
                    isinstance(track.get("language"), str)
                    and re.fullmatch("[a-z]{3}", track["language"])
                )
                require(
                    kind == "video" or track.get("codec") == ("opus" if kind == "audio" else "ass")
                )
                require(
                    all(type(track[key]) is bool for key in ("default", "forced") if key in track)
                )
    require(len(indexes) == len(set(indexes)))
    require(sum(font["bytes"] for font in value["fonts"]) <= 32 * 1024**2)
    return value


def publication_adapter(original, descriptor, counts):
    def adapted(db, scope, anilist_id, season, episode):
        # Real whole-publication validation and exact target selection happen
        # before replacing byte identity. No claim of original byte equivalence.
        relative, _, identity = original(db, scope, anilist_id, season, episode)
        counts["validated_publication_adapter"] += 1
        from omakase.plus.language_source import digest

        return (
            relative,
            descriptor["bytes"],
            digest(["synthetic-pipeline-v1", identity, descriptor["sha256"]]),
        )

    return adapted


class FixtureStream:
    """Wrapped by httpx.AsyncByteStream below; bounded reads and cancellation."""

    def __init__(self, path, length, counts, *, stall=None):
        self.path, self.length, self.counts, self.stall = path, length, counts, stall

    async def chunks(self):
        delivered = 0
        with self.path.open("rb") as handle:
            while delivered < self.length:
                block = handle.read(min(65536, self.length - delivered))
                require(bool(block))
                delivered += len(block)
                self.counts["bytes"] += len(block)
                yield block
                if self.stall is not None:
                    self.stall.set()
                    await asyncio.Event().wait()
                await asyncio.sleep(0)


class RequestPublicationBoundary:
    """Model production's fresh request DB connection conservatively.

    The gate shares its isolated memory DB so writes survive between requests.
    Clear source-local memo evidence once at entry, never during that request.
    A plain ASGI wrapper adds no task, await or receive/disconnect adaptation.
    """

    def __init__(self, app, *, sources, counts):
        self.app, self.sources, self.counts = app, sources, counts

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.counts["http_request_boundaries"] += 1
            for resolved, _ in self.sources:
                resolved._publication_cache.clear()
                self.counts["request_publication_cache_clears"] += 1
        await self.app(scope, receive, send)


def metadata_clone(original):
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    try:
        original.backup(db)
        db.execute("PRAGMA foreign_keys=ON")
        require(db.execute("PRAGMA foreign_keys").fetchone()[0] == 1)
    except BaseException:
        db.close()
        raise
    return db


def _measurement():
    import resource

    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "wall": time.monotonic(),
        "parent_cpu": time.process_time(),
        "children_user": children.ru_utime,
        "children_system": children.ru_stime,
        "cgroup_cpu": int(
            dict(line.split() for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines())[
                "usage_usec"
            ]
        )
        / 1_000_000,
    }


def _delta(before):
    after = _measurement()
    return {key: after[key] - before[key] for key in after}


def _proc_rows():
    processes = []
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
            except FileNotFoundError:
                continue
            processes.append(
                {
                    "pid": int(path.name),
                    "ppid": int(stat[1]),
                    "pgrp": int(stat[2]),
                    "state": stat[0],
                    "cpu_ticks": int(stat[11]) + int(stat[12]),
                }
            )
            require(len(processes) <= 256)
    return processes


async def workers_absent(workers, *, read_rows=_proc_rows):
    # Watchdogs inherit the worker's owned process group. Include zombies and
    # every other process state; --init must actually reap the orphan watchdog.
    groups = {worker.pid for worker in workers}
    until = time.monotonic() + 0.3
    while True:
        remaining = [row for row in read_rows() if row["pid"] in groups or row["pgrp"] in groups]
        if not remaining and all(worker.returncode is not None for worker in workers):
            return
        require(time.monotonic() < until)
        await asyncio.sleep(0.02)


def _sample(cache):
    files = list(cache.glob("*/*"))
    require(len(files) <= 4096 and all(not path.is_symlink() for path in files))
    sizes = []
    for path in files:
        try:
            if path.is_file():
                sizes.append(path.stat().st_size)
        except FileNotFoundError:
            pass  # A known partial can be removed between sampled observations.
    return {
        "cache_files": len(files),
        "cache_bytes": sum(sizes),
        "pids": _proc_rows(),
        "memory_current": int(Path("/sys/fs/cgroup/memory.current").read_text()),
        "pids_current": int(Path("/sys/fs/cgroup/pids.current").read_text()),
        "measurement": _measurement(),
    }


async def qualify(db, target, descriptor, media_path, cache_root):
    """Single cancellation case then one completed synthetic production pipeline."""
    import httpx
    from fastapi import FastAPI
    from starlette.requests import Request

    from omakase.plus import auth, connections, households, manifest, quality_publication
    from omakase.plus import web_playback_remux as remux
    from omakase.plus import web_playback_routes as routes
    from omakase.plus import web_playback_source as source
    from omakase.plus.deps import get_db
    from omakase.plus.web_security import csrf_token

    validate_fixture(descriptor)
    require(isinstance(target, dict) and set(target) == TARGET_KEYS)
    require(
        media_path.is_file()
        and not media_path.is_symlink()
        and media_path.stat().st_size == descriptor["bytes"]
    )
    require(not db.in_transaction)
    require(db.execute("PRAGMA foreign_keys").fetchone()[0] == 1)
    require(not db.execute("SELECT 1 FROM web_playback_remux WHERE state='RUNNING'").fetchone())
    setup_before = _measurement()
    counts = {
        "validated_publication_adapter": 0,
        "resolver": 0,
        "identity": 0,
        "metadata": 0,
        "prefix_requests": 0,
        "full_requests": 0,
        "prefix_bytes": 0,
        "full_bytes": 0,
        "http_request_boundaries": 0,
        "request_publication_cache_clears": 0,
    }
    original_publication = source._publication
    source._publication = publication_adapter(original_publication, descriptor, counts)
    workers, samples, resolved_sources, artifacts = [], [], [], []
    phase = "setup"
    timings, originals = {}, []

    def instrument(module, name):
        original = getattr(module, name)

        def measured(*args, **kwargs):
            row = timings.setdefault(phase + ":" + name, {"calls": 0, "cpu": 0.0, "wall": 0.0})
            row["calls"] += 1
            cpu, wall = time.process_time(), time.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                row["cpu"] += time.process_time() - cpu
                row["wall"] += time.monotonic() - wall

        originals.append((module, name, original))
        setattr(module, name, measured)

    for module, name in (
        (source, "_snapshot"),
        (quality_publication, "validate_current_selection"),
        (manifest, "build_library_manifest"),
    ):
        instrument(module, name)
    stop_sampling = asyncio.Event()
    stalled = asyncio.Event()
    cancel_first = [True]
    report = {
        "schema": "omakase-player-pipeline-result-v1",
        "status": "failed",
        "real_provider_requests": 0,
        "original_media_reads": 0,
        "seam": "real validated private publication mapped explicitly to synthetic fixture bytes; trusted synthetic resolver; actual source/remote/API/remux guards",
    }
    session_token = None

    async def sample():
        while not stop_sampling.is_set():
            require(len(samples) < 100)
            samples.append({"phase": phase, **_sample(cache_root)})
            try:
                await asyncio.wait_for(stop_sampling.wait(), 1)
            except TimeoutError:
                pass

    async def launch(folder, recipe):
        process = await remux._launch(folder, recipe)
        workers.append(process)
        return process

    async def prepare(*args, **kwargs):
        artifact = await remux.prepare_remux(*args, **kwargs, launcher=launch)
        artifacts.append(artifact)
        return artifact

    async def resolver(connection, **values):
        counts["resolver"] += 1
        require(
            values["actor_user_id"] == target["actor_user_id"]
            and values["library_id"] == target["library_id"]
            and values["anilist_id"] == target["anilist_id"]
            and values["season_number"] == target["season_number"]
            and values["episode_number"] == target["episode_number"]
        )
        scope = households.resolve_scope(
            connection, viewer_id=target["actor_user_id"], library_id=target["library_id"]
        )
        snapshot = source._snapshot(
            connection,
            scope,
            target["anilist_id"],
            target["season_number"],
            target["episode_number"],
        )
        relative, size, identity, base, _, _, server, root = snapshot
        token = connections._read(connection, scope.viewer_id, "plex_token")
        media = ET.Element("Media", container="mkv", videoCodec="hevc", audioCodec="opus")
        part = ET.SubElement(
            media,
            "Part",
            key="/library/parts/1/1/synthetic.mkv",
            file=root + "/" + relative,
            size=str(size),
            duration=str(descriptor["duration_ms"]),
        )
        streams = []
        for kind, tracks in (
            ("video", [descriptor["video"]]),
            ("audio", descriptor["audio"]),
            ("subtitle", descriptor["subtitles"]),
        ):
            for track in tracks:
                stream_id = str(1000 + track["index"])
                default, forced = track.get("default", False), track.get("forced", False)
                attrs = dict(
                    id=stream_id,
                    index=str(track["index"]),
                    streamType={"video": "1", "audio": "2", "subtitle": "3"}[kind],
                    codec=track["codec"],
                    languageCode=track["language"],
                    default=str(int(default)),
                    forced=str(int(forced)),
                )
                if kind == "video":
                    attrs.update(width=str(track["width"]), height=str(track["height"]))
                ET.SubElement(part, "Stream", **attrs)
                streams.append(
                    source.PlexStream(
                        stream_id,
                        kind,
                        track["codec"],
                        track["language"],
                        default,
                        forced,
                        default,
                        track["index"],
                    )
                )
        version = source._physical_version(identity, server, "1", "1", media, part)
        resolved = source.ResolvedAnimeSource(
            target["actor_user_id"],
            target["library_id"],
            target["anilist_id"],
            target["season_number"],
            target["episode_number"],
            server,
            "synthetic-pipeline-account",
            "1",
            part.get("key"),
            size,
            descriptor["duration_ms"],
            "mkv",
            "hevc",
            "opus",
            tuple(streams),
            version,
            identity,
            base,
            token,
            snapshot,
            scope,
            "1",
        )
        envelope = ET.Element("MediaContainer", librarySectionID="1")
        video = ET.SubElement(
            envelope,
            "Video",
            librarySectionID="1",
            ratingKey="1",
            type="episode",
            parentIndex=str(target["season_number"]),
            index=str(target["episode_number"]),
        )
        video.append(media)
        resolved_sources.append((resolved, ET.tostring(envelope)))
        return source.revalidate_source(connection, resolved)

    class Stream(httpx.AsyncByteStream):
        def __init__(self, length, *, prefix):
            self.length, self.prefix = length, prefix

        async def __aiter__(self):
            key = "prefix_bytes" if self.prefix else "full_bytes"
            stream_counts = {"bytes": 0}
            try:
                stream = FixtureStream(
                    media_path,
                    self.length,
                    stream_counts,
                    stall=stalled if not self.prefix and cancel_first[0] else None,
                )
                async for block in stream.chunks():
                    yield block
            finally:
                counts[key] += stream_counts["bytes"]

    def upstream(request):
        require(request.method == "GET" and bool(resolved_sources))
        resolved, metadata = resolved_sources[-1]
        require(
            str(request.url).startswith(resolved.base_url.rstrip("/") + "/")
            and request.headers.get("x-plex-token") == resolved.token
            and "x-plex-token" not in str(request.url).lower()
        )
        if request.url.path == "/identity":
            counts["identity"] += 1
            return httpx.Response(
                200,
                content=ET.tostring(
                    ET.Element("MediaContainer", machineIdentifier=resolved.server_id)
                ),
            )
        if request.url.path == "/library/metadata/1":
            counts["metadata"] += 1
            return httpx.Response(200, content=metadata)
        require(
            request.url.path == resolved.part_key
            and request.headers.get("accept-encoding") == "identity"
        )
        prefix = "range" in request.headers
        length = min(descriptor["bytes"], 16 * 1024**2) if prefix else descriptor["bytes"]
        headers = {"content-length": str(length), "content-encoding": "identity"}
        if prefix:
            require(request.headers["range"] == f"bytes=0-{length - 1}")
            counts["prefix_requests"] += 1
            headers["content-range"] = f"bytes 0-{length - 1}/{descriptor['bytes']}"
        else:
            counts["full_requests"] += 1
        return httpx.Response(
            206 if prefix else 200, headers=headers, stream=Stream(length, prefix=prefix)
        )

    sampler = asyncio.create_task(sample())
    try:
        scope = households.resolve_scope(
            db, viewer_id=target["actor_user_id"], library_id=target["library_id"]
        )
        monitors = db.execute(
            "SELECT id FROM media_monitors WHERE user_id=? AND anilist_id=? AND season_number=?",
            (scope.operational_owner_id, target["anilist_id"], target["season_number"]),
        ).fetchall()
        require(len(monitors) == 1)
        session_token = auth.create_session(db, target["actor_user_id"])
        app = FastAPI()
        app.add_middleware(RequestPublicationBoundary, sources=resolved_sources, counts=counts)
        app.include_router(
            routes.create_player_router(
                None,
                lambda *a, **k: {},
                resolver=resolver,
                transport=httpx.MockTransport(upstream),
                remux_prepare=prepare,
                cache_root=cache_root,
            ),
            prefix="/plus",
        )
        app.dependency_overrides[get_db] = lambda: db
        request = Request(
            {"type": "http", "headers": [(b"cookie", f"omakase_session={session_token}".encode())]}
        )
        headers = {"origin": "https://pipeline.invalid", "x-csrf-token": csrf_token(request)}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://pipeline.invalid",
            cookies={"omakase_session": session_token},
            headers=headers,
            timeout=None,
        ) as client:
            body = {
                "monitor_id": monitors[0][0],
                "episode_number": target["episode_number"],
                "library_id": target["library_id"],
                "codecs": ["hevc", "opus"],
                "ass_supported": True,
                "defer_prepare": True,
            }
            report["setup"] = _delta(setup_before)
            phase = "cancellation"
            before = _measurement()
            started = await client.post("/plus/api/playback/sessions", json=body)
            require(started.status_code == 202)
            task = asyncio.create_task(client.post(started.json()["prepare_url"], json={}))
            try:
                await asyncio.wait_for(stalled.wait(), 15)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await workers_absent(workers)
            require(
                not db.execute("SELECT 1 FROM web_playback_remux WHERE state='RUNNING'").fetchone()
            )
            report["cancellation"] = _delta(before) | {
                "workers_reaped": len(workers),
                "running_leases": 0,
                "owned_process_groups_absent_all_states": True,
            }
            require(
                db.execute(
                    "SELECT closed FROM web_playback_sessions WHERE session_id=?",
                    (started.json()["session_id"],),
                ).fetchone()[0]
                == 1
            )
            require(not remux.held_media_versions(db))
            cancel_first[0] = False
            phase = "pipeline"
            before = _measurement()
            started = await client.post("/plus/api/playback/sessions", json=body)
            require(started.status_code == 202)
            prepared = await client.post(started.json()["prepare_url"], json={})
            report["prepare_status"] = prepared.status_code
            require(prepared.status_code == 201 and len(artifacts) == 1)
            await workers_absent(workers)
            artifact = artifacts[0]
            require(artifact["input_sha256"] == descriptor["sha256"])
            require(len(artifact["subtitles"]) == 2 and len(artifact["fonts"]) == 11)
            expected_fonts = {f"font-{font['index']}.bin": font for font in descriptor["fonts"]}
            require(
                all(
                    font["filename"] in expected_fonts
                    and font["sha256"] == expected_fonts[font["filename"]]["sha256"]
                    and font["bytes"] == expected_fonts[font["filename"]]["bytes"]
                    for font in artifact["fonts"]
                )
            )
            packet = json.loads((cache_root / artifact["job_id"] / remux.PACKET_FILE).read_bytes())
            packets = sum(stream["packets"] for stream in packet["streams"])
            require(
                packets >= descriptor["min_audio_packets"]
                and packet["lines"] <= remux.PACKET_LINES
                and packet["output_bytes"] <= remux.OBSERVER_BYTES
            )
            media_url = prepared.json()["media_url"]
            require((await client.head(media_url)).status_code == 200)
            require(
                (await client.get(media_url, headers={"range": "bytes=0-31"})).status_code == 206
            )
            for url in [track["url"] for track in prepared.json()["subtitles"]] + prepared.json()[
                "fonts"
            ]:
                require((await client.head(url)).status_code == 200)
            closed = await client.post(
                f"/plus/api/playback/sessions/{prepared.json()['session_id']}/close", json={}
            )
            require(closed.status_code == 204)
            require(not remux.held_media_versions(db))
            metrics = _delta(before)
            report["pipeline"] = metrics
            report["gates"] = {
                "wall_headroom": metrics["wall"] <= 30,
                "sampled_container_cpu_headroom": metrics["cgroup_cpu"] <= 20,
                "full_input_sha256": True,
                "audio_packets": True,
                "source_stream_bounds": counts["full_requests"] == 2
                and counts["full_bytes"] == descriptor["bytes"] + 65536
                and counts["prefix_requests"] == 2
                and counts["prefix_bytes"] <= 32 * 1024**2,
                "all_workers_reaped": all(worker.returncode is not None for worker in workers),
                "owned_process_groups_absent_all_states": True,
            }
            require(all(report["gates"].values()))
            report.update(
                status="pass",
                bytes=descriptor["bytes"],
                input_sha256=artifact["input_sha256"],
                audio_packets=packets,
                observer_bytes=packet["output_bytes"],
                subtitles=2,
                fonts=11,
                resource_count=len(artifact["resources"]),
            )
    except Exception as error:
        report.update(failure_type=type(error).__name__, failure_phase=phase)
    finally:
        source._publication = original_publication
        for module, name, original in reversed(originals):
            setattr(module, name, original)
        stop_sampling.set()
        await sampler
        if session_token is not None:
            auth.delete_session(db, session_token)
        report.update(
            counts=counts,
            inclusive_timings=timings,
            samples=samples,
            worker_count=len(workers),
            unreaped_workers=sum(worker.returncode is None for worker in workers),
            running_leases=db.execute(
                "SELECT COUNT(*) FROM web_playback_remux WHERE state='RUNNING'"
            ).fetchone()[0],
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--media", required=True)
    parser.add_argument("--cache", default="/run/omakase-playback")
    args = parser.parse_args()
    require(sys.platform == "linux" and os.getpid() != 1)
    if Path("/probe/src").is_dir():
        sys.path.insert(0, "/probe/src")

    def deadline(*_):
        raise TimeoutError("finite qualification deadline")

    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, 95)
    db = original = None
    try:
        dbpath, targetpath, descriptorpath = map(Path, (args.db, args.target, args.fixture))
        require(dbpath.name == "omakase-plus.db" and dbpath.is_file() and not dbpath.is_symlink())
        require(
            all(
                path.is_file() and not path.is_symlink() and path.stat().st_size <= 65536
                for path in (targetpath, descriptorpath)
            )
        )
        original = sqlite3.connect(dbpath.resolve().as_uri() + "?mode=ro", uri=True)
        db = metadata_clone(original)
        report = asyncio.run(
            qualify(
                db,
                json.loads(targetpath.read_text()),
                json.loads(descriptorpath.read_text()),
                Path(args.media),
                Path(args.cache),
            )
        )
        report["source_sha256"] = {
            name: hashlib.sha256((Path("/probe/src/omakase/plus") / name).read_bytes()).hexdigest()
            for name in (
                "web_playback_source.py",
                "web_playback_routes.py",
                "web_playback_remux.py",
                "web_playback_exec.py",
            )
        }
        report["helper_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        report["fixture_descriptor_sha256"] = hashlib.sha256(
            descriptorpath.read_bytes()
        ).hexdigest()
    except Exception as error:
        report = {
            "status": "failed",
            "failure_type": type(error).__name__,
            "real_provider_requests": 0,
            "original_media_reads": 0,
        }
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if db is not None:
            db.close()
        if original is not None:
            original.close()
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
