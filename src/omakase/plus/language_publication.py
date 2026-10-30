"""Closed publication-v3 facts; no viewer history or credential export."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re

from omakase.plus.language_policy import evaluate_language, evidence_from_mapping, rule_from_mapping

LANGUAGE_PUBLICATION_QUALIFIED = (
    os.getenv("OMAKASE_PLUS_LANGUAGE_PUBLICATION_QUALIFIED", "false").strip().lower() == "true"
)
CAPABILITY = "language-policy-v1"
CONTRACT_FIELDS = {
    "schema_version",
    "contract_id",
    "resource_id",
    "policy_revision",
    "policy",
    "inventory_generation",
    "source_generation",
}
FILE_FIELDS = {
    "anilist_id",
    "season_number",
    "torrent_hash",
    "contract",
    "file_locator",
    "source",
    "episodes",
    "bytes",
    "video",
    "evidence",
    "scope",
    "storage_backend",
}
BYTE_FIELDS = {
    "fingerprint_kind",
    "size",
    "mtime_ns",
    "first_bytes",
    "last_bytes",
    "first_sha256",
    "last_sha256",
}


def _require(value, message="Invalid language publication contract."):
    if not value:
        from omakase.plus.manifest import ManifestContractError

        raise ManifestContractError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_contract(value):
    _require(isinstance(value, dict) and set(value) == CONTRACT_FIELDS)
    _require(
        value["schema_version"] == CAPABILITY
        and type(value["contract_id"]) is int
        and value["contract_id"] > 0
    )
    _require(
        isinstance(value["resource_id"], str)
        and bool(re.fullmatch(r"[0-9a-f]{32}", value["resource_id"]))
    )
    for field in ("policy_revision", "inventory_generation", "source_generation"):
        _require(
            isinstance(value[field], str) and bool(re.fullmatch(r"[0-9a-f]{64}", value[field]))
        )
    rule = rule_from_mapping(value["policy"])
    _require(rule.revision == value["policy_revision"])
    return value


def _owner_source(db, row):
    from omakase.plus.households import resolve_scope
    from omakase.plus.language_source import require_current_source

    resource = db.execute(
        "SELECT * FROM library_resources WHERE id=?", (row["resource_id"],)
    ).fetchone()
    _require(resource is not None)
    item = db.execute(
        "SELECT library_id FROM library_items i JOIN libraries l ON l.id=i.library_id WHERE i.resource_id=? AND i.state='active' AND l.state='active' AND l.owner_user_id=? ORDER BY i.library_id LIMIT 1",
        (resource["id"], resource["owner_user_id"]),
    ).fetchone()
    _require(item is not None)
    scope = resolve_scope(db, viewer_id=resource["owner_user_id"], library_id=item[0])
    return resource, scope, require_current_source(db, scope=scope, resource=resource)


def export_contract(db, *, asset_id):
    row = db.execute(
        "SELECT c.*,a.inventory_fingerprint FROM publication_language_contracts c JOIN media_assets a ON a.id=c.asset_id WHERE c.asset_id=?",
        (asset_id,),
    ).fetchall()
    _require(len(row) == 1, "Publication contract is absent or ambiguous.")
    row = row[0]
    _, _, source = _owner_source(db, row)
    return validate_contract(
        {
            "schema_version": CAPABILITY,
            "contract_id": row["id"],
            "resource_id": row["resource_id"],
            "policy_revision": row["policy_revision"],
            "policy": json.loads(row["policy_json"]),
            "inventory_generation": row["inventory_fingerprint"],
            "source_generation": source.source_generation,
        }
    )


def validate_manifest(payload):
    from omakase.plus.manifest import _validate_library_manifest_v2

    _require(isinstance(payload, dict) and payload.get("schema_version") == "manifest-v3")
    clean = copy.deepcopy(payload)
    clean["schema_version"] = "manifest-v2"
    _require(isinstance(clean.get("titles"), list))
    for title in clean["titles"]:
        _require(isinstance(title, dict) and isinstance(title.get("assets"), list))
        for asset in title["assets"]:
            _require(isinstance(asset, dict))
            if "language_contract" in asset:
                validate_contract(asset.pop("language_contract"))
    _validate_library_manifest_v2(clean)
    _require(len(canonical(payload).encode()) <= 2 * 1024 * 1024)
    return payload


def validate_file(value):
    _require(isinstance(value, dict) and set(value) == FILE_FIELDS)
    contract = validate_contract(value["contract"])
    _require(type(value["anilist_id"]) is int and 0 < value["anilist_id"] <= 2147483647)
    _require(type(value["season_number"]) is int and 1 <= value["season_number"] <= 99)
    _require(
        isinstance(value["torrent_hash"], str)
        and bool(re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", value["torrent_hash"]))
    )
    locator = value["file_locator"]
    _require(
        isinstance(locator, str)
        and 0 < len(locator) <= 4096
        and not locator.startswith(("/", "\\"))
        and "\\" not in locator
        and not any(p in {"", ".", ".."} for p in locator.split("/"))
        and not any(ord(c) < 32 for c in locator)
    )
    source = value["source"]
    _require(
        isinstance(source, str)
        and 0 < len(source) <= 4096
        and not source.startswith(("/", "\\"))
        and "\\" not in source
        and not any(p in {"", ".", ".."} for p in source.split("/"))
        and not any(ord(c) < 32 for c in source)
    )
    episodes = value["episodes"]
    _require(
        isinstance(episodes, list)
        and 0 < len(episodes) <= 1000
        and all(type(e) is int and 1 <= e <= 5000 for e in episodes)
        and episodes == sorted(set(episodes))
    )
    scope = value["scope"]
    _require(
        isinstance(scope, dict)
        and set(scope) == {"format", "sole_file"}
        and isinstance(scope["format"], str)
        and scope["format"] in {"TV", "TV_SHORT", "ONA", "OVA", "SPECIAL", "MOVIE"}
        and type(scope["sole_file"]) is bool
    )
    _require(scope["format"] != "MOVIE" or (scope["sole_file"] and episodes == [1]))
    _require(
        isinstance(value["storage_backend"], str) and value["storage_backend"] in {"zurg", "local"}
    )
    byte = value["bytes"]
    _require(
        isinstance(byte, dict)
        and set(byte)
        == BYTE_FIELDS
        | (
            {"content_sha256", "file_device", "file_inode"}
            if value["storage_backend"] == "local"
            else set()
        )
        and byte["fingerprint_kind"]
        == (
            "full-content-sha256-v1"
            if value["storage_backend"] == "local"
            else "bounded-identity-v1"
        )
    )
    _require(
        type(byte["size"]) is int
        and byte["size"] > 0
        and type(byte["mtime_ns"]) is int
        and byte["mtime_ns"] >= 0
    )
    for key in ("first_bytes", "last_bytes"):
        _require(type(byte[key]) is int and byte[key] == min(65536, byte["size"]))
    for key in ("first_sha256", "last_sha256"):
        _require(isinstance(byte[key], str) and bool(re.fullmatch(r"[0-9a-f]{64}", byte[key])))
    if value["storage_backend"] == "local":
        _require(
            isinstance(byte["content_sha256"], str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", byte["content_sha256"]))
            and all(type(byte[k]) is int and byte[k] >= 0 for k in ("file_device", "file_inode"))
        )
        _require(
            byte["content_sha256"] == contract["inventory_generation"]
            and byte["content_sha256"].upper() == value["torrent_hash"]
        )
    videos = value["video"]
    _require(isinstance(videos, list) and 0 < len(videos) <= 16)
    for video in videos:
        _require(
            isinstance(video, dict)
            and set(video) == {"codec", "width", "height"}
            and isinstance(video["codec"], str)
            and video["codec"]
            in {"h264", "hevc", "h265", "av1", "vp9", "mpeg4", "mpeg2video", "vc1"}
            and all(type(video[k]) is int and 0 < video[k] <= 16384 for k in ("width", "height"))
        )
    evidence = evidence_from_mapping(value["evidence"])
    _require(
        evidence.kind == "exact_file"
        and evidence.inventory_complete
        and evidence.selection_context is None
        and not any(t.selected for t in evidence.tracks)
    )
    unsigned = copy.deepcopy(value["evidence"])
    unsigned["file_fingerprint"] = "0" * 64
    for track in unsigned["tracks"]:
        track["file_fingerprint"] = "0" * 64
    expected = (
        byte["content_sha256"]
        if value["storage_backend"] == "local"
        else digest([contract["resource_id"], locator, byte, videos, unsigned])
    )
    _require(
        expected == evidence.file_fingerprint,
        "Physical publication identity does not match bounded file facts.",
    )
    evaluated = evaluate_language(rule_from_mapping(contract["policy"]), evidence, domain="anime")
    _require(
        evaluated.physical_compatible, "Exact file does not satisfy the stable publication policy."
    )
    return value


def validate_receipt(payload):
    from omakase.plus.manifest import _validate_curator_receipt_v2

    _require(isinstance(payload, dict) and payload.get("schema_version") == "curator-receipt-v3")
    clean = dict(payload)
    files = clean.pop("language_files", None)
    _require(isinstance(files, list) and len(files) <= 4000)
    clean["schema_version"] = "curator-receipt-v2"
    _validate_curator_receipt_v2(clean)
    _require(len(canonical(payload).encode()) <= 1024 * 1024)
    seen = set()
    for file in files:
        validate_file(file)
        key = (file["contract"]["contract_id"], file["file_locator"])
        _require(key not in seen)
        seen.add(key)
    for file in files:
        coverage = next(
            (
                c
                for c in payload["title_coverage"]
                if c["anilist_id"] == file["anilist_id"]
                and (c["season_number"] or 1) == file["season_number"]
            ),
            None,
        )
        _require(
            coverage is not None and coverage["plex_visible_count"] >= len(file["episodes"]),
            "Language proof has no matching published title coverage.",
        )
    return payload


def import_files(db, receipt):
    validate_receipt(receipt)
    _require(
        receipt["mode"] == "live"
        and receipt["topology_converged"]
        and receipt["outcome"] in {"success", "degraded"},
        "Only converged live publication evidence can update current file proofs.",
    )
    from omakase.plus.manifest import build_library_manifest

    current = build_library_manifest(
        db,
        generated_at=receipt["manifest_generated_at"],
        peer_capabilities=(CAPABILITY,),
        qualification=True,
    )
    _import_current_files(db, receipt, current)


def _import_current_files(db, receipt, current):
    """Persist proofs only after v3 full scope or v4 exact selection was validated."""
    from omakase.plus.language_store import record_observation

    expected = {}
    for title in current["titles"]:
        for asset in title["assets"]:
            if "language_contract" in asset:
                expected[asset["language_contract"]["contract_id"]] = (title, asset)
    present = {}
    for file in receipt["language_files"]:
        identifier = file["contract"]["contract_id"]
        _require(identifier in expected, "Receipt language file is not in the current manifest.")
        present.setdefault(identifier, set()).update(file["episodes"])
    for identifier, (title, asset) in expected.items():
        blocked = any(
            b["anilist_id"] == title["anilist_id"]
            and b["torrent_hash"] == asset["torrent_hash"]
            and b["affected_episodes"] == asset["episode_coverage"]
            for b in receipt["blocked_assets"]
        )
        if blocked:
            _require(
                identifier not in present,
                "Blocked publication asset also contains successful proofs.",
            )
        else:
            _require(
                present.get(identifier, set()) == set(asset["episode_coverage"]),
                "Receipt omits exact language publication coverage.",
            )
        asset_id = db.execute(
            "SELECT asset_id FROM publication_language_contracts WHERE id=?", (identifier,)
        ).fetchone()[0]
        locators = {
            r[0]
            for r in db.execute(
                "SELECT file_locator FROM media_file_probes WHERE asset_id=? UNION SELECT file_locator FROM language_publication_current_files WHERE asset_id=?",
                (asset_id, asset_id),
            )
        }
        for locator in locators:
            db.execute(
                "INSERT INTO language_publication_current_files(asset_id,file_locator,observation_id,curator_run_id,completed_at) VALUES(?,?,NULL,?,?) ON CONFLICT(asset_id,file_locator) DO UPDATE SET observation_id=NULL,curator_run_id=excluded.curator_run_id,completed_at=excluded.completed_at",
                (asset_id, locator, receipt["run_id"], receipt["completed_at"]),
            )
    for file in receipt["language_files"]:
        validate_file(file)
        contract = file["contract"]
        row = db.execute(
            "SELECT c.*,a.monitor_id,a.torrent_hash,a.source_folder,a.inventory_fingerprint,a.usable_coverage_json,a.status,a.inventory_verified_at,m.anilist_id,m.season_number,m.format,m.user_id,a.storage_backend,a.storage_locator FROM publication_language_contracts c JOIN media_assets a ON a.id=c.asset_id JOIN media_monitors m ON m.id=a.monitor_id WHERE c.id=?",
            (contract["contract_id"],),
        ).fetchone()
        _require(
            row is not None
            and row["status"] == "downloaded"
            and row["inventory_verified_at"] is not None
        )
        _require(
            export_contract(db, asset_id=row["asset_id"]) == contract,
            "Publication source, inventory or approved policy changed.",
        )
        _require(
            file["storage_backend"] == (row["storage_backend"] or "zurg")
            and file["scope"]["format"] == row["format"]
        )
        if row["storage_backend"] == "local":
            _require(file["source"] == file["file_locator"] == row["storage_locator"])
        else:
            _require(file["source"] == row["source_folder"] + "/" + file["file_locator"])
        if row["format"] == "MOVIE":
            _require(
                sum(
                    f["contract"]["contract_id"] == contract["contract_id"]
                    for f in receipt["language_files"]
                )
                == 1
                and json.loads(row["usable_coverage_json"]) == [1]
            )
        _require(
            row["anilist_id"] == file["anilist_id"]
            and (row["season_number"] or 1) == file["season_number"]
            and row["torrent_hash"].upper() == file["torrent_hash"]
            and set(file["episodes"]) <= set(json.loads(row["usable_coverage_json"]))
        )
        _require(
            not db.execute(
                "SELECT 1 FROM media_title_removals WHERE monitor_id=?", (row["monitor_id"],)
            ).fetchone()
        )
        resource, scope, _ = _owner_source(db, row)
        _require(
            resource["legacy_id"] == str(row["monitor_id"])
            and resource["owner_user_id"] == row["user_id"]
        )
        evidence = evidence_from_mapping(file["evidence"])
        db.execute(
            "INSERT OR IGNORE INTO media_file_probes(asset_id,file_locator,inventory_fingerprint,byte_range_receipt_json,video_streams_json,audio_streams_json,outcome,detail,verified_at) VALUES(?,?,?,?,?,?,'usable','Policy-bound bounded identity; full-content hash not performed.',?)",
            (
                row["asset_id"],
                file["file_locator"],
                evidence.file_fingerprint,
                canonical({**file["bytes"], "publication_scope": file["scope"]}),
                canonical(file["video"]),
                canonical([t for t in file["evidence"]["tracks"] if t["kind"] == "audio"]),
                receipt["completed_at"],
            ),
        )
        probe = db.execute(
            "SELECT id FROM media_file_probes WHERE asset_id=? AND file_locator=? AND inventory_fingerprint=?",
            (row["asset_id"], file["file_locator"], evidence.file_fingerprint),
        ).fetchone()[0]
        observation_id = record_observation(
            db,
            observer_user_id=resource["owner_user_id"],
            resource_id=resource["id"],
            evidence=evidence,
            episodes=file["episodes"],
            source_generation=contract["source_generation"],
            asset_id=row["asset_id"],
            probe_id=probe,
            observed_at=receipt["completed_at"],
            library_id=scope.library_id,
        )

        db.execute(
            "INSERT INTO language_publication_current_files(asset_id,file_locator,observation_id,curator_run_id,completed_at) VALUES(?,?,?,?,?) ON CONFLICT(asset_id,file_locator) DO UPDATE SET observation_id=excluded.observation_id,curator_run_id=excluded.curator_run_id,completed_at=excluded.completed_at",
            (
                row["asset_id"],
                file["file_locator"],
                observation_id,
                receipt["run_id"],
                receipt["completed_at"],
            ),
        )


CURRENT_FILE_SQL = """NOT EXISTS (SELECT 1 FROM language_publication_current_files current_file LEFT JOIN asset_language_observations current_observation ON current_observation.id=current_file.observation_id WHERE current_file.asset_id=o.asset_id AND current_file.file_locator=p.file_locator AND (current_file.observation_id IS NULL OR current_observation.file_fingerprint<>o.file_fingerprint))"""


def require_current_file(db, observation):
    if observation["asset_id"] is None:
        return
    local = db.execute(
        "SELECT a.storage_backend,a.storage_locator,p.byte_range_receipt_json FROM media_assets a JOIN media_file_probes p ON p.asset_id=a.id WHERE a.id=? AND p.id=?",
        (observation["asset_id"], observation["probe_id"]),
    ).fetchone()
    if local is not None and local["storage_backend"] == "local":
        from omakase.plus.language_source import configured_local_root
        from omakase.plus.storage import require_media_file

        resource = db.execute(
            "SELECT * FROM library_resources WHERE id=?", (observation["resource_id"],)
        ).fetchone()
        root = configured_local_root(db, resource)
        _require(root is not None, "Current local root is unavailable.")
        try:
            path = require_media_file(root, local["storage_locator"])
            stat = path.stat()
        except (ValueError, OSError):
            _require(False, "Current local file is unavailable.")
        byte = json.loads(local["byte_range_receipt_json"])
        bound = byte.get("publication_stat")
        if bound is None and byte.get("fingerprint_kind") == "full-content-sha256-v1":
            bound = {
                "size": byte["size"],
                "mtime_ns": byte["mtime_ns"],
                "device": byte["file_device"],
                "inode": byte["file_inode"],
            }
        _require(
            bound
            == {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "device": stat.st_dev,
                "inode": stat.st_ino,
            },
            "Local file inventory changed; exact reinspection is required.",
        )
    row = db.execute(
        "SELECT f.observation_id,o.file_fingerprint FROM language_publication_current_files f LEFT JOIN asset_language_observations o ON o.id=f.observation_id JOIN media_file_probes p ON p.asset_id=f.asset_id AND p.file_locator=f.file_locator WHERE p.id=?",
        (observation["probe_id"],),
    ).fetchone()
    _require(
        row is None
        or (
            row["observation_id"] is not None
            and row["file_fingerprint"] == observation["file_fingerprint"]
        ),
        "Physical publication file proof was superseded or withheld; recheck current file.",
    )
