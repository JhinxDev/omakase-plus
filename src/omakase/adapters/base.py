"""Abstract base class for source adapters + registry."""

from abc import ABC, abstractmethod

from omakase.types import SourceData

_REGISTRY: dict[str, type["SourceAdapter"]] = {}


def register(name: str):
    """Decorator to register a source adapter."""

    def wrapper(cls: type[SourceAdapter]) -> type[SourceAdapter]:
        _REGISTRY[name] = cls
        return cls

    return wrapper


def get_adapter(name: str) -> "SourceAdapter":
    """Get an adapter instance by name."""
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown source '{name}'. Available: {available}")
    return _REGISTRY[name]()


def list_sources() -> list[str]:
    return sorted(_REGISTRY)


class SourceAdapter(ABC):
    """Fetch a user's history and a candidate pool from a media platform."""

    name: str = "base"

    @abstractmethod
    def fetch(self, username: str, pool_size: int = 100, **kwargs) -> SourceData:
        """Fetch history + candidates for a user.

        Args:
            username: The user's identifier on this platform.
            pool_size: Number of candidate items to fetch.
            **kwargs: Adapter-specific options (e.g. use_planning).

        Returns:
            SourceData with history (scored items) and candidates (unwatched items).
        """
        ...
