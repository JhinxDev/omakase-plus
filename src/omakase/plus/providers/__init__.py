"""Provider-neutral acquisition contracts and the playback provider registry."""

from omakase.plus.providers.contracts import (
    AcquisitionProvider,
    ProviderBlock,
    ProviderCapabilities,
    ProviderFile,
    ProviderHealth,
    ProviderItem,
)
from omakase.plus.providers.local import LocalImportProvider
from omakase.plus.providers.playback import (
    AUTO,
    AUTO_ORDER,
    CLOUD_ROUTE,
    LOCAL_ROUTE,
    PLAYBACK_PROVIDERS,
    PROVIDER_KEY,
    PROVIDERS,
    PUBLICATION_ROUTE,
    PlaybackCapabilities,
    Selection,
    get,
    linked_providers,
    playback_provider,
    provider_options,
    saved_choice,
    select,
    shared_publication,
)
from omakase.plus.providers.qbittorrent import QBittorrentProvider

__all__ = [
    "AcquisitionProvider",
    "ProviderBlock",
    "ProviderCapabilities",
    "ProviderFile",
    "ProviderHealth",
    "ProviderItem",
    "LocalImportProvider",
    "QBittorrentProvider",
    "AUTO",
    "AUTO_ORDER",
    "CLOUD_ROUTE",
    "LOCAL_ROUTE",
    "PLAYBACK_PROVIDERS",
    "PROVIDER_KEY",
    "PROVIDERS",
    "PUBLICATION_ROUTE",
    "PlaybackCapabilities",
    "Selection",
    "get",
    "linked_providers",
    "playback_provider",
    "provider_options",
    "saved_choice",
    "select",
    "shared_publication",
]
