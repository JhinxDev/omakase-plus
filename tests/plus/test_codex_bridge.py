"""The workstation supervisor must be silent, finite and source-bound."""

import contextlib
import ctypes
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from omakase.plus import codex_bridge, codex_worker


def test_worker_auth_and_ssh_never_allocate_a_console(monkeypatch):
    launches = []

    def run(command, **options):
        launches.append(options)
        if command[-2:] == ["login", "status"]:
            options["stdout"].write(b"Logged in using ChatGPT\n")
        elif command[0] == "taskkill":
            pass
        else:
            options["stdout"].write(b'{"ok":true,"online":true}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex_worker.subprocess, "run", run)
    codex_worker._require_subscription(["codex"])
    assert codex_worker.remote_call("fixture", {"op": "status"})["online"]
    if os.name == "nt":
        codex_worker._stop(SimpleNamespace(poll=lambda: None, pid=42, wait=lambda **_: 0))
    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert all(options.get("creationflags") == expected for options in launches)


def install_runtime(monkeypatch, tmp_path, *, returncode=0, duplicate=False):
    launches = []
    guards = []
    waits = []

    @contextlib.contextmanager
    def singleton():
        yield not duplicate

    def guard(repository, revision):
        guards.append((repository, revision))

    def run(command, **options):
        launches.append((command, options))
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(codex_bridge, "singleton", singleton)
    monkeypatch.setattr(codex_bridge, "verify_source", guard)
    monkeypatch.setattr(codex_bridge, "protect_child_lifetime", lambda: None)
    monkeypatch.setattr(codex_bridge.subprocess, "run", run)
    monkeypatch.setattr(codex_bridge.time, "sleep", waits.append)
    arguments = [
        "--ssh-alias",
        "fixture",
        "--expected-revision",
        "a" * 40,
        "--repository",
        str(tmp_path),
        "--cycles",
        "2",
    ]
    return arguments, launches, guards, waits


def test_hidden_supervisor_keeps_finite_children_and_suppresses_terminal_output(
    monkeypatch, tmp_path
):
    arguments, launches, guards, waits = install_runtime(monkeypatch, tmp_path)
    assert codex_bridge.main(arguments) == 0
    assert len(launches) == len(guards) == 2
    assert waits == []
    for command, options in launches:
        assert command[0] == str(tmp_path / ".venv" / "Scripts" / "python.exe")
        assert command[1:3] == ["-m", "omakase.plus.codex_worker"]
        assert command[-6:] == ["--max-jobs", "4", "--max-minutes", "30", "--job-timeout", "300"]
        assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
        assert options["shell"] is False


def test_duplicate_supervisor_never_launches_a_worker(monkeypatch, tmp_path):
    arguments, launches, guards, _ = install_runtime(monkeypatch, tmp_path, duplicate=True)
    assert codex_bridge.main(arguments) == 0
    assert launches == guards == []


def test_failed_children_have_bounded_restart_attempts(monkeypatch, tmp_path):
    arguments, launches, _, waits = install_runtime(monkeypatch, tmp_path, returncode=1)
    arguments[-1] = "0"
    assert codex_bridge.main(arguments) == 1
    assert len(launches) == 3
    assert waits == [30, 60]


def test_source_rejection_never_launches_inference(monkeypatch, tmp_path):
    arguments, launches, _, _ = install_runtime(monkeypatch, tmp_path)

    def reject(*_):
        raise codex_bridge.BridgeError("changed_source")

    monkeypatch.setattr(codex_bridge, "verify_source", reject)
    assert codex_bridge.main(arguments) == 1
    assert launches == []


def test_source_guard_rejects_dirty_runtime(monkeypatch, tmp_path):
    commands = []

    def git(repository, *arguments):
        commands.append(arguments)
        return (
            ("a" * 40)
            if arguments == ("rev-parse", "HEAD")
            else " M src/omakase/plus/codex_worker.py"
        )

    monkeypatch.setattr(codex_bridge, "git", git)
    try:
        codex_bridge.verify_source(tmp_path, "a" * 40)
    except codex_bridge.BridgeError:
        pass
    else:
        raise AssertionError("dirty runtime was admitted")
    assert commands[1][:2] == ("status", "--porcelain")


@pytest.mark.parametrize("status,owned", [(0, True), (0x80, True), (0x102, False)])
def test_singleton_recovers_abandonment_and_releases_only_its_own_handle(
    monkeypatch, status, owned
):
    closed = []

    def create(*_):
        return 17

    def wait(*_):
        return status

    def release(handle):
        closed.append(("release", handle))

    def close(handle):
        closed.append(("close", handle))

    kernel = SimpleNamespace(
        CreateMutexW=create, WaitForSingleObject=wait, ReleaseMutex=release, CloseHandle=close
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_, **__: kernel, raising=False)
    with codex_bridge.singleton() as acquired:
        assert acquired is owned
    assert closed == ([("release", 17)] if owned else []) + [("close", 17)]


def test_changed_runtime_revision_fails_before_start(monkeypatch, tmp_path):
    def git(repository, *arguments):
        if arguments == ("rev-parse", "HEAD"):
            return "b" * 40
        raise codex_bridge.BridgeError("source_unavailable")

    monkeypatch.setattr(codex_bridge, "git", git)
    with pytest.raises(codex_bridge.BridgeError):
        codex_bridge.verify_source(tmp_path, "a" * 40)


def test_ui_work_does_not_take_subscription_worker_offline_but_worker_changes_do(
    tmp_path, monkeypatch
):
    """Use a real private fixture checkout; CSS editing must not affect the worker."""
    # Git hooks export repository discovery variables. A fixture must never
    # inherit the calling repository's index, ref or worktree.
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    ):
        monkeypatch.delenv(name, raising=False)
    runtime = tmp_path / "src/omakase/plus/codex_worker.py"
    stylesheet = tmp_path / "src/omakase/plus/static/dashboard.css"
    stylesheet.parent.mkdir(parents=True)
    runtime.write_text("# reviewed worker\n", encoding="utf-8")
    stylesheet.write_text("body { color: white; }\n", encoding="utf-8")

    def run_git(*arguments):
        result = subprocess.run(
            ["git", "-C", str(tmp_path), *arguments],
            capture_output=True,
            text=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout.strip()

    run_git("init", "-q")
    run_git("add", "--", "src/omakase")
    run_git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "reviewed fixture",
    )
    revision = run_git("rev-parse", "HEAD")
    stylesheet.write_text("body { color: blue; }\n", encoding="utf-8")
    codex_bridge.verify_source(tmp_path, revision)
    runtime.write_text("# unreviewed worker change\n", encoding="utf-8")
    with pytest.raises(codex_bridge.BridgeError, match="changed_source"):
        codex_bridge.verify_source(tmp_path, revision)


