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
   protected backup. Keep current and prior rollback images until acceptance.
5. Verify exact source identity, authentication, settings persistence and affected
   desktop/phone/keyboard journeys on the target. Catalog or inventory checks do
   not prove full playback, a physical device or human creative acceptance.
6. Roll back the exact prior image/config if target verification fails. Never
   improvise a destructive migration rollback or delete shared media for cleanup.

See [Getting started](getting-started.md), [Updates](updates.md), and
[Security](../SECURITY.md). Optional Plex curator deployment is documented in
[its guide](../deploy/plex-curator/README.md).
