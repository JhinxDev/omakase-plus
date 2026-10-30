"""Negotiated publication-v4: current qualification plus an exact whole-file selection."""

from __future__ import annotations

import copy
import os
import re
from pathlib import PurePosixPath

from omakase.plus.language_publication import canonical, digest, validate_file

CAPABILITY = "quality-fallback-v1"
SELECTION = "series-quality-fallback-v1"
QUALITY_PUBLICATION_QUALIFIED = (
    os.getenv("OMAKASE_PLUS_QUALITY_PUBLICATION_QUALIFIED", "false").strip().lower() == "true"
)
FILE_FIELDS = {
    "anilist_id",
    "season_number",
    "torrent_hash",
    "inventory_generation",
    "source_generation",
    "source",
    "episodes",
    "stat",
    "video",
    "language_contract_id",
    "qualification",
    "selected",
}
RANK = {"2160p": 3, "1080p": 2, "720p": 1}


def require(value, detail="Invalid qualified quality selection."):
    if not value:
        raise ValueError(detail)


def priority(asset):
    contract = asset.get("language_contract")
    return (
        0 if contract else 1,
        canonical(contract["policy"]) if contract else "",
        -RANK.get(asset["quality"], 0),
        asset["torrent_hash"],
    )


def source_generation(db, asset_id):
    """Opaque current connector binding; credentials never leave this calculation."""
    from omakase.plus import connections
    from omakase.plus.language_publication import export_contract

    if db.execute(
        "SELECT 1 FROM publication_language_contracts WHERE asset_id=?", (asset_id,)
    ).fetchone():
        return export_contract(db, asset_id=asset_id)["source_generation"]
    row = db.execute(
        "SELECT a.storage_backend,a.storage_locator,a.source_folder,m.user_id,p.kind,p.enabled,p.config_json FROM media_assets a JOIN media_monitors m ON m.id=a.monitor_id LEFT JOIN media_providers p ON p.id=a.provider_id WHERE a.id=?",
        (asset_id,),
    ).fetchone()
    require(row is not None)
    url, token = connections._values(db, row["user_id"], connections._spec("realdebrid"))
    settings = [
        tuple(r)
        for r in db.execute(
            "SELECT library_id,generation FROM library_settings WHERE owner_user_id=? AND integration='realdebrid' ORDER BY library_id",
            (row["user_id"],),
        )
    ]
    return digest(
        [
            row["storage_backend"],
            row["storage_locator"],
            row["source_folder"],
            row["kind"] or "realdebrid",
            row["enabled"],
            row["config_json"],
            url,
            token,
            settings,
        ]
    )


def manifest_digest(manifest):
    contract = copy.deepcopy(
        {key: value for key, value in manifest.items() if key != "generated_at"}
    )
    # Aggregate indexing observations are not source/selection identity. A new
    # receipt must not invalidate itself by updating those observed counts.
    for title in contract["titles"]:
        title["coverage"].pop("plex_visible_episodes", None)
        title["coverage"].pop("plex_visible_count", None)
    return digest(contract)


def validate_manifest(payload):
    from omakase.plus.language_publication import validate_manifest as validate_v3

    require(isinstance(payload, dict) and payload.get("schema_version") == "manifest-v4")
    clean = copy.deepcopy(payload)
    require(clean.pop("quality_selection", None) == SELECTION)
    clean["schema_version"] = "manifest-v3"
    for title in clean.get("titles", []):
        original = next(t for t in payload["titles"] if t["anilist_id"] == title["anilist_id"])
        applicable_policies(original)
        require(original["assets"] == sorted(original["assets"], key=priority))
        for asset in title["assets"]:
            generation = asset.pop("inventory_generation", None)
            require(isinstance(generation, str) and re.fullmatch(r"[0-9a-f]{64}", generation))
            source = asset.pop("source_generation", None)
            require(isinstance(source, str) and re.fullmatch(r"[0-9a-f]{64}", source))
            if asset.get("language_contract"):
                require(asset["language_contract"]["source_generation"] == source)
    validate_v3(clean)
    require(len(canonical(payload).encode()) <= 2 * 1024 * 1024)
    return payload


