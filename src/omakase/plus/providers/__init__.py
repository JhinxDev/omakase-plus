"""Provider-neutral acquisition contracts and concrete adapters."""

from omakase.plus.providers.contracts import (
    AcquisitionProvider,
    ProviderBlock,
    ProviderCapabilities,
    ProviderFile,
    ProviderHealth,
    ProviderItem,
)
from omakase.plus.providers.local import LocalImportProvider
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
]
