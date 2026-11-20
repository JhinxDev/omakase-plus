"""Suite wide safety net for external processes.

The repository rule is that every external process needs a finite deadline,
captured output and a diagnostic on expiry. A test that waits forever reports
nothing and hides the defect it was written to catch, so this fixture fills in a
deadline for any call site that forgot one. A test that knows its own bound still
passes ``timeout=`` and keeps it: only a missing deadline is supplied here, the
value is never overridden. Children still alive when a test ends are killed, so a
stuck helper cannot outlive the run that started it.
"""

from __future__ import annotations

import subprocess

import pytest

DEFAULT_PROCESS_TIMEOUT_SECONDS = 180
CHILD_REAP_TIMEOUT_SECONDS = 10


def _tail(stream, limit: int = 800) -> str:
    if stream is None:
        return "<none>"
    text = stream.decode("utf-8", "ignore") if isinstance(stream, bytes) else str(stream)
    return text[-limit:] or "<empty>"


def _diagnostic(command, timeout: float, expired: subprocess.TimeoutExpired) -> str:
    return (
        f"external process exceeded its {timeout:g}s deadline: {command!r}\n"
        f"captured stdout: {_tail(expired.stdout)}\n"
        f"captured stderr: {_tail(expired.stderr)}"
    )


class _BoundedWaitTimeout(subprocess.TimeoutExpired):
    """A wait that overran, described the way the ``run`` guard describes one.

    It stays a ``TimeoutExpired`` so the standard library keeps working: ``run``
    kills the child it started and re-raises, and the guard above then turns the
    whole thing into one explained failure instead of a stalled run.
    """

    def __init__(self, expired, command, timeout: float, step: str) -> None:
        super().__init__(command, timeout, output=expired.output, stderr=expired.stderr)
        self.step = step

    def __str__(self) -> str:
        return _diagnostic(self.cmd, self.timeout, self).replace(
            "external process exceeded", f"external process Popen.{self.step} exceeded", 1
        )


@pytest.fixture(autouse=True)
def bounded_external_processes(monkeypatch):
    """Give every external process a deadline and reap the children it leaves."""
    real_run = subprocess.run
    real_check_output = subprocess.check_output
    real_check_call = subprocess.check_call
    real_popen = subprocess.Popen
    children: list = []

    def with_deadline(kwargs: dict) -> dict:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = DEFAULT_PROCESS_TIMEOUT_SECONDS
        return kwargs

    def guarded(call):
        def wrapper(*args, **kwargs):
            kwargs = with_deadline(kwargs)
            try:
                return call(*args, **kwargs)
            except subprocess.TimeoutExpired as expired:
                command = args[0] if args else kwargs.get("args")
                raise AssertionError(_diagnostic(command, kwargs["timeout"], expired)) from expired

        return wrapper

    class BoundedPopen(real_popen):
        """A child process whose blocking waits carry the same deadline as ``run``.

        ``Popen`` takes no deadline when it is constructed: the deadline belongs to
        the wait. Both blocking waits therefore supply one for a caller that forgot,
        and report it in the same words the ``run`` guard uses. Every child is also
        recorded, so the teardown below can kill one the test abandoned.
        """

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            children.append(self)

        def _deadline(self, timeout):
            return DEFAULT_PROCESS_TIMEOUT_SECONDS if timeout is None else timeout

        def wait(self, timeout=None):
            deadline = self._deadline(timeout)
            try:
                return super().wait(deadline)
            except subprocess.TimeoutExpired as expired:
                raise _BoundedWaitTimeout(expired, self.args, deadline, "wait") from expired

        def communicate(self, input=None, timeout=None):
            deadline = self._deadline(timeout)
            try:
                return super().communicate(input=input, timeout=deadline)
            except subprocess.TimeoutExpired as expired:
                raise _BoundedWaitTimeout(
                    expired, self.args, deadline, "communicate"
                ) from expired

    monkeypatch.setattr(subprocess, "run", guarded(real_run))
    monkeypatch.setattr(subprocess, "check_output", guarded(real_check_output))
    monkeypatch.setattr(subprocess, "check_call", guarded(real_check_call))
    monkeypatch.setattr(subprocess, "Popen", BoundedPopen)
    try:
        yield
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                try:
                    child.wait(timeout=CHILD_REAP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
