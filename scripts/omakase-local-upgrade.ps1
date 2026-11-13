# Kept as a fail-closed compatibility entrypoint for previously documented commands.
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$StackRoot,

    [string[]]$Profiles = @(),

    [switch]$AllowLocalDev
)

# No path resolution, configuration/secret reads, receipt writes or Docker calls.
# A successful image health check cannot qualify combined-schema writer safety.
throw @"
Automatic local upgrades are disabled. This legacy helper cannot qualify the current data schema and all active writers safely. No deployment operation was run. Keep current data and use the manually qualified update and recovery procedure in docs/release-checklist.md and docs/current-data-recovery.md. The old rollback helper is also disabled; do not restore an older database after writes have resumed.
"@
