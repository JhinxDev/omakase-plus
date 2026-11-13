"""No-mutation Doctor and VPN topology tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omakase.plus.doctor import DoctorConfig, _capacity_check, _database_check, run_doctor


@pytest.fixture(autouse=True)
def nominal_library_capacity(monkeypatch):
    # Readiness fixtures must not depend on the CI runner's remaining disk.
    monkeypatch.setattr(
        "omakase.plus.doctor.shutil.disk_usage", lambda _path: SimpleNamespace(free=100 * 1024**3)
    )


@pytest.mark.parametrize("free_gib,expected", [(19, "fail"), (20, "pass")])
def test_capacity_enforces_the_configured_threshold(tmp_path, monkeypatch, free_gib, expected):
    monkeypatch.setattr(
        "omakase.plus.doctor.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=free_gib * 1024**3),
    )
    check = _capacity_check(tmp_path, minimum_free_gib=20)
    assert check.status == expected
    assert check.reason_code == ("CAPACITY_LOW" if expected == "fail" else "CAPACITY_READY")


def test_capacity_measurement_failure_does_not_report_ready(tmp_path, monkeypatch):
    def unavailable(_path):
        raise OSError("fixture unavailable")

    monkeypatch.setattr("omakase.plus.doctor.shutil.disk_usage", unavailable)
    check = _capacity_check(tmp_path, minimum_free_gib=20)
    assert check.status == "fail"
    assert check.reason_code == "CAPACITY_UNKNOWN"


def _qbit_transport(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v2/auth/login":
        return httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=test; path=/"})
    if path == "/api/v2/app/version":
        return httpx.Response(200, text="v5.2.3")
    if path == "/api/v2/app/webapiVersion":
        return httpx.Response(200, text="2.11.4")
    if path == "/api/v2/torrents/categories":
        return httpx.Response(
            200,
            json={"omakase": {"name": "omakase", "savePath": "/downloads/omakase"}},
        )
    return httpx.Response(404)


def _plex_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/identity":
        return httpx.Response(
            200,
            content=b'<MediaContainer machineIdentifier="fixture" version="1.43.2"/>',
        )
    if request.url.path == "/library/sections":
        return httpx.Response(200, content=b"<MediaContainer/>")
    return httpx.Response(404)


def _config(tmp_path: Path, **overrides) -> DoctorConfig:
    data = tmp_path / "data"
    imported = tmp_path / "import"
    library = tmp_path / "library"
    for path in (data, imported, library):
        path.mkdir(exist_ok=True)
    values = {
        "data_dir": data,
        "import_root": imported,
        "library_root": library,
        "checked_at": "2026-07-26T12:00:00Z",
        "plex_url": "http://plex.local:32400",
        "plex_token": "private-plex-token",
    }
    values.update(overrides)
    return DoctorConfig(**values)


def test_import_only_doctor_does_not_require_vpn(tmp_path, monkeypatch):
    monkeypatch.setattr("omakase.plus.doctor.shutil.which", lambda _name: "ffprobe")

    def runner(command, _timeout):
        assert command == ["ffprobe", "-version"]
        return 0, "ffprobe version 7.1 fixture\n"

    report = run_doctor(
        _config(tmp_path),
        command_runner=runner,
        plex_transport=httpx.MockTransport(_plex_transport),
    )

    assert report.ready is True
    assert next(item for item in report.checks if item.name == "torrent-vpn").status == "skip"
    assert not (tmp_path / "data" / "omakase-plus.db").exists()


def test_database_doctor_fails_on_orphaned_relational_records(tmp_path):
    path = tmp_path / "omakase-plus.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE _migrations (name TEXT PRIMARY KEY);
            INSERT INTO _migrations (name) VALUES ('001.sql');
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id)
            );
            INSERT INTO sessions (id, user_id) VALUES ('orphan', 404);
            """
        )

    result = _database_check(path)

    assert result.status == "fail"
    assert result.reason_code == "DATABASE_FOREIGN_KEY_VIOLATION"
    assert "orphaned" in result.detail


def test_qbittorrent_vpn_requires_healthy_shared_gluetun_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr("omakase.plus.doctor.shutil.which", lambda _name: "ffprobe")
    gluetun = "|".join(
        (
            "abc123",
            "running",
            "healthy",
            json.dumps(["NET_ADMIN"]),
            json.dumps([{"PathOnHost": "/dev/net/tun"}]),
            "",
        )
    )

    def runner(command, _timeout):
        if command[0] == "ffprobe":
            return 0, "ffprobe version 7.1 fixture\n"
        if command[-1] == "omakase-local-gluetun":
            return 0, gluetun
        if command[-1] == "omakase-local-qbittorrent":
            return 0, "container:abc123|null"
        raise AssertionError(command)

    config = _config(
        tmp_path,
        acquisition_mode="qbittorrent",
        vpn_mode="gluetun",
        qbittorrent_url="http://qbit.local:8080",
        qbittorrent_username="omakase",
        qbittorrent_password="private",
    )
    report = run_doctor(
        config,
        command_runner=runner,
        plex_transport=httpx.MockTransport(_plex_transport),
        qbittorrent_transport=httpx.MockTransport(_qbit_transport),
    )

    vpn = next(item for item in report.checks if item.name == "torrent-vpn")
    assert report.ready is True
    assert vpn.reason_code == "GLUETUN_FAIL_CLOSED"
    assert "private" not in repr(report)


def test_qbittorrent_direct_port_or_namespace_bypass_fails_doctor(tmp_path, monkeypatch):
    monkeypatch.setattr("omakase.plus.doctor.shutil.which", lambda _name: "ffprobe")
    gluetun = 'abc123|running|healthy|["NET_ADMIN"]|[{"PathOnHost":"/dev/net/tun"}]|'

    def runner(command, _timeout):
        if command[0] == "ffprobe":
            return 0, "ffprobe version 7.1 fixture\n"
        if command[-1] == "omakase-local-gluetun":
            return 0, gluetun
        return 0, 'bridge|{"8080/tcp":[{"HostPort":"8080"}]}'

    report = run_doctor(
        _config(
            tmp_path,
            acquisition_mode="qbittorrent",
            vpn_mode="gluetun",
            qbittorrent_url="http://qbit.local:8080",
            qbittorrent_username="omakase",
            qbittorrent_password="private",
        ),
        command_runner=runner,
        plex_transport=httpx.MockTransport(_plex_transport),
        qbittorrent_transport=httpx.MockTransport(_qbit_transport),
    )

    assert report.ready is False
    assert (
        next(item for item in report.checks if item.name == "torrent-vpn").reason_code
        == "QBITTORRENT_BYPASS_POSSIBLE"
    )
