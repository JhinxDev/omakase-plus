"""Validate release tags against package metadata and render GitHub Release notes."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import tomllib

SEMVER_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")
INIT_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
CHANGELOG_HEADING_RE = re.compile(r"^##\s+([^\n]+)$", re.MULTILINE)
QUICKSTART_URL = "https://omakase-plus.jhinx.dev/docs/quickstart"
REPO_URL = "https://github.com/JhinxDev/omakase-plus"
GHCR_IMAGE = "ghcr.io/jhinxdev/omakase-plus"


def parse_release_tag(tag: str) -> str:
    """Validate a ``vX.Y.Z`` release tag and return the bare version string."""
    match = SEMVER_TAG_RE.fullmatch(tag.strip())
    if not match:
        raise ValueError(f"Release tag must follow vMAJOR.MINOR.PATCH (got {tag!r})")
    return match.group(1)


def read_repo_versions(repo_root: Path) -> dict[str, str]:
    """Read synchronized version fields from package metadata and ``uv.lock``."""
    pyproject_data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject_version = str(pyproject_data["project"]["version"])

    init_text = (repo_root / "src" / "omakase" / "__init__.py").read_text(encoding="utf-8")
    init_match = INIT_VERSION_RE.search(init_text)
    if not init_match:
        raise ValueError("Could not locate __version__ in src/omakase/__init__.py")
    init_version = init_match.group(1)

    lock_data = tomllib.loads((repo_root / "uv.lock").read_text(encoding="utf-8"))
    lock_version: str | None = None
    for pkg in lock_data.get("package", []):
        if pkg.get("name") == "omakase":
            lock_version = str(pkg.get("version", ""))
            if pkg.get("source", {}).get("editable") == ".":
                break
    if not lock_version:
        raise ValueError("Could not locate editable omakase package in uv.lock")

    return {
        "pyproject.toml": pyproject_version,
        "src/omakase/__init__.py": init_version,
        "uv.lock": lock_version,
    }


def verify_tag_matches_repo(tag: str, repo_root: Path) -> str:
    """Ensure the release tag matches ``pyproject.toml``, ``__version__``, and ``uv.lock``."""
    version = parse_release_tag(tag)
    versions = read_repo_versions(repo_root)
    mismatches = [f"{name}={found}" for name, found in versions.items() if found != version]
    if mismatches:
        joined = ", ".join(mismatches)
        raise ValueError(f"Tag {tag} does not match repository versions: {joined}")
    return version


def extract_changelog_section(changelog_text: str, version: str) -> str:
    """Extract the body of ``## <version>`` from ``CHANGELOG.md``."""
    matches = list(CHANGELOG_HEADING_RE.finditer(changelog_text))
    section_start: int | None = None
    section_end = len(changelog_text)
    prefix = re.compile(rf"^{re.escape(version)}(?:\s|$)")

    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        if section_start is None and prefix.match(heading):
            section_start = match.end()
            if index + 1 < len(matches):
                section_end = matches[index + 1].start()
            break

    if section_start is None:
        raise ValueError(f"CHANGELOG.md has no section for version {version}")

    body = changelog_text[section_start:section_end].strip()
    if not body:
        raise ValueError(f"CHANGELOG.md section for version {version} is empty")
    return body


def build_release_notes(tag: str, repo_root: Path) -> str:
    """Verify repository versions against ``tag`` and build Markdown release notes."""
    normalized_tag = tag.strip()
    version = verify_tag_matches_repo(normalized_tag, repo_root)
    changelog_text = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    changelog_body = extract_changelog_section(changelog_text, version)

    image_lines = [
        "## Container image",
        "",
        f"Pull the prebuilt container image from GHCR (`{GHCR_IMAGE}:{version}`):",
        "",
        "```sh",
        f"docker pull {GHCR_IMAGE}:{version}",
    ]
    if "-" not in version:
        image_lines.append(f"docker pull {GHCR_IMAGE}:latest")
    image_lines.append("```")

    sections = [
        changelog_body,
        "",
        "\n".join(image_lines),
        "",
        "## Upgrade",
        "",
        "Back up your app data directory and encryption key before upgrading.",
        "",
        f"[Quickstart]({QUICKSTART_URL}) · [Changelog]({REPO_URL}/blob/{normalized_tag}/CHANGELOG.md)",
    ]
    return "\n".join(sections).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Release Git tag (for example v0.5.0)")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Repository root containing pyproject.toml, uv.lock, and CHANGELOG.md",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional Markdown file path to write rendered release notes",
    )
    args = parser.parse_args(argv)
    notes = build_release_notes(args.tag, args.repo_root.resolve())
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(notes, encoding="utf-8")
        print(f"Wrote release notes for {args.tag} to {args.output}")
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
