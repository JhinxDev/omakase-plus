# About & updates

Omakase Plus has its own date-based product release identifier in
`src/omakase/plus/update_service.py`. It is independent of the generic Omakase
CLI/package version (`0.4.0`). Update the product constant with each Plus release.

Authenticated users of a private Plus deployment can open Settings → About &
updates (`/plus/settings/updates`). Opening the page never contacts the internet.
The explicit CSRF-protected Check for updates action makes one bounded HTTPS GET
request to `https://jhinx.dev/updates/omakase-plus.json`. No user, account, library,
credential, installation ID or installed-version data is sent. The public server
necessarily receives the host IP and a fixed generic user agent. There is no
background polling or telemetry. Disable all checks with
`OMAKASE_UPDATE_CHECKS=false`; viewing the installed version still works.

This feature only displays release information. It never pulls source, executes
commands, downloads software, changes settings or installs anything. The case
study at `https://jhinx.dev/work/omakase-plus` is release context, not a hosted
application. Source publication status and the intended source destination are
stated in the repository README. Administrators follow the manually qualified
[current-data update/recovery procedure](current-data-recovery.md) and
[release checklist](release-checklist.md). Legacy automatic upgrade/rollback
helpers are disabled; checking for updates does not authorize restoring an older
database or enabling unrestricted old writers.

## Public metadata contract

```json
{
  "schema_version": 1,
  "product": "omakase-plus",
  "channel": "stable",
  "version": "2026.10.29.1",
  "released_at": "2026-10-29T06:00:00Z",
  "notes_url": "https://jhinx.dev/work/omakase-plus",
  "summary": "Private Omakase Plus release information."
}
```

The exact seven fields are required; unknown or duplicate fields are rejected.
Versions use `YYYY.MM.DD.N` with a real calendar date and a positive numeric
revision (one to six digits). Comparison is numeric, including the revision.
Timestamps use a valid UTC `YYYY-MM-DDTHH:MM:SSZ`. Summaries are plain text,
1–1000 characters, without ASCII control characters or surrogate code points, and are HTML-escaped on
render. The notes URL is exactly allowlisted to the public HTTPS case-study URL;
URLs with credentials, alternate hosts, ports, queries or fragments are rejected.

Transport accepts only HTTP 200 application/json, no redirects or compressed
response, with a five-second HTTP timeout, ten-second overall deadline and 32 KiB
streamed response limit. Environment proxies are disabled. Transport, schema and
upstream errors use a fixed safe message without upstream bodies or exceptions.
The process-local shared cache retains the last successful release and labels it
as such after a failure. Every attempt, including failure, starts a minimum
15-minute cooldown; HTTP 429/503 Retry-After can extend it up to 24 hours.
Cooldown POSTs return cached results with HTTP 429 and Retry-After. A restart or
another application worker has its own cache. There are no automatic retries.

## Verification

`uv run --frozen --extra dev python -m pytest tests/plus/test_updates.py`
uses only mocked HTTP transports and verifies release comparison, strict parsing,
redirect/oversize/offline failures, caching and Retry-After, private authentication,
CSRF/body limits, safe rendering, cold GET and disabled-check privacy behavior.
Public-feed publication and actual deployed UI checks are separate release gates.