def applicable_policies(title):
    policies = {}
    for asset in title["assets"]:
        if asset.get("language_contract"):
            policy = canonical(asset["language_contract"]["policy"])
            for episode in asset["episode_coverage"]:
                require(
                    episode not in policies or policies[episode] == policy,
                    "Incompatible approved publication policies overlap; an explicit resolution is required.",
                )
                policies[episode] = policy
    return policies


def validate_publication_file(file):
    require(isinstance(file, dict) and set(file) == FILE_FIELDS)
    require(type(file["anilist_id"]) is int and 0 < file["anilist_id"] <= 2147483647)
    require(type(file["season_number"]) is int and 1 <= file["season_number"] <= 99)
    require(
        isinstance(file["torrent_hash"], str)
        and re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", file["torrent_hash"])
    )
    require(
        isinstance(file["inventory_generation"], str)
        and re.fullmatch(r"[0-9a-f]{64}", file["inventory_generation"])
    )
    require(
        isinstance(file["source_generation"], str)
        and re.fullmatch(r"[0-9a-f]{64}", file["source_generation"])
    )
    source = file["source"]
    require(
        isinstance(source, str)
        and 0 < len(source) <= 4096
        and not source.startswith(("/", "\\"))
        and "\\" not in source
        and not any(p in {"", ".", ".."} for p in source.split("/"))
        and not any(ord(c) < 32 for c in source)
    )
    episodes = file["episodes"]
    require(
        isinstance(episodes, list)
        and 0 < len(episodes) <= 1000
        and all(type(e) is int and 1 <= e <= 5000 for e in episodes)
        and episodes == sorted(set(episodes))
    )
    stat = file["stat"]
    require(
        isinstance(stat, dict)
        and set(stat) == {"size", "mtime_ns", "file_device", "file_inode"}
        and all(type(v) is int and v >= 0 for v in stat.values())
        and stat["size"] > 0
    )
    require(type(file["selected"]) is bool)
    identifier = file["language_contract_id"]
    require(identifier is None or (type(identifier) is int and identifier > 0))
    require(file["qualification"] == ("language-policy-v1" if identifier else "legacy-english-v1"))
    videos = file["video"]
    require(isinstance(videos, list) and len(videos) <= 16)
    for video in videos:
        require(
            isinstance(video, dict)
            and set(video) == {"codec", "width", "height"}
            and video["codec"]
            in {"h264", "hevc", "h265", "av1", "vp9", "mpeg4", "mpeg2video", "vc1"}
            and all(type(video[k]) is int and 0 < video[k] <= 16384 for k in ("width", "height"))
        )
    require(
        identifier is not None or not videos,
        "Legacy subtitle qualification does not measure video resolution.",
    )
    return file


def file_key(file):
    return (file["anilist_id"], file["season_number"], file["torrent_hash"], file["source"])


def _whole_file_cover(files, target):
    """Bounded exact cover, independently for connected physical episode groups."""
    parent = {episode: episode for episode in target}

    def root(episode):
        while parent[episode] != episode:
            parent[episode] = parent[parent[episode]]
            episode = parent[episode]
        return episode

    for file in files:
        first = file["episodes"][0]
        for episode in file["episodes"][1:]:
            parent[root(episode)] = root(first)
    groups = {}
    for episode in target:
        groups.setdefault(root(episode), set()).add(episode)
    selected = set()
    for group in sorted(groups.values(), key=min):
        options = {}
        for index, file in enumerate(files):
            if file["episodes"][0] in group:
                for episode in file["episodes"]:
                    options.setdefault(episode, []).append(index)
        pending = [(frozenset(group), ())]
        seen = set()
        solution = None
        while pending:
            remaining, chosen = pending.pop()
            if not remaining:
                solution = chosen
                break
            if remaining in seen:
                continue
            seen.add(remaining)
            require(
                len(seen) <= 4096,
                "Whole-file selection exceeded its bounded search; retain current generation.",
            )
            episode = min(remaining)
            for index in reversed(options.get(episode, [])):
                coverage = frozenset(files[index]["episodes"])
                if coverage <= remaining:
                    pending.append((remaining - coverage, chosen + (index,)))
        if solution is None:
            return None
        selected.update(file_key(files[index]) for index in solution)
    return selected


