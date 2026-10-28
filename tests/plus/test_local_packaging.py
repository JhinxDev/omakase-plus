"""Static safety contracts for the local Docker distribution."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_local_compose_has_safe_storage_and_network_defaults():
    compose = (ROOT / "compose.local.yaml").read_text(encoding="utf-8")

    assert "${OMAKASE_BIND_ADDRESS:-127.0.0.1}" in compose
    assert "target: /media/import\n        read_only: true" in compose
    assert "target: /media/library" in compose
    assert "/var/run/docker.sock" not in compose
    assert 'network_mode: "service:gluetun"' in compose
    qbit = compose.split("  qbittorrent:", 1)[1]
    assert "\n    ports:" not in qbit
    assert "qmcgaw/gluetun:v3.41.1" in compose
    assert "qbittorrent-nox:${QBITTORRENT_IMAGE_TAG:-5.2.3-1}" in compose
    assert "plexinc/pms-docker:${PLEX_IMAGE_TAG:-1.43.2.10687-563d026ea}" in compose
    assert ":latest" not in compose


def test_local_secret_template_is_ignored_but_examples_are_tracked():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    secret_example = (ROOT / ".env.local.secrets.example").read_text(encoding="utf-8")
    public_example = (ROOT / ".env.local.example").read_text(encoding="utf-8")

    assert ".env.*" in ignore
    assert "!.env.local.example" in ignore
    assert "!.env.local.secrets.example" in ignore
    assert "OMAKASE_PLUS_MASTER_KEY=" in secret_example
    assert "WIREGUARD_PRIVATE_KEY=" in secret_example
    assert "OMAKASE_PLUS_MASTER_KEY" not in public_example
