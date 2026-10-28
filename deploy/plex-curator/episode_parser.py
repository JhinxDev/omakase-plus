"""Compatibility import for the maintained shared publication episode parser."""

from omakase.plus.publication_episodes import (
    EpisodeParseError,
    EpisodeRef,
    parse_file_episode,
)

__all__ = ["EpisodeParseError", "EpisodeRef", "parse_file_episode"]
