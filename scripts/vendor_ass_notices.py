"""Preserve native named licenses and rights-bearing source comments offline.

Header extraction is deliberately conservative: full comment blocks are retained,
not guessed license labels or author lists. Included-source evidence may cover
source files not selected by the native linker; it is not a new license choice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "THIRD-PARTY/jassub-2.5.23"
SOURCES = {
    "jassub": ("ThaUnknown/jassub", "482a2fed6c8976550f09383e90d92b6ac3360fc9"),
    "brotli": ("google/brotli", "028fb5a23661f123017c060daa546b55cf4bde29"),
    "freetype": ("freetype/freetype", "0a0221a1347e2f1e07c395263540026e9a0aa7c7"),
    "fribidi": ("fribidi/fribidi", "68162babff4f39c4e2dc164a5e825af93bda9983"),
    "harfbuzz": ("harfbuzz/harfbuzz", "30485ee8c3d43c553afb9d78b9924cb71c8d2f19"),
    "libass": ("libass/libass", "fbe73bf18bd98e3ba14ce6d5e4bd1ba202a25ef4"),
}
COMMENTS = re.compile(
    r"/\*.*?\*/|(?:^[ \t]*//[^\n]*(?:\n|$))+|(?:^[ \t]*\#[^\n]*(?:\n|$))+", re.S | re.M
)
RIGHTS = re.compile(
    r"copyright|SPDX-License|permission is|redistribut|licensed under|all rights reserved", re.I
)
SUFFIXES = {".c", ".h", ".cc", ".hh", ".cpp", ".hpp", ".js", ".ts", ".py", ".sh", ".S"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def rights_comments(data):
    """Return exact complete copyright/license comment blocks, deduplicated."""
    text = data.decode("utf-8", errors="replace")
    return list(
        dict.fromkeys(m.group() for m in COMMENTS.finditer(text) if RIGHTS.search(m.group()))
    )


def eligible_source(project, path):
    parts = PurePosixPath(path).parts
    if project == "jassub":
        return parts[0] == "src"
    excluded = {"brotli": "research", "freetype": "src/tools", "fribidi": "bin", "harfbuzz": "test"}
    prefix = excluded.get(project)
    return not prefix or not (path == prefix or path.startswith(prefix + "/"))


def capture_local(source_dir, destination=DEST):
    manifest_path = destination / "native-source-access.json"
    manifest = json.loads(manifest_path.read_text())
    notices = destination / "notices"
    notices.mkdir(exist_ok=True)
    for project, (_, commit) in SOURCES.items():
        item = manifest[project]
        if item["commit"] != commit:
            raise ValueError("Preferred-source revision changed")
        captured = item["captured_preferred_source"]
        archive = source_dir / captured["archive_name"]
        data = archive.read_bytes()
        if len(data) != captured["bytes"] or sha256(data) != captured["sha256"]:
            raise ValueError("Preferred-source archive integrity mismatch")
        blocks = {}
        with tarfile.open(archive) as package:
            for member in package.getmembers():
                if not member.isfile():
                    continue
                path = member.name.split("/", 1)[-1]
                if not eligible_source(project, path):
                    continue
                name = PurePosixPath(path).name.lower()
                if any(
                    w in name
                    for w in ("license", "copying", "copyright", "authors", "notice", "ftl.txt")
                ):
                    (notices / (project + "-" + path.replace("/", "__"))).write_bytes(
                        package.extractfile(member).read()
                    )
                if PurePosixPath(path).suffix not in SUFFIXES or member.size > 4 * 1024 * 1024:
                    continue
                for block in rights_comments(package.extractfile(member).read()):
                    blocks.setdefault(block, []).append(path)
        output = []
        records = []
        for block, paths in sorted(
            blocks.items(), key=lambda pair: (pair[1][0], sha256(pair[0].encode()))
        ):
            output.extend(
                ["\n===== source comments: " + ", ".join(paths) + " =====\n", block, "\n"]
            )
            records.append({"paths": paths, "sha256": sha256(block.encode())})
        target = notices / ("source-headers-" + project + ".txt")
        target.write_text("".join(output), encoding="utf-8", newline="\n")
        item["source_header_notices"] = {
            "file": "notices/" + target.name,
            "sha256": sha256(target.read_bytes()),
            "blocks": records,
        }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    aggregate = [
        "JASSUB DERIVED CANDIDATE RIGHTS EVIDENCE. This is not an MIT-only distribution.\n",
        "Full provided notices and native source copyright/license comment blocks follow.\n",
        "Source-comment collection follows upstream source scopes conservatively; not every source file is linked.\n",
        "Native source-to-npm-WASM correspondence and bit-identical native rebuilds have not been proved.\n",
        "Preferred source and rebuild/relink instructions: SOURCE.json and corresponding-source.tar.gz beside these assets.\n",
    ]
    for file in sorted(notices.iterdir()):
        if file.is_file():
            aggregate.extend(
                ["\n\n===== " + file.name + " =====\n", file.read_text(errors="replace")]
            )
    aggregate.append((destination / "upstream-build-license_fullnotice").read_text())
    (destination / "THIRD-PARTY-NOTICES.txt").write_text(
        "".join(aggregate), encoding="utf-8", newline="\n"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="Six captured complete preferred-source archives",
    )
    args = parser.parse_args()
    capture_local(args.source_dir)


if __name__ == "__main__":
    main()
