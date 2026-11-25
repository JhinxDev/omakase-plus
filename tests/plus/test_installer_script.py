"""Unit tests for the friend-friendly installer script and GHCR workflow."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = ROOT / "install.sh"
GHCR_WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"


def _bash() -> str:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable on this host")
    # Prefer Git Bash / POSIX bash over WSL stubs that fail on Windows paths.
    if os.name == "nt" and bash.lower().endswith("\\system32\\bash.exe"):
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        if not git_bash.exists():
            pytest.skip("WSL bash stub cannot translate temp paths without a distro")
        return str(git_bash)
    return bash


def test_ghcr_workflow_is_valid_yaml():
    assert GHCR_WORKFLOW.is_file()
    workflow = yaml.safe_load(GHCR_WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["name"] == "Publish container image (GHCR)"
    assert "packages" in workflow["permissions"]
    assert workflow["permissions"]["packages"] == "write"
    steps = workflow["jobs"]["publish"]["steps"]
    text = GHCR_WORKFLOW.read_text(encoding="utf-8")
    assert "ghcr.io/jhinxdev/omakase-plus" in text
    assert any("docker/build-push-action" in step.get("uses", "") for step in steps)


def test_install_script_syntax_and_lf_endings():
    raw = INSTALL_SCRIPT.read_bytes()
    assert b"\r\n" not in raw, "install.sh must keep LF line endings"
    # Keep install.sh free of private hostnames/RFC1918 literals so docs public boundary checks pass.
    text = raw.decode("utf-8")
    assert "192.168." not in text
    assert "anime.jhinx.dev" not in text

    res = subprocess.run([_bash(), "-n", str(INSTALL_SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_install_script_generates_compose_and_preserves_master_key(tmp_path):
    bash = _bash()
    target = tmp_path / "omakase-install"
    cmd = [
        bash,
        str(INSTALL_SCRIPT),
        "--non-interactive",
        "--no-start",
        "--dir",
        str(target),
        "--port",
        "8899",
        "--bind",
        "0.0.0.0",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr + res.stdout

    env_text = (target / ".env.local").read_text(encoding="utf-8")
    secrets_text = (target / ".env.local.secrets").read_text(encoding="utf-8")
    compose_text = (target / "compose.yaml").read_text(encoding="utf-8")

    assert "OMAKASE_PLUS_MASTER_KEY" not in env_text
    assert "OMAKASE_PORT=8899" in env_text
    assert "OMAKASE_BIND_ADDRESS=0.0.0.0" in env_text

    match = re.search(r"^OMAKASE_PLUS_MASTER_KEY=([0-9a-f]{64})$", secrets_text, re.MULTILINE)
    assert match is not None
    first_key = match.group(1)

    parsed = yaml.safe_load(compose_text)
    svc = parsed["services"]["omakase-plus"]
    assert svc["image"] == "${OMAKASE_IMAGE:-ghcr.io/jhinxdev/omakase-plus:latest}"
    assert svc["security_opt"] == ["no-new-privileges:true"]
    assert svc["cap_drop"] == ["ALL"]
    assert any(v["target"] == "/home/omakase/data" for v in svc["volumes"])
    assert "plex" not in parsed["services"]

    # Second run against same directory must preserve the generated encryption key.
    res2 = subprocess.run(cmd, capture_output=True, text=True)
    assert res2.returncode == 0, res2.stderr + res2.stdout
    secrets_again = (target / ".env.local.secrets").read_text(encoding="utf-8")
    assert f"OMAKASE_PLUS_MASTER_KEY={first_key}" in secrets_again


def test_install_script_optional_media_and_plex_flags(tmp_path):
    bash = _bash()
    target = tmp_path / "omakase-media"
    media = tmp_path / "my-media"
    media.mkdir()
    res = subprocess.run(
        [
            bash,
            str(INSTALL_SCRIPT),
            "--non-interactive",
            "--no-start",
            "--dir",
            str(target),
            "--media-path",
            str(media),
            "--with-plex",
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr + res.stdout

    compose = yaml.safe_load((target / "compose.yaml").read_text(encoding="utf-8"))
    volumes = compose["services"]["omakase-plus"]["volumes"]
    targets = {v["target"]: v for v in volumes}
    assert "/media/import" in targets
    assert targets["/media/import"]["read_only"] is True
    assert "/media/library" in targets
    assert "plex" in compose["services"]
