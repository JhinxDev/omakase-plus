"""Every environment variable read by the source must be documented."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs" / "environment.md"
PATTERNS = (
    re.compile(r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]+)["']"""),
    re.compile(r"""envvar=["']([A-Z][A-Z0-9_]+)["']"""),
)


def _documented_names() -> set[str]:
    text = REFERENCE.read_text(encoding="utf-8")
    return set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", text))


def _source_names() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in PATTERNS:
            names.update(pattern.findall(text))
    return names


def test_all_environment_variables_are_documented():
    missing = sorted(_source_names() - _documented_names())
    assert missing == [], "Add these environment variables to docs/environment.md: " + ", ".join(
        missing
    )


def test_reference_does_not_document_unknown_variables():
    # Guard against a typo that would silently document a variable the source
    # never reads. A new source variable must be added here or in the source.
    known = _source_names()
    documented = {
        name
        for name in _documented_names()
        if name.startswith(("OMAKASE_", "ANILIST_", "MAL_", "GOOGLE_"))
    }
    unknown = sorted(documented - known)
    assert unknown == [], "Remove these undocumented source variables: " + ", ".join(unknown)
