"""Fake files/ffprobe -> real curator plan/receipt -> actual SQLite import/evaluations."""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from omakase.plus import households as hh
from omakase.plus import language_store as store
from omakase.plus.language_policy import LanguageRule
from omakase.plus.language_publication import CAPABILITY, LANGUAGE_PUBLICATION_QUALIFIED
from omakase.plus.manifest import (
    build_library_manifest,
    store_curator_receipt,
    validate_curator_receipt,
    validate_library_manifest,
)
from tests.plus import test_language_store as helpers

CURATOR = Path(__file__).resolve().parents[2] / "deploy" / "plex-curator"
sys.path.insert(0, str(CURATOR))
from curator_runtime import _receipt  # noqa: E402
from language_publication import probe_file  # noqa: E402
from plex_curator import (  # noqa: E402
    CuratorConfig,
    OverlayPlan,
    apply_overlay_plan,
    build_overlay_plan,
)

STAMP = "2026-10-28T12:00:00Z"


def runner(*args, **kwargs):
    if args:
        command = args[0]
        assert command[command.index("-protocol_whitelist") + 1] == "file"
    original = json.loads(helpers.runner().stdout)
    original["streams"] = [
        s for s in original["streams"] if s.get("tags", {}).get("language") != "eng"
    ]
    return SimpleNamespace(returncode=0, stdout=json.dumps(original), stderr="")


@pytest.fixture
def data(tmp_path):
    generated = helpers.setup.__wrapped__(tmp_path)
    original = next(generated)
    db, lib, item, resource, _, path, _, asset = original
    db.execute(
        "UPDATE media_monitors SET release_status='FINISHED',expected_episodes=1,aired_episodes=1 WHERE id=1"
    )
    db.execute(
        "UPDATE media_assets SET source_folder='Batch',storage_locator='Batch',quality='1080p' WHERE id=?",
        (asset,),
    )
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    defaults = store.library_defaults(db, owner)[0]
    french = LanguageRule(
        audio_required=("fr",), subtitle_required=("fr",), subtitles_mode="required"
    )
    store.set_library_defaults(db, owner, replace(defaults, anime=french))
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    store.approve_publication_contract(
        db, scope=owner, resource_id=resource, asset_id=asset, rule=french
    )
    source = tmp_path / "curator-source"
    (source / "Batch").mkdir(parents=True)
    (source / "Batch" / path.name).write_bytes(path.read_bytes())
    config = CuratorConfig(source_root=source, overlay_root=tmp_path / "overlays")
    yield original, config
    try:
        next(generated)
    except StopIteration:
        pass


def build(data, *, probe=runner):
    original, config = data
    manifest = build_library_manifest(
        original[0], generated_at=STAMP, peer_capabilities=(CAPABILITY,), qualification=True
    )

    def english_forbidden(*args):
        raise AssertionError("The English heuristic must not certify French")

    plan = build_overlay_plan(
        config,
        manifest,
        subtitle_probe=english_forbidden,
        require_english_subtitles=True,
        language_probe=partial(probe_file, runner=probe),
        language_qualification=True,
    )
    return manifest, plan


def receipt(plan, monkeypatch):
    import curator_runtime

    monkeypatch.setattr(curator_runtime, "_utc_now", lambda: "2026-10-28T12:01:00Z")
    titles = ("Anime", "Anime Movies", "TV Shows", "Movies")
    return _receipt(
        plan=SimpleNamespace(
            overlay_plan=plan, plan_hash=plan.plan_hash, manifest_generated_at=STAMP
        ),
        mode="live",
        started_at=STAMP,
        sections=tuple(SimpleNamespace(title=t) for t in titles),
        observed=[
            {"key": i + 1, "title": t, "count": 1 if i == 0 else 0} for i, t in enumerate(titles)
        ],
        title_coverage=[
            {
                "anilist_id": 21,
                "section": "Anime",
                "season_number": 1,
                "usable_count": 1,
                "plex_visible_count": len(plan.language_files),
            }
        ],
        topology_converged=True,
    )


