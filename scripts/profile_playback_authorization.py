"""Finite media-free source-authorization profile on a caller-owned metadata clone.

Only the CLI installs process-local CPU/wall limits. Output contains aggregate
counts/timings, never tokens, private paths, account IDs or provider payloads.
"""

import argparse
import asyncio
import hashlib
import json
import signal
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


class ProfileLimit(RuntimeError):
    pass


async def profile_authorization(db, target, *, callbacks=1):
    import httpx

    from omakase.plus import manifest, quality_publication
    from omakase.plus import web_playback_source as source
    from omakase.plus.households import resolve_scope

    if callbacks not in (1, 2):
        raise ValueError("Only one or two callbacks are admitted")
    expected = {
        "actor_user_id",
        "library_id",
        "anilist_id",
        "season_number",
        "episode_number",
        "duration_ms",
    }
    if not isinstance(target, dict) or set(target) != expected:
        raise ValueError("Use the exact profile target schema")
    counts = {}
    phase = "setup"
    originals = []
    requests = {"identity": 0, "detail": 0}

    def instrument(module, name):
        original = getattr(module, name)

        def measured(*args, **kwargs):
            key = phase + ":" + module.__name__ + "." + name
            row = counts.setdefault(key, {"calls": 0, "cpu_seconds": 0.0, "wall_seconds": 0.0})
            row["calls"] += 1
            cpu = time.process_time()
            wall = time.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                row["cpu_seconds"] += time.process_time() - cpu
                row["wall_seconds"] += time.monotonic() - wall

        originals.append((module, name, original))
        setattr(module, name, measured)

    for module, names in (
        (source, ("_snapshot", "_publication", "revalidate_source")),
        (quality_publication, ("validate_current_selection", "source_generation")),
        (manifest, ("build_library_manifest",)),
    ):
        for name in names:
            instrument(module, name)
    old_query_only = db.execute("PRAGMA query_only").fetchone()[0]
    db.execute("PRAGMA query_only=ON")
    changes = db.total_changes
    overall_cpu = time.process_time()
    overall_wall = time.monotonic()
    status = "failed"
    failure = None
    stages = []
    try:
        scope = resolve_scope(
            db, viewer_id=target["actor_user_id"], library_id=target["library_id"]
        )
        snapshot = source._snapshot(
            db, scope, target["anilist_id"], target["season_number"], target["episode_number"]
        )
        relative, size, identity, _, _, _, server, root = snapshot
        # A deterministic synthetic Plex envelope exercises the exact current
        # remote guard. Publication/scope/credentials are the real cloned metadata;
        # no provider entitlement or physical-media identity proof is claimed.
        media = ET.Element("Media", container="mkv", videoCodec="hevc", audioCodec="opus")
        part = ET.SubElement(
            media,
            "Part",
            key="/library/parts/1/1/profile.mkv",
            file=root + "/" + relative,
            size=str(size),
            duration=str(target["duration_ms"]),
        )
        ET.SubElement(
            part, "Stream", id="101", index="0", streamType="1", codec="hevc", languageCode="und"
        )
        ET.SubElement(
            part, "Stream", id="102", index="1", streamType="2", codec="opus", languageCode="jpn"
        )
        version = source._physical_version(identity, server, "1", "1", media, part)
        resolved = source.ResolvedAnimeSource(
            target["actor_user_id"],
            target["library_id"],
            target["anilist_id"],
            target["season_number"],
            target["episode_number"],
            server,
            "profile-synthetic-account",
            "1",
            part.get("key"),
            size,
            target["duration_ms"],
            "mkv",
            "hevc",
            "opus",
            (
                source.PlexStream("101", "video", "hevc", "und", False, False, True, 0),
                source.PlexStream("102", "audio", "opus", "jpn", True, False, True, 1),
            ),
            version,
            identity,
            "https://profile.example",
            "synthetic-no-provider-token",
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
        identity_xml = ET.tostring(ET.Element("MediaContainer", machineIdentifier=server))
        detail_xml = ET.tostring(envelope)

        def respond(request):
            if request.method != "GET":
                raise ValueError("No write methods in profile")
            if request.url.path == "/identity":
                requests["identity"] += 1
                body = identity_xml
            elif request.url.path == "/library/metadata/1":
                requests["detail"] += 1
                body = detail_xml
            else:
                raise ValueError("Nonmetadata request rejected")
            return httpx.Response(200, content=body, headers={"Content-Type": "application/xml"})

        transport = httpx.MockTransport(respond)
        phase = "authorization"
        for _ in range(callbacks):
            cpu = time.process_time()
            wall = time.monotonic()
            # Exact source-guard composition of current -> remote -> current.
            # Login/store/session checks are deliberately not impersonated here.
            source.revalidate_source(db, resolved)
            await source.revalidate_remote_source(db, resolved, transport=transport)
            source.revalidate_source(db, resolved)
            stages.append(
                {"cpu_seconds": time.process_time() - cpu, "wall_seconds": time.monotonic() - wall}
            )
        if db.total_changes != changes:
            raise ValueError("Metadata profile wrote data")
        status = "pass"
    except Exception as error:
        failure = type(error).__name__
    finally:
        for module, name, original in reversed(originals):
            setattr(module, name, original)
        db.execute("PRAGMA query_only=" + str(int(old_query_only)))
    return {
        "status": status,
        "failure_type": failure,
        "callbacks_requested": callbacks,
        "callbacks_completed": len(stages),
        "callbacks": stages,
        "profile_counts": counts,
        "total_cpu_seconds": time.process_time() - overall_cpu,
        "total_wall_seconds": time.monotonic() - overall_wall,
        "mock_metadata_requests": requests,
        "real_provider_requests": 0,
        "original_media_reads": 0,
        "ffmpeg_jobs": 0,
        "database_changes": db.total_changes - changes,
        "scope": "real private cloned publication metadata; synthetic remote metadata; source guards only; inclusive nested timings not additive",
        "source_module_sha256": hashlib.sha256(Path(source.__file__).read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--callbacks", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    import resource

    def limit(*_):
        raise ProfileLimit("profile bound")

    signal.signal(signal.SIGALRM, limit)
    signal.signal(signal.SIGXCPU, limit)
    resource.setrlimit(resource.RLIMIT_CPU, (14, 15))
    signal.setitimer(signal.ITIMER_REAL, 20)
    if Path("/probe/src").is_dir():
        sys.path.insert(0, "/probe/src")
    db = None
    try:
        dbpath = Path(args.db)
        if dbpath.name != "omakase-plus.db" or dbpath.is_symlink() or not dbpath.is_file():
            raise ValueError("Expected private metadata clone")
        target_path = Path(args.target)
        if (
            target_path.is_symlink()
            or not target_path.is_file()
            or target_path.stat().st_size > 4096
        ):
            raise ValueError("Expected bounded profile target")
        db = sqlite3.connect(dbpath.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        result = asyncio.run(
            profile_authorization(db, json.loads(target_path.read_text()), callbacks=args.callbacks)
        )
    except Exception as error:
        result = {
            "status": "failed",
            "failure_type": type(error).__name__,
            "real_provider_requests": 0,
            "original_media_reads": 0,
            "ffmpeg_jobs": 0,
        }
    finally:
        if db is not None:
            db.close()
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
