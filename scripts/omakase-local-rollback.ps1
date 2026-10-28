[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$StackRoot,

    [Parameter(Mandatory)]
    [string]$ReceiptPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed without completing rollback."
    }
}

$resolvedRoot = (Resolve-Path -LiteralPath $StackRoot).Path
$resolvedReceipt = (Resolve-Path -LiteralPath $ReceiptPath).Path
$receiptRoot = [IO.Path]::GetFullPath((Join-Path $resolvedRoot "receipts"))
if (-not $resolvedReceipt.StartsWith($receiptRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw "Rollback receipt must be inside this stack's receipts directory."
}
$receipt = Get-Content -LiteralPath $resolvedReceipt -Raw | ConvertFrom-Json
if (
    $receipt.schema_version -ne "omakase-local-upgrade-receipt-v1" -or
    $receipt.stack_root -ne $resolvedRoot
) {
    throw "Rollback receipt schema or stack binding is invalid."
}
if (-not $receipt.previous_image) {
    throw "Receipt has no previous image to restore."
}

$composePath = Join-Path $resolvedRoot "compose.local.yaml"
$envPath = Join-Path $resolvedRoot ".env.local"
foreach ($requiredPath in @($composePath, $envPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required rollback file is missing: $requiredPath"
    }
}

$databasePath = Join-Path $receipt.data_path "omakase-plus.db"
if ($receipt.database_backup) {
    $backupPath = (Resolve-Path -LiteralPath $receipt.database_backup).Path
    $actualHash = (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $receipt.database_backup_sha256) {
        throw "Rollback database backup failed its SHA-256 receipt."
    }
}

$composeArgs = @("compose", "--env-file", $envPath, "-f", $composePath)
foreach ($profile in $receipt.profiles) {
    $composeArgs += @("--profile", [string]$profile)
}
Invoke-Docker @composeArgs stop omakase-plus

if ($receipt.database_backup) {
    if (Test-Path -LiteralPath $databasePath -PathType Leaf) {
        $safetyPath = "$databasePath.pre-rollback-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))"
        Copy-Item -LiteralPath $databasePath -Destination $safetyPath
    }
    Copy-Item -LiteralPath $receipt.database_backup -Destination $databasePath -Force
}

$rollbackTag = if ($receipt.previous_revision -match "^[0-9a-f]{40}$") {
    [string]$receipt.previous_revision
} else {
    "rollback"
}
Invoke-Docker tag $receipt.previous_image "omakase-plus:$rollbackTag"
$env:OMAKASE_REVISION = $rollbackTag
try {
    Invoke-Docker @composeArgs up -d --no-build
} finally {
    Remove-Item Env:\OMAKASE_REVISION -ErrorAction SilentlyContinue
}

$deadline = [DateTime]::UtcNow.AddMinutes(4)
$health = ""
do {
    Start-Sleep -Seconds 3
    $health = (& docker inspect omakase-local-app --format "{{.State.Health.Status}}").Trim()
} while ($health -ne "healthy" -and [DateTime]::UtcNow -lt $deadline)
if ($health -ne "healthy") {
    throw "Rollback started but the restored container is not healthy."
}
Write-Output "Rollback verified healthy using receipt $resolvedReceipt"