def derive_selection(manifest, files, blocked):
    """Reconstruct exactly from all qualified files; failed sources claim nothing."""
    validate_manifest(manifest)
    require(isinstance(files, (list, tuple)) and len(files) <= 4000)
    for file in files:
        validate_publication_file(file)
    require(len({file_key(file) for file in files}) == len(files))
    available = {file_key(file) for file in files}
    known = set()
    known_blocks = set()
    ordered = []
    selected = set()
    hold = False
    for title in manifest["titles"]:
        candidates = []
        union = set()
        overlap = False
        declared = set()
        policies = applicable_policies(title)
        for asset in title["assets"]:
            coverage = set(asset["episode_coverage"])
            overlap = overlap or bool(declared & coverage)
            declared.update(coverage)
            matched = sorted(
                (
                    f
                    for f in files
                    if f["anilist_id"] == title["anilist_id"]
                    and f["season_number"] == (title["season_number"] or 1)
                    and f["torrent_hash"] == asset["torrent_hash"]
                ),
                key=lambda f: f["source"],
            )
            failures = [
                b
                for b in blocked
                if b["anilist_id"] == title["anilist_id"]
                and b["season_number"]
                == (None if title["format"] == "MOVIE" else title["season_number"] or 1)
                and b["torrent_hash"] == asset["torrent_hash"]
            ]
            if failures:
                require(
                    len(failures) == 1
                    and failures[0]["affected_episodes"] == asset["episode_coverage"]
                    and not matched
                )
                known_blocks.add((title["anilist_id"], asset["torrent_hash"]))
                continue
            require(
                matched, "A manifest source has neither full qualification nor an exact failure."
            )
            covered = set()
            for file in matched:
                require(file["inventory_generation"] == asset["inventory_generation"])
                require(file["source_generation"] == asset["source_generation"])
                require(not covered.intersection(file["episodes"]))
                covered.update(file["episodes"])
                contract = asset.get("language_contract")
                require(
                    file["language_contract_id"] == (contract["contract_id"] if contract else None)
                )
                if asset["storage_backend"] == "local":
                    require(file["source"] == asset["storage_locator"])
                else:
                    require(file["source"].startswith(asset["source_folder"] + "/"))
                if title["format"] != "MOVIE":
                    from omakase.plus.publication_episodes import parse_file_episode

                    parsed = parse_file_episode(
                        PurePosixPath(file["source"]).name,
                        season=title["season_number"] or 1,
                        expected=title["expected_episode_count"],
                        numbering_override=asset.get("numbering_override"),
                        anilist_id=title["anilist_id"],
                    )
                    require(
                        not parsed.special and parsed.episodes == set(file["episodes"]),
                        "Physical episode range does not match exact file selection.",
                    )
                known.add(file_key(file))
            require(
                covered == coverage, "Qualification omits a source's exact full episode inventory."
            )
            policy = (
                canonical(asset["language_contract"]["policy"])
                if asset.get("language_contract")
                else None
            )
            compatible = [
                file
                for file in matched
                if all(
                    episode not in policies or policies[episode] == policy
                    for episode in file["episodes"]
                )
            ]
            union.update(episode for file in compatible for episode in file["episodes"])
            candidates.extend(compatible)
            ordered.extend(matched)
        if title["format"] == "MOVIE":
            selected.update(file_key(file) for file in candidates)
            continue
        partition = _whole_file_cover(candidates, union)
        if partition is None or (overlap and union != declared):
            hold = True
        elif partition:
            selected.update(partition)
    require(
        known == available and len(known_blocks) == len(blocked),
        "Foreign publication source or failure.",
    )
    require(list(files) == ordered, "Qualified files are not in deterministic manifest order.")
    return (set() if hold else selected), hold


def validate_receipt(payload):
    from omakase.plus.manifest import _validate_curator_receipt_v2

    require(isinstance(payload, dict) and payload.get("schema_version") == "curator-receipt-v4")
    clean = copy.deepcopy(payload)
    require(clean.pop("quality_selection", None) == SELECTION)
    digest_value = clean.pop("manifest_sha256", None)
    require(isinstance(digest_value, str) and re.fullmatch(r"[0-9a-f]{64}", digest_value))
    require(type(clean.pop("selection_applied", None)) is bool)
    files = clean.pop("publication_files", None)
    languages = clean.pop("language_files", None)
    require(
        isinstance(files, list)
        and len(files) <= 4000
        and isinstance(languages, list)
        and len(languages) <= 4000
    )
    for file in files:
        validate_publication_file(file)
    require(len({file_key(file) for file in files}) == len(files))
    for language in languages:
        validate_file(language)
    clean["schema_version"] = "curator-receipt-v2"
    _validate_curator_receipt_v2(clean)
    require(len(canonical(payload).encode()) <= 1024 * 1024)
    return payload


