"""Qualify complete sources before selecting whole physical episode files."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace

from omakase.plus.quality_publication import (
    QUALITY_PUBLICATION_QUALIFIED,
    derive_selection,
    file_key,
    manifest_digest,
)


def build_quality_plan(
    config,
    manifest,
    *,
    subtitle_probe,
    require_english_subtitles,
    language_probe,
    language_qualification,
    quality_qualification,
):
    from plex_curator import (
        CuratorValidationError,
        OverlayPlan,
        PublishedTitle,
        _canonical_bytes,
        build_overlay_plan,
    )

    from omakase.plus.language_publication import LANGUAGE_PUBLICATION_QUALIFIED
    from omakase.plus.publication_episodes import EpisodeParseError, parse_file_episode

    if not (
        (QUALITY_PUBLICATION_QUALIFIED or quality_qualification is True)
        and (LANGUAGE_PUBLICATION_QUALIFIED or language_qualification is True)
    ):
        raise CuratorValidationError("Manifest4 paired quality publication is not qualified.")
    if not require_english_subtitles or subtitle_probe is None:
        raise CuratorValidationError(
            "Quality fallback requires the existing legacy English qualification."
        )
    isolated = replace(config, trusted_allowlist=(), plex_match_hints=())
    root = config.source_root.resolve(strict=True)
    records, proofs, blocked, entries, subtitles, ancillary = [], [], [], [], [], []
    claimed = set()
    for title in manifest["titles"]:
        for asset in title["assets"]:
            locator = (
                asset["storage_backend"],
                asset["storage_locator"]
                if asset["storage_backend"] == "local"
                else asset["source_folder"].casefold(),
            )
            if locator in claimed:
                raise CuratorValidationError("source folder matches more than one manifest asset")
            claimed.add(locator)
            single = copy.deepcopy(title)
            single["assets"] = [
                {
                    key: value
                    for key, value in asset.items()
                    if key not in {"inventory_generation", "source_generation"}
                }
            ]
            single["coverage"].update(
                usable_episodes=asset["episode_coverage"],
                usable_count=len(asset["episode_coverage"]),
                plex_visible_episodes=[],
                plex_visible_count=0,
            )
            child = build_overlay_plan(
                isolated,
                {
                    "schema_version": "manifest-v3"
                    if asset.get("language_contract")
                    else "manifest-v2",
                    "generated_at": manifest["generated_at"],
                    "titles": [single],
                },
                subtitle_probe=subtitle_probe,
                require_english_subtitles=require_english_subtitles,
                language_probe=language_probe,
                language_qualification=language_qualification,
            )
            blocked.extend(child.blocked_assets)
            if child.blocked_assets:
                continue
            child_records = []
            for entry in child.entries:
                source = (root / entry.source).resolve(strict=True)
                source.relative_to(root)
                if title["format"] == "MOVIE":
                    episodes = [1]
                else:
                    try:
                        parsed = parse_file_episode(
                            source.name,
                            season=title["season_number"] or 1,
                            expected=title["expected_episode_count"],
                            numbering_override=asset.get("numbering_override"),
                            anilist_id=title["anilist_id"],
                        )
                    except EpisodeParseError:
                        continue
                    if parsed.special:
                        ancillary.append((title["anilist_id"], asset["torrent_hash"], entry))
                        continue
                    episodes = sorted(parsed.episodes)
                stat = source.stat()
                proof = next(
                    (proof for proof in child.language_files if proof["source"] == entry.source),
                    None,
                )
                if proof and (
                    stat.st_size != proof["bytes"]["size"]
                    or stat.st_mtime_ns != proof["bytes"]["mtime_ns"]
                ):
                    raise CuratorValidationError("qualified source stat changed during planning")
                child_records.append(
                    {
                        "anilist_id": title["anilist_id"],
                        "season_number": title["season_number"] or 1,
                        "torrent_hash": asset["torrent_hash"],
                        "inventory_generation": asset["inventory_generation"],
                        "source_generation": asset["source_generation"],
                        "source": entry.source,
                        "episodes": episodes,
                        "stat": {
                            "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                            "file_device": stat.st_dev,
                            "file_inode": stat.st_ino,
                        },
                        "video": proof["video"] if proof else [],
                        "language_contract_id": asset["language_contract"]["contract_id"]
                        if proof
                        else None,
                        "qualification": "language-policy-v1" if proof else "legacy-english-v1",
                        "selected": False,
                    }
                )
            records.extend(sorted(child_records, key=lambda file: file["source"]))
            proofs.extend(child.language_files)
            entries.extend(child.entries)
            subtitles.extend(child.embedded_subtitles)
    blocked.sort(key=lambda b: (b.anilist_id, b.section, b.season_number or 0, b.torrent_hash))
    selected, hold = derive_selection(manifest, records, [asset.to_dict() for asset in blocked])
    for file in records:
        file["selected"] = file_key(file) in selected
    sources = {file["source"] for file in records if file["selected"]}
    selected_assets = {
        (file["anilist_id"], file["torrent_hash"]) for file in records if file["selected"]
    }
    entries = [entry for entry in entries if entry.source in sources]
    destinations = {entry.destination.casefold() for entry in entries}
    for anilist_id, torrent_hash, entry in ancillary:
        if (
            anilist_id,
            torrent_hash,
        ) in selected_assets and entry.destination.casefold() not in destinations:
            entries.append(entry)
            sources.add(entry.source)
            destinations.add(entry.destination.casefold())
    subtitles = [subtitle for subtitle in subtitles if subtitle.source in sources]
    trusted = build_overlay_plan(
        replace(config, plex_match_hints=()),
        {"schema_version": "manifest-v2", "generated_at": manifest["generated_at"], "titles": []},
    )
    if {entry.source.split("/")[0].casefold() for entry in trusted.entries}.intersection(
        asset["source_folder"].casefold()
        for title in manifest["titles"]
        for asset in title["assets"]
    ):
        raise CuratorValidationError("trusted folder overlaps a manifest asset")
    entries.extend(trusted.entries)
    publications = [
        PublishedTitle(
            title["anilist_id"],
            "Anime Movies" if title["format"] == "MOVIE" else "Anime",
            None if title["format"] == "MOVIE" else title["season_number"] or 1,
            tuple(title["coverage"]["usable_episodes"]),
            tuple(
                sorted(
                    {
                        episode
                        for file in records
                        if file["selected"] and file["anilist_id"] == title["anilist_id"]
                        for episode in file["episodes"]
                    }
                )
            ),
        )
        for title in manifest["titles"]
    ]
    title_names = {
        (title["canonical_title"].casefold(), title["season_number"] or 1)
        for title in manifest["titles"]
        if title["format"] != "MOVIE"
    }
    title_names.update(
        (item.canonical_title.casefold(), item.season_number) for item in config.trusted_allowlist
    )
    if any(
        (hint.canonical_title.casefold(), hint.season_number) not in title_names
        for hint in config.plex_match_hints
    ):
        raise CuratorValidationError("Plex match hint does not identify a staged series")
    unsigned = {
        "schema_version": "overlay-plan-v8",
        "manifest_digest": manifest_digest(manifest),
        "excluded_riven_folders": list(trusted.excluded_riven_folders),
        "entries": [entry.to_dict() for entry in sorted(entries, key=lambda e: e.destination)],
        "embedded_subtitles": [
            item.to_dict()
            for item in sorted(subtitles, key=lambda s: (s.source, s.stream_index, s.role))
        ],
        "plex_match_hints": [
            hint.to_dict()
            for hint in sorted(
                config.plex_match_hints,
                key=lambda h: (
                    h.canonical_title.casefold(),
                    h.season_number,
                    h.source,
                    h.source_id,
                ),
            )
        ],
        "title_publications": [
            item.to_dict()
            for item in sorted(
                publications, key=lambda p: (p.anilist_id, p.section, p.season_number or 0)
            )
        ],
        "blocked_assets": [asset.to_dict() for asset in blocked],
        "language_files": proofs,
        "publication_files": records,
        "quality_hold": hold,
    }
    return OverlayPlan.from_dict(
        {**unsigned, "plan_hash": hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()}
    )
