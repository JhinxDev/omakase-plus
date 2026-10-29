"""Legacy mutators fail before touching current data or invoking Docker/Git."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("helper", ["omakase-local-upgrade.ps1", "omakase-local-rollback.ps1"])
@pytest.mark.parametrize("existing_stack", [True, False])
def test_legacy_helper_is_disabled_before_any_operation(tmp_path, helper, existing_stack):
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell 7 is required for the Windows helper execution boundary")
    stack = tmp_path / "sample-stack"
    data = stack / "data"
    receipts = stack / "receipts"
    data.mkdir(parents=True)
    receipts.mkdir()
    with sqlite3.connect(data / "omakase-plus.db") as db:
        db.execute("CREATE TABLE preserved_history(id INTEGER PRIMARY KEY, title TEXT)")
        db.execute("INSERT INTO preserved_history VALUES(1,'Fictional account history')")
    for name in ("omakase-plus.db-wal", "omakase-plus.db-shm"):
        (data / name).write_bytes(b"current-sidecar-preservation-sentinel")
    (stack / "compose.local.yaml").write_text("services: {}", encoding="utf-8")
    (stack / ".env.local").write_text(f"OMAKASE_DATA_PATH={data}", encoding="utf-8")
    (stack / ".env.local.secrets").write_text("SYNTHETIC_SENTINEL=not-a-key", encoding="utf-8")
    backup = data / "old-backup.db"
    backup.write_bytes(b"older-backup-must-not-replace-current-data")
    receipt = receipts / "legacy.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "omakase-local-upgrade-receipt-v1",
                "stack_root": str(stack),
                "data_path": str(data),
                "previous_image": "sha256:synthetic-previous-image",
                "previous_revision": "a" * 40,
                "database_backup": str(backup),
                "database_backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "profiles": [],
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "external-command-attempted"
    wrapper = tmp_path / "invoke-legacy.ps1"
    wrapper.write_text(
        """param([string]$LegacyScript,[string]$FakeStack,[string]$Marker,[string]$FakeReceipt)
$ErrorActionPreference='Stop'
$env:OMAKASE_REVISION='preserved-synthetic-revision'
function global:docker { Set-Content -LiteralPath $Marker -Value 'docker'; throw 'Fake Docker must not run' }
function global:git {
    $global:LASTEXITCODE=0
    if($args -contains 'rev-parse'){ return ('a'*40) }
}
try {
    if($LegacyScript.EndsWith('rollback.ps1')) { & $LegacyScript -StackRoot $FakeStack -ReceiptPath $FakeReceipt }
    else { & $LegacyScript -StackRoot $FakeStack -Profiles plex -AllowLocalDev }
} catch {
    if($env:OMAKASE_REVISION -ne 'preserved-synthetic-revision') { [Console]::Error.WriteLine('Environment changed'); exit 4 }
    [Console]::Error.WriteLine($_.Exception.Message); exit 1
}
""",
        encoding="utf-8",
    )

    def snapshot():
        return {str(p.relative_to(stack)): p.read_bytes() for p in stack.rglob("*") if p.is_file()}

    before = snapshot()
    script = Path(__file__).resolve().parents[1] / "scripts" / helper
    supplied_stack = stack if existing_stack else tmp_path / "never-created-stack"
    supplied_receipt = receipt if existing_stack else supplied_stack / "receipts" / "missing.json"
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(wrapper),
            str(script),
            str(supplied_stack),
            str(marker),
            str(supplied_receipt),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert "disabled" in result.stderr.lower()
    assert "current data" in result.stderr.lower()
    assert not marker.exists(), "No Docker command may run before the legacy helper stops"
    assert snapshot() == before, (
        "Current main DB, WAL/SHM, backup, secret and receipts stay byte-identical"
    )
    if not existing_stack:
        assert not supplied_stack.exists()
