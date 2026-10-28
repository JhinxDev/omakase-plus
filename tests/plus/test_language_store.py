"""Real033/034 SQLite + exact-file fake probe: one copy, separate viewer policies."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from omakase.plus import households as hh
from omakase.plus import language_store as store
from omakase.plus.db import run_migrations
from omakase.plus.language_policy import (
    LanguageRequestSnapshot,
    LanguageRule,
    resolve_effective_language,
)
from omakase.plus.media_probe import persist_probe_result, probe_media_file
from omakase.plus.storage import configure_root, file_receipt

STAMP = "2026-10-28T12:00:00Z"


@pytest.fixture
def setup(tmp_path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    run_migrations(db)
    for user in (1, 2, 3):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (user, f"{user}@fixture.invalid", "x"),
        )
    db.commit()
    lib = hh.create_household(db, user_id=1, name="Shared", acquisition_requests=True)
    invitation = hh.create_invite(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), now=1000)
    hh.accept_invite(db, viewer_id=2, token=invitation["token"], now=1001)
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number,expected_episodes) VALUES(1,21,'Exact Anime',1,2)"
    ).lastrowid
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    item = hh.LibraryRepository(db, owner).add(
        domain="anime", canonical_id="21", title="Exact Anime", season=1, legacy_id=monitor
    )
    resource = db.execute("SELECT resource_id FROM library_items WHERE id=?", (item,)).fetchone()[0]
    path = tmp_path / "Exact Anime S01E01.mkv"
    path.write_bytes(b"synthetic physical fixture, no real media")
    root = configure_root(tmp_path, owner_id=1)
    fp = file_receipt(root, path.name).sha256
    provider = db.execute(
        "INSERT INTO media_providers(user_id,kind,label) VALUES(1,'realdebrid','Synthetic')"
    ).lastrowid
    asset = db.execute(
        "INSERT INTO media_assets(monitor_id,torrent_hash,source_title,provider_id,provider_asset_id,storage_backend,storage_locator,inventory_fingerprint,planned_coverage_json,usable_coverage_json,status,downloaded_once,inventory_verified_at) VALUES(?,?,?, ?,?,'zurg',?,?,'[1]','[1]','downloaded',1,?)",
        (monitor, "a" * 40, path.name, provider, path.name, path.name, fp, STAMP),
    ).lastrowid
    from omakase.plus.secrets import store_secret

    for user, key, value in (
        (1, "realdebrid_api_key", "synthetic-rd"),
        (1, "plex_url", "https://plex.fixture.invalid"),
        (1, "plex_token", "synthetic-owner"),
        (1, "plex_machine_id", "fixture-server"),
        (1, "plex_movies_section_id", "1"),
        (2, "plex_token", "synthetic-member"),
    ):
        store_secret(db, user, key, value)
    for integration in ("plex", "realdebrid"):
        db.execute(
            "INSERT INTO library_settings(library_id,integration,owner_user_id) VALUES(?,?,1)",
            (lib, integration),
        )
    db.commit()
    yield db, lib, item, resource, root, path, fp, asset
    db.close()


def current_generation(data):
    from omakase.plus.language_source import require_current_source

    db, lib, _, resource, *_ = data
    scope = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    row = db.execute("SELECT * FROM library_resources WHERE id=?", (resource,)).fetchone()
    return require_current_source(db, scope=scope, resource=row).source_generation


def snapshot(data, user, identifier, rule):
    db, lib, item, resource, *_ = data
    scope = hh.resolve_scope(db, viewer_id=user, library_id=lib)
    default = store.library_defaults(db, scope)[0].anime
    effective = resolve_effective_language(default, request_override=rule, allow_override=True)
    return LanguageRequestSnapshot(
        identifier, user, lib, "anime", "anilist:21:season:1", effective, item, resource
    )


def runner(*args, **kwargs):
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080}
    ]
    for index, kind, language in (
        (1, "audio", "jpn"),
        (2, "audio", "fra"),
        (3, "subtitle", "eng"),
        (4, "subtitle", "fra"),
    ):
        streams.append(
            {
                "index": index,
                "codec_type": kind,
                "tags": {"language": language},
                "disposition": {"forced": 0},
            }
        )
    return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": streams}), stderr="")


def record_a_probe(data):
    db, lib, item, resource, root, path, fp, asset = data
    a = snapshot(
        data,
        1,
        "request-a",
        LanguageRule(audio_required=("ja",), subtitle_required=("en",), subtitles_mode="required"),
    )
    store.record_request(db, a, required_episodes=(1,))
    result = probe_media_file(root, path.name, inventory_fingerprint=fp, runner=runner)
    persist_probe_result(
        db,
        asset_id=asset,
        file_locator=path.name,
        verified_at=STAMP,
        result=result,
        language_request_id=a.request_id,
        language_actor_user_id=1,
        observed_episodes=(1,),
        source_generation=current_generation(data),
    )
    return a, result, db.execute("SELECT id FROM asset_language_observations").fetchone()[0]


def test_same_exact_multitrack_copy_satisfies_two_viewers_without_new_media_or_global_selection(
    setup,
):
    db, *_ = setup
    before = [tuple(r) for r in db.execute("SELECT * FROM media_assets")]
    a, result, observation = record_a_probe(setup)
    b = snapshot(
        setup,
        2,
        "request-b",
        LanguageRule(audio_required=("fr",), subtitle_required=("fr",), subtitles_mode="required"),
    )
    store.record_request(db, b, required_episodes=(1,))
    _, evaluated = store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=b.request_id,
        observation_id=observation,
        source_generation=current_generation(setup),
    )
    assert (
        evaluated.physical_compatible
        and evaluated.audio_required_confirmed
        and evaluated.subtitle_required_confirmed
    )
    assert store.request_language_status(
        db, actor_user_id=2, request_id=b.request_id, source_generation=current_generation(setup)
    )["language_coverage_complete"]
    assert db.execute("SELECT COUNT(*) FROM asset_language_observations").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM language_evaluations").fetchone()[0] == 2
    assert [tuple(r) for r in db.execute("SELECT * FROM media_assets")] == before
    assert not any(t.selected for t in result.language_evidence.tracks)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unavailable_new_language_does_not_replace_working_copy_or_prior_evaluations(setup):
    db, *_ = setup
    a, _, observation = record_a_probe(setup)
    before = [tuple(r) for r in db.execute("SELECT * FROM media_assets")]
    german = snapshot(setup, 2, "request-de", LanguageRule(audio_required=("de",)))
    store.record_request(db, german, required_episodes=(1,))
    _, result = store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=german.request_id,
        observation_id=observation,
        source_generation=current_generation(setup),
    )
    assert result.state == "language_unavailable" and not result.replacement_allowed
    assert [tuple(r) for r in db.execute("SELECT * FROM media_assets")] == before
    assert store.load_request(db, actor_user_id=1, request_id=a.request_id)[0] == a


def test_cross_account_request_and_revoked_member_cannot_reuse_cached_evidence(setup):
    db, lib, *_ = setup
    _, _, observation = record_a_probe(setup)
    b = snapshot(setup, 2, "request-b", LanguageRule(audio_required=("fr",)))
    store.record_request(db, b)
    with pytest.raises(ValueError):
        store.evaluate_request(
            db,
            actor_user_id=3,
            request_id=b.request_id,
            observation_id=observation,
            source_generation=current_generation(setup),
        )
    hh.leave_or_revoke(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), member_id=2)
    with pytest.raises(ValueError):
        store.evaluate_request(
            db,
            actor_user_id=2,
            request_id=b.request_id,
            observation_id=observation,
            source_generation=current_generation(setup),
        )


def test_snapshots_and_physical_observations_are_immutable_and_exact_generation_bound(setup):
    db, *_ = setup
    a, _, observation = record_a_probe(setup)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE language_request_policies SET policy_revision=? WHERE id=?",
            ("f" * 64, a.request_id),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE asset_language_observations SET evidence_json='{}' WHERE id=?", (observation,)
        )
    with pytest.raises(ValueError):
        store.evaluate_request(
            db,
            actor_user_id=1,
            request_id=a.request_id,
            observation_id=observation,
            source_generation="c" * 64,
        )
    db.execute("UPDATE media_assets SET inventory_fingerprint=?", ("d" * 64,))
    with pytest.raises(ValueError):
        store.evaluate_request(
            db,
            actor_user_id=1,
            request_id=a.request_id,
            observation_id=observation,
            source_generation=current_generation(setup),
        )


def test_snapshot_idempotency_conflict_and_explicit_acquisition_intent(setup):
    db, *_ = setup
    a = snapshot(setup, 1, "immutable", LanguageRule(audio_required=("ja",)))
    store.record_request(db, a)
    assert store.record_request(db, a) == a.request_id
    with pytest.raises(ValueError):
        store.load_request(db, actor_user_id=1, request_id=a.request_id, for_acquisition=True)
    other = snapshot(setup, 1, "immutable", LanguageRule(audio_required=("fr",)))
    with pytest.raises(ValueError):
        store.record_request(db, other)


def test_observation_episode_claim_must_match_exact_file_not_another_episode(setup):
    db, lib, item, resource, root, path, fp, asset = setup
    request = snapshot(setup, 1, "wrong-episode", LanguageRule())
    store.record_request(db, request, required_episodes=(3,))
    result = probe_media_file(root, path.name, inventory_fingerprint=fp, runner=runner)
    with pytest.raises(ValueError):
        persist_probe_result(
            db,
            asset_id=asset,
            file_locator=path.name,
            verified_at=STAMP,
            result=result,
            language_request_id=request.request_id,
            language_actor_user_id=1,
            observed_episodes=(3,),
            source_generation=current_generation(setup),
        )
    assert db.execute("SELECT count(*) FROM media_file_probes").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM asset_language_observations").fetchone()[0] == 0


def test_owner_default_permissions_and_frozen_request_do_not_follow_latest_settings(setup):
    from omakase.plus.language_policy import DomainLanguagePolicies

    db, lib, *_ = setup
    request, _, observation = record_a_probe(setup)
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    store.set_library_defaults(
        db,
        owner,
        DomainLanguagePolicies.new_defaults(),
        allow_member_preferences=False,
        allow_request_overrides=False,
    )
    assert store.load_request(db, actor_user_id=1, request_id=request.request_id)[0] == request
    _, result = store.evaluate_request(
        db,
        actor_user_id=1,
        request_id=request.request_id,
        observation_id=observation,
        source_generation=current_generation(setup),
    )
    assert result.physical_compatible
    b = snapshot(setup, 2, "disallowed", LanguageRule(audio_required=("fr",)))
    with pytest.raises(ValueError):
        store.record_request(db, b)


def test_existing_publication_contract_is_stable_against_later_member_preference(setup):
    db, lib, item, resource, root, path, fp, asset = setup
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    rule = store.library_defaults(db, owner)[0].anime
    contract = store.approve_publication_contract(
        db, scope=owner, resource_id=resource, asset_id=asset, rule=rule
    )
    assert (
        store.approve_publication_contract(
            db, scope=owner, resource_id=resource, asset_id=asset, rule=rule
        )
        == contract
    )
    member = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    with pytest.raises(ValueError):
        store.approve_publication_contract(
            db,
            scope=member,
            resource_id=resource,
            asset_id=asset,
            rule=LanguageRule(audio_required=("fr",)),
        )
    with pytest.raises(ValueError):
        store.approve_publication_contract(
            db,
            scope=owner,
            resource_id=resource,
            asset_id=asset,
            rule=LanguageRule(audio_required=("fr",)),
        )
    assert (
        db.execute("SELECT policy_revision FROM publication_language_contracts").fetchone()[0]
        == rule.revision
    )


def test_partial_episode_evidence_does_not_claim_complete_season_language_coverage(setup):
    db, *_ = setup
    request, _, observation = record_a_probe(setup)
    b = snapshot(setup, 2, "two-episodes", LanguageRule(audio_required=("fr",)))
    store.record_request(db, b, required_episodes=(1, 2))
    store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=b.request_id,
        observation_id=observation,
        source_generation=current_generation(setup),
    )
    status = store.request_language_status(
        db, actor_user_id=2, request_id=b.request_id, source_generation=current_generation(setup)
    )
    assert status["language_coverage_complete"] is False
    assert status["state"] == "pending_verification"


def test_wrong_source_generation_cannot_bypass_exact_observation_binding(setup):
    db, *_ = setup
    request, _, observation = record_a_probe(setup)
    with pytest.raises(ValueError, match="source generation"):
        store.evaluate_request(
            db,
            actor_user_id=1,
            request_id=request.request_id,
            observation_id=observation,
            source_generation="0" * 64,
        )


def test_034_migration_preserves_legacy_rows_and_marks_only_preexisting_monitors(
    tmp_path, monkeypatch
):
    import shutil

    from omakase.plus import db as database

    originals = database._MIGRATIONS_DIR
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for original in originals.glob("*.sql"):
        if int(original.name.split("-", 1)[0]) <= 33:
            shutil.copyfile(original, migrations / original.name)
    monkeypatch.setattr(database, "_MIGRATIONS_DIR", migrations)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    database.run_migrations(db)
    db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'legacy@fixture.invalid','x')")
    db.execute("INSERT INTO media_monitors(id,user_id,anilist_id,title) VALUES(1,1,21,'Preserved')")
    db.commit()
    names = [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='_migrations'"
        )
    ]
    before = {
        name: [tuple(r) for r in db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
        for name in names
    }
    migration = originals / "034-language-contracts.sql"
    shutil.copyfile(migration, migrations / migration.name)
    database.run_migrations(db)
    assert {
        name: [tuple(r) for r in db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
        for name in names
    } == before
    assert [r[0] for r in db.execute("SELECT monitor_id FROM legacy_language_monitors")] == [1]
    db.execute(
        "INSERT INTO media_monitors(id,user_id,anilist_id,title) VALUES(2,1,22,'New request required')"
    )
    db.commit()
    database.run_migrations(db)
    assert [r[0] for r in db.execute("SELECT monitor_id FROM legacy_language_monitors")] == [1]
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_movie_observation_cannot_borrow_anime_asset_or_probe(setup):
    db, lib, *_ = setup
    _, result, _ = record_a_probe(setup)
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    hh.LibraryRepository(db, owner).add(
        domain="movie", canonical_id="tt0000001", title="Synthetic movie"
    )
    resource = "e" * 32
    db.execute(
        "INSERT INTO library_resources(id,owner_user_id,domain,canonical_id,season_number) VALUES(?,1,'movie','tt0000001',0)",
        (resource,),
    )
    with pytest.raises(ValueError, match="cannot borrow"):
        store.record_observation(
            db,
            observer_user_id=1,
            resource_id=resource,
            evidence=result.language_evidence,
            source_generation=None,
            library_id=lib,
            asset_id=setup[-1],
            probe_id=1,
        )
