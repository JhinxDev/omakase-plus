"""Local publication plan/apply/rollback tests."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from omakase.plus.materialize import (
    MaterializationError,
    MaterializationRequest,
    apply_materialization_plan,
    plan_materialization,
    rollback_materialization,
)
from omakase.plus.storage import configure_root, file_receipt


def _roots(tmp_path):
    source_path = tmp_path / "imports"
    destination_path = tmp_path / "library"
    source_path.mkdir()
    destination_path.mkdir()
    return (
        configure_root(source_path, owner_id=1),
        configure_root(destination_path, owner_id=1),
    )


def _request(source_root, name="Fixture Show S01E01.mkv"):
    path = source_root.path / name
    path.write_bytes(b"media-content")
    digest = file_receipt(source_root, name).sha256
    return MaterializationRequest(
        source_locator=name,
        destination_locator=f"managed/Fixture Show/{name}",
        content_sha256=digest,
    )


def test_auto_hardlink_apply_and_exact_rollback(tmp_path):
    source_root, destination_root = _roots(tmp_path)
    request = _request(source_root)
    plan = plan_materialization(
        source_root=source_root,
        destination_root=destination_root,
        requests=(request,),
    )

    assert plan["items"][0]["strategy"] == "hardlink"
    receipt = apply_materialization_plan(
        plan=plan,
        expected_plan_hash=plan["plan_hash"],
        source_root=source_root,
        destination_root=destination_root,
    )

    destination = destination_root.path / request.destination_locator
    assert destination.read_bytes() == b"media-content"
    assert destination.stat().st_ino == (source_root.path / request.source_locator).stat().st_ino
    assert rollback_materialization(receipt=receipt, destination_root=destination_root) == 1
    assert not destination.exists()
    assert (source_root.path / request.source_locator).exists()


def test_plan_refuses_existing_destination(tmp_path):
    source_root, destination_root = _roots(tmp_path)
    request = _request(source_root)
    destination = destination_root.path / request.destination_locator
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"do-not-overwrite")

    with pytest.raises(MaterializationError, match="already exists"):
        plan_materialization(
            source_root=source_root,
            destination_root=destination_root,
            requests=(request,),
        )
    assert destination.read_bytes() == b"do-not-overwrite"


def test_apply_refuses_source_changed_after_plan(tmp_path):
    source_root, destination_root = _roots(tmp_path)
    request = _request(source_root)
    plan = plan_materialization(
        source_root=source_root,
        destination_root=destination_root,
        requests=(request,),
    )
    (source_root.path / request.source_locator).write_bytes(b"changed")

    with pytest.raises(MaterializationError, match="source changed"):
        apply_materialization_plan(
            plan=plan,
            expected_plan_hash=plan["plan_hash"],
            source_root=source_root,
            destination_root=destination_root,
        )


def test_explicit_copy_and_symlink_strategies(tmp_path):
    for strategy in ("copy", "symlink"):
        case = tmp_path / strategy
        case.mkdir()
        source_root, destination_root = _roots(case)
        request = _request(source_root)
        plan = plan_materialization(
            source_root=source_root,
            destination_root=destination_root,
            requests=(request,),
            strategy=strategy,
        )
        receipt = apply_materialization_plan(
            plan=plan,
            expected_plan_hash=plan["plan_hash"],
            source_root=source_root,
            destination_root=destination_root,
        )
        destination = destination_root.path / request.destination_locator
        assert destination.read_bytes() == b"media-content"
        assert destination.is_symlink() is (strategy == "symlink")
        rollback_materialization(receipt=receipt, destination_root=destination_root)


def test_failed_batch_rolls_back_only_files_created_by_batch(tmp_path, monkeypatch):
    source_root, destination_root = _roots(tmp_path)
    first = _request(source_root, "Fixture Show S01E01.mkv")
    second = _request(source_root, "Fixture Show S01E02.mkv")
    plan = plan_materialization(
        source_root=source_root,
        destination_root=destination_root,
        requests=(first, second),
        strategy="hardlink",
    )
    original_link = os.link
    calls = 0

    def fail_second(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated")
        return original_link(source, destination)

    monkeypatch.setattr("omakase.plus.materialize.os.link", fail_second)

    with pytest.raises(MaterializationError, match="filesystem operation"):
        apply_materialization_plan(
            plan=plan,
            expected_plan_hash=plan["plan_hash"],
            source_root=source_root,
            destination_root=destination_root,
        )
    assert not (destination_root.path / first.destination_locator).exists()
    assert not (destination_root.path / second.destination_locator).exists()


def test_reflink_is_verified_with_injected_runner(tmp_path, monkeypatch):
    source_root, destination_root = _roots(tmp_path)
    request = _request(source_root)
    monkeypatch.setattr("omakase.plus.materialize.shutil.which", lambda _name: "cp")
    plan = plan_materialization(
        source_root=source_root,
        destination_root=destination_root,
        requests=(request,),
        strategy="reflink",
    )

    def reflink(command, **_kwargs):
        source = command[-2]
        destination = command[-1]
        with open(source, "rb") as input_stream, open(destination, "xb") as output_stream:
            output_stream.write(input_stream.read())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    receipt = apply_materialization_plan(
        plan=plan,
        expected_plan_hash=plan["plan_hash"],
        source_root=source_root,
        destination_root=destination_root,
        reflink_runner=reflink,
    )

    assert receipt["created_files"][0]["strategy"] == "reflink"
