# Omakase Plus — Agent & Contributor Instructions

Self-hosted anime, movie, and TV library command center (Python/FastAPI/SQLite backend + native SwiftUI `ios-source` & `tv-source` apps). Read `README.md`, `CONTRIBUTING.md`, and `DESIGN.md` before changing the application.

## Stack & Layout

- **Backend / Web:** Python 3.11+, FastAPI, SQLite (WAL), Jinja2 templates, locked via `uv.lock` (`src/omakase/`, `tests/`).
- **Native Apple:** `ios-source/` (iPhone) and `tv-source/` (Apple TV), built via GitHub Actions TestFlight workflows.
- **Ops & Deploy:** `ops/`, `deploy/`, `scripts/`, `compose-plus.yaml`.

## Verify & Run Commands

```powershell
uv sync --frozen --extra dev
uv run --frozen --extra dev pytest
uv run --frozen --extra dev ruff check .
```

## Active Skills & Tools

- **Apple / SwiftUI:** `swiftui-expert` (`ios-source` and `tv-source`).
- **Web UI & Quality:** `modern-web-guidance`, `hallmark`, `playwright`.
- **Security & CI:** `security-audit`, `gh-fix-ci`, `gh-address-comments`, `humanizer`.

## Guardrails & Security

- Preserve concurrent work and keep changes scoped.
- Keep accounts owner-scoped, secrets encrypted, CSRF checks intact, network reads bounded, and source/import directories read-only.
- Watch progress, availability, catalog metadata, and playback proof are separate; protect shared/adopted media and keep exact IDs and numbering contracts.
- Use synthetic fixtures in tests; never commit runtime databases, credentials, account exports, private endpoints, or personal screenshots. Follow `SECURITY.md` and `THIRD-PARTY-NOTICES.md`.
- `OMAKASE_PLUS_PRIVATE` controls authenticated deployment access; never weaken or bypass that boundary.
- UI changes require desktop, phone, keyboard, and reduced-motion verification matching `DESIGN.md`.

## Release & Deploy

- Follow SemVer across `pyproject.toml`, package metadata, `uv.lock`, and `CHANGELOG.md`; merge via green PR CI and push an annotated `vX.Y.Z` tag so automated GitHub Releases, GHCR container images, and TestFlight workflows run cleanly.
- Use `docs/getting-started.md` for setup and `docs/release-checklist.md` for release verification; keep host-specific access details outside this repository.
