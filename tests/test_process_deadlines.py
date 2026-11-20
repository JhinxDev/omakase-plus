"""Negative cases for the suite-wide process deadline guard.

The repository rule is that every external process carries a finite deadline, a
diagnostic on expiry and cleanup of the children it leaves behind. The guard in
``tests/conftest.py`` fills a deadline in for a call site that forgot one. A
guard nobody has seen reject anything is not a gate, so these tests make it
reject a child that overruns, prove that an explicit ``timeout=`` still wins, prove
that a child waited on without a deadline is bounded the same way, and prove that a
child left running is killed before the next test starts.

The deadline in the guard is lowered here instead of waiting out the real one, so
the negative case costs about a second rather than three minutes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

CONFTEST_PATH = Path(__file__).with_name("conftest.py")

# Long enough that any leak into a real deadline would show up as a stalled run.
SLEEPING_CHILD = [sys.executable, "-c", "import time; time.sleep(30)"]

# Kept so the test after it can see what the guard did with the child.
left_running: list = []


def _guard():
    """Return the conftest module pytest actually loaded.

    Loading the file a second time would build a separate module object whose
    constants the running fixture never reads, so the deadline would silently
    stay at its real value and the negative case would pass nothing.
    """
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and Path(path).resolve() == CONFTEST_PATH.resolve():
            return module
    raise AssertionError("the suite conftest is not loaded, so the guard cannot be exercised")


def test_a_run_without_a_deadline_is_given_one_and_reports_the_overrun(monkeypatch):
    guard = _guard()
    monkeypatch.setattr(guard, "DEFAULT_PROCESS_TIMEOUT_SECONDS", 1)

    with pytest.raises(AssertionError) as failure:
        subprocess.run(SLEEPING_CHILD, capture_output=True, text=True)

    message = str(failure.value)
    assert "exceeded its 1s deadline" in message, message
    assert "time.sleep(30)" in message, message


def test_an_explicit_deadline_is_honoured_and_not_replaced(monkeypatch):
    guard = _guard()
    monkeypatch.setattr(guard, "DEFAULT_PROCESS_TIMEOUT_SECONDS", 180)

    with pytest.raises(AssertionError) as failure:
        subprocess.run(SLEEPING_CHILD, capture_output=True, text=True, timeout=1)

    assert "exceeded its 1s deadline" in str(failure.value), str(failure.value)


def test_a_command_inside_its_deadline_still_returns_normally():
    completed = subprocess.run(
        [sys.executable, "-c", "print('ok')"], capture_output=True, text=True
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_a_child_spawned_without_a_deadline_is_recorded_for_cleanup():
    left_running.append(subprocess.Popen(SLEEPING_CHILD))

    assert left_running[-1].poll() is None, "the child should still be running here"


def test_a_child_waited_on_without_a_deadline_is_bounded_and_explained(monkeypatch):
    guard = _guard()
    monkeypatch.setattr(guard, "DEFAULT_PROCESS_TIMEOUT_SECONDS", 1)
    left_running.append(subprocess.Popen(SLEEPING_CHILD))

    with pytest.raises(subprocess.TimeoutExpired) as failure:
        left_running[-1].wait()

    message = str(failure.value)
    assert "Popen.wait exceeded its 1s deadline" in message, message
    assert "time.sleep(30)" in message, message


def test_the_previous_test_left_no_running_child_behind():
    assert left_running, "the spawning test did not run"
    child = left_running[-1]

    assert child.poll() is not None, (
        "a child started by a test was still running after that test ended"
    )
