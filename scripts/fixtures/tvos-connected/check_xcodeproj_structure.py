"""Structural check for the OmakaseQualification Xcode project.

`plutil -lint` is a macOS tool, so this script does the part of that check that a
text parser can do reliably on any host:

  * balanced braces and parentheses in the property-list source
  * every object id is defined once and every referenced id exists
  * every PBXBuildFile resolves to a PBXFileReference that exists on disk
  * every PBXBuildFile is used by exactly one build phase
  * every ``*.swift`` file in a target's source directory has exactly one entry in
    that target's Sources build phase, and vice versa

It is not an Xcode parser: it cannot prove that Xcode accepts the project. It
catches the wiring defects this fixture kept hitting (a file present in the group
but missing from Sources, a duplicate id, a dangling fileRef).

Usage:
    python check_xcodeproj_structure.py [--project PATH] [--json PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DEFAULT_PROJECT = REPO / "tv-source/OmakaseQualification.xcodeproj/project.pbxproj"
DEFAULT_SOURCE_ROOT = REPO / "tv-source"

# One `ID = { ... };` object or `ID /* comment */ = { ... };` or
# `ID = {isa = X; ... };` entry. The pbxproj is one-line-per-object in places and
# multi-line in others, so match the id then scan to the matching brace.
OBJECT = re.compile(r"^\s*([0-9A-F]{24})\b[^=\n]*= \{", re.MULTILINE)
FILE_REF = re.compile(r"fileRef = ([0-9A-F]{24})")
FILE_REF_OBJECT = re.compile(
    r"^\s*([0-9A-F]{24})(?: /\*.*?\*/)? = \{isa = PBXFileReference;.*?path = ([^;]+);",
    re.MULTILINE,
)
SOURCES_PHASE = re.compile(
    r"^\s*([0-9A-F]{24})(?: /\*.*?\*/)? = \{isa = PBXSourcesBuildPhase;.*?files = \((.*?)\)",
    re.MULTILINE,
)
UITTEST_TARGET = "000000000000000000000067"


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def load(project: Path) -> tuple[str, list[str]]:
    text = project.read_text(encoding="utf-8")
    problems: list[str] = []

    # 1. Balanced braces and parentheses, outside of quoted strings.
    depth = {"{": 0, "(": 0}
    pairs = {"}": "{", ")": "("}
    quote = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            quote = not quote
        elif not quote:
            if char in depth:
                depth[char] += 1
            elif char in pairs:
                depth[pairs[char]] -= 1
                if depth[pairs[char]] < 0:
                    problems.append(f"unbalanced '{char}' at offset {index}")
                    depth = {"{": 0, "(": 0}
        index += 1
    if quote:
        problems.append("unterminated quoted string")
    for opener, count in depth.items():
        if count:
            problems.append(f"{count} unclosed '{opener}'")
    return text, problems


def objects(text: str) -> dict[str, str]:
    """Map object id to the full `{...}` body of its definition."""
    found: dict[str, str] = {}
    duplicates: list[str] = []
    for match in OBJECT.finditer(text):
        identifier = match.group(1)
        start = text.index("{", match.start())
        cursor, level = start, 0
        quote = False
        while cursor < len(text):
            char = text[cursor]
            if char == '"':
                quote = not quote
            elif not quote:
                if char == "{":
                    level += 1
                elif char == "}":
                    level -= 1
                    if level == 0:
                        break
            cursor += 1
        if identifier in found:
            duplicates.append(identifier)
        found[identifier] = text[start : cursor + 1]
    if duplicates:
        found["__duplicates__"] = ",".join(sorted(set(duplicates)))
    return found


def phases(body: dict[str, str]) -> dict[str, list[str]]:
    result = {}
    for identifier, block in body.items():
        if not block.startswith("{") or "__duplicates__" in identifier:
            continue
        if "isa = PBXSourcesBuildPhase" not in block:
            continue
        files = re.search(r"files = \((.*?)\)", block)
        ids = re.findall(r"[0-9A-F]{24}", files.group(1)) if files else []
        result[identifier] = ids
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--target", default=UITTEST_TARGET)
    arguments = parser.parse_args(argv)

    text, problems = load(arguments.project)
    body = objects(text)
    duplicates = body.pop("__duplicates__", "")
    if duplicates:
        problems.append(f"duplicate object ids: {duplicates}")

    build_files: dict[str, str] = {}
    file_refs: dict[str, str] = {}
    for identifier, block in body.items():
        if "isa = PBXBuildFile" in block:
            reference = FILE_REF.search(block)
            if not reference:
                # Swift-package products carry `productRef` instead of `fileRef`.
                if "productRef" in block:
                    continue
                problems.append(f"PBXBuildFile {identifier} has no fileRef")
                continue
            build_files[identifier] = reference.group(1)
        elif "isa = PBXFileReference;" in block:
            path = re.search(r"path = ([^;]+);", block)
            if path:
                file_refs[identifier] = unquote(path.group(1))

    for identifier, reference in build_files.items():
        if reference not in body:
            problems.append(f"PBXBuildFile {identifier} points at missing fileRef {reference}")
        elif reference not in file_refs:
            problems.append(f"PBXBuildFile {identifier} fileRef {reference} is not a file")

    # Every build file must be used by exactly one build phase. Any phase kind
    # counts here; only the UITests Sources phase is checked for membership below.
    used: dict[str, list[str]] = {}
    for phase_id, block in body.items():
        if "BuildPhase" not in block:
            continue
        files = re.search(r"files = \((.*?)\)", block)
        for item in re.findall(r"[0-9A-F]{24}", files.group(1)) if files else []:
            used.setdefault(item, []).append(phase_id)
    for identifier in build_files:
        count = len(used.get(identifier, []))
        if count != 1:
            problems.append(f"PBXBuildFile {identifier} used by {count} build phases, want 1")

    # The UI-test target's Sources phase must list every UITests *.swift once.
    targets = {
        identifier: re.search(r"name = ([^;]+);", block)
        for identifier, block in body.items()
        if "isa = PBXNativeTarget" in block
    }
    target_name = ""
    if arguments.target in targets and targets[arguments.target]:
        target_name = unquote(targets[arguments.target].group(1))
    else:
        problems.append(f"target {arguments.target} not found")

    source_ids = [
        identifier for identifier, block in body.items() if "isa = PBXSourcesBuildPhase" in block
    ]
    ui_phase = None
    for phase_id in source_ids:
        block = body[phase_id]
        if target_name and phase_id in _target_phases(body, arguments.target):
            ui_phase = phase_id
            break
    listing: list[str] = []
    if ui_phase is None:
        problems.append(f"no Sources phase found for target {target_name or arguments.target}")
    else:
        entries = phases(body)[ui_phase]
        listed = []
        for item in entries:
            reference = build_files.get(item)
            if reference is None:
                problems.append(
                    f"Sources phase {ui_phase} lists {item} which is not a PBXBuildFile"
                )
                continue
            listed.append(file_refs.get(reference, reference))
        listing = listed
        expected = sorted(
            f"OmakasePlusTVUITests/{path.name}"
            for path in (arguments.source_root / "OmakasePlusTVUITests").glob("*.swift")
        )
        for name in listed:
            if name.endswith(".swift") and name not in expected:
                problems.append(f"Sources phase lists {name}, which is not a UITests source")
        for name in expected:
            count = listed.count(name)
            if count != 1:
                problems.append(f"Sources phase lists {name} {count} times, want 1")
        if len(entries) != len(set(entries)):
            problems.append(f"Sources phase {ui_phase} repeats a build file id")

    report = {
        "project": str(arguments.project),
        "target": target_name,
        "sources_phase": ui_phase,
        "sources": listing,
        "objects": len(body),
        "build_files": len(build_files),
        "file_references": len(file_refs),
        "problems": problems,
        "ok": not problems,
    }
    if arguments.json:
        arguments.json.parent.mkdir(parents=True, exist_ok=True)
        arguments.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"project: {arguments.project}")
    print(f"target:  {target_name or arguments.target}")
    print(f"phase:   {ui_phase}")
    for name in listing:
        print(f"  - {name}")
    print(f"objects: {len(body)}  build files: {len(build_files)}  file refs: {len(file_refs)}")
    if problems:
        for problem in problems:
            print(f"PROBLEM {problem}", file=sys.stderr)
        print("RESULT FAIL")
        return 1
    print("RESULT OK")
    return 0


def _target_phases(body: dict[str, str], target: str) -> set[str]:
    block = body.get(target, "")
    match = re.search(r"buildPhases = \((.*?)\)", block)
    return set(re.findall(r"[0-9A-F]{24}", match.group(1))) if match else set()


if __name__ == "__main__":
    sys.exit(main())
