"""Source adapters for anime data (AniList, MyAnimeList, etc.)."""

# Import adapters to trigger @register decorators
from omakase.adapters.anilist import AniListAdapter  # noqa: F401
from omakase.adapters.base import get_adapter as get_adapter
from omakase.adapters.base import list_sources as list_sources
from omakase.adapters.myanimelist import MALAdapter  # noqa: F401