def validate_current_selection(db, receipt):
    from omakase.plus.language_publication import CAPABILITY as LANGUAGE_CAPABILITY
    from omakase.plus.manifest import build_library_manifest

    validate_receipt(receipt)
    manifest = build_library_manifest(
        db,
        generated_at=receipt["manifest_generated_at"],
        peer_capabilities=(LANGUAGE_CAPABILITY, CAPABILITY),
        qualification=True,
        quality_qualification=True,
    )
    require(
        manifest_digest(manifest) == receipt["manifest_sha256"],
        "Publication inventory or approved source policy changed.",
    )
    selected, hold = derive_selection(
        manifest, receipt["publication_files"], receipt["blocked_assets"]
    )
    require(
        all(
            file["selected"] == (file_key(file) in selected)
            for file in receipt["publication_files"]
        ),
        "Publication selection differs from the exact qualified partition.",
    )
    require(not receipt["selection_applied"] or not hold)
    by_source = {
        file_key(file): file
        for file in receipt["publication_files"]
        if file["language_contract_id"] is not None
    }
    require(len(by_source) == len(receipt["language_files"]))
    for proof in receipt["language_files"]:
        key = (proof["anilist_id"], proof["season_number"], proof["torrent_hash"], proof["source"])
        file = by_source.pop(key, None)
        require(
            file is not None
            and file["language_contract_id"] == proof["contract"]["contract_id"]
            and file["inventory_generation"] == proof["contract"]["inventory_generation"]
            and file["episodes"] == proof["episodes"]
            and file["video"] == proof["video"]
        )
        require(
            file["stat"]["size"] == proof["bytes"]["size"]
            and file["stat"]["mtime_ns"] == proof["bytes"]["mtime_ns"]
        )
        asset = next(
            a
            for t in manifest["titles"]
            if t["anilist_id"] == proof["anilist_id"]
            for a in t["assets"]
            if a["torrent_hash"] == proof["torrent_hash"]
        )
        require(proof["contract"] == asset.get("language_contract"))
    return manifest


def import_selection(db, receipt):
    from omakase.plus.language_publication import _import_current_files

    manifest = validate_current_selection(db, receipt)
    if not receipt["selection_applied"]:
        return
    selected = [file for file in receipt["publication_files"] if file["selected"]]
    current = copy.deepcopy(manifest)
    for title in current["titles"]:
        for asset in title["assets"]:
            asset["episode_coverage"] = sorted(
                {
                    episode
                    for file in selected
                    if file["anilist_id"] == title["anilist_id"]
                    and file["torrent_hash"] == asset["torrent_hash"]
                    for episode in file["episodes"]
                }
            )
    receipt = {
        **receipt,
        "language_files": [
            proof
            for proof in receipt["language_files"]
            if any(
                file_key(file)
                == (
                    proof["anilist_id"],
                    proof["season_number"],
                    proof["torrent_hash"],
                    proof["source"],
                )
                for file in selected
            )
        ],
    }
    _import_current_files(db, receipt, current)


def failure_binding(db, asset_id):
    from omakase.plus.language_publication import export_contract

    row = db.execute(
        "SELECT a.*,m.anilist_id,m.title,m.format,m.season_number FROM media_assets a JOIN media_monitors m ON m.id=a.monitor_id WHERE a.id=?",
        (asset_id,),
    ).fetchone()
    require(row is not None)
    contract = (
        export_contract(db, asset_id=asset_id)
        if db.execute(
            "SELECT 1 FROM publication_language_contracts WHERE asset_id=?", (asset_id,)
        ).fetchone()
        else None
    )
    return {
        "i": row["inventory_fingerprint"],
        "s": source_generation(db, asset_id),
        "p": digest(contract) if contract else None,
        "l": digest(
            [
                row["anilist_id"],
                row["title"],
                row["format"],
                row["season_number"],
                row["source_title"],
                row["source_folder"],
                row["storage_backend"],
                row["storage_locator"],
                row["numbering_override"],
            ]
        ),
    }