@pytest.mark.skipif(os.name != "nt", reason="native Windows child lifetime")
def test_worker_child_dies_when_its_supervisor_is_interrupted(tmp_path):
    """Run only a sleeping fixture child; never touch a real task or Codex job."""
    from ctypes import wintypes
    from pathlib import Path

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    source = """
import contextlib,json,os,subprocess,sys
from types import SimpleNamespace
from omakase.plus import codex_bridge
codex_bridge.singleton=lambda:contextlib.nullcontext(True)
codex_bridge.verify_source=lambda *args:None
def fixture(command,**options):
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    print(json.dumps({'supervisor':os.getpid(),'child':child.pid}),flush=True)
    child.wait()
    return SimpleNamespace(returncode=0)
codex_bridge.subprocess.run=fixture
raise SystemExit(codex_bridge.main(['--ssh-alias','fixture','--expected-revision',
    'a'*40,'--repository',sys.argv[1],'--cycles','1']))
"""
    environment = codex_worker._cli_env()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    child_handle = parent_handle = None
    process = subprocess.Popen(
        [sys._base_executable, "-c", source, str(tmp_path)],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
        text=True,
    )
    try:
        line = process.stdout.readline()
        assert line, process.stderr.read()
        owned = json.loads(line)
        assert owned["supervisor"] == process.pid
        parent_handle = kernel.OpenProcess(0x100001, False, owned["supervisor"])
        child_handle = kernel.OpenProcess(0x101001, False, owned["child"])
        assert parent_handle and child_handle
        assert kernel.TerminateProcess(parent_handle, 1)
        process.wait(timeout=10)
        kernel.WaitForSingleObject(child_handle, 2_000)
        exit_code = wintypes.DWORD()
        assert kernel.GetExitCodeProcess(child_handle, ctypes.byref(exit_code))
        assert exit_code.value != 259, "interrupted supervisor left its fixture child running"
    finally:
        if child_handle:
            kernel.TerminateProcess(child_handle, 1)
            kernel.CloseHandle(child_handle)
        if parent_handle:
            kernel.TerminateProcess(parent_handle, 1)
            kernel.CloseHandle(parent_handle)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
