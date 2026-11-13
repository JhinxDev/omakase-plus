#!/usr/bin/env python3
"""Plan-first Proxmox bootstrap for a trusted-home Omakase Stack LXC.

Only the ``apply`` subcommand mutates Proxmox. Apply is bound to the exact
canonical JSON plan hash and revalidates every external fact immediately before
creating the container.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "omakase-proxmox-plan-v1"
RECEIPT_SCHEMA = "omakase-proxmox-receipt-v1"
ALLOWED_KEYS = {
    "LAYOUT",
    "OMAKASE_CTID",
    "OMAKASE_HOSTNAME",
    "OMAKASE_IP",
    "GATEWAY",
    "BRIDGE",
    "ROOTFS_STORAGE",
    "TEMPLATE_STORAGE",
    "DEBIAN_TEMPLATE",
    "CORES",
    "MEMORY_MB",
    "SWAP_MB",
    "ROOTFS_GB",
    "STARTUP_ORDER",
    "TIMEZONE",
    "SSH_PUBLIC_KEY_FILE",
    "MEDIA_MOUNT_SOURCE",
    "MEDIA_MOUNT_TARGET",
    "ENABLE_VPN",
    "SOURCE_ARCHIVE",
    "SOURCE_SHA256",
}
HOSTNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
TEMPLATE_RE = re.compile(
    r"debian-(?:12|13)-standard_[A-Za-z0-9.+~-]+_(?:amd64|arm64)\.tar\.(?:zst|gz)"
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class BootstrapError(RuntimeError):
    """Configuration, preflight, or apply failure."""


def run(*args: str, check: bool = True, input_text: str | None = None) -> str:
    completed = subprocess.run(
        args,
        check=False,
        input=input_text,
        text=True,
        capture_output=True,
    )
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise BootstrapError(f"{args[0]} failed: {detail[:300]}")
    return completed.stdout


def load_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise BootstrapError(f"config line {number} must be KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in ALLOWED_KEYS:
            raise BootstrapError(f"unsupported config key: {key}")
        if key in values:
            raise BootstrapError(f"duplicate config key: {key}")
        values[key] = value.strip()
    return values


def integer(values: dict[str, str], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(values.get(key, str(default)))
    except ValueError as exc:
        raise BootstrapError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise BootstrapError(f"{key} must be between {minimum} and {maximum}")
    return value


def boolean(values: dict[str, str], key: str, default: bool = False) -> bool:
    value = values.get(key, str(default)).casefold()
    if value not in {"true", "false"}:
        raise BootstrapError(f"{key} must be true or false")
    return value == "true"


def storage_names() -> set[str]:
    lines = run("pvesm", "status").splitlines()[1:]
    return {line.split()[0] for line in lines if line.split()}


def template_names(template_storage: str) -> set[str]:
    output = run("pveam", "list", template_storage, check=False)
    return {
        Path(line.split()[0].split("/")[-1]).name
        for line in output.splitlines()[1:]
        if line.split()
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_source_archive(path: Path) -> None:
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                name = PurePosixPath(member.name)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                ):
                    raise BootstrapError("SOURCE_ARCHIVE contains an unsafe member")
    except tarfile.TarError as exc:
        raise BootstrapError("SOURCE_ARCHIVE must be a readable tar archive") from exc


def choose_template(values: dict[str, str], template_storage: str) -> tuple[str, bool]:
    requested = values.get("DEBIAN_TEMPLATE", "")
    installed = template_names(template_storage)
    if requested:
        if not TEMPLATE_RE.fullmatch(requested):
            raise BootstrapError("DEBIAN_TEMPLATE must be a Debian 12 or 13 standard archive name")
        return requested, requested not in installed
    available = []
    for line in run("pveam", "available", "--section", "system").splitlines():
        parts = line.split()
        if len(parts) >= 2 and TEMPLATE_RE.fullmatch(parts[1]):
            available.append(parts[1])
    if not available:
        raise BootstrapError("no supported Debian 12/13 template is available")
    selected = sorted(available)[-1]
    return selected, selected not in installed


def validate_media_mount(raw_source: str, raw_target: str) -> tuple[str, str] | None:
    if not raw_source:
        return None
    source = Path(raw_source)
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise BootstrapError(
            "MEDIA_MOUNT_SOURCE must be an existing absolute non-symlink directory"
        )
    current = Path(source.anchor)
    for part in source.parts[1:]:
        current /= part
        if current.is_symlink():
            raise BootstrapError("MEDIA_MOUNT_SOURCE cannot contain symlink components")
    resolved = source.resolve()
    safe_root = Path("/mnt/bindmounts").resolve()
    if resolved == safe_root or safe_root not in resolved.parents:
        raise BootstrapError("MEDIA_MOUNT_SOURCE must be below /mnt/bindmounts")
    target = Path(raw_target)
    if not target.is_absolute() or str(target) in {"/", "/var", "/etc"}:
        raise BootstrapError("MEDIA_MOUNT_TARGET must be a dedicated absolute guest path")
    return str(resolved), str(target)


def normalize_network(values: dict[str, str]) -> tuple[str, str]:
    configured = values.get("OMAKASE_IP", "dhcp")
    gateway = values.get("GATEWAY", "")
    if configured == "dhcp":
        if gateway:
            raise BootstrapError("GATEWAY must be empty when OMAKASE_IP=dhcp")
        return configured, ""
    try:
        interface = ipaddress.ip_interface(configured)
    except ValueError as exc:
        raise BootstrapError("OMAKASE_IP must be dhcp or an IPv4 CIDR") from exc
    if interface.version != 4:
        raise BootstrapError("only IPv4 is supported by this reference bootstrap")
    if not gateway:
        raise BootstrapError("GATEWAY is required for a static address")
    try:
        gateway_ip = ipaddress.ip_address(gateway)
    except ValueError as exc:
        raise BootstrapError("GATEWAY is invalid") from exc
    if gateway_ip not in interface.network:
        raise BootstrapError("GATEWAY must be inside the configured network")
    return configured, gateway


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def plan_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def build_plan(config_path: Path) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise BootstrapError("run planning on the Proxmox node as root")
    for command in ("pct", "pvesm", "pveam", "ip", "python3"):
        if shutil.which(command) is None:
            raise BootstrapError(f"required Proxmox command is missing: {command}")
    values = load_config(config_path)
    if values.get("LAYOUT", "compact") != "compact":
        raise BootstrapError(
            "this release automates the compact LXC layout; use the split guide manually"
        )

    ctid = integer(values, "OMAKASE_CTID", 120, 100, 999999999)
    if Path(f"/etc/pve/lxc/{ctid}.conf").exists():
        raise BootstrapError(f"CTID {ctid} is already occupied")
    hostname = values.get("OMAKASE_HOSTNAME", "omakase-stack")
    if not HOSTNAME_RE.fullmatch(hostname):
        raise BootstrapError("OMAKASE_HOSTNAME is invalid")
    bridge = values.get("BRIDGE", "vmbr0")
    if not Path(f"/sys/class/net/{bridge}").exists():
        raise BootstrapError(f"bridge does not exist: {bridge}")
    rootfs_storage = values.get("ROOTFS_STORAGE", "local-lvm")
    template_storage = values.get("TEMPLATE_STORAGE", "local")
    discovered_storages = storage_names()
    for storage in (rootfs_storage, template_storage):
        if storage not in discovered_storages:
            raise BootstrapError(f"storage does not exist: {storage}")
    template, download_template = choose_template(values, template_storage)
    address, gateway = normalize_network(values)
    media_mount = validate_media_mount(
        values.get("MEDIA_MOUNT_SOURCE", ""),
        values.get("MEDIA_MOUNT_TARGET", "/srv/omakase"),
    )
    ssh_key = Path(values.get("SSH_PUBLIC_KEY_FILE", ""))
    if not ssh_key.is_absolute() or not ssh_key.is_file():
        raise BootstrapError("SSH_PUBLIC_KEY_FILE must be an existing public-key file")
    source_archive = values.get("SOURCE_ARCHIVE", "")
    source_sha256 = values.get("SOURCE_SHA256", "").casefold()
    if source_archive:
        archive_path = Path(source_archive)
        if not archive_path.is_absolute() or not archive_path.is_file():
            raise BootstrapError("SOURCE_ARCHIVE must be an existing absolute file")
        if not SHA256_RE.fullmatch(source_sha256):
            raise BootstrapError("SOURCE_SHA256 is required with SOURCE_ARCHIVE")
        validate_source_archive(archive_path)
        actual = sha256_file(archive_path)
        if actual != source_sha256:
            raise BootstrapError("SOURCE_ARCHIVE does not match SOURCE_SHA256")
        source_archive = str(archive_path.resolve())
    elif source_sha256:
        raise BootstrapError("SOURCE_SHA256 requires SOURCE_ARCHIVE")

    enable_vpn = boolean(values, "ENABLE_VPN")
    if enable_vpn and not Path("/dev/net/tun").exists():
        raise BootstrapError("ENABLE_VPN requires /dev/net/tun on the Proxmox node")
    if enable_vpn and "--dev[n]" not in run("pct", "help", "set", check=False):
        raise BootstrapError("this Proxmox version cannot safely pass /dev/net/tun with pct")

    return {
        "schema_version": SCHEMA,
        "layout": "compact",
        "container": {
            "ctid": ctid,
            "hostname": hostname,
            "unprivileged": True,
            "features": "keyctl=1,nesting=1",
            "protection": True,
            "cores": integer(values, "CORES", 4, 1, 64),
            "memory_mb": integer(values, "MEMORY_MB", 4096, 1024, 262144),
            "swap_mb": integer(values, "SWAP_MB", 512, 0, 65536),
            "rootfs_gb": integer(values, "ROOTFS_GB", 32, 16, 4096),
            "startup_order": integer(values, "STARTUP_ORDER", 6, 0, 999),
            "timezone": values.get("TIMEZONE", "host"),
            "bridge": bridge,
            "address": address,
            "gateway": gateway,
            "rootfs_storage": rootfs_storage,
            "template_storage": template_storage,
            "template": template,
            "download_template": download_template,
            "ssh_public_key_file": str(ssh_key.resolve()),
            "media_mount": (
                {"source": media_mount[0], "target": media_mount[1]} if media_mount else None
            ),
            "enable_vpn": enable_vpn,
        },
        "source": {
            "archive": source_archive,
            "sha256": source_sha256,
            "staged_only": bool(source_archive),
        },
        "posture": {
            "public_exposure": False,
            "application_started": False,
            "acquisition_mode": "import-only",
            "docker_socket_exposed_to_application": False,
        },
    }


GUEST_BOOTSTRAP = r"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
printf '%s\n' \
  'Types: deb' \
  'URIs: https://download.docker.com/linux/debian' \
  "Suites: ${VERSION_CODENAME}" \
  'Components: stable' \
  "Architectures: $(dpkg --print-architecture)" \
  'Signed-By: /etc/apt/keyrings/docker.asc' \
  > /etc/apt/sources.list.d/docker.sources
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
install -d -m 0700 /opt/omakase
install -d -o 1000 -g 1000 -m 0750 /srv/omakase/import /srv/omakase/library
install -d -o 1000 -g 1000 -m 0700 /srv/omakase/data /srv/omakase/plex /srv/omakase/transcode
docker version --format '{{.Server.Version}}'
docker compose version --short
"""


