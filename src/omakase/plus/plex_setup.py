"""Dry-run-first Plex library setup without section deletion."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from omakase.plus.plex import PlexClient, PlexError, PlexSection

PLEX_SETUP_PLAN_SCHEMA = "plex-setup-plan-v1"
_SECTION_TYPES = frozenset({"movie", "show"})


class PlexSetupError(ValueError):
    """Observed Plex topology cannot be safely converged."""


@dataclass(frozen=True, slots=True)
class PlexLibrarySpec:
    title: str
    section_type: str
    path: str

    def __post_init__(self) -> None:
        title = self.title.strip()
        if not title or len(title) > 80:
            raise PlexSetupError("Plex library title is invalid")
        if self.section_type not in _SECTION_TYPES:
            raise PlexSetupError("Plex library type is invalid")
        if (
            not self.path.startswith("/")
            or self.path.endswith("/")
            or "/../" in f"{self.path}/"
            or "/./" in f"{self.path}/"
        ):
            raise PlexSetupError("Plex library path is invalid")
        object.__setattr__(self, "title", title)


def plan_plex_setup(
    sections: tuple[PlexSection, ...],
    *,
    libraries: tuple[PlexLibrarySpec, ...],
) -> dict[str, Any]:
    """Plan only required creates/path updates; never propose a deletion."""
    if not libraries:
        raise PlexSetupError("at least one Plex library is required")
    title_keys = [item.title.casefold() for item in libraries]
    if len(set(title_keys)) != len(title_keys):
        raise PlexSetupError("desired Plex library titles must be unique")
    path_keys = [item.path.casefold() for item in libraries]
    if len(set(path_keys)) != len(path_keys):
        raise PlexSetupError("desired Plex library paths must be unique")
    existing_by_title: dict[str, PlexSection] = {}
    for section in sections:
        key = section.title.casefold()
        if key in existing_by_title:
            raise PlexSetupError("Plex contains duplicate section titles")
        existing_by_title[key] = section

    actions: list[dict[str, Any]] = []
    for spec in sorted(libraries, key=lambda item: item.title.casefold()):
        existing = existing_by_title.get(spec.title.casefold())
        if existing is None:
            actions.append(
                {
                    "kind": "create",
                    "section_key": None,
                    "title": spec.title,
                    "section_type": spec.section_type,
                    "current_paths": [],
                    "proposed_path": spec.path,
                }
            )
            continue
        if existing.section_type != spec.section_type:
            raise PlexSetupError("existing Plex library has the wrong media type")
        if existing.paths != (spec.path,):
            actions.append(
                {
                    "kind": "update_path",
                    "section_key": existing.key,
                    "title": spec.title,
                    "section_type": spec.section_type,
                    "current_paths": list(existing.paths),
                    "proposed_path": spec.path,
                }
            )

    unsigned = {
        "schema_version": PLEX_SETUP_PLAN_SCHEMA,
        "observed_sections": [
            {
                "key": section.key,
                "title": section.title,
                "section_type": section.section_type,
                "agent": section.agent,
                "scanner": section.scanner,
                "language": section.language,
                "paths": list(section.paths),
            }
            for section in sorted(sections, key=lambda item: item.key)
        ],
        "libraries": [
            {
                "title": item.title,
                "section_type": item.section_type,
                "path": item.path,
            }
            for item in sorted(libraries, key=lambda item: item.title.casefold())
        ],
        "actions": actions,
    }
    return {**unsigned, "plan_hash": _hash(unsigned)}


def apply_plex_setup_plan(
    client: PlexClient,
    *,
    plan: object,
    expected_plan_hash: str,
) -> dict[str, Any]:
    """Re-observe, apply exact non-destructive actions, then request partial scans."""
    validated = _validated_plan(plan)
    if not hmac.compare_digest(str(validated["plan_hash"]), expected_plan_hash.casefold()):
        raise PlexSetupError("approved Plex setup plan hash does not match")
    libraries = tuple(PlexLibrarySpec(**item) for item in validated["libraries"])
    current_sections = client.list_sections()
    observed = plan_plex_setup(current_sections, libraries=libraries)
    if observed != validated:
        raise PlexSetupError("Plex topology changed after setup planning")
    sections_by_key = {section.key: section for section in current_sections}
    applied: list[dict[str, Any]] = []
    try:
        for action in validated["actions"]:
            if action["kind"] == "create":
                client.create_section(
                    title=str(action["title"]),
                    section_type=str(action["section_type"]),
                    path=str(action["proposed_path"]),
                )
            elif action["kind"] == "update_path":
                section = sections_by_key.get(int(action["section_key"]))
                if section is None:
                    raise PlexSetupError("planned Plex section disappeared")
                client.update_section_path(section, path=str(action["proposed_path"]))
            else:
                raise PlexSetupError("Plex setup action is unsupported")
            applied.append(dict(action))

        converged = client.list_sections()
        converged_by_title = {section.title.casefold(): section for section in converged}
        for library in libraries:
            section = converged_by_title.get(library.title.casefold())
            if (
                section is None
                or section.section_type != library.section_type
                or section.paths != (library.path,)
            ):
                raise PlexSetupError("Plex libraries did not converge after setup")
            client.scan_section(section.key, path=library.path)
    except PlexError as exc:
        raise PlexSetupError("Plex setup request failed") from exc
    return {
        "plan_hash": validated["plan_hash"],
        "actions_applied": len(applied),
        "scans_requested": len(libraries),
    }


def _validated_plan(plan: object) -> dict[str, Any]:
    expected = {
        "schema_version",
        "observed_sections",
        "libraries",
        "actions",
        "plan_hash",
    }
    if (
        not isinstance(plan, dict)
        or set(plan) != expected
        or plan.get("schema_version") != PLEX_SETUP_PLAN_SCHEMA
    ):
        raise PlexSetupError("Plex setup plan is invalid")
    unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
    supplied = plan.get("plan_hash")
    if (
        not isinstance(supplied, str)
        or len(supplied) != 64
        or not hmac.compare_digest(supplied, _hash(unsigned))
    ):
        raise PlexSetupError("Plex setup plan hash is invalid")
    if not isinstance(plan.get("libraries"), list) or not isinstance(plan.get("actions"), list):
        raise PlexSetupError("Plex setup plan entries are invalid")
    return plan


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
