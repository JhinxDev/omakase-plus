"""Pure contract tests for the plan-first Proxmox bootstrap."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[2] / "infra" / "proxmox" / "bootstrap.py"
SPEC = importlib.util.spec_from_file_location("omakase_proxmox_bootstrap", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


def test_config_is_parsed_as_data_and_rejects_unknown_keys(tmp_path):
    config = tmp_path / "stack.env"
    config.write_text("LAYOUT=compact\nOMAKASE_CTID=120\n", encoding="utf-8")
    assert bootstrap.load_config(config)["OMAKASE_CTID"] == "120"

    config.write_text("LAYOUT=compact\nMALICIOUS=$(touch /tmp/nope)\n", encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError, match="unsupported config key"):
        bootstrap.load_config(config)


def test_network_requires_explicit_valid_gateway_for_static_address():
    assert bootstrap.normalize_network({"OMAKASE_IP": "dhcp"}) == ("dhcp", "")
    assert bootstrap.normalize_network(
        {"OMAKASE_IP": "192.168.50.150/24", "GATEWAY": "192.168.50.1"}
    ) == ("192.168.50.150/24", "192.168.50.1")
    with pytest.raises(bootstrap.BootstrapError, match="GATEWAY is required"):
        bootstrap.normalize_network({"OMAKASE_IP": "192.168.50.150/24"})
    with pytest.raises(bootstrap.BootstrapError, match="inside"):
        bootstrap.normalize_network({"OMAKASE_IP": "192.168.50.150/24", "GATEWAY": "10.0.0.1"})


def test_canonical_plan_hash_is_stable_and_sensitive_to_changes():
    first = {"schema_version": bootstrap.SCHEMA, "container": {"ctid": 120}}
    reordered = {"container": {"ctid": 120}, "schema_version": bootstrap.SCHEMA}
    changed = {"schema_version": bootstrap.SCHEMA, "container": {"ctid": 121}}
    assert bootstrap.canonical_bytes(first) == bootstrap.canonical_bytes(reordered)
    assert bootstrap.plan_hash(first) == bootstrap.plan_hash(reordered)
    assert bootstrap.plan_hash(first) != bootstrap.plan_hash(changed)
    assert json.loads(bootstrap.canonical_bytes(first)) == first


def _archive(path: Path, member_name: str) -> None:
    data = b"release"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(member_name)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))


def test_source_archive_rejects_traversal(tmp_path):
    safe = tmp_path / "safe.tar.gz"
    _archive(safe, "omakase/README.md")
    bootstrap.validate_source_archive(safe)

    unsafe = tmp_path / "unsafe.tar.gz"
    _archive(unsafe, "../../etc/cron.d/escape")
    with pytest.raises(bootstrap.BootstrapError, match="unsafe member"):
        bootstrap.validate_source_archive(unsafe)


def test_boolean_and_integer_values_fail_closed():
    assert bootstrap.boolean({"ENABLE_VPN": "false"}, "ENABLE_VPN") is False
    with pytest.raises(bootstrap.BootstrapError, match="true or false"):
        bootstrap.boolean({"ENABLE_VPN": "yes"}, "ENABLE_VPN")
    assert bootstrap.integer({"CORES": "4"}, "CORES", 2, 1, 64) == 4
    with pytest.raises(bootstrap.BootstrapError, match="between"):
        bootstrap.integer({"CORES": "0"}, "CORES", 2, 1, 64)
