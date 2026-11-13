"""Opt-in, read-only acceptance of the real workstation installation."""

import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("OMAKASE_TEST_LIVE_CODEX_TASK") != "1",
    reason="requires explicit read-only workstation task acceptance",
)
def test_installed_bridge_is_console_free_and_recovers_without_another_logon():
    shell = shutil.which("pwsh")
    assert shell
    script = r"""
    $task = Get-ScheduledTask -TaskName 'OmakasePlusCodexDiscovery' -ErrorAction Stop
    [pscustomobject]@{
        executable = $task.Actions[0].Execute
        uses_bridge = $task.Actions[0].Arguments.Contains('scripts\run_codex_discovery_hidden.py" --ssh-alias ')
        restart_count = $task.Settings.RestartCount
        restart_interval = $task.Settings.RestartInterval
        single_instance = [string]$task.Settings.MultipleInstances
        logon_triggers = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' }).Count
        recovery_intervals = @($task.Triggers | Where-Object { $_.Id -eq 'OmakaseCodexRecovery' } | ForEach-Object { $_.Repetition.Interval })
    } | ConvertTo-Json -Compress
    """
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=True,
    )
    task = json.loads(result.stdout)
    executable = Path(task["executable"])
    assert executable.name.lower() == "pythonw.exe"
    image = executable.read_bytes()
    pe_offset = struct.unpack_from("<I", image, 0x3C)[0]
    subsystem = struct.unpack_from("<H", image, pe_offset + 24 + 68)[0]
    assert subsystem == 2, "task entry must use a real GUI executable, not a console redirector"
    assert task["uses_bridge"]
    assert task["restart_count"] == 3
    assert task["restart_interval"] == "PT1M"
    assert task["single_instance"] == "IgnoreNew"
    assert task["logon_triggers"] == 1
    assert task["recovery_intervals"] == ["PT1M"]