def record_failures(db, receipt):
    """Retain scoped publication failures without changing the current selection."""
    from omakase.plus.media_store import record_candidate_attempt, record_sync_run

    if receipt["mode"] != "live":
        return
    failures = list(receipt["blocked_assets"])
    groups = {}
    for file in receipt["publication_files"]:
        groups.setdefault(
            (file["anilist_id"], file["season_number"], file["torrent_hash"]), []
        ).append(file)
    for (anilist_id, season, torrent_hash), files in groups.items():
        if any(file["selected"] for file in files):
            continue
        movie = any(
            title["anilist_id"] == anilist_id and title["section"] == "Anime Movies"
            for title in receipt["title_coverage"]
        )
        failures.append(
            {
                "anilist_id": anilist_id,
                "season_number": None if movie else season,
                "torrent_hash": torrent_hash,
                "affected_episodes": sorted(
                    {episode for file in files for episode in file["episodes"]}
                ),
                "code": "SELECTION_DEFERRED",
                "detail": "This release was not selected. Existing usable episode coverage and approved language scope were preserved.",
            }
        )
    for failure in failures:
        rows = db.execute(
            "SELECT a.*,m.user_id FROM media_assets a JOIN media_monitors m ON m.id=a.monitor_id WHERE m.anilist_id=? AND m.season_number IS ? AND upper(a.torrent_hash)=? AND a.status='downloaded' AND a.inventory_verified_at IS NOT NULL",
            (failure["anilist_id"], failure["season_number"], failure["torrent_hash"]),
        ).fetchall()
        for asset in rows:
            detail = canonical(
                {
                    "v": 1,
                    **failure_binding(db, asset["id"]),
                    "r": receipt["run_id"],
                    "c": failure["code"],
                }
            )
            require(len(detail) <= 500)
            if db.execute(
                "SELECT 1 FROM media_candidate_attempts WHERE asset_id=? AND decision='PUBLICATION_WITHHELD' AND reason=?",
                (asset["id"], detail),
            ).fetchone():
                continue
            run = record_sync_run(
                db,
                monitor_id=asset["monitor_id"],
                outcome="publication_withheld",
                detail=failure["detail"],
                completed_at=receipt["completed_at"],
            )
            record_candidate_attempt(
                db,
                monitor_id=asset["monitor_id"],
                sync_run_id=run,
                asset_id=asset["id"],
                search_alias="publication",
                source_title=asset["source_title"],
                torrent_hash=asset["torrent_hash"],
                candidate_rank=0,
                quality=asset["quality"],
                seeders=asset["seeders"],
                proposed_coverage=failure["affected_episodes"],
                decision="PUBLICATION_WITHHELD",
                reason=detail,
                rd_outcome="not_requested",
                provider_kind="publication",
                catalog_kind="curator",
                terminal_at=receipt["completed_at"],
            )


def current_failed_hashes(db, monitor_id):
    """A new unrelated candidate cannot erase a still-bound failed copy."""
    import json

    failed = set()
    latest = db.execute(
        "SELECT receipt_json,completed_at FROM media_curator_receipt_latest WHERE id=1"
    ).fetchone()
    try:
        latest_files = json.loads(latest[0]).get("publication_files", []) if latest else []
    except (ValueError, TypeError):
        latest_files = []
    for row in db.execute(
        "SELECT attempt.asset_id,attempt.torrent_hash,attempt.reason,attempt.terminal_at FROM media_candidate_attempts attempt WHERE attempt.monitor_id=? AND attempt.decision='PUBLICATION_WITHHELD' ORDER BY attempt.terminal_at DESC,attempt.id DESC",
        (monitor_id,),
    ):
        try:
            evidence = json.loads(row["reason"])
            binding = failure_binding(db, row["asset_id"])
            if (
                set(evidence) != {"v", "i", "s", "p", "l", "r", "c"}
                or evidence["v"] != 1
                or any(evidence[key] != value for key, value in binding.items())
            ):
                continue
            if (
                latest
                and latest["completed_at"] >= row["terminal_at"]
                and any(
                    file["selected"]
                    and file["torrent_hash"] == row["torrent_hash"]
                    and file["inventory_generation"] == binding["i"]
                    and file["source_generation"] == binding["s"]
                    for file in latest_files
                )
            ):
                continue
            failed.add(row["torrent_hash"])
        except (TypeError, ValueError, KeyError, OSError):
            continue
    return failed