def test_real_plan_receipt_import_satisfies_two_viewers_same_multitrack_file(data, monkeypatch):
    original, config = data
    db = original[0]
    owner = helpers.snapshot(
        original,
        1,
        "owner-fr",
        LanguageRule(audio_required=("fr",), subtitle_required=("fr",), subtitles_mode="required"),
    )
    member = helpers.snapshot(
        original,
        2,
        "member-ja",
        LanguageRule(audio_required=("ja",), subtitle_required=("fr",), subtitles_mode="required"),
    )
    for snapshot in (owner, member):
        store.record_request(db, snapshot, required_episodes=(1,))
    manifest, plan = build(data)
    assert (
        manifest["schema_version"] == "manifest-v3"
        and len(plan.entries) == 1
        and len(plan.language_files) == 1
    )
    assert not plan.embedded_subtitles
    assert not any(t["language"] == "en" for t in plan.language_files[0]["evidence"]["tracks"])
    assert OverlayPlan.from_dict(plan.to_dict()) == plan
    value = receipt(plan, monkeypatch)
    validate_curator_receipt(value)
    db.commit()
    accepted = store_curator_receipt(db, value, language_qualification=True)
    assert accepted["stored"]
    observation = db.execute("SELECT id FROM asset_language_observations").fetchone()[0]
    generation = helpers.current_generation(original)
    for snapshot in (owner, member):
        _, evaluated = store.evaluate_request(
            db,
            actor_user_id=snapshot.actor_user_id,
            request_id=snapshot.request_id,
            observation_id=observation,
            source_generation=generation,
        )
        assert evaluated.physical_compatible
        assert store.request_language_status(
            db,
            actor_user_id=snapshot.actor_user_id,
            request_id=snapshot.request_id,
            source_generation=generation,
        )["language_coverage_complete"]
    assert db.execute("SELECT count(*) FROM asset_language_observations").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM language_evaluations").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM media_assets").fetchone()[0] == 1
    assert not any(
        k in json.dumps(value)
        for k in ("actor_user_id", "user_id", "synthetic-owner", "synthetic-member")
    )


def test_unqualified_peer_and_import_are_fail_closed(data, monkeypatch):
    assert LANGUAGE_PUBLICATION_QUALIFIED is False
    db = data[0][0]
    with pytest.raises(ValueError):
        build_library_manifest(db)
    manifest, plan = build(data)
    with pytest.raises(ValueError):
        build_overlay_plan(data[1], manifest, language_probe=partial(probe_file, runner=runner))
    db.commit()
    with pytest.raises(ValueError):
        store_curator_receipt(db, receipt(plan, monkeypatch))
    assert db.execute("SELECT count(*) FROM asset_language_observations").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["inventory", "source", "resource", "episodes", "policy"])
def test_import_checks_actual_owner_contract_source_and_asset_not_receipt_labels(
    data, monkeypatch, field
):
    db = data[0][0]
    _, plan = build(data)
    value = receipt(plan, monkeypatch)
    if field == "inventory":
        db.execute("UPDATE media_assets SET inventory_fingerprint=?", ("d" * 64,))
    elif field == "source":
        hh.connection_generation_changed(db, owner_user_id=1, integration="realdebrid")
    elif field == "resource":
        value["language_files"][0]["contract"]["resource_id"] = "d" * 32
    elif field == "episodes":
        value["language_files"][0]["episodes"] = [2]
    else:
        value["language_files"][0]["contract"]["policy"]["audio_required"] = ["de"]
    db.commit()
    with pytest.raises(ValueError):
        store_curator_receipt(db, value, language_qualification=True)
    assert db.execute("SELECT count(*) FROM media_file_probes").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM asset_language_observations").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM media_curator_receipt_latest").fetchone()[0] == 0


def test_unknown_language_cannot_publish_and_holds_previous_generation(data):
    original, config = data

    def unknown(*args, **kwargs):
        reply = runner()
        raw = json.loads(reply.stdout)
        for stream in raw["streams"]:
            if stream.get("codec_type") == "subtitle":
                stream["tags"]["language"] = "und"
        return SimpleNamespace(returncode=0, stdout=json.dumps(raw), stderr="")

    _, plan = build(data, probe=unknown)
    assert not plan.entries and plan.holds_current_generation
    assert plan.blocked_assets[0].code == "LANGUAGE_POLICY_UNCONFIRMED"
    assert not apply_overlay_plan(config, plan, expected_plan_hash=plan.plan_hash).changed


def test_changed_bounded_bytes_after_approval_refuses_overlay_mutation(data):
    _, config = data
    _, plan = build(data)
    file = config.source_root / plan.language_files[0]["source"]
    file.write_bytes(b"changed fake bytes")
    with pytest.raises(ValueError):
        apply_overlay_plan(config, plan, expected_plan_hash=plan.plan_hash)
    assert not config.current_pointer.exists()


def test_closed_v3_shapes_reject_unrecognized_fields_and_old_schema(data, monkeypatch):
    manifest, plan = build(data)
    bad = copy.deepcopy(manifest)
    bad["titles"][0]["assets"][0]["language_contract"]["viewer"] = "forbidden"
    with pytest.raises(ValueError):
        validate_library_manifest(bad)
    bad = copy.deepcopy(manifest)
    bad["schema_version"] = "manifest-v2"
    with pytest.raises(ValueError):
        validate_library_manifest(bad)
    value = receipt(plan, monkeypatch)
    value["language_files"][0]["unexpected"] = "field"
    with pytest.raises(ValueError):
        validate_curator_receipt(value)


