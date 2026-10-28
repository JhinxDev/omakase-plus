"""Local storage containment and receipt tests."""

from __future__ import annotations

import os

import pytest

from omakase.plus.storage import (
    StorageSafetyError,
    configure_root,
    file_receipt,
    normalize_locator,
    require_media_file,
    resolve_locator,
)


def test_locator_rejects_absolute_parent_and_drive_paths():
    for locator in ("/etc/passwd", "../escape.mkv", "show/../../escape.mkv", "C:/escape.mkv"):
        with pytest.raises(StorageSafetyError):
            normalize_locator(locator)


def test_root_resolution_requires_absolute_existing_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(StorageSafetyError, match="absolute"):
        configure_root(".", owner_id=1)
    with pytest.raises(StorageSafetyError, match="unavailable"):
        configure_root(tmp_path / "missing", owner_id=1)


def test_media_receipt_is_stable_and_rejects_wrong_extensions(tmp_path):
    media = tmp_path / "Show S01E01.mkv"
    media.write_bytes(b"media")
    note = tmp_path / "readme.txt"
    note.write_text("not media", encoding="utf-8")
    root = configure_root(tmp_path, owner_id=1)

    first = file_receipt(root, media.name)
    second = file_receipt(root, media.name)

    assert first == second
    assert first.locator == "Show S01E01.mkv"
    assert len(first.sha256) == 64
    with pytest.raises(StorageSafetyError, match="supported media"):
        require_media_file(root, note.name)


def test_symlink_escape_is_refused(tmp_path):
    root_path = tmp_path / "root"
    outside = tmp_path / "outside"
    root_path.mkdir()
    outside.mkdir()
    target = outside / "Episode.mkv"
    target.write_bytes(b"outside")
    link = root_path / "escaped.mkv"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    root = configure_root(root_path, owner_id=1)

    with pytest.raises(StorageSafetyError, match="escapes"):
        resolve_locator(root, link.name)
