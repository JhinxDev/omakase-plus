# Contributing to Omakase Plus

This early release combines anime discovery/tracking, movie
workflows, TV inventory and an authenticated self-hosted media interface.
See README.md for implemented scope and current release limits.

## Set up and verify

Use Python 3.11+ and uv. From the checkout:

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev python -m pytest
uv run --frozen --extra dev python -m ruff check .
uv run --frozen --extra dev python -m ruff format --check .
uv run --frozen omakase --help
```

Use a separate branch and a focused pull request against this repository. Describe
the concrete behavior, affected routes, tests and remaining limits. Preserve
unrelated changes. Use synthetic fixtures and never include account databases,
credentials, private URLs, provider responses or personal viewing history.

Preserve per-account access, encrypted secrets, CSRF protection, bounded network
requests, read-only imports and explicit media mutation boundaries. A test pass
does not authorize live provider work, inference spending or deployment.

## Extension points

- LLM adapters implement BaseLLM in src/omakase/llm/base.py and register in
  src/omakase/llm/__init__.py. Add focused adapter tests and document configuration.
- History adapters implement SourceAdapter under src/omakase/adapters and use its
  registry. Verify exact media identity and distinguish recorded from observed viewing.
- Domain services and authenticated routes live in src/omakase/plus. Maintain
  compatibility with migrated databases and preserved watch history.

UI changes need affected desktop/phone, keyboard, reduced-motion and readable
contrast checks. Use DESIGN.md and docs/browser-qa.md. Tests of a fake provider
are not actual playback, physical-device or production acceptance.

Keep user guides aligned with implementation. TV acquisition is not automated;
movie Plex history import is explicit. Do not claim unsupported capabilities.
Retain MIT copyright attribution, the Geist font license, and third-party asset
provenance. See SECURITY.md and THIRD-PARTY-NOTICES.md.
