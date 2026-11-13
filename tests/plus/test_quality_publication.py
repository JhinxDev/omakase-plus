"""A qualified whole-file partition preserves fallback episodes during upgrades."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.plex_curator.test_curator import _config, _manifest
from tests.plus.test_episode_quality_recheck import asset
from tests.plus.test_language_publication import data as data
from tests.plus.test_language_publication import runner as french_runner
from tests.plus.test_viewing_history import db as db
from tests.plus.test_viewing_history import entry

CURATOR = Path(__file__).resolve().parents[2] / "deploy" / "plex-curator"
sys.path.insert(0, str(CURATOR))
from plex_curator import CuratorValidationError, OverlayPlan, build_overlay_plan  # noqa: E402
from subtitle_support import SubtitleAuditError, SubtitleProbe, SubtitleTrack  # noqa: E402


def manifest():
    value = _manifest(source_folder="Preferred", filename_coverage=(1,))
    value["schema_version"] = "manifest-v4"
    value["quality_selection"] = "series-quality-fallback-v1"
    title = value["titles"][0]
    title["coverage"].update(usable_episodes=[1, 2], usable_count=2)
    preferred = title["assets"][0]
    preferred["inventory_generation"] = "a" * 64
    preferred["source_generation"] = "d" * 64
    fallback = {
        **preferred,
        "torrent_hash": "B" * 40,
        "source_folder": "Fallback",
        "storage_locator": "Fallback",
        "quality": "720p",
        "episode_coverage": [1, 2],
        "inventory_generation": "b" * 64,
    }
    title["assets"].append(fallback)
    return value


def probe(path):
    return SubtitleProbe(
        duration_ms=60_000,
        tracks=(SubtitleTrack(index=2, codec="ass", language="eng", default=True),),
    )


def build(tmp_path, *, multi=False, failed=False, second=False):
    config = _config(tmp_path)
    for folder, names in (
        ("Preferred", ["Fixture Anime S01E01.mkv"]),
        (
            "Fallback",
            ["Fixture Anime S01E01-E02.mkv"]
            if multi
            else ["Fixture Anime S01E01.mkv", "Fixture Anime S01E02.mkv"],
        ),
    ):
        (config.source_root / folder).mkdir()
        for name in names:
            (config.source_root / folder / name).write_bytes(b"fixture video")
    value = manifest()
    if second:
        other = copy.deepcopy(value["titles"][0])
        other.update(anilist_id=43, canonical_title="Other Anime")
        other["assets"] = [
            {
                **other["assets"][0],
                "torrent_hash": "C" * 40,
                "source_folder": "Other",
                "storage_locator": "Other",
                "inventory_generation": "c" * 64,
            }
        ]
        other["coverage"].update(usable_episodes=[1], usable_count=1)
        value["titles"].append(other)
        (config.source_root / "Other").mkdir()
        (config.source_root / "Other" / "Other Anime S01E01.mkv").write_bytes(b"other")

    def checked(path):
        if failed and path.parent.name == "Preferred":
            raise SubtitleAuditError("fixture preferred probe failure")
        return probe(path)

    plan = build_overlay_plan(
        config,
        value,
        subtitle_probe=checked,
        require_english_subtitles=True,
        language_qualification=True,
        quality_qualification=True,
    )
    return config, value, plan


def test_failed_preferred_copy_keeps_full_qualified_fallback(tmp_path):
    _, _, plan = build(tmp_path, failed=True)
    assert {entry.source.split("/")[0] for entry in plan.entries} == {"Fallback"}
    assert plan.title_publications[0].published_episodes == (1, 2)
    assert not plan.holds_current_generation
    assert plan.blocked_assets[0].torrent_hash == "A" * 40


def test_preferred_episode_and_separate_fallback_file_form_exact_partition(tmp_path):
    _, _, plan = build(tmp_path)
    selected = {
        (file["torrent_hash"][0], tuple(file["episodes"]))
        for file in plan.publication_files
        if file["selected"]
    }
    assert selected == {("A", (1,)), ("B", (2,))}
    assert len(plan.publication_files) == 3
    assert OverlayPlan.from_dict(plan.to_dict()) == plan


def test_physical_range_defers_partial_upgrade_and_does_not_hold_other_title(tmp_path):
    _, _, plan = build(tmp_path, multi=True, second=True)
    selected = {
        (file["torrent_hash"][0], tuple(file["episodes"]))
        for file in plan.publication_files
        if file["selected"]
    }
    assert selected == {("B", (1, 2)), ("C", (1,))}
    assert not plan.holds_current_generation
    assert len(plan.entries) == 2


def test_v4_requires_both_positive_qualification_gates(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(CuratorValidationError, match="qualif"):
        build_overlay_plan(
            config,
            manifest(),
            subtitle_probe=probe,
            require_english_subtitles=True,
            language_qualification=True,
        )


def test_current_file_change_refuses_swap(tmp_path):
    from plex_curator import apply_overlay_plan

    config, _, plan = build(tmp_path)
    (config.source_root / "Preferred" / "Fixture Anime S01E01.mkv").write_bytes(b"changed")
    with pytest.raises((ValueError, OSError), match="changed|identity|stat"):
        apply_overlay_plan(config, plan, expected_plan_hash=plan.plan_hash)
    assert not config.overlay_root.exists()


def actual_receipt(plan, monkeypatch, *, anilist_id=21, count=2):
    import curator_runtime

    monkeypatch.setattr(curator_runtime, "_utc_now", lambda: "2026-10-29T15:01:00Z")
    names = ("Anime", "Anime Movies", "TV Shows", "Movies")
    return curator_runtime._receipt(
        plan=SimpleNamespace(
            overlay_plan=plan,
            plan_hash=plan.plan_hash,
            manifest_generated_at="2026-10-29T15:00:00Z",
        ),
        mode="live",
        started_at="2026-10-29T15:00:00Z",
        sections=tuple(SimpleNamespace(title=name) for name in names),
        observed=[
            {"key": n + 1, "title": name, "count": count if n == 0 else 0}
            for n, name in enumerate(names)
        ],
        title_coverage=[
            {
                "anilist_id": anilist_id,
                "section": "Anime",
                "season_number": 1,
                "usable_count": count,
                "plex_visible_count": count,
            }
        ],
        topology_converged=True,
    )


def db_plan(db, tmp_path):
    from omakase.plus.manifest import build_library_manifest

    monitor, _ = entry(db)
    db.execute(
        "UPDATE media_monitors SET release_status='FINISHED',expected_episodes=2,aired_episodes=2 WHERE id=?",
        (monitor,),
    )
    asset(db, monitor, "A", (1, 2), "720p")
    asset(db, monitor, "B", (1,), "1080p")
    config = _config(tmp_path)
    for folder, episodes in (("One Piece A", (1, 2)), ("One Piece B", (1,))):
        (config.source_root / folder).mkdir()
        for episode in episodes:
            (config.source_root / folder / f"One Piece S01E{episode:02d}.mkv").write_bytes(
                b"fake playable media"
            )
    manifest = build_library_manifest(
        db,
        generated_at="2026-10-29T15:00:00Z",
        peer_capabilities=("language-policy-v1", "quality-fallback-v1"),
        qualification=True,
        quality_qualification=True,
    )
    plan = build_overlay_plan(
        config,
        manifest,
        subtitle_probe=probe,
        require_english_subtitles=True,
        language_qualification=True,
        quality_qualification=True,
    )
    return monitor, manifest, plan


def test_real_receipt_store_and_readback_keep_exact_selection_current(db, tmp_path, monkeypatch):
    from omakase.plus.manifest import store_curator_receipt
    from omakase.plus.quality_publication import validate_current_selection

    _, manifest, plan = db_plan(db, tmp_path)
    assert [a["quality"] for a in manifest["titles"][0]["assets"]] == ["1080p", "720p"]
    value = actual_receipt(plan, monkeypatch)
    db.commit()
    assert store_curator_receipt(
        db, value, language_qualification=True, quality_qualification=True
    )["stored"]
    validate_current_selection(db, value)
    assert store_curator_receipt(
        db, value, language_qualification=True, quality_qualification=True
    )["idempotent"]


@pytest.mark.parametrize(
    "tamper",
    ["omit_candidate", "omit_selected", "stale_inventory", "foreign_source", "partial_range"],
)
def test_import_rejects_unaccounted_omissions_stale_or_foreign_sources(
    db, tmp_path, monkeypatch, tamper
):
    from omakase.plus.manifest import store_curator_receipt

    _, _, plan = db_plan(db, tmp_path)
    value = actual_receipt(plan, monkeypatch)
    if tamper == "omit_candidate":
        value["publication_files"].pop()
    elif tamper == "omit_selected":
        value["publication_files"][0]["selected"] = False
    elif tamper == "stale_inventory":
        db.execute(
            "UPDATE media_assets SET inventory_fingerprint=? WHERE torrent_hash=?",
            ("d" * 64, "B" * 40),
        )
    elif tamper == "foreign_source":
        value["publication_files"][0]["source"] = "Other/One Piece S01E01.mkv"
    else:
        value["publication_files"][0]["episodes"] = [2]
    db.commit()
    with pytest.raises(ValueError):
        store_curator_receipt(db, value, language_qualification=True, quality_qualification=True)
    assert db.execute("SELECT count(*) FROM media_curator_receipt_latest").fetchone()[0] == 0


def test_old_peers_keep_their_existing_nonoverlap_manifest_and_gates(db, tmp_path):
    from omakase.plus.manifest import build_library_manifest

    _, _, _ = db_plan(db, tmp_path)
    old = build_library_manifest(db)
    assert old["schema_version"] == "manifest-v2"
    assert len(old["titles"][0]["assets"]) == 1
    with pytest.raises(ValueError, match="qualified"):
        build_library_manifest(
            db, peer_capabilities=("language-policy-v1", "quality-fallback-v1"), qualification=True
        )


def test_legacy_specials_stay_with_a_selected_source(tmp_path):
    config, value, _ = build(tmp_path)
    special = config.source_root / "Fallback" / "Fixture Anime SP01.mkv"
    special.write_bytes(b"special fixture")
    plan = build_overlay_plan(
        config,
        value,
        subtitle_probe=probe,
        require_english_subtitles=True,
        language_qualification=True,
        quality_qualification=True,
    )
    assert any(
        "Season 00" in entry.destination and entry.source.endswith("SP01.mkv")
        for entry in plan.entries
    )
    assert any(subtitle.source.endswith("SP01.mkv") for subtitle in plan.embedded_subtitles)


@pytest.mark.parametrize("overlap", [True, False])
@pytest.mark.parametrize("french_failed", [True, False])
def test_approved_policy_protects_only_its_exact_replacement_episodes(data, overlap, french_failed):
    from functools import partial

    from language_publication import probe_file

    from omakase.plus.manifest import build_library_manifest

    original, config = data
    db = original[0]
    db.execute("UPDATE media_assets SET quality='720p' WHERE id=?", (original[-1],))
    episode = 1 if overlap else 2
    higher = asset(db, 1, "B", (episode,), "1080p")
    db.execute(
        "UPDATE media_assets SET source_title=?,source_folder='Legacy',storage_locator='Legacy' WHERE id=?",
        (f"Exact Anime S01E{episode:02d} [1080p]", higher),
    )
    db.execute(
        "UPDATE media_monitors SET expected_episodes=?,aired_episodes=? WHERE id=1",
        (max(1, episode), max(1, episode)),
    )
    (config.source_root / "Legacy").mkdir()
    (config.source_root / "Legacy" / f"Exact Anime S01E{episode:02d}.mkv").write_bytes(
        b"legacy fixture"
    )

    def checked(*args, **kwargs):
        if french_failed:
            raise ValueError("fixture French qualification failed")
        return french_runner(*args, **kwargs)

    value = build_library_manifest(
        db,
        generated_at="2026-10-29T15:00:00Z",
        peer_capabilities=("language-policy-v1", "quality-fallback-v1"),
        qualification=True,
        quality_qualification=True,
    )
    plan = build_overlay_plan(
        config,
        value,
        subtitle_probe=probe,
        require_english_subtitles=True,
        language_probe=partial(probe_file, runner=checked),
        language_qualification=True,
        quality_qualification=True,
    )
    selected = {
        (file["torrent_hash"][0], tuple(file["episodes"]))
        for file in plan.publication_files
        if file["selected"]
    }
    if overlap:
        assert selected == (set() if french_failed else {("A", (1,))})
        assert plan.holds_current_generation == french_failed
    else:
        assert ("B", (2,)) in selected
        assert (("A", (1,)) in selected) != french_failed
        assert not plan.holds_current_generation


def test_quality_labels_change_only_after_applied_selected_receipt(db, tmp_path, monkeypatch):
    from omakase.plus.manifest import store_curator_receipt
    from omakase.plus.product_views import title_detail

    monitor, _, plan = db_plan(db, tmp_path)
    assert title_detail(db, user_id=1, monitor_id=monitor)["fallback_episodes"] == [1, 2]
    value = actual_receipt(plan, monkeypatch)
    db.commit()
    store_curator_receipt(db, value, language_qualification=True, quality_qualification=True)
    card = title_detail(db, user_id=1, monitor_id=monitor)
    assert card["fallback_episodes"] == [2]
    assert card["episode_qualities"][1]["publication"] == "selected"
    assert card["episode_qualities"][1]["label"] == "1080p release"
    assert card["episode_qualities"][2]["label"] == "720p fallback release"
    db.execute(
        "UPDATE media_assets SET inventory_fingerprint=? WHERE torrent_hash=?", ("e" * 64, "B" * 40)
    )
    db.commit()
    assert title_detail(db, user_id=1, monitor_id=monitor)["fallback_episodes"] == [1, 2]


@pytest.mark.parametrize("changed", ["title", "identity", "season", "numbering", "folder"])
def test_selected_readback_rejects_changed_logical_episode_mapping(
    db, tmp_path, monkeypatch, changed
):
    from omakase.plus.episode_quality import _selected_files
    from omakase.plus.manifest import store_curator_receipt

    monitor, _, plan = db_plan(db, tmp_path)
    value = actual_receipt(plan, monkeypatch)
    db.commit()
    store_curator_receipt(db, value, language_qualification=True, quality_qualification=True)
    if changed == "title":
        db.execute(
            "UPDATE media_monitors SET title='Other Series',search_titles_json='[]' WHERE id=?",
            (monitor,),
        )
    elif changed == "identity":
        db.execute("UPDATE media_monitors SET anilist_id=22 WHERE id=?", (monitor,))
    elif changed == "season":
        db.execute("UPDATE media_monitors SET season_number=2 WHERE id=?", (monitor,))
    elif changed == "numbering":
        db.execute("UPDATE media_assets SET numbering_override='bleach-tybw-cour4-absolute-v1'")
    else:
        db.execute("UPDATE media_assets SET source_folder='Other'")
    rows = db.execute("SELECT * FROM media_assets WHERE monitor_id=?", (monitor,)).fetchall()
    changes = db.total_changes
    assert not _selected_files(db, rows)
    assert db.total_changes == changes


def test_same_policy_partial_fallback_import_keeps_exact_current_file_quality(data, monkeypatch):
    import json
    from functools import partial

    from language_publication import probe_file

    from omakase.plus import households as hh
    from omakase.plus import language_store
    from omakase.plus.language_policy import rule_from_mapping
    from omakase.plus.language_publication import export_contract
    from omakase.plus.manifest import build_library_manifest, store_curator_receipt
    from omakase.plus.product_views import title_detail

    original, config = data
    db, library_id, _, resource, *_ = original
    old = original[-1]
    db.execute("UPDATE media_monitors SET expected_episodes=2,aired_episodes=2 WHERE id=1")
    db.execute(
        "UPDATE media_assets SET quality='720p',usable_coverage_json='[1,2]',planned_coverage_json='[1,2]' WHERE id=?",
        (old,),
    )
    (config.source_root / "Batch" / "Exact Anime S01E02.mkv").write_bytes(b"fallback episode two")
    higher = asset(db, 1, "B", (1,), "1080p")
    db.execute(
        "UPDATE media_assets SET source_title='Exact Anime S01E01 [1080p]',source_folder='Preferred',storage_locator='Preferred' WHERE id=?",
        (higher,),
    )
    (config.source_root / "Preferred").mkdir()
    (config.source_root / "Preferred" / "Exact Anime S01E01.mkv").write_bytes(
        b"preferred episode one"
    )
    language_store.approve_publication_contract(
        db,
        scope=hh.resolve_scope(db, viewer_id=1, library_id=library_id),
        resource_id=resource,
        asset_id=higher,
        rule=rule_from_mapping(export_contract(db, asset_id=old)["policy"]),
    )
    value = build_library_manifest(
        db,
        generated_at="2026-10-29T15:00:00Z",
        peer_capabilities=("language-policy-v1", "quality-fallback-v1"),
        qualification=True,
        quality_qualification=True,
    )

    def measured(command, **kwargs):
        reply = french_runner(command, **kwargs)
        raw = json.loads(reply.stdout)
        if "Batch" in str(command[-1]):
            raw["streams"][0].update(width=1280, height=720)
        return SimpleNamespace(returncode=0, stdout=json.dumps(raw), stderr="")

    plan = build_overlay_plan(
        config,
        value,
        subtitle_probe=probe,
        require_english_subtitles=True,
        language_probe=partial(probe_file, runner=measured),
        language_qualification=True,
        quality_qualification=True,
    )
    receipt = actual_receipt(plan, monkeypatch)
    assert len(receipt["language_files"]) == 3
    db.commit()
    store_curator_receipt(db, receipt, language_qualification=True, quality_qualification=True)
    card = title_detail(db, user_id=1, monitor_id=1)
    assert card["fallback_episodes"] == [2]
    assert card["episode_qualities"][1]["label"] == "1080p verified"
    assert card["episode_qualities"][2]["label"] == "720p fallback · verified"
    assert (
        db.execute(
            "SELECT count(*) FROM language_publication_current_files WHERE observation_id IS NOT NULL"
        ).fetchone()[0]
        == 2
    )
    assert db.execute("SELECT count(*) FROM media_assets").fetchone()[0] == 2


def test_mixed_envelope_preserves_all_successful_movie_versions(tmp_path):
    config, value, _ = build(tmp_path)
    movie = copy.deepcopy(value["titles"][0])
    movie.update(
        anilist_id=50,
        canonical_title="Fixture Movie",
        format="MOVIE",
        season_number=None,
        expected_episode_count=1,
        aired_episode_count=1,
    )
    movie["coverage"].update(
        required_episodes=[1], required_count=1, usable_episodes=[1], usable_count=1
    )
    movie["assets"] = []
    for suffix, quality in (("C", "2160p"), ("D", "1080p")):
        source = "Movie" + suffix
        (config.source_root / source).mkdir()
        (config.source_root / source / "Fixture Movie.mkv").write_bytes(b"movie version")
        movie["assets"].append(
            {
                **value["titles"][0]["assets"][0],
                "torrent_hash": suffix * 40,
                "source_folder": source,
                "storage_locator": source,
                "source_title": "Fixture Movie",
                "quality": quality,
                "episode_coverage": [1],
                "inventory_generation": suffix.lower() * 64,
            }
        )
    value["titles"].append(movie)
    plan = build_overlay_plan(
        config,
        value,
        subtitle_probe=probe,
        require_english_subtitles=True,
        language_qualification=True,
        quality_qualification=True,
    )
    assert (
        len(
            [
                file
                for file in plan.publication_files
                if file["anilist_id"] == 50 and file["selected"]
            ]
        )
        == 2
    )
    assert len([entry for entry in plan.entries if entry.section == "Anime Movies"]) == 2
    assert plan.title_publications[-1].published_episodes == (1,)


def test_scoped_failure_survives_unrelated_candidates_and_is_inventory_bound(
    db, tmp_path, monkeypatch
):
    from omakase.plus.acquisition_policy import AcquisitionPolicy
    from omakase.plus.episode_recheck import targets
    from omakase.plus.manifest import store_curator_receipt
    from omakase.plus.media_types import MediaRecheckIntent
    from omakase.plus.quality_publication import current_failed_hashes

    monitor, value, _ = db_plan(db, tmp_path)

    def failure(path):
        if path.parent.name == "One Piece B":
            raise SubtitleAuditError("fixture blocked preferred copy")
        return probe(path)

    plan = build_overlay_plan(
        _config(tmp_path),
        value,
        subtitle_probe=failure,
        require_english_subtitles=True,
        language_qualification=True,
        quality_qualification=True,
    )
    receipt = actual_receipt(plan, monkeypatch)
    db.commit()
    store_curator_receipt(db, receipt, language_qualification=True, quality_qualification=True)
    assert current_failed_hashes(db, monitor) == {"B" * 40}
    unrelated, _ = entry(db, anilist_id=20, title="Other Anime")
    asset(db, unrelated, "C", (1,), "1080p")
    db.commit()
    assert current_failed_hashes(db, monitor) == {"B" * 40}
    owned = db.execute("SELECT * FROM media_monitors WHERE id=?", (monitor,)).fetchone()
    checks = targets(
        db, owned, {1, 2}, AcquisitionPolicy(), MediaRecheckIntent(1, monitor, upgrade_to_1080=True)
    )
    assert checks.search == {1, 2} and not checks.pending
    changes = db.total_changes
    # A replay does not add duplicate failure history, despite unrelated data.
    assert store_curator_receipt(
        db, receipt, language_qualification=True, quality_qualification=True
    )["idempotent"]
    assert (
        db.execute(
            "SELECT count(*) FROM media_candidate_attempts WHERE decision='PUBLICATION_WITHHELD'"
        ).fetchone()[0]
        == 1
    )
    assert db.total_changes == changes
    db.execute(
        "UPDATE media_assets SET inventory_fingerprint=? WHERE torrent_hash=?", ("e" * 64, "B" * 40)
    )
    assert not current_failed_hashes(db, monitor)


@pytest.mark.parametrize("target", ["manifest", "receipt", "file", "old_schema"])
def test_v4_remains_closed_and_is_not_an_old_peer_subset_override(
    db, tmp_path, monkeypatch, target
):
    from omakase.plus.manifest import validate_curator_receipt, validate_library_manifest

    _, manifest, plan = db_plan(db, tmp_path)
    receipt = actual_receipt(plan, monkeypatch)
    if target == "manifest":
        manifest["allow_subsets"] = True
        with pytest.raises(ValueError):
            validate_library_manifest(manifest)
    else:
        if target == "receipt":
            receipt["allow_subsets"] = True
        elif target == "file":
            receipt["publication_files"][0]["arbitrary_scope"] = True
        else:
            receipt["schema_version"] = "curator-receipt-v3"
        with pytest.raises(ValueError):
            validate_curator_receipt(receipt)


def test_held_selection_keeps_prior_quality_only_while_inventory_is_current(
    db, tmp_path, monkeypatch
):
    from omakase.plus.manifest import store_curator_receipt
    from omakase.plus.product_views import title_detail

    monitor, manifest, first = db_plan(db, tmp_path)
    old = actual_receipt(first, monkeypatch)
    db.commit()
    store_curator_receipt(db, old, language_qualification=True, quality_qualification=True)

    def unavailable(path):
        raise SubtitleAuditError("fixture unavailable publication")

    held = build_overlay_plan(
        _config(tmp_path),
        manifest,
        subtitle_probe=unavailable,
        require_english_subtitles=True,
        language_qualification=True,
        quality_qualification=True,
    )
    assert held.holds_current_generation
    value = actual_receipt(held, monkeypatch)
    value["completed_at"] = "2026-10-29T15:02:00Z"
    value["selection_applied"] = False
    result = store_curator_receipt(
        db, value, language_qualification=True, quality_qualification=True
    )
    assert not result["stored"]
    assert (
        db.execute("SELECT curator_run_id FROM media_curator_receipt_latest").fetchone()[0]
        == old["run_id"]
    )
    assert title_detail(db, user_id=1, monitor_id=monitor)["fallback_episodes"] == [2]
    db.execute(
        "UPDATE media_assets SET inventory_fingerprint=? WHERE torrent_hash=?", ("e" * 64, "B" * 40)
    )
    db.commit()
    quality = title_detail(db, user_id=1, monitor_id=monitor)["episode_qualities"]
    assert quality[1]["quality"] == "720p" and quality[1]["publication"] == "unconfirmed"
