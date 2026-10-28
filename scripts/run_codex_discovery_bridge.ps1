param(
    [Parameter(Mandatory = $true)][string]$SshAlias,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ExpectedRevision,
    [string]$Repository = (Split-Path -Parent $PSScriptRoot),
    [ValidateRange(0, 48)][int]$Cycles = 0
)

# Keeps the private workstation available. Each child is still finite, and
# inference happens only for an explicitly queued owner request. Terminal jobs
# are never retried, and no API login or paid fallback is introduced here.
$ErrorActionPreference = 'Stop'
$bridgeRoot = (Resolve-Path -LiteralPath $Repository).Path
$bridgeUv = (Get-Command uv -CommandType Application | Select-Object -First 1).Source
$bridgeMutex = [System.Threading.Mutex]::new($false, 'Local\OmakasePlusCodexDiscovery')
$bridgeAcquired = $false
$bridgeFailures = 0
$bridgeCycle = 0
try {
    $bridgeAcquired = $bridgeMutex.WaitOne(0)
    if (-not $bridgeAcquired) { exit 0 }
    Set-Location -LiteralPath $bridgeRoot
    while ($Cycles -eq 0 -or $bridgeCycle -lt $Cycles) {
        $bridgeHead = (& git rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Bridge source revision is unavailable.' }
        if ($bridgeHead -ne $ExpectedRevision) {
            & git diff --quiet $ExpectedRevision HEAD -- src/omakase pyproject.toml uv.lock scripts/run_codex_discovery_bridge.ps1
            if ($LASTEXITCODE -ne 0) {
                throw 'Bridge runtime differs from the installed task.'
            }
        }
        $bridgeDirty = & git status --porcelain -- src/omakase pyproject.toml uv.lock scripts/run_codex_discovery_bridge.ps1
        if ($LASTEXITCODE -ne 0 -or $bridgeDirty) {
            throw 'Bridge runtime source must remain committed.'
        }
        & $bridgeUv run --frozen python -m omakase.plus.codex_worker --ssh-alias $SshAlias --max-jobs 4 --max-minutes 30 --job-timeout 300
        $bridgeCycle++
        if ($LASTEXITCODE -eq 0) {
            $bridgeFailures = 0
        } else {
            $bridgeFailures++
            if ($bridgeFailures -ge 3) {
                throw 'Bridge unavailable after three bounded startup attempts.'
            }
            Start-Sleep -Seconds ([Math]::Min(60, 30 * $bridgeFailures))
        }
    }
} catch {
    # Never print child stderr, credential status, prompts, or command details.
    Write-Output 'Omakase Codex bridge stopped. Check the private discovery status.'
    exit 1
} finally {
    if ($bridgeAcquired) { $bridgeMutex.ReleaseMutex() }
    $bridgeMutex.Dispose()
}
