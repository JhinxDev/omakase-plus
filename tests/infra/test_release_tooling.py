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
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
RELEASE_DIR = REPO_ROOT / "ops" / "release"
DOCKERFILE = REPO_ROOT / "Dockerfile"
SUBTITLES_REQUIREMENTS = REPO_ROOT / "requirements-subtitles.txt"
FIXTURES = Path(__file__).parent / "fixtures" / "release"
GIT = shutil.which("git")
# Every external process carries a finite deadline of its own, independent of the
# suite-wide guard in tests/conftest.py.
GIT_TIMEOUT_SECONDS = 120
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
collapse = _load("collapse")
github_release_notes = _load("github_release_notes")
promote = _load("promote")


def _fixture_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted((FIXTURES / "pkg").rglob("*")):
        if path.is_file():
            files[path.relative_to(FIXTURES / "pkg").as_posix()] = path.read_bytes()
    return files


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# Isolate Git writes to temporary fixture repos and the release-tool reads that
# follow them, even when pytest is running inside a hook in a linked worktree.
pytestmark = pytest.mark.usefixtures("isolated_git_environment")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
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
        timeout=GIT_TIMEOUT_SECONDS,
        text=True,
    )
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    package = repo / "src" / "omakase" / "plus"
    package.mkdir(parents=True)
    (package / "alpha.py").write_bytes(b"VALUE = 1\n")
    _git(repo, "init")
    assert (repo / ".git").is_dir(), "fixture must initialize its own Git directory"
    return repo


requires_git = pytest.mark.skipif(GIT is None, reason="git is required for tree tests")


def test_git_hook_environment_cannot_redirect_fixture_writes(tmp_path):
    canary = _repository(tmp_path / "canary")
    initial = _commit(canary, "unrelated canary commit")
    metadata = canary / ".git"
    before = {name: (metadata / name).read_bytes() for name in ("HEAD", "config", "index")}
    env = {
        **os.environ,
        "GIT_DIR": str(metadata),
        "GIT_COMMON_DIR": str(metadata),
        "GIT_WORK_TREE": str(canary),
        "GIT_INDEX_FILE": str(metadata / "index"),
        "GIT_OBJECT_DIRECTORY": str(metadata / "objects"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.bare",
        "GIT_CONFIG_VALUE_0": "true",
    }
    # Exercise the actual autouse fixture in a fresh pytest process, so every
    # mutating Git operation and release-tool read sees a hook-like environment.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"{Path(__file__).resolve()}::test_revision_is_stable_across_commits_and_changes_with_content",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout + result.stderr
    assert _git(canary, "rev-parse", "HEAD") == initial
    assert {name: (metadata / name).read_bytes() for name in before} == before
    assert not _git(canary, "status", "--porcelain")


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
        check_drift.STATUS_LINE_ENDINGS: 0,
        check_drift.STATUS_CONTENT: 1,
        check_drift.STATUS_MISSING: 1,
        check_drift.STATUS_EXTRA: 1,
    }


def test_drift_separates_line_endings_from_changed_content():
    """A copy that differs only by carriage returns is drift, but named as such."""
    expected = {"a.py": "1" * 64, "b.py": "2" * 64}
    observed = {"a.py": "5" * 64, "b.py": "9" * 64}
    # The CR-stripped digest is what the live reader measures alongside the raw one.
    text = {"a.py": "1" * 64, "b.py": "9" * 64}

    rows = check_drift.compare_files(expected, observed, text=text)
    statuses = {row.path: row.status for row in rows}
    assert statuses == {
        "a.py": check_drift.STATUS_LINE_ENDINGS,
        "b.py": check_drift.STATUS_CONTENT,
    }
    # The bytes still differ from the commit, so the container is still drifted.
    assert check_drift.drift_exit_code(rows) == 1
    counts = check_drift.summarize(rows)
    assert counts[check_drift.STATUS_LINE_ENDINGS] == 1
    assert counts[check_drift.STATUS_CONTENT] == 1
    table = check_drift.format_table(rows)
    assert "line-endings" in table and "b.py" in table
    note = check_drift.line_ending_note(counts)
    assert note is not None and "LINE ENDINGS" in note


