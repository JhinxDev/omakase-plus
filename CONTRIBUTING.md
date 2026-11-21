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

## Branches and releases

- Start work from a current `origin/main` on a named `codex/` branch. Review
  open pull requests before editing a route, schema, workflow, or shared asset.
  Coordinate ownership first when several agents edit the same checkout.
- Run the affected tests locally and wait for the required GitHub checks on
  the exact pull request head. CI covers Python, Docker, browser navigation,
  and the existing quality gates. Use the tvOS workflow for an installable
  arm64 IPA and record the build number, source revision, and artifact SHA-256.
- Remove the feature branch after merging. Before pruning a divergent
  branch, compare its clean application changes with current main and preserve
  its exact tip in a verified archive if it still contains unique work.
  Keep shipped device revisions reachable for rollback.
- For a private deployment, rehearse new migrations against a protected copy
  and follow the release runbook. Verify the live revision, signed-in routes,
  provider receipts and device build. The automated tests and physical playback
  checks verify different parts of the release.

Preserve per-account access, encrypted secrets, CSRF protection, bounded network
requests, read-only imports and explicit media mutation boundaries. A test pass
does not authorize live provider work, inference spending or deployment.

## Extension points

- [Apple TV](docs/apple-tv.md) contains the build and verification steps for the
  app and Top Shelf extension in `tv-source`.
- The [iPhone prototype](ios-source/README.md) reuses the shared Swift package and
  has its own synthetic simulator workflow.

- LLM adapters implement BaseLLM in src/omakase/llm/base.py and register in
  src/omakase/llm/__init__.py. Add focused adapter tests and document configuration.
- History adapters implement SourceAdapter under src/omakase/adapters and use its
  registry. Verify exact media identity and distinguish recorded from observed viewing.
- Domain services and authenticated routes live in src/omakase/plus. Maintain
  compatibility with migrated databases and preserved watch history.

UI changes need affected desktop/phone, keyboard, reduced-motion and readable
contrast checks. Use DESIGN.md and docs/browser-qa.md. Tests of a fake provider
are not actual playback, physical-device or production acceptance.

Keep user guides aligned with implementation. TV requests can use Riven when
the operator enables TV intake. Show provider progress and verified library
availability independently. Plex history import for movies is an explicit action.
Retain MIT copyright attribution, the Geist font license, and third-party asset
provenance. See SECURITY.md and THIRD-PARTY-NOTICES.md.
