"""Encrypted per-user configuration for optional catalog expansion."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from omakase.plus.animetosho import AnimeToshoCatalog
from omakase.plus.catalog import CatalogSource
from omakase.plus.prowlarr import (
    ProwlarrCatalog,
    ProwlarrConfigurationError,
    normalize_base_url,
    parse_positive_ids,
    private_catalog_urls_allowed,
)
from omakase.plus.secrets import read_secret

_DEFAULT_CATEGORIES = (5070, 5000)


@dataclass(frozen=True, slots=True)
class CatalogConfiguration:
    """Secret-free readiness facts plus runtime catalog adapters."""

    state: str
    label: str
    detail: str
    sources: tuple[CatalogSource, ...] = field(default=(), repr=False)


def configured_catalogs(db, user_id: int) -> CatalogConfiguration:
    """Build optional catalog sources without returning stored values to callers."""
    base_url = read_secret(db, user_id, "prowlarr_url") or ""
    public_sources = (
        (AnimeToshoCatalog(),)
        if os.getenv("OMAKASE_ANIMETOSHO_ENABLED", "").casefold() == "true"
        else ()
    )
    api_key = read_secret(db, user_id, "prowlarr_api_key") or ""
    if not base_url and not api_key:
        return CatalogConfiguration(
            state="ready" if public_sources else "optional",
            label="Expanded search active" if public_sources else "Built-in source",
            detail="Nyaa and AnimeTosho are active."
            if public_sources
            else "Nyaa is active. Add Prowlarr to search more configured sources.",
            sources=public_sources,
        )
    if not base_url or not api_key:
        return CatalogConfiguration(
            state="incomplete",
            label="Prowlarr needs setup",
            detail="Both the Prowlarr server address and API key are required.",
            sources=public_sources,
        )

    try:
        normalized_url = normalize_base_url(base_url)
        indexer_ids = parse_positive_ids(read_secret(db, user_id, "prowlarr_indexer_ids"))
        categories = parse_positive_ids(
            read_secret(db, user_id, "prowlarr_categories"),
            default=_DEFAULT_CATEGORIES,
        )
    except ProwlarrConfigurationError:
        return CatalogConfiguration(
            state="invalid",
            label="Prowlarr settings need attention",
            detail="Replace the saved Prowlarr address or numeric source filters.",
            sources=public_sources,
        )

    source = ProwlarrCatalog(
        base_url=normalized_url,
        api_key=api_key,
        indexer_ids=indexer_ids,
        categories=categories,
        allow_private=private_catalog_urls_allowed(),
    )
    return CatalogConfiguration(
        state="ready",
        label="Expanded search active",
        detail="Nyaa and every enabled Prowlarr source are searched and deduplicated."
        + (" AnimeTosho is also active." if public_sources else ""),
        sources=(source, *public_sources),
    )