def revalidate(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != SCHEMA:
        raise BootstrapError("plan schema is unsupported")
    container = plan["container"]
    ctid = int(container["ctid"])
    if Path(f"/etc/pve/lxc/{ctid}.conf").exists():
        raise BootstrapError(f"CTID {ctid} became occupied")
    if container["rootfs_storage"] not in storage_names():
        raise BootstrapError("rootfs storage is no longer available")
    if container["template_storage"] not in storage_names():
        raise BootstrapError("template storage is no longer available")
    if not Path(f"/sys/class/net/{container['bridge']}").exists():
        raise BootstrapError("planned bridge is no longer available")
    if container.get("media_mount"):
        validate_media_mount(
            container["media_mount"]["source"],
            container["media_mount"]["target"],
        )
    source = plan.get("source", {})
    if source.get("archive"):
        validate_source_archive(Path(source["archive"]))
        actual = sha256_file(Path(source["archive"]))
        if actual != source["sha256"]:
            raise BootstrapError("source archive changed after planning")


def apply_plan(plan_path: Path, expected_hash: str, receipt_path: Path) -> None:
    if os.geteuid() != 0:
        raise BootstrapError("run apply on the Proxmox node as root")
    raw = plan_path.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    if not SHA256_RE.fullmatch(expected_hash) or actual_hash != expected_hash:
        raise BootstrapError("expected plan hash does not match the exact plan file")
    plan = json.loads(raw)
    revalidate(plan)
    container = plan["container"]
    ctid = str(container["ctid"])
    template_storage = container["template_storage"]
    template = container["template"]
    created = False
    started = False
    try:
        if container["download_template"] and template not in template_names(template_storage):
            run("pveam", "download", template_storage, template)
        net0 = f"name=eth0,bridge={container['bridge']},ip={container['address']},type=veth"
        if container["gateway"]:
            net0 += f",gw={container['gateway']}"
        args = [
            "pct",
            "create",
            ctid,
            f"{template_storage}:vztmpl/{template}",
            "--hostname",
            container["hostname"],
            "--ostype",
            "debian",
            "--unprivileged",
            "1",
            "--features",
            container["features"],
            "--cores",
            str(container["cores"]),
            "--memory",
            str(container["memory_mb"]),
            "--swap",
            str(container["swap_mb"]),
            "--rootfs",
            f"{container['rootfs_storage']}:{container['rootfs_gb']}",
            "--net0",
            net0,
            "--onboot",
            "1",
            "--startup",
            f"order={container['startup_order']},up=30",
            "--timezone",
            container["timezone"],
            "--ssh-public-keys",
            container["ssh_public_key_file"],
            "--protection",
            "1",
        ]
        if container.get("media_mount"):
            mount = container["media_mount"]
            args.extend(["--mp0", f"{mount['source']},mp={mount['target']},backup=0"])
        if container["enable_vpn"]:
            args.extend(["--dev0", "path=/dev/net/tun"])
        run(*args)
        created = True
        run("pct", "start", ctid)
        started = True
        run("pct", "exec", ctid, "--", "bash", "-s", input_text=GUEST_BOOTSTRAP)
        source = plan.get("source", {})
        if source.get("archive"):
            archive_name = Path(source["archive"]).name
            guest_archive = f"/tmp/{archive_name}"
            run("pct", "push", ctid, source["archive"], guest_archive)
            run("pct", "exec", ctid, "--", "mkdir", "-p", "/opt/omakase/source")
            run(
                "pct",
                "exec",
                ctid,
                "--",
                "tar",
                "-xf",
                guest_archive,
                "-C",
                "/opt/omakase/source",
            )
            run("pct", "exec", ctid, "--", "rm", "-f", guest_archive)
        docker_version = run(
            "pct",
            "exec",
            ctid,
            "--",
            "docker",
            "version",
            "--format",
            "{{.Server.Version}}",
        ).strip()
        compose_version = run(
            "pct", "exec", ctid, "--", "docker", "compose", "version", "--short"
        ).strip()
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "outcome": "prepared",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "plan_hash": actual_hash,
            "ctid": int(ctid),
            "docker_version": docker_version,
            "compose_version": compose_version,
            "source_staged": bool(source.get("archive")),
            "application_started": False,
            "rollback": [
                f"pct stop {ctid}",
                f"pct set {ctid} --protection 0",
                f"pct destroy {ctid}",
            ],
        }
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        os.chmod(receipt_path, 0o600)
    except Exception:
        state = f"created={created}, started={started}, ctid={ctid}"
        raise BootstrapError(
            f"apply stopped safely ({state}); inspect the CT and use the documented rollback"
        ) from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan", help="discover facts and write an immutable plan")
    plan_parser.add_argument("--config", type=Path, required=True)
    plan_parser.add_argument("--out", type=Path, required=True)
    apply_parser = subparsers.add_parser("apply", help="apply an exact reviewed plan")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--expected-plan-hash", required=True)
    apply_parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            payload = build_plan(args.config.resolve())
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(canonical_bytes(payload))
            digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
            print(f"plan={args.out.resolve()}")
            print(f"plan_hash={digest}")
            print("mutation=none")
        else:
            apply_plan(
                args.plan.resolve(),
                args.expected_plan_hash.casefold(),
                args.receipt.resolve(),
            )
            print(f"receipt={args.receipt.resolve()}")
            print("outcome=prepared")
            print("application_started=false")
        return 0
    except (BootstrapError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