def test_changed_or_withheld_file_cannot_reuse_a_historical_language_success(data, monkeypatch):
    original, config = data
    db = original[0]
    snapshot = helpers.snapshot(
        original,
        2,
        "current-fr",
        LanguageRule(audio_required=("fr",), subtitle_required=("fr",), subtitles_mode="required"),
    )
    store.record_request(db, snapshot, required_episodes=(1,))
    _, first = build(data)
    db.commit()
    store_curator_receipt(db, receipt(first, monkeypatch), language_qualification=True)
    old = db.execute("SELECT id FROM asset_language_observations").fetchone()[0]
    generation = helpers.current_generation(original)
    store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=snapshot.request_id,
        observation_id=old,
        source_generation=generation,
    )
    source = config.source_root / first.language_files[0]["source"]
    source.write_bytes(b"new synthetic physical file contents")
    _, second = build(data)
    second_receipt = receipt(second, monkeypatch)
    second_receipt["completed_at"] = "2026-10-28T12:02:00Z"
    db.commit()
    store_curator_receipt(db, second_receipt, language_qualification=True)
    with pytest.raises(ValueError, match="superseded"):
        store.evaluate_request(
            db,
            actor_user_id=2,
            request_id=snapshot.request_id,
            observation_id=old,
            source_generation=generation,
        )
    assert not store.request_language_status(
        db, actor_user_id=2, request_id=snapshot.request_id, source_generation=generation
    )["language_coverage_complete"]
    new = db.execute("SELECT max(id) FROM asset_language_observations").fetchone()[0]
    assert new != old
    store.evaluate_request(
        db,
        actor_user_id=2,
        request_id=snapshot.request_id,
        observation_id=new,
        source_generation=generation,
    )
    assert store.request_language_status(
        db, actor_user_id=2, request_id=snapshot.request_id, source_generation=generation
    )["language_coverage_complete"]

    def unknown(*args, **kwargs):
        raw = json.loads(runner().stdout)
        for stream in raw["streams"]:
            if stream.get("codec_type") == "subtitle":
                stream["tags"]["language"] = "und"
        return SimpleNamespace(returncode=0, stdout=json.dumps(raw), stderr="")

    _, withheld = build(data, probe=unknown)
    last = receipt(withheld, monkeypatch)
    last["completed_at"] = "2026-10-28T12:03:00Z"
    db.commit()
    store_curator_receipt(db, last, language_qualification=True)
    from omakase.plus.media_sync import reconcile_curator_blocked_assets

    assert reconcile_curator_blocked_assets(db, last) == ()
    with pytest.raises(ValueError, match="withheld"):
        store.evaluate_request(
            db,
            actor_user_id=2,
            request_id=snapshot.request_id,
            observation_id=new,
            source_generation=generation,
        )
    assert db.execute("SELECT count(*) FROM asset_language_observations").fetchone()[0] == 2
    assert (
        db.execute("SELECT count(*) FROM media_assets WHERE status='downloaded'").fetchone()[0] == 1
    )


def test_receipt_cannot_omit_language_evidence_for_a_successfully_published_asset(
    data, monkeypatch
):
    db = data[0][0]
    _, plan = build(data)
    value = receipt(plan, monkeypatch)
    value["language_files"] = []
    db.commit()
    with pytest.raises(ValueError, match="omits"):
        store_curator_receipt(db, value, language_qualification=True)
    assert db.execute("SELECT count(*) FROM media_curator_receipt_latest").fetchone()[0] == 0


def test_probe_process_stdout_is_bounded_before_buffering_and_deadline_is_enforced():
    import subprocess

    from language_publication import bounded_probe_runner

    with pytest.raises(ValueError, match="output exceeded"):
        bounded_probe_runner(
            [sys.executable, "-c", "import sys;sys.stdout.write('x'*300000)"], timeout=5
        )
    with pytest.raises(subprocess.TimeoutExpired):
        bounded_probe_runner([sys.executable, "-c", "import time;time.sleep(5)"], timeout=0.1)


def test_current_file_projection_rejects_cross_file_pointer(data, monkeypatch):
    import sqlite3

    db = data[0][0]
    _, plan = build(data)
    db.commit()
    store_curator_receipt(db, receipt(plan, monkeypatch), language_qualification=True)
    with pytest.raises(sqlite3.IntegrityError, match="binding mismatch"):
        db.execute("UPDATE language_publication_current_files SET file_locator='other.mkv'")


def test_malformed_wire_types_are_contract_errors_before_any_import(data, monkeypatch):
    manifest, plan = build(data)
    manifest["titles"] = ["not an object"]
    with pytest.raises(ValueError):
        validate_library_manifest(manifest)
    value = receipt(plan, monkeypatch)
    value["language_files"][0]["video"][0]["codec"] = []
    with pytest.raises(ValueError):
        validate_curator_receipt(value)
