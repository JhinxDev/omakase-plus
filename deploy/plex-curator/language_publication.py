"""Bounded exact-file publication probe for manifest3; English heuristics unused."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from omakase.plus.language_policy import evidence_to_mapping
from omakase.plus.language_publication import digest, validate_file
from omakase.plus.media_probe import _language_evidence
from omakase.plus.publication_probe import bounded_probe_runner


def bounded_bytes(path):
    before = path.stat()
    with path.open("rb") as handle:
        first = handle.read(65536)
        handle.seek(max(0, before.st_size - 65536))
        last = handle.read(65536)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or not first:
        raise ValueError("Physical publication file changed during the bounded read.")
    return {
        "fingerprint_kind": "bounded-identity-v1",
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "first_bytes": len(first),
        "last_bytes": len(last),
        "first_sha256": hashlib.sha256(first).hexdigest(),
        "last_sha256": hashlib.sha256(last).hexdigest(),
    }


def probe_file(
    path,
    *,
    contract,
    anilist_id,
    season_number,
    torrent_hash,
    file_locator,
    source_relative,
    episodes,
    media_format="TV",
    sole_file=False,
    expected_episodes=None,
    numbering_override=None,
    approved_coverage=None,
    storage_backend="zurg",
    runner=bounded_probe_runner,
    ffprobe="/usr/bin/ffprobe",
):
    from omakase.plus.publication_episodes import approved_file_episodes

    actual = approved_file_episodes(
        file_locator,
        media_format=media_format,
        season=season_number,
        expected=expected_episodes,
        anilist_id=anilist_id,
        numbering_override=numbering_override,
        approved_coverage=approved_coverage or episodes,
        sole_file=sole_file,
    )
    if set(episodes) != set(actual):
        raise ValueError("Publication file requires exact supported episode identity.")
    before = bounded_bytes(path)
    if storage_backend == "local":
        # Same complete streaming SHA contract as LocalImportProvider inventory.
        content = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                content.update(chunk)
        before["content_sha256"] = content.hexdigest()
        before["fingerprint_kind"] = "full-content-sha256-v1"
        before["file_device"] = path.stat().st_dev
        before["file_inode"] = path.stat().st_ino
        if (
            before["content_sha256"] != contract["inventory_generation"]
            or before["content_sha256"].upper() != torrent_hash
        ):
            raise ValueError("Current local file no longer matches its approved SHA inventory.")
    result = runner(
        [
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-probesize",
            "1048576",
            "-analyzeduration",
            "5000000",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height:stream_tags=language,title:stream_disposition=forced,hearing_impaired,comment",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode or len(result.stdout) > 256 * 1024:
        raise ValueError("Exact-file language probe failed or exceeded its bound.")
    payload = json.loads(result.stdout)
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("streams"), list)
        or len(payload["streams"]) > 128
    ):
        raise ValueError("Invalid exact-file stream inventory.")
    streams = payload["streams"]
    evidence = _language_evidence(streams, (), "0" * 64)
    if evidence is None:
        raise ValueError("Exact-file track evidence is unknown.")
    videos = [
        {"codec": r.get("codec_name"), "width": r.get("width"), "height": r.get("height")}
        for r in streams
        if isinstance(r, dict) and r.get("codec_type") == "video"
    ]
    unsigned = evidence_to_mapping(evidence)
    fingerprint = (
        before["content_sha256"]
        if storage_backend == "local"
        else digest([contract["resource_id"], file_locator, before, videos, unsigned])
    )
    evidence = replace(
        evidence,
        file_fingerprint=fingerprint,
        tracks=tuple(replace(t, file_fingerprint=fingerprint) for t in evidence.tracks),
    )
    after = bounded_bytes(path)
    compared = {
        k: v for k, v in before.items() if k not in {"content_sha256", "file_device", "file_inode"}
    }
    compared["fingerprint_kind"] = "bounded-identity-v1"
    if after != compared:
        raise ValueError("Physical publication file changed during the probe.")
    proof = {
        "anilist_id": anilist_id,
        "season_number": season_number,
        "torrent_hash": torrent_hash,
        "contract": contract,
        "file_locator": file_locator,
        "source": source_relative,
        "episodes": list(episodes),
        "bytes": before,
        "video": videos,
        "evidence": evidence_to_mapping(evidence),
        "scope": {"format": media_format, "sole_file": sole_file},
        "storage_backend": storage_backend,
    }
    # JSON canonicalization turns tuples into the closed wire-format arrays.
    return validate_file(json.loads(json.dumps(proof)))


def validate_source(path, proof):
    validate_file(proof)
    compared = {
        k: v
        for k, v in proof["bytes"].items()
        if k not in {"content_sha256", "file_device", "file_inode"}
    }
    compared["fingerprint_kind"] = "bounded-identity-v1"
    if bounded_bytes(path) != compared:
        raise ValueError("Approved bounded publication file changed.")

    if proof["storage_backend"] == "local":
        stat = path.stat()
        if (
            stat.st_dev != proof["bytes"]["file_device"]
            or stat.st_ino != proof["bytes"]["file_inode"]
        ):
            raise ValueError("Approved local publication file identity changed.")
    elif proof["scope"]["format"] == "MOVIE":
        from pathlib import PurePosixPath

        from plex_curator import _source_files

        folder = path
        for _ in PurePosixPath(proof["file_locator"]).parts:
            folder = folder.parent
        files = _source_files(folder, folder)
        if files != (path.resolve(strict=True),):
            raise ValueError("Approved anime movie is no longer the sole bound media file.")
