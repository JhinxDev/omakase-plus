# Release and recovery checklist

Use an exact reviewed revision and keep deployment-specific access and recovery
records outside source control. A public source release is not a hosted service.

1. Review intentional source changes, public links, documentation and the locked
   dependency graph. Exclude secrets, databases, private endpoints and personal data.
2. Run relevant tests, Ruff checks and package/container builds. Use isolated fake
   providers and safe public/synthetic screenshots.
3. Verify a clean app-only install with compose.standalone.yaml and explicit
   storage setups with compose.local.yaml. Confirm migrations and database health.
4. Record the currently running revision/image and a tested database/config backup
   before changing a live installation. Preserve the master key through a separate
   protected backup. Keep current and immediate-prior recovery images until acceptance.
   Qualify enabled consumers against an isolated combined current-schema copy and
   save durable prepared recovery metadata before activation.
5. Verify exact source identity, authentication, settings persistence and affected
   desktop/phone/keyboard journeys on the target. Catalog or inventory checks do
   not prove full playback, a physical device or human creative acceptance.
6. If target verification fails, retain current migrated data and fence relevant
   writers. Use a qualified forward fix or restricted read-only recovery.
   Unrestricted prior-image writes and a database rewind are unsafe. Deliberate
   disaster restoration is a separate operator/data-loss decision. Never delete
   shared media for release cleanup.

Legacy automatic upgrade/rollback scripts are disabled before any operation.
Follow [current-data recovery](current-data-recovery.md) for manual qualification,
backup, activation/failure records and recovery boundaries. HTTP health alone
does not prove migration, persistence or writer compatibility.

See [Getting started](getting-started.md), [Updates](updates.md), and
[Security](../SECURITY.md). Optional Plex curator deployment is documented in
[its guide](../deploy/plex-curator/README.md).
