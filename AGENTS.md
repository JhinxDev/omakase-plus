# Omakase Plus contributor and agent instructions

Read README.md and CONTRIBUTING.md before changing the application. This is a
Python/FastAPI/SQLite app with an optional JSON CLI and MCP server. Source is in
src/omakase; tests are in tests. Dependencies are locked in uv.lock.

- Preserve concurrent work. Keep changes scoped; never reset or broadly discard
  files to make a checkout clean.
- Use `uv sync --frozen --extra dev`, then relevant pytest and Ruff checks.
  New dependencies need a reproducible lock update and relevant security checks.
- Keep accounts owner-scoped, secrets encrypted, CSRF checks intact, network reads
  bounded and source/import directories read-only.
- Watch progress, availability, catalog metadata and playback proof are separate.
  Shared/adopted media must remain protected. Use exact IDs and numbering contracts.
- Use synthetic fixtures. No test or code edit authorizes provider mutations,
  paid model calls, server access, publication or deployment.
- UI changes need relevant desktop, phone, keyboard and reduced-motion checks.
  DESIGN.md describes the current interface contract.
- Never commit runtime databases, credentials, account exports, private service
  endpoints or personal screenshots. Follow SECURITY.md and third-party notices.
- `OMAKASE_PLUS_PRIVATE` controls authenticated deployment access. Public source
  is not permission to remove that boundary or expose an installation publicly.

Use docs/getting-started.md for installation and docs/release-checklist.md for
release verification. Site-specific access and recovery records belong outside
this repository.
