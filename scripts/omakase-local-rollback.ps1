# Kept as a fail-closed compatibility entrypoint for previously documented commands.
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$StackRoot,

    [Parameter(Mandatory)]
    [string]$ReceiptPath
)

# Stop before resolving even a missing stack/receipt path. No override is provided.
# Copying a main SQLite file over current data ignores WAL/SHM and later writes.
throw @"
Automatic local rollback is disabled. Keep current data, including SQLite WAL/SHM, and fence all writers under a reviewed recovery plan. An old receipt does not authorize a database rewind or unrestricted old-image writes. No file, configuration or Docker operation was run. Follow docs/current-data-recovery.md for qualified read-only recovery or a forward fix; deliberate disaster restoration is a separate operator decision with explicit data-loss consequences.
"@