def test_drift_never_excuses_a_real_content_difference():
    """Negative cases: without the second digest, and when bodies really differ."""
    expected = {"a.py": "1" * 64}
    observed = {"a.py": "5" * 64}

    # No CR-stripped digest was measured, so a raw difference stays content.
    plain = check_drift.compare_files(expected, observed)
    assert [row.status for row in plain] == [check_drift.STATUS_CONTENT]
    assert check_drift.line_ending_note(check_drift.summarize(plain)) is None

    # A stripped digest that still differs is not a line-ending difference.
    differing = check_drift.compare_files(expected, observed, text={"a.py": "7" * 64})
    assert [row.status for row in differing] == [check_drift.STATUS_CONTENT]
    assert check_drift.line_ending_note(check_drift.summarize(differing)) is None


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


def test_drift_reader_ships_the_hash_pass_without_a_placeholder(monkeypatch):
    """The program actually sent over ssh carries both digest maps, not __HASH_PASS__."""
    payload = {
        "image": IMAGE_A,
        "label": NEW_REVISION,
        "package": "/pkg",
        "files": {"a.py": "1" * 64},
        "text_files": {"a.py": "2" * 64},
    }
    sent: list[dict[str, object]] = []

    def fake_run(args, **kwargs):
        sent.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(
        check_drift,
        "subprocess",
        types.SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired),
    )
    live = check_drift.read_live("omakase-ct", "omakase-plus", timeout=5)

    program = sent[0]["input"].decode("utf-8")  # type: ignore[union-attr]
    assert "__HASH_PASS__" not in program
    assert "text_files" in program
    # The digest pass is one python program, not a shell pipeline of coreutils.
    assert "sha256sum" not in program
    assert live["text_files"] == {"a.py": "2" * 64}


def test_drift_timeout_reports_captured_output(monkeypatch):
    """A timeout names the deadline and dumps what the read had produced."""

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 5, output=b"partial stdout", stderr=b"partial stderr")

    monkeypatch.setattr(
        check_drift,
        "subprocess",
        types.SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired),
    )
    with pytest.raises(check_drift.DriftError) as caught:
        check_drift.read_live("omakase-ct", "omakase-plus", timeout=5)

    message = str(caught.value)
    assert "timed out after 5s" in message
    assert "partial stdout" in message and "partial stderr" in message


# Cutover guards


