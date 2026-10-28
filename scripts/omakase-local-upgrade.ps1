[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$StackRoot,

    [string[]]$Profiles = @(),

    [switch]$AllowLocalDev
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed without completing the upgrade."
    }
}

function Read-DotEnvValue {
    param([string]$Path, [string]$Name)
    $line = Get-Content -LiteralPath $Path |
        Where-Object { $_ -match "^\s*$([regex]::Escape($Name))=" } |
        Select-Object -Last 1
    if (-not $line) {
        return $null
    }
    return ($line -split "=", 2)[1].Trim()
}

$resolvedRoot = (Resolve-Path -LiteralPath $StackRoot).Path
$composePath = Join-Path $resolvedRoot "compose.local.yaml"
$envPath = Join-Path $resolvedRoot ".env.local"
$secretPath = Join-Path $resolvedRoot ".env.local.secrets"
foreach ($requiredPath in @($composePath, $envPath, $secretPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required deployment file is missing: $requiredPath"
    }
}

$revision = (& git -C $resolvedRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "StackRoot is not a readable Git worktree."
}
if (-not $AllowLocalDev -and $revision -notmatch "^[0-9a-f]{40}$") {
    throw "A release upgrade requires an exact 40-character Git revision."
}
$dirty = & git -C $resolvedRoot status --porcelain
if ($LASTEXITCODE -ne 0 -or ($dirty -and -not $AllowLocalDev)) {
    throw "Release upgrades require a clean worktree; preserve or commit changes first."
}

$dataValue = Read-DotEnvValue -Path $envPath -Name "OMAKASE_DATA_PATH"
if (-not $dataValue) {
    throw "OMAKASE_DATA_PATH must be explicit in .env.local."
}
$dataPath = if ([IO.Path]::IsPathRooted($dataValue)) {
    [IO.Path]::GetFullPath($dataValue)
} else {
    [IO.Path]::GetFullPath((Join-Path $resolvedRoot $dataValue))
}
if (-not (Test-Path -LiteralPath $dataPath -PathType Container)) {
    throw "The configured Omakase data directory does not exist."
}

Invoke-Docker version
$composeArgs = @("compose", "--env-file", $envPath, "-f", $composePath)
foreach ($profile in $Profiles) {
    if ($profile -notmatch "^[a-z0-9][a-z0-9-]{0,39}$") {
        throw "Compose profile name is invalid."
    }
    $composeArgs += @("--profile", $profile)
}
Invoke-Docker @composeArgs config --quiet

$stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$receiptDirectory = Join-Path $resolvedRoot "receipts"
$backupDirectory = Join-Path $dataPath "backups"
New-Item -ItemType Directory -Path $receiptDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null

$previousImage = ""
$previousRevision = ""
$containerExists = (& docker container inspect omakase-local-app --format "{{.Id}}" 2>$null)
if ($LASTEXITCODE -eq 0) {
    $previousImage = (& docker inspect omakase-local-app --format "{{.Image}}").Trim()
    $previousRevision = (
        & docker inspect omakase-local-app `
            --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
    ).Trim()
}

$backupPath = Join-Path $backupDirectory "omakase-plus-pre-$stamp.db"
$databasePath = Join-Path $dataPath "omakase-plus.db"
if (Test-Path -LiteralPath $databasePath -PathType Leaf) {
    $backupCode = @'
import sqlite3, sys
source, target = sys.argv[1], sys.argv[2]
src = sqlite3.connect(source)
dst = sqlite3.connect(target)
src.backup(dst)
assert dst.execute("PRAGMA quick_check").fetchone()[0] == "ok"
dst.close()
src.close()
'@
    & python -c $backupCode $databasePath $backupPath
    if ($LASTEXITCODE -ne 0) {
        throw "SQLite backup or integrity validation failed."
    }
}

$env:OMAKASE_REVISION = $revision
try {
    Invoke-Docker @composeArgs build --pull omakase-plus
    Invoke-Docker @composeArgs up -d
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
    throw "The upgraded Omakase container did not become healthy; use the rollback receipt."
}

$deployedRevision = (
    & docker inspect omakase-local-app `
        --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
).Trim()
if ($deployedRevision -ne $revision) {
    throw "The running image revision does not match the intended Git revision."
}

$receipt = [ordered]@{
    schema_version = "omakase-local-upgrade-receipt-v1"
    created_at = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    stack_root = $resolvedRoot
    data_path = $dataPath
    deployed_revision = $revision
    previous_revision = $previousRevision
    previous_image = $previousImage
    database_backup = if (Test-Path -LiteralPath $backupPath) { $backupPath } else { "" }
    database_backup_sha256 = if (Test-Path -LiteralPath $backupPath) {
        (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { "" }
    profiles = @($Profiles)
    health = $health
}
$receiptPath = Join-Path $receiptDirectory "upgrade-$stamp.json"
$receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $receiptPath -Encoding utf8
Write-Output "Upgrade verified. Rollback receipt: $receiptPath"
