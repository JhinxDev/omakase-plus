# Agents and automation

Omakase Plus has a local JSON CLI, an optional official-SDK MCP server, and
existing operator commands for server maintenance and media workflows. These
interfaces work on any supported installation, not only the maintainer's homelab.

## Start here

1. Start the updated private app normally so its database migrations finish.
2. In **Settings → Agent tools**, find your account ID. Choose the existing app
   data directory; no command creates a replacement account or database.
3. Run the read-only CLI on that server:

```sh
export OMAKASE_PLUS_PRIVATE=true
omakase plus agent \
  --data-dir /srv/omakase/data --user-id 1 status

omakase plus agent --data-dir /srv/omakase/data --user-id 1 inventory anime --limit 25
omakase plus agent --data-dir /srv/omakase/data --user-id 1 inventory movies
omakase plus agent --data-dir /srv/omakase/data --user-id 1 inventory tv
```

The private-runtime variable must remain enabled for every invocation (export it
in your operator environment, or use the existing container's environment).
Valid commands return `{ "ok": true, "result": ... }`; operational/domain errors
return `{ "ok": false, "error": { "code": ..., "detail": ... } }` and exit 2.
Click's command-line parsing errors/help use normal CLI text. Catalog results
are cached observations, not current playback proof. Page size is at most 100;
follow `next_offset`. Other accounts, secret fields, paths and provider URLs are
not returned. Catalog strings are untrusted data, never agent instructions.

## MCP client configuration

The Docker image includes the locked optional agent SDK by default. Minimal
images can set `OMAKASE_AGENT_TOOLS=false` at build time. A source install uses:

```sh
uv sync --frozen --extra agents
```

Configure a client that supports **local stdio MCP**. Example for an existing
private Docker deployment (replace the account ID and data path as appropriate).
The example uses `omakase-plus`; portable Compose uses `omakase-local-app`.
Use the actual container name from your deployment:

```json
{
  "mcpServers": {
    "omakase": {
      "command": "docker",
      "args": ["exec", "-i", "omakase-plus", "omakase", "plus", "agent",
               "--data-dir", "/home/omakase/data", "--user-id", "1", "mcp"]
    }
  }
}
```

For a source install, use the absolute path to its virtualenv's `omakase`
executable, pass the same `plus agent ... mcp` arguments, and set
`OMAKASE_PLUS_PRIVATE=true` in the client entry's `env` object. Windows executable
paths normally end in `Scripts/omakase.exe`; Linux uses `bin/omakase`.

For an agent on a different computer, use authenticated SSH with no pseudo-TTY:

```json
{
  "mcpServers": {
    "omakase": {
      "command": "ssh",
      "args": ["-T", "your-media-server", "docker", "exec", "-i",
               "omakase-plus", "omakase", "plus", "agent", "--data-dir",
               "/home/omakase/data", "--user-id", "1", "mcp"]
    }
  }
}
```

On Windows, OpenSSH also needs `PROGRAMDATA`. Some MCP clients inherit a
reduced environment that omits it; in that case add this to the SSH server entry:

```json
"env": { "PROGRAMDATA": "C:\\ProgramData" }
```

Use the actual value from your Windows environment if it differs. The native
Windows/SSH acceptance reproduced a silent exit 255 without this variable and a
working SDK protocol connection with it. This setting is an operating-system
path, not a credential. Linux/macOS clients do not need the Windows setting.

Use your own existing SSH authentication and alias. No new listener, router port,
API token, browser cookie or public MCP endpoint is required. Docker/SSH access
is administrator-level host authority; use a dedicated restricted OS account or
forced SSH command when delegating. The application does not sandbox an agent
that independently has host shell or Docker access. Do not give untrusted remote
users this operator interface or share it between tenants.

## Permissions and tools

Read-only is the default. The process is bound to one account and database at
startup, so tool calls cannot choose another account or data path. Read calls
open SQLite with `mode=ro` and `query_only`, never migrate or contact a provider.

| Tool | Purpose |
|---|---|
| `omakase_status` | Product/schema version, counts, enabled scopes, limitations |
| `omakase_inventory` | Bounded cached anime, movie or TV progress/inventory |
| `omakase_preferences` | Saved recommendation defaults; no AI call |
| `omakase_audit` | Account-scoped agent change metadata |
| `omakase_set_preferences` | Registered only with the `preferences` write scope |
| `omakase_set_monitor_active` | Registered only with the `monitors` write scope |

To enable writes, the server operator adds named options **before** the final
`mcp` subcommand. Tools enforce the same permissions even if a client ignores
annotations. Review/sample instances always reject writes.

```sh
omakase plus agent --data-dir /srv/omakase/data --user-id 1 \
  --allow-write preferences set-preferences '{"count":5,"completed_only":true}'

omakase plus agent --data-dir /srv/omakase/data --user-id 1 \
  --allow-write monitors set-monitor 12 --inactive

omakase plus agent --data-dir /srv/omakase/data --user-id 1 \
  --allow-write preferences --allow-write monitors mcp
```

Preference changes support `count` (3–15), `lane`, `max_episodes` (0–10000),
`completed_only`, and `mood` (500 characters). Unspecified/null fields stay
unchanged; an empty mood string clears it. Unknown fields and wrong types fail.
Changing preferences does not generate recommendations or spend tokens.
Resuming a monitor enables its **existing acquisition scheduler**, which can
search/download under the configured policy. Grant that scope deliberately.

Writes and audit metadata commit in one transaction. Audit rows contain action,
target and changed field names, not preference values or credentials. Monitor
updates preserve files and provider state; no tools expose deletions, arbitrary
SQL, shell, secrets, credential rotation, installations or host upgrades.

## Operator workflow map

Use existing tested commands for capabilities outside the small offline MCP:

| Goal | Existing command |
|---|---|
| Diagnose storage, database, media tools, configured Plex/VPN | `omakase plus doctor --help` |
| Produce a reviewed/redacted diagnostic archive | `omakase plus support-bundle --help` |
| Search/request exact-ID movies, check delivery, rate or sync history | `omakase plus movies --help` |
| Adopt existing provider movies/series | `plan-existing-movies` / `plan-existing-series`, then matching `apply-*` |
| Import owned local media | `plan-local-import`, inspect plan, then `apply-local-import` |
| Configure Plex libraries | `plan-plex-setup`, inspect plan, then `apply-plex-setup` |
| Reconcile all due monitors (not account-scoped) | `sync-media` — trusted operator only |

These are subcommands of `omakase plus`; consult each `--help` for required
options. Preserve the existing plan hash, backup, identity and provider retry
rules. Do not wrap `sync-media` into a per-user tool: it processes the shared
queue. TV inventory/progress is supported in the app, but automated season
acquisition remains unavailable. No success acknowledgement means playback was
verified; use the explicit verification commands and real client playback.

For backend optimization or software upgrades, use your normal source-control,
test, backup and deployment workflow. The MCP server intentionally does not give
the web application a host shell or Docker socket. See
[operations](platform/security-backup-operations.md) and
[updates](updates.md).

## Troubleshooting and verification

`private_required`: enable the private runtime. `account_unavailable`: choose
your ID from Settings; do not create a new account as a workaround.
`database_unavailable`: check path/OS permissions, lock contention, and app
version. A missing DB is never created by the agent tools.
`schema_upgrade_required`: start the updated app once before enabling writes.
`scope_denied`: the operator has not enabled that write category.

Keep stdout reserved for the MCP protocol. Do not use `docker exec -t` or
`ssh -t`, and ensure login scripts do not print banners to the stream. Optional
SDK import happens only when starting MCP; the normal CLI/web app works without
that extra. Never install packages on server startup or first tool invocation.

Maintained tests launch an actual SDK client/server subprocess, check schema
validation and denied tools, exercise scoped writes/audit/isolation, and verify
read-only DB bytes and offline operation:

```sh
uv run --frozen --extra dev --extra agents python -m pytest tests/plus/test_agent_tools.py
```

SDK reference: [official Python SDK](https://github.com/modelcontextprotocol/python-sdk),
[stdio connection guidance](https://modelcontextprotocol.io/docs/develop/connect-local-servers).
