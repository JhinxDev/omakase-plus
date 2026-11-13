"""Contract tests for the versioned release tooling in ``ops/release``.

The tests exercise the reviewed decision logic only. They never call ssh,
Docker, sqlite or the network. The build module is proved against real git
trees in temporary repositories so that revision derivation and the
working-tree isolation claim are measured, not assumed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
RELEASE_DIR = REPO_ROOT / "ops" / "release"
DOCKERFILE = REPO_ROOT / "Dockerfile"
SUBTITLES_REQUIREMENTS = REPO_ROOT / "requirements-subtitles.txt"
FIXTURES = Path(__file__).parent / "fixtures" / "release"
GIT = shutil.which("git")
IMAGE_A = "sha256:" + "1" * 64
IMAGE_B = "sha256:" + "2" * 64
OLD_REVISION = "debrid-reliability-aaaaaaaaaaaa"
NEW_REVISION = "debrid-reliability-bbbbbbbbbbbb"
SERVICE = "app"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, RELEASE_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_image = _load("build_image")
check_drift = _load("check_drift")
promote = _load("promote")


def _fixture_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted((FIXTURES / "pkg").rglob("*")):
        if path.is_file():
            files[path.relative_to(FIXTURES / "pkg").as_posix()] = path.read_bytes()
    return files


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Release Test",
            "-c",
            "user.email=release@example.invalid",
            "commit",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    package = repo / "src" / "omakase" / "plus"
    package.mkdir(parents=True)
    (package / "alpha.py").write_bytes(b"VALUE = 1\n")
    _git(repo, "init")
    return repo


requires_git = pytest.mark.skipif(GIT is None, reason="git is required for tree tests")


# Manifest and revision derivation


def test_manifest_is_deterministic_and_records_every_file_hash():
    files = _fixture_files()
    hashes = build_image.file_hashes(files)
    manifest = build_image.build_manifest("a" * 40, hashes)
    reordered = build_image.build_manifest("a" * 40, dict(reversed(list(hashes.items()))))

    assert build_image.manifest_bytes(manifest) == build_image.manifest_bytes(reordered)
    assert manifest["schema"] == "omakase.release.manifest/1"
    assert manifest["git_commit"] == "a" * 40
    assert manifest["package_tree"] == "src/omakase/plus"
    assert manifest["file_count"] == len(files) == 2
    assert sorted(manifest["files"]) == ["alpha.py", "nested/beta.sql"]
    assert manifest["files"]["alpha.py"] == hashlib.sha256(b"VALUE = 1\n").hexdigest()
    assert manifest["revision"] == build_image.derive_revision(hashes)
    assert len(manifest["revision"]) == len("debrid-reliability-") + 12


def test_revision_is_stable_across_commits_and_changes_with_content(tmp_path):
    repo = _repository(tmp_path)
    first = _commit(repo, "one")
    (repo / "README.md").write_bytes(b"notes\n")
    second = _commit(repo, "docs only")

    first_files = build_image.tree_files(repo, first)
    second_files = build_image.tree_files(repo, second)
    assert first_files == second_files
    first_hashes = build_image.file_hashes(first_files)
    second_hashes = build_image.file_hashes(second_files)
    assert build_image.derive_revision(first_hashes) == build_image.derive_revision(second_hashes)
    assert build_image.build_manifest(first, first_hashes)["git_commit"] == first
    assert build_image.build_manifest(second, second_hashes)["git_commit"] == second

    (repo / "src" / "omakase" / "plus" / "alpha.py").write_bytes(b"VALUE = 2\n")
    third = _commit(repo, "app change")
    third_hashes = build_image.file_hashes(build_image.tree_files(repo, third))
    assert build_image.derive_revision(third_hashes) != build_image.derive_revision(first_hashes)


def test_tree_files_ignore_the_working_tree(tmp_path):
    repo = _repository(tmp_path)
    commit = _commit(repo, "one")
    (repo / "src" / "omakase" / "plus" / "alpha.py").write_bytes(b"VALUE = 99\n")

    files = build_image.tree_files(repo, commit)
    assert files["alpha.py"] == b"VALUE = 1\n"


def test_tree_files_reject_a_missing_package(tmp_path):
    repo = _repository(tmp_path)
    commit = _commit(repo, "one")
    with pytest.raises(build_image.ReleaseError, match="no files under"):
        build_image.tree_files(repo, commit, "src/omakase/missing")


@requires_git
def test_repository_head_manifest_matches_the_checked_out_tree():
    commit = build_image.resolve_commit(REPO_ROOT, "HEAD")
    files = build_image.tree_files(REPO_ROOT, commit)
    manifest = build_image.build_manifest(commit, build_image.file_hashes(files))

    assert manifest["file_count"] == len(files) > 300
    assert "native_tv_hls.py" in manifest["files"]
    assert "migrations/056-native-completion-sources.sql" in manifest["files"]
    assert manifest["revision"].startswith("debrid-reliability-")


@requires_git
def test_build_image_dry_run_cli_writes_a_manifest_without_docker(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    result = subprocess.run(
        [
            sys.executable,
            str(RELEASE_DIR / "build_image.py"),
            "--dry-run",
            "--manifest",
            str(manifest_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert plan["image"] is None
    assert plan["revision"] == manifest["revision"]
    assert plan["mode"] == "dry-run"
    assert "DRY RUN" in result.stderr


# Drift comparison


def test_drift_reports_each_status_and_sets_the_exit_code():
    expected = {"a.py": "1" * 64, "b.py": "2" * 64, "c.py": "3" * 64}
    observed = {"a.py": "1" * 64, "b.py": "9" * 64, "d.py": "4" * 64}

    rows = check_drift.compare_files(expected, observed)
    statuses = {row.path: row.status for row in rows}
    assert statuses == {
        "a.py": check_drift.STATUS_MATCH,
        "b.py": check_drift.STATUS_CONTENT,
        "c.py": check_drift.STATUS_MISSING,
        "d.py": check_drift.STATUS_EXTRA,
    }
    assert check_drift.drift_exit_code(rows) == 1
    assert check_drift.summarize(rows) == {
        check_drift.STATUS_MATCH: 1,
        check_drift.STATUS_CONTENT: 1,
        check_drift.STATUS_MISSING: 1,
        check_drift.STATUS_EXTRA: 1,
    }


def test_drift_table_lists_only_drifted_files_by_default():
    expected = {"a.py": "1" * 64, "b.py": "2" * 64}
    observed = {"a.py": "1" * 64, "b.py": "9" * 64}
    rows = check_drift.compare_files(expected, observed)

    table = check_drift.format_table(rows)
    assert "b.py" in table
    assert "a.py" not in table
    assert check_drift.drift_exit_code(rows) == 1

    full = check_drift.format_table(rows, show_matches=True)
    assert "a.py" in full
    assert check_drift.format_table([row for row in rows if row.status == "match"]) == (
        "no per-file differences"
    )


def test_drift_passes_when_files_and_label_match():
    observed = {"a.py": "1" * 64}
    rows = check_drift.compare_files({"a.py": "1" * 64}, observed)

    assert check_drift.drift_exit_code(rows) == 0
    assert check_drift.drift_exit_code(rows, label_matches=False) == 1


def test_drift_ignores_runtime_bytecode():
    assert check_drift.is_runtime_artifact("__pycache__/x.cpython-312.pyc")
    assert check_drift.is_runtime_artifact("nested/__pycache__/y.pyc")
    assert not check_drift.is_runtime_artifact("native_tv_hls.py")
    observed = {"a.py": "1" * 64, "__pycache__/a.cpython-312.pyc": "2" * 64}

    shipped = check_drift.shipped_files(observed)
    rows = check_drift.compare_files({"a.py": "1" * 64}, shipped)
    assert shipped == {"a.py": "1" * 64}
    assert check_drift.drift_exit_code(rows) == 0


def test_check_drift_defaults_are_read_only_and_remote():
    args = check_drift.parse_args([])
    assert args.host == "omakase-ct"
    assert args.container == "omakase-plus"
    assert args.commit == "HEAD"
    assert args.all is False
    assert args.json is False


# Cutover guards


@pytest.mark.parametrize(
    ("cutover_started", "candidate_healthy", "expected"),
    [
        (False, False, False),
        (False, True, False),
        (True, False, True),
        (True, True, False),
    ],
)
def test_rollback_decision(cutover_started, candidate_healthy, expected):
    assert (
        promote.rollback_decision(
            cutover_started=cutover_started, candidate_healthy=candidate_healthy
        )
        is expected
    )


def _scope(before: dict[str, object], after: dict[str, object]) -> str | None:
    return promote.compose_scope_error(
        before,
        after,
        service=SERVICE,
        expected_before_image=IMAGE_A,
        expected_after_image=IMAGE_B,
        expected_flags={"OMAKASE_REVISION": NEW_REVISION},
        expected_before_flags={"OMAKASE_REVISION": OLD_REVISION},
    )


def test_compose_scope_accepts_an_image_only_diff():
    assert _scope(_fixture("compose-before.json"), _fixture("compose-image-only.json")) is None


def test_compose_scope_refuses_any_other_change():
    error = _scope(_fixture("compose-before.json"), _fixture("compose-extra-change.json"))
    assert error is not None
    assert "services.app.ports" in error
    assert "9999" not in error


def test_compose_scope_refuses_a_wrong_image_or_unapplied_flag():
    before = _fixture("compose-before.json")
    after = _fixture("compose-image-only.json")
    wrong = json.loads(json.dumps(after))
    wrong["services"][SERVICE]["image"] = IMAGE_A
    assert (
        promote.compose_scope_error(
            before,
            wrong,
            service=SERVICE,
            expected_before_image=IMAGE_A,
            expected_after_image=IMAGE_B,
            expected_flags={"OMAKASE_REVISION": NEW_REVISION},
            expected_before_flags={"OMAKASE_REVISION": OLD_REVISION},
        )
        == f"candidate compose image is not {IMAGE_B}"
    )

    flagless = json.loads(json.dumps(after))
    flagless["services"][SERVICE]["environment"]["OMAKASE_REVISION"] = OLD_REVISION
    assert (
        promote.compose_scope_error(
            before,
            flagless,
            service=SERVICE,
            expected_before_image=IMAGE_A,
            expected_after_image=IMAGE_B,
            expected_flags={"OMAKASE_REVISION": NEW_REVISION},
            expected_before_flags={"OMAKASE_REVISION": OLD_REVISION},
        )
        == "candidate flag OMAKASE_REVISION was not applied"
    )


def test_compose_scope_normalises_list_environment_entries():
    before = {
        "services": {
            SERVICE: {
                "image": IMAGE_A,
                "environment": ["OMAKASE_ENV=production", f"OMAKASE_REVISION={OLD_REVISION}"],
            }
        }
    }
    after = {
        "services": {
            SERVICE: {
                "image": IMAGE_B,
                "environment": ["OMAKASE_ENV=production", f"OMAKASE_REVISION={NEW_REVISION}"],
            }
        }
    }
    assert _scope(before, after) is None
    with pytest.raises(promote.ReleaseError, match="duplicate"):
        promote.normalize_environment(["OMAKASE_ENV=a", "OMAKASE_ENV=b"])


def test_disk_gate_requires_reserve_plus_backup():
    reserve, backup = 1000, 200
    assert promote.disk_gate_error(2000, reserve, backup) is None
    assert promote.disk_gate_error(reserve + backup, reserve, backup) is not None
    assert promote.disk_gate_error(reserve, reserve, 0) is not None
    assert promote.disk_gate_error(1200, reserve, backup) is not None


def test_migration_gate_allows_only_reviewed_migrations():
    before = ["001-one.sql", "002-two.sql"]
    assert promote.migration_error(before, before) is None
    assert promote.migration_error(before, [*before, "003-three.sql"], ["003-three.sql"]) is None
    assert promote.migration_error(before, [*before, "003-other.sql"], ["003-three.sql"])
    assert promote.migration_error(before, [*before, "003-three.sql"]) is not None
    assert promote.migration_error(before, before[1:]) is not None


def test_playback_and_health_gates():
    assert promote.native_playback_active(
        ["GET /plus/api/native-tv-test/v1/media/abc"], force_idle=False
    )
    assert promote.native_playback_active(
        ["POST /plus/api/native-tv-test/v1/library/media/abc/resume done"], force_idle=False
    )
    assert not promote.native_playback_active(["GET /health"], force_idle=False)
    assert not promote.native_playback_active(
        ["GET /plus/api/native-tv-test/v1/media/abc"], force_idle=True
    )
    assert promote.health_ok({"status": "ok", "revision": NEW_REVISION}, NEW_REVISION)
    assert not promote.health_ok({"status": "ok", "revision": OLD_REVISION}, NEW_REVISION)
    assert not promote.health_ok({"status": "degraded", "revision": NEW_REVISION}, NEW_REVISION)


def test_override_changes_only_the_image_and_revision():
    override = promote.deploy_override("omakase-plus", IMAGE_B, NEW_REVISION)
    assert override == {
        "services": {
            "omakase-plus": {"image": IMAGE_B, "environment": {"OMAKASE_REVISION": NEW_REVISION}}
        }
    }


def test_tree_problems_reports_content_missing_and_unexpected():
    expected = {"a.py": "1" * 64, "b.py": "2" * 64, "__pycache__/a.pyc": "3" * 64}
    observed = {"a.py": "9" * 64, "c.py": "4" * 64, "__pycache__/a.pyc": "5" * 64}

    problems = promote.tree_problems(expected, observed)
    assert any("content differs: a.py" in problem for problem in problems)
    assert any("missing: b.py" in problem for problem in problems)
    assert any("unexpected: c.py" in problem for problem in problems)
    assert promote.tree_problems({"a.py": "1" * 64}, {"a.py": "1" * 64}) == []


# Subtitle runtime parity with the production overlay


def test_dockerfile_declares_the_subtitle_runtime():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "tesseract-ocr" in dockerfile
    assert "mkvtoolnix" in dockerfile
    assert "ffmpeg" in dockerfile
    assert "requirements-subtitles.txt" in dockerfile
    assert "--require-hashes" in dockerfile
    for marker in ("tesseract", "mkvmerge", "faster_whisper", "pgsrip"):
        assert marker in build_image.TOOL_READBACK


def test_subtitle_requirements_are_complete_and_hash_pinned():
    text = SUBTITLES_REQUIREMENTS.read_text(encoding="utf-8")
    entries = dict(re.findall(r"(?m)^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s\\]+)", text))
    names = {name.lower().replace("_", "-") for name in entries}

    assert {"faster-whisper", "pgsrip"} <= names
    assert entries["faster-whisper"] == "1.2.1"
    assert entries["pgsrip"] == "0.3.0"
    # Shared packages are pinned to the application versions so the subtitle
    # install can never rewrite the app stack.
    assert entries["anyio"] == "4.14.2"
    assert entries["typing-extensions"] == "4.15.0"
    blocks = re.split(r"(?m)^(?=[A-Za-z0-9][A-Za-z0-9_.-]*==)", text)
    assert len(blocks) > 1
    for block in blocks[1:]:
        assert "--hash=sha256:" in block, block.splitlines()[0]


def test_remote_build_refuses_under_the_free_space_floor():
    floor = build_image.MIN_FREE_BYTES
    assert floor == 4 * 1024**3
    assert build_image.disk_refusal(floor + 1) is None
    assert build_image.disk_refusal(floor) is None
    message = build_image.disk_refusal(floor - 1)
    assert message is not None
    assert "refusing to build" in message


def test_remote_build_defaults_are_local_and_explicit():
    args = build_image.parse_args([])
    assert args.remote is None
    assert args.min_free_bytes == build_image.MIN_FREE_BYTES
    assert args.readback is False
    assert build_image.parse_args(["--remote", "omakase-ct", "--readback"]).readback is True


def test_build_verification_ignores_installed_bytecode_only():
    expected = {"alpha.py": "1" * 64}
    record = {
        "files": {
            "alpha.py": "1" * 64,
            "__pycache__/alpha.cpython-312.pyc": "2" * 64,
        }
    }
    assert build_image.verify_tree(record, expected) is record
    assert build_image.is_runtime_artifact("__pycache__/alpha.cpython-312.pyc")
    assert not build_image.is_runtime_artifact("alpha.py")

    with pytest.raises(build_image.ReleaseError, match="content differs"):
        build_image.verify_tree({"files": {"alpha.py": "9" * 64}}, expected)
    with pytest.raises(build_image.ReleaseError, match="not in the commit tree"):
        build_image.verify_tree({"files": {"alpha.py": "1" * 64, "extra.py": "2" * 64}}, expected)
    with pytest.raises(build_image.ReleaseError, match="missing from image"):
        build_image.verify_tree({"files": {}}, expected)


# Manifest acceptance and dry-run defaults
def _valid_manifest(commit: str = "c" * 40) -> dict[str, object]:
    hashes = build_image.file_hashes(_fixture_files())
    return build_image.build_manifest(commit, hashes)


def test_manifest_validation_accepts_a_built_manifest():
    manifest = _valid_manifest()
    assert promote.validate_manifest(manifest, "c" * 40) == manifest


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.update(schema="other/1"),
        lambda document: document.update(revision="debrid-reliability-short"),
        lambda document: document.update(revision="other-prefix-aaaaaaaaaaaa"),
        lambda document: document.update(files={}),
        lambda document: document["files"].update({"broken.py": "abc"}),
    ],
)
def test_manifest_validation_rejects_broken_documents(mutate):
    manifest = json.loads(json.dumps(_valid_manifest()))
    mutate(manifest)
    with pytest.raises(promote.ReleaseError):
        promote.validate_manifest(manifest)


def test_manifest_validation_cross_checks_the_commit():
    manifest = _valid_manifest()
    with pytest.raises(promote.ReleaseError, match="for commit"):
        promote.validate_manifest(manifest, "d" * 40)


def test_promote_is_dry_run_by_default(tmp_path):
    manifest = tmp_path / "manifest.json"
    default = promote.parse_args(["--manifest", str(manifest)])
    assert default.execute is False
    assert default.rollback is False
    assert default.check is False
    assert promote.parse_args(["--manifest", str(manifest), "--execute"]).execute is True
    assert promote.parse_args(["--check"]).check is True


def test_help_text_states_the_dry_run_default():
    result = subprocess.run(
        [sys.executable, str(RELEASE_DIR / "promote.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Dry run by default" in result.stdout
