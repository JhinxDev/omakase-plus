# Set up with Claude Code or Codex

Open a coding agent on the computer that will run Omakase Plus. Paste the prompt below. It starts with the smallest useful setup and adapts to your host; you keep account passwords and provider keys out of chat.

| Your starting point | What the agent sets up |
| --- | --- |
| App alone (default) | One app, your account, anime search and personal planning. No provider account or Plex required; anime catalog search needs internet. |
| Existing media stack | The app plus connections you choose. Movie and TV catalog search needs Seerr; other services stay optional. |
| Optional Plex | The documented app/Plex profile after storage is ready. You sign in to and claim Plex yourself. |

```text
Set up Omakase Plus on this computer. Perform the installation and verify it; do not stop at a plan.
First detect an existing checkout, app container, data directory or occupied port; reuse a valid installation instead of cloning or starting a second app.
Use only https://github.com/JhinxDev/omakase-plus as the source; read its AGENTS.md if present, README.md and docs/getting-started.md before acting, and record the exact revision.
Ask which path I want: app alone (default), my existing stack, or optional Plex; ask only unresolved host, storage-path and access choices that materially affect setup.
Inspect the host OS, Git, Docker Engine/Desktop and Compose, Python 3, available storage and permissions; choose documented commands for that OS and explain any unsupported host requirement.
This prompt authorizes installation in the agreed scope; handle missing prerequisites through supported installer/approval flows, without bypassing runtime permissions.
Preserve existing databases, encryption keys, mounts, ports and backups; never destroy a VM, expose a Docker socket, overwrite an existing install or copy someone else's private deployment.
For a fresh install, create dedicated writable app data and a local master encryption key; write the key directly to the protected secret file without printing it or sending it to chat, logs or Git.
Validate the chosen Compose configuration, then build/start the documented services; use compose.standalone.yaml alone for app-only use and never run competing apps against one database.
Keep default loopback/private access and existing authentication, CSRF and URL-validation protections; do not open public access or alter credentials without my direction.
Do not add paid inference, provider accounts, downloads or media acquisition without my direction; app-only needs no provider account, and movie/TV catalog search requires optional Seerr.
Verify container health with the documented checks and the app's /plus/health endpoint; give me the correct browser URL for this host and private access route.
Have me create/sign in to my own account in the browser and complete Choose, Connect, Review, Enjoy; do not request passwords or API keys in chat or treat setup as permission to acquire media.
Check the selected setup's result without acquiring media, then report the source revision, URL, status, start/stop commands, data location and secure backup/restore, rollback and update steps.
If a required check fails, stop the affected step, preserve the working state and report the exact error and next action; do not invent success or unsupported installation methods.
```

**Expected result:** a healthy private app and a clear handoff. Account creation may require the server's registration/invitation settings; Google sign-in appears only if configured. Setup saves guidance and connection results; it does not mount a NAS, claim Plex or acquire media.

Then follow [Quickstart](https://omakase-plus.jhinx.dev/docs/quickstart), [Setup choices](https://omakase-plus.jhinx.dev/docs/setup-choices) or [Backup & recovery](https://omakase-plus.jhinx.dev/docs/backup-upgrade-recovery).
