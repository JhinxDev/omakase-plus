"""Silent Windows supervisor for finite, owner-requested Codex discovery workers.

Launch through the GUI bootstrap using the base interpreter's real pythonw.exe.
Locked-environment console children use CREATE_NO_WINDOW.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import re
import subprocess
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_job_handle = None
RUNTIME_PATHS = (
    # Guard the workstation runtime, rather than unrelated app/UI development.
    "src/omakase/__init__.py",
    "src/omakase/plus/__init__.py",
    "src/omakase/plus/codex_bridge.py",
    "src/omakase/plus/codex_jobs.py",
    "src/omakase/plus/codex_worker.py",
    "pyproject.toml",
    "uv.lock",
    "scripts/run_codex_discovery_bridge.ps1",
    "scripts/configure_codex_discovery_task.ps1",
    "scripts/run_codex_discovery_hidden.py",
)


class BridgeError(RuntimeError):
    pass


def protect_child_lifetime() -> None:
    """Windows closes this job handle on exit, terminating every worker descendant.

    Keep the handle for the supervisor's entire process lifetime. Closing it in a
    Python finally block would also terminate this supervisor before its normal
    exit code is delivered to Task Scheduler.
    """
    global _job_handle
    if _job_handle is not None:
        return
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("ProcessUserTime", ctypes.c_int64),
            ("JobUserTime", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSet", ctypes.c_size_t),
            ("MaximumWorkingSet", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperations",
                "WriteOperations",
                "OtherOperations",
                "ReadBytes",
                "WriteBytes",
                "OtherBytes",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("Basic", BasicLimits),
            ("Io", IoCounters),
            ("ProcessMemory", ctypes.c_size_t),
            ("JobMemory", ctypes.c_size_t),
            ("PeakProcessMemory", ctypes.c_size_t),
            ("PeakJobMemory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise BridgeError("child_lifetime_unavailable")
    limits = ExtendedLimits()
    limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel.CloseHandle(handle)
        raise BridgeError("child_lifetime_unavailable")
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        kernel.CloseHandle(handle)
        raise BridgeError("child_lifetime_unavailable")
    _job_handle = handle


@contextlib.contextmanager
def singleton():
    """Use the same mutex as the old launcher; abandoned ownership is recoverable."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateMutexW(None, False, "Local\\OmakasePlusCodexDiscovery")
    if not handle:
        raise BridgeError("mutex_unavailable")
    owned = False
    try:
        status = kernel.WaitForSingleObject(handle, 0)
        owned = status in (0, 0x80)  # WAIT_OBJECT_0 and WAIT_ABANDONED both grant ownership.
        if not owned and status != 0x102:  # WAIT_TIMEOUT means another supervisor owns it.
            raise BridgeError("mutex_unavailable")
        yield owned
    finally:
        if owned:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
        shell=False,
        timeout=15,
        check=False,
    )
    if result.returncode or len(result.stdout) > 64_000:
        raise BridgeError("source_unavailable")
    return result.stdout.decode("utf-8", errors="strict").strip()


def verify_source(repository: Path, revision: str) -> None:
    head = git(repository, "rev-parse", "HEAD")
    if not re.fullmatch(r"[a-f0-9]{40}", head):
        raise BridgeError("source_unavailable")
    if head != revision:
        git(repository, "diff", "--quiet", revision, "HEAD", "--", *RUNTIME_PATHS)
    if git(repository, "status", "--porcelain", "--", *RUNTIME_PATHS):
        raise BridgeError("changed_source")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-alias", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--cycles", type=int, default=0)
    args = parser.parse_args(argv)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", args.ssh_alias)
        or not re.fullmatch(r"[a-f0-9]{40}", args.expected_revision)
        or not 0 <= args.cycles <= 48
    ):
        parser.error("Invalid private discovery supervisor arguments.")
    try:
        repository = args.repository.resolve(strict=True)
        with singleton() as acquired:
            if not acquired:
                return 0
            protect_child_lifetime()
            cycles = failures = 0
            while args.cycles == 0 or cycles < args.cycles:
                verify_source(repository, args.expected_revision)
                result = subprocess.run(
                    [
                        str(repository / ".venv" / "Scripts" / "python.exe"),
                        "-m",
                        "omakase.plus.codex_worker",
                        "--ssh-alias",
                        args.ssh_alias,
                        "--max-jobs",
                        "4",
                        "--max-minutes",
                        "30",
                        "--job-timeout",
                        "300",
                    ],
                    cwd=repository,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW,
                    shell=False,
                    timeout=35 * 60,
                    check=False,
                )
                cycles += 1
                if result.returncode == 0:
                    failures = 0
                else:
                    failures += 1
                    if failures >= 3:
                        return 1
                    time.sleep(min(60, 30 * failures))
        return 0
    except (BridgeError, OSError, subprocess.TimeoutExpired, UnicodeError):
        # pythonw has no standard console. Never forward prompts, errors or auth output.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
