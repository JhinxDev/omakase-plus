"""Playback provider registry and the household playback selection policy.

This is the single place that knows playback provider identifiers, what each
provider can do, and how a saved choice resolves for one household. Call sites
ask capability or route questions instead of comparing provider ids, so adding
a provider is one module (its link check and route) plus one entry in
``PROVIDERS``. See docs/PROVIDERS.md for the capability matrix.

Selection is deterministic:

1. A saved choice wins while its link is present.
2. A saved choice whose link is missing falls back to the auto order and keeps
   the reason, so the caller can explain the change.
3. The auto order is Real-Debrid, then Plex, then local storage. Real-Debrid is
   therefore the default whenever the household has connected an account, and
   Plex is used only by households that linked Plex and did not pick another
   provider explicitly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omakase.plus import connections

AUTO = "auto"
CLOUD_ROUTE = "cloud"
PUBLICATION_ROUTE = "publication"
LOCAL_ROUTE = "local"

PROVIDER_KEY = "preferred_playback_provider"


@dataclass(frozen=True, slots=True)
class PlaybackCapabilities:
    """The behaviour one household may rely on from a playback provider."""

    catalog: bool
    play: bool
    subtitles: bool
    seek: bool
    direct_file: bool
    requires_token: bool
    requires_link: bool


@dataclass(frozen=True, slots=True)
class Provider:
    """One playback provider with its registry data and link check."""

    id: str
    label: str
    auto_rank: int
    route: str
    capabilities: PlaybackCapabilities
    linked: Callable[[Any, int], bool]


def realdebrid_linked(db, user_id: int) -> bool:
    """A Real-Debrid account key is the whole link; there is no server to pair."""
    if not connections.private_connections_enabled():
        return False
    return bool(connections._read(db, user_id, "realdebrid_api_key"))


def plex_linked(db, user_id: int) -> bool:
    """Plex needs both a server address and a token to be a usable link."""
    return bool(
        connections._read(db, user_id, "plex_url") and connections._read(db, user_id, "plex_token")
    )


def local_linked(db, user_id: int) -> bool:
    """A finished local import registers the household's storage backend."""
    row = db.execute(
        "SELECT 1 FROM media_providers "
        "WHERE user_id=? AND kind='local_import' AND enabled=1 LIMIT 1",
        (user_id,),
    ).fetchone()
    return row is not None


PROVIDERS: dict[str, Provider] = {
    "realdebrid": Provider(
        id="realdebrid",
        label="Real-Debrid",
        auto_rank=0,
        route=CLOUD_ROUTE,
        capabilities=PlaybackCapabilities(
            catalog=True,
            play=True,
            subtitles=True,
            seek=True,
            direct_file=False,
            requires_token=True,
            requires_link=False,
        ),
        linked=realdebrid_linked,
    ),
    "plex": Provider(
        id="plex",
        label="Plex",
        auto_rank=1,
        route=PUBLICATION_ROUTE,
        capabilities=PlaybackCapabilities(
            catalog=True,
            play=True,
            subtitles=True,
            seek=True,
            direct_file=False,
            requires_token=True,
            requires_link=True,
        ),
        linked=plex_linked,
    ),
    "local": Provider(
        id="local",
        label="Local storage or NAS",
        auto_rank=2,
        route=LOCAL_ROUTE,
        capabilities=PlaybackCapabilities(
            catalog=True,
            play=False,
            subtitles=False,
            seek=False,
            direct_file=True,
            requires_token=False,
            requires_link=True,
        ),
        linked=local_linked,
    ),
}

AUTO_ORDER: tuple[Provider, ...] = tuple(
    sorted(PROVIDERS.values(), key=lambda provider: provider.auto_rank)
)
PLAYBACK_PROVIDERS = frozenset({AUTO, *PROVIDERS})


@dataclass(frozen=True, slots=True)
class Selection:
    """The one provider a household plays from, plus why it was chosen."""

    provider: Provider | None
    saved: str
    source: str
    reason: str = ""

    @property
    def id(self) -> str:
        return self.provider.id if self.provider is not None else ""

    @property
    def label(self) -> str:
        return self.provider.label if self.provider is not None else "No media provider"

    @property
    def route(self) -> str:
        return self.provider.route if self.provider is not None else ""

    @property
    def cloud_route(self) -> bool:
        """Playback is served from the provider's own cloud file inventory."""
        return self.route == CLOUD_ROUTE

    @property
    def publication_route(self) -> bool:
        """Playback is served from the managed Plex publication."""
        return self.route == PUBLICATION_ROUTE

    def supports(self, capability: str) -> bool:
        if self.provider is None:
            return False
        return bool(getattr(self.provider.capabilities, capability, False))


def get(provider_id: str) -> Provider | None:
    return PROVIDERS.get(provider_id)


def saved_choice(db, user_id: int) -> str:
    """Return the saved provider id, or ``auto`` when unset or unrecognised."""
    saved = connections._read(db, user_id, PROVIDER_KEY).strip().casefold()
    return saved if saved in PLAYBACK_PROVIDERS else AUTO


def linked_providers(db, user_id: int) -> tuple[Provider, ...]:
    """Every provider whose link requirement is satisfied, in auto order."""
    return tuple(provider for provider in AUTO_ORDER if provider.linked(db, user_id))


def _auto_provider(db, user_id: int) -> Provider | None:
    for provider in AUTO_ORDER:
        if provider.linked(db, user_id):
            return provider
    return None


def select(db, user_id: int) -> Selection:
    """Resolve one deterministic provider for this household."""
    saved = saved_choice(db, user_id)
    if saved != AUTO:
        chosen = PROVIDERS[saved]
        if chosen.linked(db, user_id):
            return Selection(chosen, saved, "saved")
        return Selection(
            _auto_provider(db, user_id),
            saved,
            "unavailable",
            f"{chosen.label} is not connected.",
        )
    return Selection(_auto_provider(db, user_id), saved, "auto")


def playback_provider(db, user_id: int) -> str:
    """Legacy provider id for saved settings, falling back to ``auto``."""
    return select(db, user_id).id or AUTO


def shared_publication() -> Selection:
    """Shared household libraries play through the managed publication."""
    return Selection(PROVIDERS["plex"], "household", "household")


def provider_options() -> tuple[tuple[str, str], ...]:
    """Settings choices in presentation order: automatic first, then registry."""
    return ((AUTO, "Automatic"),) + tuple((provider.id, provider.label) for provider in AUTO_ORDER)
