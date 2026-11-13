"""Portable configured local files and genuine movie/range publication coverage."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial

import pytest

from omakase.plus import households as hh
from omakase.plus import language_store as store
from omakase.plus.language_policy import (
    LanguageRequestSnapshot,
    LanguageRule,
    resolve_effective_language,
)
from omakase.plus.language_publication import CAPABILITY
from omakase.plus.language_source import local_connector_scope
from omakase.plus.local_import import (
    LocalImportRequest,
    apply_language_local_import_plan,
    plan_local_import,
)
from omakase.plus.manifest import build_library_manifest, store_curator_receipt
from omakase.plus.media_probe import probe_media_file
from omakase.plus.providers.local import LocalImportProvider
from omakase.plus.publication_episodes import EpisodeParseError, approved_file_episodes
from omakase.plus.storage import configure_root
from tests.plus import test_language_store as helpers
from tests.plus.test_language_publication import (
    CuratorConfig,
    build_overlay_plan,
    probe_file,
    receipt,
    runner,
)

STAMP = "2026-10-28T12:00:00Z"


def local_runner(*args, **kwargs):
    return runner()


@pytest.fixture
def base(tmp_path):
    yield from helpers.setup.__wrapped__(tmp_path)


def prepare(base, tmp_path, monkeypatch, *, movie=False):
    db, lib, *_ = base
    db.execute("DELETE FROM media_assets")
    path = tmp_path / "local-root"
    path.mkdir()
    (path / "nested").mkdir()
    filename = "Local Film.mkv" if movie else "Local Anime S01E01-E02.mkv"
    file = path / "nested" / filename
    file.write_bytes(b"synthetic local physical fixture, not actual media")
    root = configure_root(path, owner_id=1)
    monkeypatch.setenv("OMAKASE_IMPORT_ROOT", str(root.path))
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,format,season_number,expected_episodes,release_status) VALUES(1,22,?,?,?,?,'FINISHED')",
        (
            "Local Film" if movie else "Local Anime",
            "MOVIE" if movie else "TV",
            None if movie else 1,
            1 if movie else 2,
        ),
    ).lastrowid
    db.execute(
        "UPDATE media_monitors SET aired_episodes=? WHERE id=?", (1 if movie else 2, monitor)
    )
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    old = store.library_defaults(db, owner)[0]
    french = LanguageRule(
        audio_required=("fr",), subtitle_required=("fr",), subtitles_mode="required"
    )
    store.set_library_defaults(db, owner, replace(old, anime=french))
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    item = hh.LibraryRepository(db, owner).add(
        domain="anime",
        canonical_id="22",
        title="Local Film" if movie else "Local Anime",
        season=0 if movie else 1,
        legacy_id=monitor,
        connector_scope=local_connector_scope(root),
    )
    resource = db.execute("SELECT resource_id FROM library_items WHERE id=?", (item,)).fetchone()[0]
    snapshots = []
    for user, rule in (
        (1, french),
        (
            2,
            LanguageRule(
                audio_required=("ja",), subtitle_required=("fr",), subtitles_mode="required"
            ),
        ),
    ):
        snap = LanguageRequestSnapshot(
            "local-" + str(user),
            user,
            lib,
            "anime",
            "anilist:22:season:" + str(0 if movie else 1),
            resolve_effective_language(french, request_override=rule, allow_override=True),
            item,
            resource,
        )
        store.record_request(db, snap, required_episodes=(1,) if movie else (1, 2))
        snapshots.append(snap)
    plan = plan_local_import(
        db,
        user_id=1,
        root=root,
        requests=(LocalImportRequest(monitor, "nested/" + filename),),
        created_at=STAMP,
    )
    db.commit()
    return root, file, monitor, resource, snapshots, plan


@pytest.mark.parametrize("movie", [False, True])
def test_real_local_provider_french_only_import_curator_receipt_and_two_viewers(
    base, tmp_path, monkeypatch, movie
):
    db = base[0]
    root, file, monitor, resource, snapshots, plan = prepare(
        base, tmp_path, monkeypatch, movie=movie
    )
    before = file.read_bytes()
    provider = LocalImportProvider(root)
    inventory = asyncio.run(provider.get_item(plan["files"][0]["locator"]))
    assert inventory.content_hash.lower() == plan["files"][0]["content_sha256"]
    assert not provider.capabilities.acquisition and not provider.capabilities.deletion
    raw = probe_media_file(
        root,
        plan["files"][0]["locator"],
        inventory_fingerprint=inventory.content_hash.lower(),
        runner=local_runner,
    )
    assert not raw.usable  # Legacy English gate remains unchanged.
    chosen = snapshots[1]
    generation = store.current_request_source(db, chosen).source_generation
    result = apply_language_local_import_plan(
        db,
        plan=plan,
        expected_plan_hash=plan["plan_hash"],
        root=root,
        applied_at=STAMP,
        actor_user_id=2,
        request_id=chosen.request_id,
        source_generation=generation,
        probe=partial(probe_media_file, runner=local_runner),
        qualification=True,
    )
    assert result["imported"] == 1 and result["usable"] == 1 and result["physical_compatible"]
    assert file.read_bytes() == before
    repeated = apply_language_local_import_plan(
        db,
        plan=plan,
        expected_plan_hash=plan["plan_hash"],
        root=root,
        applied_at=STAMP,
        actor_user_id=2,
        request_id=chosen.request_id,
        source_generation=generation,
        probe=partial(probe_media_file, runner=local_runner),
        qualification=True,
    )
    assert repeated["already_present"] == 1 and repeated["imported"] == 0
    manifest = build_library_manifest(
        db, generated_at=STAMP, peer_capabilities=(CAPABILITY,), qualification=True
    )
    config = CuratorConfig(source_root=root.path, overlay_root=tmp_path / "overlay")
    plan_v3 = build_overlay_plan(
        config,
        manifest,
        language_probe=partial(probe_file, runner=local_runner),
        language_qualification=True,
    )
    assert len(plan_v3.entries) == 1 and len(plan_v3.language_files) == 1
    proof = plan_v3.language_files[0]
    assert proof["bytes"]["fingerprint_kind"] == "full-content-sha256-v1"
    assert proof["evidence"]["file_fingerprint"] == inventory.content_hash.lower()
    value = receipt(plan_v3, monkeypatch)
    value["title_coverage"][0].update(
        anilist_id=22,
        section="Anime Movies" if movie else "Anime",
        season_number=None if movie else 1,
        usable_count=1 if movie else 2,
        plex_visible_count=1 if movie else 2,
    )
    db.commit()
    store_curator_receipt(db, value, language_qualification=True)
    observation = db.execute("SELECT max(id) FROM asset_language_observations").fetchone()[0]
    for snap in snapshots:
        _, evaluated = store.evaluate_request(
            db,
            actor_user_id=snap.actor_user_id,
            request_id=snap.request_id,
            observation_id=observation,
        )
        assert evaluated.physical_compatible
        assert store.request_language_status(
            db,
            actor_user_id=snap.actor_user_id,
            request_id=snap.request_id,
            source_generation=generation,
        )["language_coverage_complete"]
    assert db.execute("SELECT count(*) FROM media_assets").fetchone()[0] == 1
    assert file.read_bytes() == before
    file.write_bytes(b"changed local file")
    for snap in snapshots:
        assert not store.request_language_status(
            db,
            actor_user_id=snap.actor_user_id,
            request_id=snap.request_id,
            source_generation=generation,
        )["language_coverage_complete"]
        with pytest.raises(ValueError, match="Local file inventory changed"):
            store.evaluate_request(
                db,
                actor_user_id=snap.actor_user_id,
                request_id=snap.request_id,
                observation_id=observation,
            )


def test_actual_configured_root_replacement_invalidates_old_request_without_inventing_transport(
    base, tmp_path, monkeypatch
):
    db = base[0]
    root, file, monitor, resource, snapshots, plan = prepare(base, tmp_path, monkeypatch)
    old = store.current_request_source(db, snapshots[0]).source_generation
    archived = tmp_path / "old-owned-test-root"
    root.path.rename(archived)
    root.path.mkdir()
    with pytest.raises(ValueError, match="generation"):
        store.load_request(db, actor_user_id=1, request_id=snapshots[0].request_id)
    row = db.execute("SELECT * FROM library_resources WHERE id=?", (resource,)).fetchone()
    from omakase.plus.language_source import require_current_source

    scope = hh.resolve_scope(db, viewer_id=1, library_id=base[1])
    assert require_current_source(db, scope=scope, resource=row).source_generation != old
    assert (archived / plan["files"][0]["locator"]).read_bytes()


@pytest.mark.parametrize(
    "name",
    [
        "Local Anime S01E01-S02E02.mkv",
        "Local Anime S01E01 E02.mkv",
        "Local Anime S01E02-E01.mkv",
        "Local Anime S01E01-E03.mkv",
    ],
)
def test_range_never_clips_or_overwrites_actual_owned_scope(name):
    with pytest.raises(EpisodeParseError):
        approved_file_episodes(
            name,
            media_format="TV",
            season=1,
            expected=2,
            anilist_id=22,
            numbering_override=None,
            approved_coverage=(1, 2),
        )


def test_unnumbered_file_cannot_turn_a_tv_monitor_into_a_movie():
    with pytest.raises(EpisodeParseError):
        approved_file_episodes(
            "Local Film.mkv",
            media_format="TV",
            season=1,
            expected=1,
            anilist_id=22,
            numbering_override=None,
            approved_coverage=(1,),
            sole_file=True,
        )
    with pytest.raises(EpisodeParseError):
        approved_file_episodes(
            "Local Film.mkv",
            media_format="MOVIE",
            season=0,
            expected=1,
            anilist_id=22,
            numbering_override=None,
            approved_coverage=(1,),
            sole_file=False,
        )


def test_local_actor_and_source_boundaries_fail_before_probe_or_import(base, tmp_path, monkeypatch):
    db = base[0]
    root, file, monitor, resource, snapshots, plan = prepare(base, tmp_path, monkeypatch)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Unauthorized local request must not probe")

    for actor, generation, qual in ((3, None, True), (2, "0" * 64, True), (2, None, False)):
        with pytest.raises(ValueError):
            apply_language_local_import_plan(
                db,
                plan=plan,
                expected_plan_hash=plan["plan_hash"],
                root=root,
                applied_at=STAMP,
                actor_user_id=actor,
                request_id=snapshots[1].request_id,
                source_generation=generation,
                probe=forbidden,
                qualification=qual,
            )
    assert not calls
    assert db.execute("SELECT count(*) FROM media_assets").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM media_file_probes").fetchone()[0] == 0


def test_anime_film_request_cannot_omit_or_guess_its_required_scope(base, tmp_path, monkeypatch):
    db = base[0]
    root, file, monitor, resource, snapshots, plan = prepare(
        base, tmp_path, monkeypatch, movie=True
    )
    invalid = replace(snapshots[0], request_id="film-without-scope")
    with pytest.raises(ValueError, match="whole-film"):
        store.record_request(db, invalid, required_episodes=())
    with pytest.raises(ValueError, match="whole-film"):
        store.record_request(
            db, replace(invalid, request_id="film-multiple"), required_episodes=(1, 2)
        )


def test_typed_local_failure_after_asset_write_rolls_back_without_touching_source(
    base, tmp_path, monkeypatch
):
    db = base[0]
    root, file, monitor, resource, snapshots, plan = prepare(base, tmp_path, monkeypatch)
    before = file.read_bytes()
    import omakase.plus.language_store as language_store

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("synthetic interruption after local asset/probe insertion")

    monkeypatch.setattr(language_store, "persist_request_probe", interrupt)
    with pytest.raises(KeyboardInterrupt):
        apply_language_local_import_plan(
            db,
            plan=plan,
            expected_plan_hash=plan["plan_hash"],
            root=root,
            applied_at=STAMP,
            actor_user_id=2,
            request_id=snapshots[1].request_id,
            probe=partial(probe_media_file, runner=local_runner),
            qualification=True,
        )
    assert not db.in_transaction
    for table in (
        "media_assets",
        "media_file_probes",
        "publication_language_contracts",
        "asset_language_observations",
    ):
        assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    assert file.read_bytes() == before


def test_typed_import_does_not_commit_or_rollback_callers_preparation(base, tmp_path, monkeypatch):
    db = base[0]
    root, file, monitor, resource, snapshots, plan = prepare(base, tmp_path, monkeypatch)
    db.execute("UPDATE media_monitors SET last_result='pending caller work' WHERE id=?", (monitor,))
    with pytest.raises(ValueError, match="Commit request preparation"):
        apply_language_local_import_plan(
            db,
            plan=plan,
            expected_plan_hash=plan["plan_hash"],
            root=root,
            applied_at=STAMP,
            actor_user_id=2,
            request_id=snapshots[1].request_id,
            qualification=True,
        )
    assert db.in_transaction
    db.rollback()
    assert (
        db.execute("SELECT last_result FROM media_monitors WHERE id=?", (monitor,)).fetchone()[0]
        != "pending caller work"
    )


@pytest.mark.parametrize("multiple", [False, True])
def test_unnumbered_owned_rd_anime_movie_requires_exact_sole_file(
    base, tmp_path, monkeypatch, multiple
):
    from omakase.plus.media_store import upsert_asset

    db, lib, *_ = base
    db.execute("DELETE FROM media_assets")
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,format,expected_episodes,aired_episodes,release_status) VALUES(1,23,'Curator Film','MOVIE',1,1,'FINISHED')"
    ).lastrowid
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    french = LanguageRule(
        audio_required=("fr",), subtitle_required=("fr",), subtitles_mode="required"
    )
    old = store.library_defaults(db, owner)[0]
    store.set_library_defaults(db, owner, replace(old, anime=french))
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    item = hh.LibraryRepository(db, owner).add(
        domain="anime", canonical_id="23", title="Curator Film", season=0, legacy_id=monitor
    )
    resource = db.execute("SELECT resource_id FROM library_items WHERE id=?", (item,)).fetchone()[0]
    asset = upsert_asset(
        db,
        monitor_id=monitor,
        torrent_hash="d" * 40,
        rd_torrent_id="synthetic-rd",
        source_title="Curator Film [1080p]",
        source_folder="Film.Batch",
        quality="1080p",
        planned_coverage=(1,),
        usable_coverage=(1,),
        status="downloaded",
        downloaded_once=True,
        inventory_fingerprint="e" * 64,
        inventory_verified_at=STAMP,
    )
    store.approve_publication_contract(
        db, scope=owner, resource_id=resource, asset_id=asset, rule=french
    )
    snapshot = LanguageRequestSnapshot(
        "rd-film",
        2,
        lib,
        "anime",
        "anilist:23:season:0",
        resolve_effective_language(french),
        item,
        resource,
    )
    store.record_request(db, snapshot, required_episodes=(1,))
    source = tmp_path / "rd-source"
    (source / "Film.Batch").mkdir(parents=True)
    file = source / "Film.Batch" / "Curator Film.mkv"
    file.write_bytes(b"fake movie")
    if multiple:
        (file.parent / "Other part.mkv").write_bytes(b"different fake part")
    config = CuratorConfig(source_root=source, overlay_root=tmp_path / "overlay")
    manifest = build_library_manifest(
        db, generated_at=STAMP, peer_capabilities=(CAPABILITY,), qualification=True
    )
    plan = build_overlay_plan(
        config,
        manifest,
        language_probe=partial(probe_file, runner=local_runner),
        language_qualification=True,
    )
    if multiple:
        assert not plan.entries and plan.holds_current_generation
        return
    assert len(plan.language_files) == 1 and plan.language_files[0]["scope"] == {
        "format": "MOVIE",
        "sole_file": True,
    }
    value = receipt(plan, monkeypatch)
    value["title_coverage"][0].update(
        anilist_id=23,
        section="Anime Movies",
        season_number=None,
        usable_count=1,
        plex_visible_count=1,
    )
    db.commit()
    store_curator_receipt(db, value, language_qualification=True)
    observation = db.execute("SELECT id FROM asset_language_observations").fetchone()[0]
    _, evaluated = store.evaluate_request(
        db, actor_user_id=2, request_id=snapshot.request_id, observation_id=observation
    )
    assert evaluated.physical_compatible
    (file.parent / "Appeared after approval.mkv").write_bytes(b"new fake part")
    from plex_curator import apply_overlay_plan

    with pytest.raises(ValueError, match="sole bound"):
        apply_overlay_plan(config, plan, expected_plan_hash=plan.plan_hash)
