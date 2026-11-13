"""Read-only readiness diagnostics for Omakase Plus deployments."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from omakase.plus.plex import PlexClient, PlexError
from omakase.plus.providers.qbittorrent import QBittorrentProvider

DOCTOR_REPORT_SCHEMA = "omakase-plus-doctor-v1"
_ACQUISITION_MODES = frozenset({"import-only", "real-debrid", "qbittorrent"})
_VPN_MODES = frozenset({"none", "gluetun", "external"})
_STATUSES = frozenset({"pass", "warn", "fail", "skip"})


@dataclass(frozen=True, slots=True)
class DoctorConfig:
    data_dir: Path
    import_root: Path
    library_root: Path
    checked_at: str
    acquisition_mode: str = "import-only"
    minimum_free_gib: int = 20
    plex_url: str = ""
    plex_token: str = field(default="", repr=False)
    qbittorrent_url: str = ""
    qbittorrent_username: str = field(default="", repr=False)
    qbittorrent_password: str = field(default="", repr=False)
    qbittorrent_category: str = "omakase"
    qbittorrent_save_root: str = "/downloads/omakase"
    vpn_mode: str = "none"
    gluetun_container: str = "omakase-local-gluetun"
    qbittorrent_container: str = "omakase-local-qbittorrent"
    external_vpn_interface: str = ""


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: str
    reason_code: str
    detail: str
    remediation: str = ""


@dataclass(frozen=True, slots=True)
class DoctorReport:
    schema_version: str
    checked_at: str
    ready: bool
    checks: tuple[DoctorCheck, ...]


class DoctorError(ValueError):
    """Doctor configuration is invalid."""


CommandRunner = Callable[[list[str], float], tuple[int, str]]


def run_doctor(
    config: DoctorConfig,
    *,
    command_runner: CommandRunner | None = None,
    plex_transport: httpx.BaseTransport | None = None,
    qbittorrent_transport: httpx.AsyncBaseTransport | None = None,
    external_transport: httpx.BaseTransport | None = None,
) -> DoctorReport:
    """Run bounded, non-mutating readiness checks."""
    _validate_config(config)
    runner = command_runner or _run_command
    checks = [
        _directory_check("data", config.data_dir, writable=True),
        _directory_check("import", config.import_root, writable=False),
        _directory_check("library", config.library_root, writable=True),
        _capacity_check(config.library_root, minimum_free_gib=config.minimum_free_gib),
        _hardlink_check(config.import_root, config.library_root),
        _ffprobe_check(runner),
        _database_check(config.data_dir / "omakase-plus.db"),
        _plex_check(config, transport=plex_transport),
    ]
    if config.acquisition_mode == "qbittorrent":
        checks.append(
            _qbittorrent_check(
                config,
                transport=qbittorrent_transport,
            )
        )
        checks.append(
            _vpn_check(
                config,
                runner=runner,
                external_transport=external_transport,
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="torrent-vpn",
                status="skip",
                reason_code="NOT_REQUIRED",
                detail="No peer-to-peer acquisition is enabled",
            )
        )
    ready = all(item.status not in {"fail"} for item in checks)
    return DoctorReport(
        schema_version=DOCTOR_REPORT_SCHEMA,
        checked_at=config.checked_at,
        ready=ready,
        checks=tuple(checks),
    )


def report_payload(report: DoctorReport) -> dict:
    """Return a stable serializable report."""
    return {
        "schema_version": report.schema_version,
        "checked_at": report.checked_at,
        "ready": report.ready,
        "checks": [
            {
                "name": item.name,
                "status": item.status,
                "reason_code": item.reason_code,
                "detail": item.detail,
                "remediation": item.remediation,
            }
            for item in report.checks
        ],
    }


def _directory_check(name: str, path: Path, *, writable: bool) -> DoctorCheck:
    if not path.exists() or not path.is_dir():
        return DoctorCheck(
            name=f"{name}-root",
            status="fail",
            reason_code="ROOT_MISSING",
            detail=f"The configured {name} root is unavailable",
            remediation="Create and mount the configured root, then run Doctor again.",
        )
    if not os.access(path, os.R_OK | os.X_OK):
        return DoctorCheck(
            name=f"{name}-root",
            status="fail",
            reason_code="ROOT_UNREADABLE",
            detail=f"The configured {name} root is not readable",
            remediation="Correct the host/container UID, GID, and mount permissions.",
        )
    if writable and not os.access(path, os.W_OK):
        return DoctorCheck(
            name=f"{name}-root",
            status="fail",
            reason_code="ROOT_READ_ONLY",
            detail=f"The configured {name} root is not writable",
            remediation="Grant write access only to the Omakase-managed root.",
        )
    return DoctorCheck(
        name=f"{name}-root",
        status="pass",
        reason_code="ROOT_READY",
        detail=f"The configured {name} root is accessible",
    )


def _capacity_check(path: Path, *, minimum_free_gib: int) -> DoctorCheck:
    if not path.is_dir():
        return DoctorCheck(
            name="library-capacity",
            status="fail",
            reason_code="CAPACITY_UNKNOWN",
            detail="Library capacity cannot be measured",
        )
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return DoctorCheck(
            name="library-capacity",
            status="fail",
            reason_code="CAPACITY_UNKNOWN",
            detail="Library capacity cannot be measured",
        )
    free_gib = free // (1024**3)
    if free_gib < minimum_free_gib:
        return DoctorCheck(
            name="library-capacity",
            status="fail",
            reason_code="CAPACITY_LOW",
            detail=f"Library storage has {free_gib} GiB free",
            remediation=f"Free space or lower the explicit {minimum_free_gib} GiB threshold.",
        )
    return DoctorCheck(
        name="library-capacity",
        status="pass",
        reason_code="CAPACITY_READY",
        detail=f"Library storage has {free_gib} GiB free",
    )


def _hardlink_check(import_root: Path, library_root: Path) -> DoctorCheck:
    if not import_root.is_dir() or not library_root.is_dir():
        return DoctorCheck(
            name="materialization",
            status="skip",
            reason_code="ROOTS_UNAVAILABLE",
            detail="Materialization compatibility cannot be measured",
        )
    try:
        same_device = import_root.stat().st_dev == library_root.stat().st_dev
    except OSError:
        same_device = False
    if same_device:
        return DoctorCheck(
            name="materialization",
            status="pass",
            reason_code="HARDLINK_READY",
            detail="Import and library roots can use zero-copy hardlinks",
        )
    return DoctorCheck(
        name="materialization",
        status="warn",
        reason_code="COPY_REQUIRED",
        detail="Import and library roots are on different filesystems",
        remediation="Use reflink/copy mode and budget capacity for a second media copy.",
    )


def _ffprobe_check(runner: CommandRunner) -> DoctorCheck:
    binary = shutil.which("ffprobe")
    if binary is None:
        return DoctorCheck(
            name="media-probe",
            status="fail",
            reason_code="FFPROBE_MISSING",
            detail="ffprobe is unavailable",
            remediation="Install FFmpeg/ffprobe in the runtime image or host.",
        )
    code, output = runner([binary, "-version"], 10)
    if code != 0 or not output.casefold().startswith("ffprobe version"):
        return DoctorCheck(
            name="media-probe",
            status="fail",
            reason_code="FFPROBE_FAILED",
            detail="ffprobe did not return a valid version",
        )
    version = output.splitlines()[0][:120]
    return DoctorCheck(
        name="media-probe",
        status="pass",
        reason_code="FFPROBE_READY",
        detail=version,
    )


def _database_check(path: Path) -> DoctorCheck:
    if not path.exists():
        return DoctorCheck(
            name="database",
            status="warn",
            reason_code="DATABASE_NOT_INITIALIZED",
            detail="The Omakase database has not been initialized",
            remediation="Start Omakase once, then re-run Doctor.",
        )
    uri = f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            result = connection.execute("PRAGMA quick_check").fetchone()
            migrations = connection.execute("SELECT COUNT(*) FROM _migrations").fetchone()
            foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return DoctorCheck(
            name="database",
            status="fail",
            reason_code="DATABASE_UNHEALTHY",
            detail="The Omakase database failed a read-only integrity check",
            remediation="Restore the latest validated database backup.",
        )
    if result is None or result[0] != "ok" or migrations is None or migrations[0] <= 0:
        return DoctorCheck(
            name="database",
            status="fail",
            reason_code="DATABASE_UNHEALTHY",
            detail="The Omakase database schema or integrity is incomplete",
        )
    if foreign_key_error is not None:
        return DoctorCheck(
            name="database",
            status="fail",
            reason_code="DATABASE_FOREIGN_KEY_VIOLATION",
            detail="The Omakase database contains orphaned relational records",
            remediation="Create a validated backup, repair the orphaned rows, and run Doctor again.",
        )
    return DoctorCheck(
        name="database",
        status="pass",
        reason_code="DATABASE_READY",
        detail=f"SQLite integrity is healthy with {migrations[0]} migrations",
    )


def _plex_check(
    config: DoctorConfig,
    *,
    transport: httpx.BaseTransport | None,
) -> DoctorCheck:
    if not config.plex_url and not config.plex_token:
        return DoctorCheck(
            name="plex",
            status="fail",
            reason_code="PLEX_NOT_CONFIGURED",
            detail="Plex has not been connected",
            remediation="Add a Plex URL and token, then plan the library setup.",
        )
    if not config.plex_url or not config.plex_token:
        return DoctorCheck(
            name="plex",
            status="fail",
            reason_code="PLEX_CONFIG_INCOMPLETE",
            detail="Plex connection settings are incomplete",
        )
    try:
        with PlexClient(config.plex_url, config.plex_token, transport=transport) as client:
            identity = client.identity()
            sections = client.list_sections()
    except (PlexError, ValueError):
        return DoctorCheck(
            name="plex",
            status="fail",
            reason_code="PLEX_UNREACHABLE",
            detail="Plex identity or library inventory could not be read",
            remediation="Check the local URL, token, firewall, and container path mapping.",
        )
    return DoctorCheck(
        name="plex",
        status="pass",
        reason_code="PLEX_READY",
        detail=f"Plex {identity.version} exposes {len(sections)} libraries",
    )


def _qbittorrent_check(
    config: DoctorConfig,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> DoctorCheck:
    if not all(
        (
            config.qbittorrent_url,
            config.qbittorrent_username,
            config.qbittorrent_password,
        )
    ):
        return DoctorCheck(
            name="qbittorrent",
            status="fail",
            reason_code="QBITTORRENT_CONFIG_INCOMPLETE",
            detail="qBittorrent connection settings are incomplete",
        )

    async def check() -> object:
        provider = QBittorrentProvider(
            config.qbittorrent_url,
            config.qbittorrent_username,
            config.qbittorrent_password,
            category=config.qbittorrent_category,
            save_root=config.qbittorrent_save_root,
            transport=transport,
        )
        try:
            return await provider.health(checked_at=config.checked_at)
        finally:
            await provider.close()

    try:
        health = asyncio.run(check())
    except (ValueError, RuntimeError):
        health = None
    if health is None or health.status != "healthy":
        reason = getattr(health, "reason_code", "QBITTORRENT_UNREACHABLE")
        return DoctorCheck(
            name="qbittorrent",
            status="fail",
            reason_code=str(reason),
            detail="qBittorrent authentication, category, or root scope is not ready",
            remediation="Configure the exact Omakase category and save root.",
        )
    return DoctorCheck(
        name="qbittorrent",
        status="pass",
        reason_code="QBITTORRENT_READY",
        detail="qBittorrent is authenticated and confined to the Omakase scope",
    )


def _vpn_check(
    config: DoctorConfig,
    *,
    runner: CommandRunner,
    external_transport: httpx.BaseTransport | None,
) -> DoctorCheck:
    if config.vpn_mode == "none":
        return DoctorCheck(
            name="torrent-vpn",
            status="fail",
            reason_code="VPN_REQUIRED",
            detail="qBittorrent acquisition has no verified tunnel",
            remediation="Use the qbittorrent-vpn profile or an explicitly bound external tunnel.",
        )
    if config.vpn_mode == "external":
        return _external_vpn_check(config, transport=external_transport)
    return _gluetun_check(config, runner=runner)


def _gluetun_check(config: DoctorConfig, *, runner: CommandRunner) -> DoctorCheck:
    fields = (
        "{{.Id}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|"
        "{{json .HostConfig.CapAdd}}|{{json .HostConfig.Devices}}|"
        "{{range .Config.Env}}{{if or "
        '(eq . "FIREWALL_ENABLED=off") '
        '(eq . "FIREWALL_ENABLED_DISABLING_IT_SHOOTS_YOU_IN_YOUR_FOOT=off")'
        "}}disabled{{end}}{{end}}"
    )
    code, output = runner(
        ["docker", "inspect", "--format", fields, config.gluetun_container],
        15,
    )
    if code != 0:
        return DoctorCheck(
            name="torrent-vpn",
            status="fail",
            reason_code="GLUETUN_UNAVAILABLE",
            detail="The Gluetun gateway could not be inspected",
            remediation="Start the qbittorrent-vpn profile and wait for a healthy tunnel.",
        )
    parts = output.strip().split("|")
    if len(parts) != 6:
        return DoctorCheck(
            name="torrent-vpn",
            status="fail",
            reason_code="GLUETUN_INSPECT_INVALID",
            detail="The Gluetun inspection receipt is invalid",
        )
    container_id, state, health, capabilities_json, devices_json, firewall = parts
    try:
        capabilities = json.loads(capabilities_json or "[]")
        devices = json.loads(devices_json or "[]")
    except json.JSONDecodeError:
        capabilities, devices = [], []
    tun_present = any(
        isinstance(item, dict) and item.get("PathOnHost") == "/dev/net/tun" for item in devices
    )
    if (
        state != "running"
        or health != "healthy"
        or "NET_ADMIN" not in capabilities
        or not tun_present
        or firewall == "disabled"
    ):
        return DoctorCheck(
            name="torrent-vpn",
            status="fail",
            reason_code="GLUETUN_NOT_FAIL_CLOSED",
            detail="The Gluetun gateway is not healthy with its tunnel safety posture intact",
            remediation="Restore /dev/net/tun, NET_ADMIN, health, and the built-in firewall.",
        )

    qbit_fields = "{{.HostConfig.NetworkMode}}|{{json .HostConfig.PortBindings}}"
    code, output = runner(
        ["docker", "inspect", "--format", qbit_fields, config.qbittorrent_container],
        15,
    )
    if code != 0:
        return DoctorCheck(
            name="torrent-vpn",
            status="fail",
            reason_code="QBITTORRENT_TOPOLOGY_UNKNOWN",
            detail="The qBittorrent network namespace could not be inspected",
        )
    parts = output.strip().split("|")
    if len(parts) != 2:
        return DoctorCheck(
            name="torrent-vpn",
            status="fail",
            reason_code="QBITTORRENT_TOPOLOGY_INVALID",
            detail="The qBittorrent network namespace receipt is invalid",
        )
    network_mode, port_bindings_json = parts
    try:
        port_bindings = json.loads(port_bindings_json or "null")
    except json.JSONDecodeError:
        port_bindings = {"invalid": True}
    if network_mode != f"container:{container_id}" or (
        port_bindings is not None and port_bindings != {}
    ):
        return DoctorCheck(
            name="torrent-vpn",
            status="fail",
            reason_code="QBITTORRENT_BYPASS_POSSIBLE",
            detail="qBittorrent can bypass the verified Gluetun namespace",
            remediation="Use network_mode service:gluetun and publish ports only on Gluetun.",
        )
    return DoctorCheck(
        name="torrent-vpn",
        status="pass",
        reason_code="GLUETUN_FAIL_CLOSED",
        detail="qBittorrent shares the healthy Gluetun namespace with no direct host ports",
    )


def _external_vpn_check(
    config: DoctorConfig,
    *,
    transport: httpx.BaseTransport | None,
) -> DoctorCheck:
    if not config.external_vpn_interface:
        return DoctorCheck(
            name="torrent-vpn",
            status="fail",
            reason_code="VPN_INTERFACE_REQUIRED",
            detail="The expected external VPN interface is not configured",
        )
    try:
        parsed = urlsplit(config.qbittorrent_url)
        with httpx.Client(
            base_url=f"{parsed.scheme}://{parsed.netloc}",
            headers={
                "Referer": f"{parsed.scheme}://{parsed.netloc}/",
                "Origin": f"{parsed.scheme}://{parsed.netloc}",
            },
            timeout=15,
            transport=transport,
        ) as client:
            login = client.post(
                "/api/v2/auth/login",
                data={
                    "username": config.qbittorrent_username,
                    "password": config.qbittorrent_password,
                },
            )
            if login.status_code != 200 or login.text.strip() != "Ok.":
                raise ValueError
            response = client.get("/api/v2/app/preferences")
            response.raise_for_status()
            preferences = response.json()
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return DoctorCheck(
            name="torrent-vpn",
            status="fail",
            reason_code="VPN_BINDING_UNVERIFIED",
            detail="qBittorrent's external interface binding could not be verified",
        )
    interface = str(preferences.get("current_network_interface", ""))
    address = str(preferences.get("current_interface_address", ""))
    if interface != config.external_vpn_interface or not address:
        return DoctorCheck(
            name="torrent-vpn",
            status="fail",
            reason_code="VPN_BINDING_MISMATCH",
            detail="qBittorrent is not locked to the expected external VPN interface",
            remediation="Set qBittorrent's network interface and optional IP address explicitly.",
        )
    return DoctorCheck(
        name="torrent-vpn",
        status="pass",
        reason_code="EXTERNAL_VPN_BOUND",
        detail="qBittorrent is bound to the expected external VPN interface and address",
    )


def _validate_config(config: DoctorConfig) -> None:
    if config.acquisition_mode not in _ACQUISITION_MODES:
        raise DoctorError("acquisition mode is invalid")
    if config.vpn_mode not in _VPN_MODES:
        raise DoctorError("VPN mode is invalid")
    if config.minimum_free_gib < 1 or config.minimum_free_gib > 1_000_000:
        raise DoctorError("minimum free space is invalid")
    if not isinstance(config.checked_at, str) or len(config.checked_at) != 20:
        raise DoctorError("checked_at must be canonical UTC")
    if config.acquisition_mode != "qbittorrent" and config.vpn_mode != "none":
        raise DoctorError("VPN mode applies only to qBittorrent acquisition")
    if any(
        not isinstance(value, Path)
        for value in (config.data_dir, config.import_root, config.library_root)
    ):
        raise DoctorError("Doctor paths are invalid")


def _run_command(command: list[str], timeout: float) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return completed.returncode, completed.stdout[:32_768]
