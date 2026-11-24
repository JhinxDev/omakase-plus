"""Read-only episode quality with release claims distinct from current file proof."""

from __future__ import annotations

import functools
import json
from pathlib import PurePosixPath

from omakase.plus.media_store import verified_asset_coverage

QUALITY_RANK = {"2160p": 3, "1080p": 2, "720p": 1}


def _current_file_quality(db, asset):
    from omakase.plus.language_policy import (
        evaluate_language,
        evidence_from_mapping,
        rule_from_mapping,
    )
    from omakase.plus.language_publication import export_contract, require_current_file

    try:
        contract = export_contract(db, asset_id=asset["id"])
    except (TypeError, ValueError, OSError):
        return {}
    rows = db.execute(
        """SELECT o.*, p.video_streams_json, p.outcome AS probe_outcome
           FROM language_publication_current_files current_file
           JOIN asset_language_observations o ON o.id=current_file.observation_id
             AND o.asset_id=current_file.asset_id
           JOIN media_file_probes p ON p.id=o.probe_id AND p.asset_id=o.asset_id
             AND p.file_locator=current_file.file_locator
             AND p.inventory_fingerprint=o.file_fingerprint
           WHERE current_file.asset_id=? AND o.inventory_generation=?
             AND o.source_generation=? AND o.resource_id=?""",
        (
            asset["id"],
            asset["inventory_fingerprint"],
            contract["source_generation"],
            contract["resource_id"],
        ),
    ).fetchall()
    observed = {}
    for row in rows:
        try:
            require_current_file(db, row)
            if (
                row["probe_outcome"] != "usable"
                or not evaluate_language(
                    rule_from_mapping(contract["policy"]),
                    evidence_from_mapping(json.loads(row["evidence_json"])),
                    domain="anime",
                ).physical_compatible
            ):
                continue
            dimensions = {
                (video.get("width"), video.get("height"))
                for video in json.loads(row["video_streams_json"])
                if isinstance(video, dict)
                and type(video.get("width")) is int
                and type(video.get("height")) is int
                and 0 < video["width"] <= 16384
                and 0 < video["height"] <= 16384
            }
            # Multiple video streams do not identify which one a player selected.
            if len(dimensions) != 1:
                continue
            width, height = dimensions.pop()
            quality = (
                "2160p"
                if width >= 3840 or height >= 2160
                else "1080p"
                if width >= 1920 or height >= 1080
                else "720p"
                if width >= 1280 or height >= 720
                else "unknown"
            )
            value = {
                "quality": quality,
                "source": "file",
                "label": f"{quality} verified"
                if quality != "unknown"
                else f"{width}×{height} video",
                "dimensions": [width, height],
                "detail": f"{width}×{height} video; current source and file evidence.",
            }
            for episode in json.loads(row["episodes_json"]):
                if type(episode) is int and episode > 0:
                    observed.setdefault(episode, []).append(value)
        except (TypeError, ValueError, OSError, KeyError):
            continue
    return {
        episode: values[0]
        for episode, values in observed.items()
        if all(value == values[0] for value in values)
    }


def _index_receipt(receipt):
    files_by_key = {}
    for file in receipt.get("publication_files") or ():
        if file.get("selected"):
            key = (file["torrent_hash"], file["anilist_id"], file["season_number"])
            files_by_key.setdefault(key, []).append(file)
    coverage_keys = frozenset(
        (title["anilist_id"], title["section"], title["season_number"])
        for title in (receipt.get("title_coverage") or ())
    )
    return files_by_key, coverage_keys


@functools.lru_cache(maxsize=8)
def _validated_receipt_bundle(raw_json: str):
    from omakase.plus.quality_publication import validate_receipt

    try:
        receipt = validate_receipt(json.loads(raw_json))
        if (
            not receipt["selection_applied"]
            or receipt["mode"] != "live"
            or not receipt["topology_converged"]
        ):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    files_by_key, coverage_keys = _index_receipt(receipt)
    return receipt, files_by_key, coverage_keys


def read_publication_receipt(db):
    """Read the current qualified publication for an episode-quality view."""
    latest = db.execute(
        "SELECT receipt_json FROM media_curator_receipt_latest WHERE id=1"
    ).fetchone()
    if latest is None or not isinstance(latest[0], str):
        return None
    bundle = _validated_receipt_bundle(latest[0])
    return bundle[0] if bundle is not None else None


