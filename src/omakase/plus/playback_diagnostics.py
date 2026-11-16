"""Playback warning fields contain fixed classifications and local correlation IDs."""

import re
import secrets

_DIAGNOSTIC_ID = re.compile(r"[a-f0-9]{16}")


def diagnostic_id():
    return secrets.token_hex(8)


def warn(logger, stage, code, mode, correlation):
    if not isinstance(correlation, str) or not _DIAGNOSTIC_ID.fullmatch(correlation):
        correlation = None
    logger.warning(
        "playback stage=%s code=%s delivery_mode=%s request_id=%s",
        stage,
        code,
        mode,
        correlation,
        extra={"stage": stage, "code": code, "delivery_mode": mode, "request_id": correlation},
    )
