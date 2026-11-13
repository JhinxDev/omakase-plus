"""Privacy-preserving support bundle tests."""

from __future__ import annotations

import zipfile
from pathlib import Path

from omakase.plus.doctor import DoctorCheck, DoctorReport
from omakase.plus.setup import new_setup_state
from omakase.plus.support import create_support_bundle, inspect_support_bundle


def test_support_bundle_redacts_secrets_names_paths_trackers_and_magnets(tmp_path: Path):
    report = DoctorReport(
        schema_version="omakase-plus-doctor-v1",
        checked_at="2026-07-26T12:00:00Z",
        ready=True,
        checks=(
            DoctorCheck(
                name="fixture",
                status="pass",
                reason_code="READY",
                detail="Fixture is ready",
            ),
        ),
    )
    output = tmp_path / "support.zip"
    receipt = create_support_bundle(
        output,
        report=report,
        setup_state=new_setup_state(
            deployment_mode="local",
            now="2026-07-26T12:00:00Z",
        ),
        app_version="0.4.0",
        created_at="2026-07-26T12:01:00Z",
        log_lines=(
            "PLEX_TOKEN=top-secret",
            "magnet:?xt=urn:btih:AAAA&tr=https://tracker.invalid/announce",
            r"opened C:\Anime\Hell's Paradise S02E01.mkv",
            "opened /srv/anime/Hells.Paradise.S02E01.mkv",
        ),
    )

    assert receipt["sha256"]
    assert inspect_support_bundle(output)["doctor_ready"] is True
    with zipfile.ZipFile(output) as archive:
        text = archive.read("logs/redacted.log").decode()
    assert "top-secret" not in text
    assert "magnet:?" not in text
    assert "tracker.invalid" not in text
    assert "Paradise" not in text
    assert "<media:" in text or "<path:" in text
