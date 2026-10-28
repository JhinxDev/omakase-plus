"""Omakase — an LLM-powered anime sommelier."""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.4.0"

# Load .env file at import time so CLI and web entry points pick up env vars
# without needing python-dotenv or manual export. Docker compose handles this
# separately via env_file; this is for local dev only.
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _value = _line.partition("=")
        _key = _key.strip()
        _value = _value.strip().strip('"').strip("'")
        if _key and _key not in os.environ:
            os.environ[_key] = _value