def _release_db(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE _migrations (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO _migrations VALUES (1, '001-plus.sql');
            CREATE TABLE native_tv_devices (id INTEGER PRIMARY KEY, user_id INTEGER, expires_at INTEGER);
            CREATE TABLE web_playback_sessions (closed INTEGER, expires_at INTEGER, last_seen INTEGER);
            CREATE TABLE movie_cleanup_jobs (id INTEGER);
            CREATE TABLE movie_cleanup_preferences (enabled INTEGER);
        """)


def test_idle_gate_queries_full_five_minutes_and_checks_paused_leases(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    _release_db(path)
    settings = promote.settings_from_args(promote.parse_args(["--db-path", str(path)]))

    def logs(argv, **kwargs):
        assert argv == ["docker", "logs", "--since", "5m", settings.service]
        return subprocess.CompletedProcess(argv, 0, "GET /health", "")

    monkeypatch.setattr(promote.subprocess, "run", logs)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO web_playback_sessions VALUES (0, unixepoch()+600, unixepoch()-2400)"
        )
    with pytest.raises(promote.ReleaseError, match="browser playback"):
        promote.idle_and_schema(settings)


def test_execute_requires_native_closure_and_writer_fence():
    for args in (["--execute"], ["--execute", "--native-playback-closed"]):
        with pytest.raises(promote.ReleaseError, match="native.*writers"):
            promote._require_execution_fence(promote.parse_args(args))
    promote._require_execution_fence(
        promote.parse_args(["--execute", "--native-playback-closed", "--writers-fenced"])
    )


def test_restore_rejects_a_snapshot_with_a_different_migration_name(tmp_path):
    db = tmp_path / "app.db"
    _release_db(db)
    schema = {"schema": [1, 1], "migrations": ["001-other.sql"]}
    with pytest.raises(promote.ReleaseError, match="migration ledger"):
        promote._snapshot_facts(db, schema, observed_at=100)


def test_durable_sqlite_backup_reopens_verified_archive(tmp_path, monkeypatch):
    db = tmp_path / "source.db"
    _release_db(db)
    with sqlite3.connect(db) as connection:
        connection.executemany(
            "INSERT INTO native_tv_devices VALUES (?, ?, ?)",
            [(1, 1, 101), (2, 1, 99), (3, None, 110)],
        )
    settings = promote.settings_from_args(promote.parse_args(["--db-path", str(db)]))
    original_temp = promote.tempfile.TemporaryDirectory
    monkeypatch.setattr(
        promote.tempfile, "TemporaryDirectory", lambda **_: original_temp(dir=tmp_path)
    )
    monkeypatch.setattr(promote, "disk_gate", lambda *args: 0)
    monkeypatch.setattr(promote, "_fsync_dir", lambda *args: None)
    stage = tmp_path / "staged"
    stage.mkdir()
    expected = {"schema": [1, 1], "migrations": ["001-plus.sql"]}
    assert promote.native_device_count(settings, observed_at=100) == 1
    assert promote.native_device_count(settings, observed_at=101) == 0
    receipt = promote.durable_sqlite_backup(settings, expected, stage, observed_at=100)
    assert receipt["restore"] == expected | {
        "quick_check": "ok",
        "foreign_keys": "ok",
        "linked_native_devices": 1,
        "native_device_rows": 3,
        "device_observed_at": 100,
    }
    assert (
        receipt["archive_sha256"] == hashlib.sha256(Path(receipt["path"]).read_bytes()).hexdigest()
    )
    assert (stage / "pre-swap.db.sha256").read_text().split()[0] == receipt["sha256"]
    if os.name == "posix":
        for path in stage.iterdir():
            assert path.stat().st_mode & 0o777 == 0o600


def test_compose_command_has_a_timeout(monkeypatch):
    monkeypatch.setenv("PLUS_UI_REVISION", "stale")
    monkeypatch.setenv("UNRELATED_INHERITED_INPUT", "preserved")
    bindings = {"PLUS_UI_REVISION": OLD_REVISION, "PLUS_UI_IMAGE_ID": IMAGE_A}

    def call(command, *, cwd, check, timeout, env, capture_output, text):
        assert command == ("docker", "compose", "up")
        assert cwd is None and check is True and timeout == 90 and capture_output and text
        assert env["PLUS_UI_REVISION"] == OLD_REVISION and env["PLUS_UI_IMAGE_ID"] == IMAGE_A
        assert env["UNRELATED_INHERITED_INPUT"] == "preserved"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(promote.subprocess, "run", call)
    promote.run("docker", "compose", "up", compose_bindings=bindings)
    assert os.environ["PLUS_UI_REVISION"] == "stale"


def test_compose_config_receives_bindings_without_resolving_secrets(monkeypatch, tmp_path):
    compose = tmp_path / "compose.yaml"
    compose.touch()
    settings = promote.settings_from_args(promote.parse_args(["--stack-dir", str(tmp_path)]))
    bindings = {"PLUS_UI_REVISION": OLD_REVISION, "PLUS_UI_IMAGE_ID": IMAGE_A}
    calls = []

    def run(*command, **kwargs):
        assert kwargs == {"cwd": tmp_path, "compose_bindings": bindings}
        calls.append(command[command.index("config") :])
        return "{}"

    monkeypatch.setattr(promote, "run", run)
    assert promote.compose_config(settings, [str(compose)], bindings) == {}
    assert calls == [
        ("config", "--quiet"),
        ("config", "--no-interpolate", "--no-env-resolution", "--format", "json"),
    ]


def test_compose_failure_does_not_echo_diagnostics(monkeypatch):
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="private sentinel")

    monkeypatch.setattr(promote.subprocess, "run", fail)
    with pytest.raises(promote.ReleaseError, match=r"^compose config failed \(exit 1\)$"):
        promote.run("docker", "compose", "-f", "private-argv.yaml", "config", compose_bindings={})


@pytest.mark.parametrize("fail_at", ["backup", "candidate_start", "candidate_stop"])
def test_failed_cutover_preserves_fence_without_starting_previous_image(
    tmp_path, monkeypatch, fail_at
):
    args = promote.parse_args(
        ["--stack-dir", str(tmp_path), "--execute", "--native-playback-closed", "--writers-fenced"]
    )
    settings = promote.settings_from_args(args)
    settings.backup_root.mkdir()
    state = settings.state_dir(NEW_REVISION)
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}")
    schema = {"schema": [1, 1], "migrations": ["001-plus.sql"]}
    plan = {
        "revision": NEW_REVISION,
        "manifest": {"files": {"x": "1" * 64}},
        "compose_files": [str(compose)],
        "schema": schema,
        "override": {},
        "image": IMAGE_B,
        "previous_image": IMAGE_A,
        "previous_image_ref": f"omakase-plus:{OLD_REVISION}",
        "previous_revision": OLD_REVISION,
        "compose_bindings": {"PLUS_UI_REVISION": OLD_REVISION, "PLUS_UI_IMAGE_ID": IMAGE_A},
        "git_commit": "a" * 40,
        "devices": 1,
        "devices_observed_at": 100,
    }
    actions = []

    def run(identity, action, *flags, cwd, compose_bindings):
        assert cwd == tmp_path
        assert compose_bindings == plan["compose_bindings"]
        actions.append((identity, action))
        if action == "up":
            assert (state / "original-compose-files.txt").read_text().splitlines() == [str(compose)]
            prepared = json.loads((state / "prepared.json").read_text())
            assert prepared["backup"]["restore"] == {"linked_native_devices": 1}
            assert prepared["compose_bindings"] == plan["compose_bindings"]
            raise promote.ReleaseError("startup test failure")
        if (identity, action, fail_at) == (2, "stop", "candidate_stop"):
            raise subprocess.TimeoutExpired("synthetic compose stop", 90)

    def backup(_settings, _schema, _state, observed_at):
        assert observed_at == 100
        if fail_at == "backup":
            raise promote.ReleaseError("restore failure")
        return {"restore": {"linked_native_devices": 1}}

    monkeypatch.setattr(promote, "compose_command", lambda _settings, files: [len(files)])
    monkeypatch.setattr(promote, "_fsync_dir", lambda *args: None)
    monkeypatch.setattr(promote, "idle_and_schema", lambda *args: schema)
    monkeypatch.setattr(promote, "disk_gate", lambda *args: 0)
    monkeypatch.setattr(promote, "_verify_before_cutover", lambda *args: None)
    monkeypatch.setattr(promote, "run", run)
    monkeypatch.setattr(promote, "durable_sqlite_backup", backup)
    with pytest.raises(promote.ReleaseError, match="restore failure|startup test failure"):
        promote.execute_flow(settings, args, plan)
    assert actions == (
        [(1, "stop")] if fail_at == "backup" else [(1, "stop"), (2, "up"), (2, "stop")]
    )
    failure = json.loads((state / "failure.json").read_text())
    assert failure["status"] == "failed_keep_fence"
    assert failure["phase"] == (
        "backup_preparation" if fail_at == "backup" else "candidate_start_attempted"
    )
    assert (state / "prepared.json").exists() == (fail_at != "backup")
    assert (
        failure["stop_status"]
        == {
            "backup": "candidate_not_started",
            "candidate_start": "candidate_stop_command_succeeded",
            "candidate_stop": "candidate_stop_failed",
        }[fail_at]
    )
    if os.name == "posix":
        assert state.stat().st_mode & 0o777 == 0o700
        for path in state.iterdir():
            assert path.stat().st_mode & 0o777 == 0o600


def test_database_backup_must_use_the_reviewed_live_bind(tmp_path):
    path = tmp_path / "omakase-plus.db"
    path.touch()
    settings = promote.settings_from_args(promote.parse_args(["--db-path", str(path)]))
    promote._verify_database_binding(settings, {"database_mounts": [str(tmp_path)]})
    with pytest.raises(promote.ReleaseError, match="data bind"):
        promote._verify_database_binding(settings, {"database_mounts": [str(tmp_path / "other")]})


def test_cutover_rechecks_bound_values_against_live_identity(tmp_path, monkeypatch):
    path = tmp_path / "omakase-plus.db"
    path.touch()
    settings = promote.settings_from_args(promote.parse_args(["--db-path", str(path)]))
    current = {
        "image": IMAGE_A,
        "revision": OLD_REVISION,
        "config_files": ("compose.yaml",),
        "health": "healthy",
        "database_mounts": [str(tmp_path)],
    }
    monkeypatch.setattr(promote, "service_state", lambda _settings: current)
    plan = {
        "previous_image": IMAGE_A,
        "previous_image_ref": f"omakase-plus:{OLD_REVISION}",
        "previous_revision": OLD_REVISION,
        "compose_files": ["compose.yaml"],
        "compose_bindings": promote._live_compose_bindings(current),
    }
    monkeypatch.setattr(
        promote,
        "image_details",
        lambda ref: {"Id": IMAGE_A, "Config": {"Labels": {promote.REVISION_LABEL: OLD_REVISION}}},
    )
    promote._verify_before_cutover(settings, plan)
    plan["compose_bindings"]["PLUS_UI_IMAGE_ID"] = IMAGE_B
    with pytest.raises(promote.ReleaseError, match="bindings changed"):
        promote._verify_before_cutover(settings, plan)


@pytest.mark.parametrize(
    ("reported_id", "reported_revision", "accepted"),
    [(IMAGE_A, OLD_REVISION, True), (IMAGE_B, OLD_REVISION, False), (IMAGE_A, NEW_REVISION, False)],
)
def test_prior_compose_tag_requires_exact_running_image_and_revision(
    monkeypatch, reported_id, reported_revision, accepted
):
    compose_tag = f"omakase-plus:{OLD_REVISION}"

    def inspect(reference):
        assert reference == compose_tag
        return {
            "Id": reported_id,
            "Config": {"Labels": {promote.REVISION_LABEL: reported_revision}},
        }

    monkeypatch.setattr(promote, "image_details", inspect)
    if not accepted:
        with pytest.raises(promote.ReleaseError, match="does not match the running release"):
            promote._verify_prior_image(compose_tag, IMAGE_A, OLD_REVISION)
    else:
        assert promote._verify_prior_image(compose_tag, IMAGE_A, OLD_REVISION) == compose_tag


def test_automatic_rollback_refused_before_any_docker_action(monkeypatch):
    def unexpected(*args):
        pytest.fail("Deployment actions must be fenced")

    monkeypatch.setattr(promote, "service_state", unexpected)
    assert promote.main(["--rollback", "--execute"]) == 1


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
    for path in (
        "media/abc",
        "library/media/abc/resume",
        "library/media/abc/next",
        "library/play",
        "direct/[redacted]",
    ):
        assert promote.native_playback_active([f"GET /plus/api/native-tv-test/v1/{path}"])
    assert not promote.native_playback_active(["GET /health"])
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


def test_dockerfile_installs_wheel_without_copying_a_runtime_layer():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert not re.search(r"^COPY\s+.*(?:/wheels|\.whl)", dockerfile, re.MULTILINE)
    assert "RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \\" in dockerfile
    assert "pip install --no-cache-dir --no-deps /wheels/*.whl" in dockerfile


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


def test_collapse_env_secret_parsing_and_leak_detection():
    env_text = "\n".join(
        [
            "# comment",
            "OMAKASE_ENV=production",
            'export SESSION_SECRET="super-secret-value-123"',
            "SHORT_KEY=abc",
            "REALDEBRID_API_TOKEN='rd-token-987654321'",
        ]
    )
    secrets = collapse.parse_env_secrets(env_text)
    assert secrets == {
        "SESSION_SECRET": "super-secret-value-123",
        "REALDEBRID_API_TOKEN": "rd-token-987654321",
    }

    safe_yaml = "services:\n  app:\n    environment:\n      SESSION_SECRET: ${SESSION_SECRET}\n"
    assert collapse.find_secret_leaks(safe_yaml, secrets) == []

    leaked_yaml = (
        "services:\n  app:\n    environment:\n      SESSION_SECRET: super-secret-value-123\n"
    )
    leaked = collapse.find_secret_leaks(leaked_yaml, secrets)
    assert leaked == ["SESSION_SECRET"]
    assert "super-secret-value-123" not in str(leaked)


def test_collapse_transient_paths_and_rehoming():
    files = [
        "/opt/stacks/omakase-plus/compose.yaml",
        "/var/tmp/omakase/overlay.yaml",
        "/tmp/extra/sub.env",
    ]
    assert collapse.find_transient_paths(files) == [
        "/var/tmp/omakase/overlay.yaml",
        "/tmp/extra/sub.env",
    ]

    yaml_text = (
        "services:\n"
        "  app:\n"
        "    env_file:\n"
        "      - /var/tmp/omakase/overlay.env\n"
        "    volumes:\n"
        "      - /tmp/one/cfg.json:/app/cfg.json:ro\n"
        "      - /tmp/two/cfg.json:/app/other.json:ro\n"
    )
    rewritten, mappings = collapse.rehome_transient_paths(yaml_text, "/opt/stacks/omakase-plus")
    assert "/var/tmp/" not in rewritten
    assert "/tmp/" not in rewritten
    assert "/opt/stacks/omakase-plus/rehomed/overlay.env" in rewritten
    assert len(mappings) == 3
    assert len({entry["target"] for entry in mappings}) == 3
    assert ":/app/cfg.json:ro" in rewritten


def test_collapse_remote_script_preserves_placeholders_and_guards_secrets():
    assert "--no-interpolate" in collapse.REMOTE
    assert "--no-env-resolution" in collapse.REMOTE
    assert "secret_leak" in collapse.REMOTE
    assert '.replace("$", "$$")' not in collapse.REMOTE


def test_promote_collapse_chain_option_and_guards(tmp_path, monkeypatch):
    args = promote.parse_args(["--manifest", str(tmp_path / "m.json"), "--collapse-chain"])
    assert args.collapse_chain is True

    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / ".env").write_text(
        "OMAKASE_SESSION_SECRET=very-secret-token-value\n", encoding="utf-8"
    )
    base = stack / "compose.yaml"
    candidate = stack / "candidate.json"
    base.write_text("services:\n  app:\n    image: old\n", encoding="utf-8")
    candidate.write_text("{}", encoding="utf-8")
    settings = promote.settings_from_args(promote.parse_args(["--stack-dir", str(stack)]))
    bindings = {"PLUS_UI_REVISION": OLD_REVISION, "PLUS_UI_IMAGE_ID": IMAGE_A}

    # Happy path writes compose-collapsed.yaml with placeholders intact.
    monkeypatch.setattr(promote, "_fsync_dir", lambda *_: None)

    def happy_run(*command, cwd, compose_bindings):
        if "--format" in command and command[command.index("--format") + 1] == "yaml":
            return "services:\n  app:\n    image: new\n    environment:\n      OMAKASE_SESSION_SECRET: ${OMAKASE_SESSION_SECRET}\n"
        return '{"services": {"app": {"image": "new"}}}'

    monkeypatch.setattr(promote, "run", happy_run)
    collapsed = promote.collapse_candidate_chain(settings, [str(base)], candidate, bindings)
    assert collapsed == stack / "compose-collapsed.yaml"
    assert collapsed.is_file()
    assert "${OMAKASE_SESSION_SECRET}" in collapsed.read_text(encoding="utf-8")

    # Secret leak refuses before writing.
    def leak_run(*command, cwd, compose_bindings):
        if "--format" in command and command[command.index("--format") + 1] == "yaml":
            return "services:\n  app:\n    environment:\n      OMAKASE_SESSION_SECRET: very-secret-token-value\n"
        return '{"services": {"app": {"image": "new"}}}'

    monkeypatch.setattr(promote, "run", leak_run)
    with pytest.raises(promote.ReleaseError, match="leaked resolved secret"):
        promote.collapse_candidate_chain(settings, [str(base)], candidate, bindings)

    # Equivalence mismatch refuses and leaves no draft file behind.
    calls = {"json": 0}

    def drift_run(*command, cwd, compose_bindings):
        if "--format" in command and command[command.index("--format") + 1] == "yaml":
            return "services:\n  app:\n    image: new\n"
        calls["json"] += 1
        return (
            '{"services": {"app": {"image": "first"}}}'
            if calls["json"] == 1
            else '{"services": {"app": {"image": "second"}}}'
        )

    monkeypatch.setattr(promote, "run", drift_run)
    with pytest.raises(promote.ReleaseError, match="differs from the reviewed chain"):
        promote.collapse_candidate_chain(settings, [str(base)], candidate, bindings)
    assert not (stack / ".compose-collapsed.draft.yaml").exists()


def test_repo_release_versions_stay_synchronized():
    versions = github_release_notes.read_repo_versions(REPO_ROOT)
    unique = set(versions.values())
    assert len(unique) == 1, f"Version mismatch across repo files: {versions}"
    current_version = next(iter(unique))
    assert github_release_notes.parse_release_tag(f"v{current_version}") == current_version

    changelog_text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = github_release_notes.extract_changelog_section(changelog_text, current_version)
    assert "### Added" in section or "### Changed" in section or "### Fixed" in section


def test_github_release_notes_extracts_current_release():
    versions = github_release_notes.read_repo_versions(REPO_ROOT)
    current_version = versions["pyproject.toml"]
    tag = f"v{current_version}"
    notes = github_release_notes.build_release_notes(tag, REPO_ROOT)

    assert f"ghcr.io/jhinxdev/omakase-plus:{current_version}" in notes
    assert f"https://github.com/JhinxDev/omakase-plus/blob/{tag}/CHANGELOG.md" in notes
    assert "https://omakase-plus.jhinx.dev/docs/quickstart" in notes

    in_code_block = False
    for line in notes.splitlines():
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if not in_code_block:
            assert "\u2014" not in line, f"Em dash found in release notes prose: {line!r}"
            assert " -- " not in line, f"Double hyphen found in release notes prose: {line!r}"


def test_github_release_notes_rejects_mismatched_or_invalid_tags():
    with pytest.raises(ValueError, match="vMAJOR.MINOR.PATCH"):
        github_release_notes.parse_release_tag("0.5.0")
    with pytest.raises(ValueError, match="vMAJOR.MINOR.PATCH"):
        github_release_notes.parse_release_tag("v0.5")
    with pytest.raises(ValueError, match="does not match repository versions"):
        github_release_notes.verify_tag_matches_repo("v9.9.9", REPO_ROOT)


def test_release_workflow_uses_least_privilege_and_tag_trigger():
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert '"v*"' in workflow or "'v*'" in workflow or "- v*" in workflow
    assert "contents: write" in workflow
    assert "packages: write" not in workflow
    assert "ops/release/github_release_notes.py" in workflow
