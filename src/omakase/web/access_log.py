"""Redact request-path credentials before Uvicorn formats its log record."""

from __future__ import annotations

import logging
import re

_DIRECT_PATH = re.compile(
    r"(/api/native-tv-test/v1/direct)(?:/[^\s?#]*)?(?:\?[^\s#]*)?(?:#[^\s]*)?(?=$|[\s\"'])"
)


def redact_direct_path(value: str) -> str:
    return _DIRECT_PATH.sub(r"\1/[redacted]", value)


class _DirectAccessFilter(logging.Filter):
    def filter(self, record):
        args = record.args
        # Both Uvicorn HTTP implementations use the same five access arguments.
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path = redact_direct_path(args[2])
            if path != args[2]:
                record.args = (*args[:2], path, *args[3:])
        return True


_FILTER = _DirectAccessFilter()


def protect_access_log() -> None:
    logging.getLogger("uvicorn.access").addFilter(_FILTER)