def _selected_files(db, assets, *, read_receipt=None):
    from omakase.plus.quality_publication import source_generation

    if read_receipt is None:
        latest = db.execute(
            "SELECT receipt_json FROM media_curator_receipt_latest WHERE id=1"
        ).fetchone()
        bundle = (
            _validated_receipt_bundle(latest[0])
            if latest is not None and isinstance(latest[0], str)
            else None
        )
        if bundle is None:
            return {}
        _receipt, files_by_key, coverage_keys = bundle
    else:
        receipt = read_receipt()
        if receipt is None:
            return {}
        files_by_key, coverage_keys = _index_receipt(receipt)
    result = {}
    for asset in assets:
        if not verified_asset_coverage(asset):
            continue
        try:
            binding = source_generation(db, asset["id"])
        except (TypeError, ValueError, OSError):
            continue
        monitor = db.execute(
            "SELECT * FROM media_monitors WHERE id=?", (asset["monitor_id"],)
        ).fetchone()
        if monitor is None:
            continue
        section = "Anime Movies" if monitor["format"] == "MOVIE" else "Anime"
        expected_season = None if monitor["format"] == "MOVIE" else (monitor["season_number"] or 1)
        if (monitor["anilist_id"], section, expected_season) not in coverage_keys:
            continue
        candidates = files_by_key.get(
            (
                str(asset["torrent_hash"]).upper(),
                monitor["anilist_id"],
                monitor["season_number"] or 1,
            ),
            (),
        )
        matches = []
        for file in candidates:
            if (
                file["inventory_generation"] != asset["inventory_fingerprint"]
                or file["source_generation"] != binding
            ):
                continue
            try:
                from omakase.plus.identity import identity_rejection_reason
                from omakase.plus.publication_episodes import parse_file_episode

                if identity_rejection_reason(
                    str(asset["source_title"]),
                    target_title=monitor["title"],
                    target_aliases=json.loads(monitor["search_titles_json"]),
                    season=monitor["season_number"] or 1,
                    anilist_id=monitor["anilist_id"],
                    media_format=monitor["format"],
                ):
                    continue
                if (asset["storage_backend"] or "zurg") == "local":
                    if file["source"] != asset["storage_locator"]:
                        continue
                elif not file["source"].startswith(asset["source_folder"] + "/"):
                    continue
                if not set(file["episodes"]) <= verified_asset_coverage(asset):
                    continue
                if monitor["format"] != "MOVIE":
                    parsed = parse_file_episode(
                        PurePosixPath(file["source"]).name,
                        season=monitor["season_number"] or 1,
                        expected=monitor["expected_episodes"],
                        numbering_override=asset["numbering_override"],
                        anilist_id=monitor["anilist_id"],
                    )
                    if parsed.special or parsed.episodes != set(file["episodes"]):
                        continue
                elif file["episodes"] != [1]:
                    continue
                matches.append(file)
            except (TypeError, ValueError, KeyError):
                continue
        if matches:
            result[int(asset["id"])] = matches
    return result


def episode_qualities(db, assets, *, read_receipt=None):
    """Keep the selected fallback until a qualified publication replaces it."""
    result = {}
    selected = _selected_files(db, assets, read_receipt=read_receipt)
    alternatives = {}
    for asset in assets:
        coverage = verified_asset_coverage(asset)
        if not coverage:
            continue
        quality = str(asset["quality"] or "").lower()
        quality = quality if quality in QUALITY_RANK else "unknown"
        release = {
            "quality": quality,
            "source": "release",
            "label": "720p fallback release"
            if quality == "720p"
            else f"{quality} release"
            if quality != "unknown"
            else "Quality unknown",
            "detail": "Verified file inventory; resolution is the release label, not a current video measurement.",
        }
        observed = _current_file_quality(db, asset)
        published = {
            episode
            for file in selected.get(int(asset["id"]), [])
            for episode in file["episodes"]
            if file["language_contract_id"] is None or episode in observed
        }
        for episode in coverage:
            value = {
                **observed.get(episode, release),
                "asset_id": int(asset["id"]),
                "publication": "selected"
                if episode in published or episode in observed
                else "unconfirmed",
            }
            alternatives.setdefault(episode, []).append(value)
            if value["quality"] == "720p" and value["source"] == "file":
                value["label"] = "720p fallback · verified"
            prior = result.get(episode)
            rank = (
                value["publication"] == "selected",
                value["source"] == "file",
                -int(asset["id"]),
            )
            old_rank = (
                (prior["publication"] == "selected", prior["source"] == "file", -prior["asset_id"])
                if prior
                else (-1, False, 0)
            )
            if rank > old_rank:
                result[episode] = value
    for episode, value in result.items():
        value["alternates"] = sorted(
            {
                item["quality"]
                for item in alternatives[episode]
                if QUALITY_RANK.get(item["quality"], 0) > QUALITY_RANK.get(value["quality"], 0)
            },
            key=lambda q: -QUALITY_RANK[q],
        )
        if value["alternates"]:
            value["detail"] += " Higher release found; replacement publication is unconfirmed."
        if value["publication"] == "unconfirmed":
            value["detail"] += " Current publication is unconfirmed."
    return result
