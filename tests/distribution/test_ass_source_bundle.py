"""Verify the actual local renderer source offer, not just a top-level filename."""

import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts import build_ass_source_bundle as source
from scripts import vendor_ass_notices as notices

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "src/omakase/plus/static/ass"
PROVENANCE = ROOT / "THIRD-PARTY/jassub-2.5.23"


def test_asset_adjacent_index_resolves_exact_complete_local_bundle():
    index = json.loads((ASSETS / "SOURCE.json").read_text())
    assert Path(index["bundle"]).name == index["bundle"]
    bundle = ASSETS / index["bundle"]
    assert bundle.stat().st_size == index["bytes"]
    assert source.sha256(bundle.read_bytes()) == index["sha256"]
    assert index["native_binary_correspondence_proven"] is False
    with tarfile.open(bundle) as package:
        names = set(package.getnames())
        assert "LICENSE" in names
        assert index["instructions"] in names
        assert {
            "scripts/vendor_ass.py",
            "scripts/vendor_ass_notices.py",
            "scripts/build_ass_source_bundle.py",
        } <= names
        manifest = json.load(
            package.extractfile("THIRD-PARTY/jassub-2.5.23/native-source-access.json")
        )
        assert set(manifest) == set(source.PROJECTS) == set(index["native_projects"])
        for project in source.PROJECTS:
            item = manifest[project]["captured_preferred_source"]
            data = package.extractfile("sources/native/" + item["archive_name"]).read()
            assert len(data) == item["bytes"]
            assert source.sha256(data) == item["sha256"]
            assert index["native_projects"][project]["sha256"] == item["sha256"]
        npm_lock = json.load(package.extractfile("THIRD-PARTY/jassub-2.5.23/npm-source-lock.json"))
        for name, item in npm_lock.items():
            assert (
                "sources/npm/"
                + name.replace("/", "__").replace("@", "")
                + "-"
                + item["version"]
                + ".tgz"
                in names
            )
        for name in ("vendor_ass.py", "vendor_ass_notices.py", "build_ass_source_bundle.py"):
            assert (
                package.extractfile("scripts/" + name).read()
                == (ROOT / "scripts" / name).read_bytes()
            )
        assert "E:/Temp" not in (ASSETS / "SOURCE.json").read_text()


def test_exact_missing_freetype_native_permission_block_is_distributed():
    index = json.loads((ASSETS / "SOURCE.json").read_text())
    native = index["native_projects"]["freetype"]["archive_name"]
    with tarfile.open(ASSETS / index["bundle"]) as bundle:
        data = bundle.extractfile("sources/native/" + native).read()
    with tarfile.open(fileobj=io.BytesIO(data)) as preferred:
        member = next(m for m in preferred.getmembers() if m.name.endswith("/src/base/fthash.c"))
        blocks = notices.rights_comments(preferred.extractfile(member).read())
    expected = next(b for b in blocks if "New Mexico State University" in b)
    assert "Francesco Zappa Nardelli" in expected
    assert "Permission is hereby granted" in expected and "THE SOFTWARE IS PROVIDED" in expected
    aggregate = (PROVENANCE / "THIRD-PARTY-NOTICES.txt").read_text()
    assert expected.replace("\r\n", "\n") in aggregate
    assert (ASSETS / "LICENSES.txt").read_bytes() == (
        PROVENANCE / "THIRD-PARTY-NOTICES.txt"
    ).read_bytes()


def test_complete_archives_assemble_buildable_source_layout_without_fake_git(tmp_path):
    with tarfile.open(ASSETS / "corresponding-source.tar.gz") as bundle:
        bundle.extractall(tmp_path / "offer", filter="data")
    tree = source.assemble(tmp_path / "offer", tmp_path / "assembled")
    assert not (tree / ".git").exists()
    assert b"emscripten/emsdk:6.0.12" in (tree / "Dockerfile").read_bytes()
    assert (tree / "build/patches/freetype/0002-disable-pthread.patch").is_file()
    assert (tree / "build/patches/harfbuzz/0001-force_disable_pthread.patch").is_file()
    for project in source.PROJECTS[1:]:
        assert (tree / "lib" / project).is_dir()
    for path in [
        "src/JASSUB.cpp",
        "functions.mk",
        "lib/fribidi/autogen.sh",
        "lib/libass/RELEASEVERSION",
        "lib/freetype/autogen.sh",
    ]:
        assert (tree / path).is_file()
    assert "git submodule" not in (tree / "Dockerfile").read_text()
    with pytest.raises(ValueError, match="must not exist"):
        source.assemble(tmp_path / "offer", tree)


def test_source_extractor_rejects_parent_escape(tmp_path):
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as package:
        member = tarfile.TarInfo("../outside")
        member.size = 1
        package.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unsafe"):
        source.extract_source(archive, tmp_path / "out")
    assert not (tmp_path / "outside").exists()


def test_both_generated_javascript_assets_discover_derivative_licenses_and_source():
    for name in ("jassub.js", "worker.js"):
        header = (ASSETS / name).read_text()[:500]
        assert header.startswith("/*! Modified JASSUB 2.5.23 derivative.")
        assert "not MIT-only" in header
        for target in ("./LICENSES.txt", "./SOURCE.json", "./corresponding-source.tar.gz"):
            assert target in header
    from scripts import vendor_ass

    assert vendor_ass.RIGHTS_BANNER == source.RIGHTS_BANNER


def test_asset_manifest_covers_every_current_asset_without_source_bundle_cycle():
    manifest = json.loads((PROVENANCE / "asset-manifest.json").read_text())
    files = {p.name: p for p in ASSETS.iterdir() if p.is_file()}
    assert set(manifest) == set(files)
    for name, file in files.items():
        assert manifest[name] == {
            "sha256": source.sha256(file.read_bytes()),
            "bytes": file.stat().st_size,
        }
    with tarfile.open(ASSETS / "corresponding-source.tar.gz") as package:
        assert "THIRD-PARTY/jassub-2.5.23/asset-manifest.json" not in package.getnames()
