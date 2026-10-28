param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{40}$')][string]$Revision,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{40}$')][string]$PriorRevision,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')][string]$SshAlias,
    [string]$Repository = (Split-Path -Parent $PSScriptRoot),
    [string]$BackupPath,
    [switch]$Apply,
    [switch]$StartImmediately
)

# Updates this existing owner task only. No credentials, principal changes,
# inference, model retries, service deployment or paid fallback are introduced.
$ErrorActionPreference = 'Stop'
$bridgeName = 'OmakasePlusCodexDiscovery'
$bridgeRoot = (Resolve-Path -LiteralPath $Repository).Path
$bridgePython = Join-Path $bridgeRoot '.venv\Scripts\python.exe'
$bridgeRedirector = Join-Path $bridgeRoot '.venv\Scripts\pythonw.exe'
$bridgeBootstrap = Join-Path $bridgeRoot 'scripts\run_codex_discovery_hidden.py'
if (-not (Test-Path -LiteralPath $bridgePython -PathType Leaf) -or -not (Test-Path -LiteralPath $bridgeBootstrap -PathType Leaf)) { throw 'Locked interpreter or GUI bootstrap is missing.' }
$bridgePythonwText = & $bridgePython -c 'import sys; from pathlib import Path; print(Path(sys.base_prefix) / "pythonw.exe")'
if ($LASTEXITCODE -ne 0) { throw 'Base GUI interpreter is unavailable.' }
$bridgePythonw = (Resolve-Path -LiteralPath $bridgePythonwText).Path
$bridgeImage = [IO.File]::ReadAllBytes($bridgePythonw)
if ($bridgeImage.Length -lt 64) { throw 'Invalid GUI executable.' }
$bridgePe = [BitConverter]::ToInt32($bridgeImage, 0x3c)
if ($bridgePe -lt 0 -or $bridgeImage.Length -lt ($bridgePe + 94) -or [BitConverter]::ToUInt32($bridgeImage, $bridgePe) -ne 0x00004550 -or [BitConverter]::ToUInt16($bridgeImage, $bridgePe + 92) -ne 2) { throw 'Task entry must be a real GUI executable, not a console redirector.' }
$bridgeHead = (& git -C $bridgeRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $bridgeHead -ne $Revision) { throw 'Source revision does not match.' }
$bridgeRuntime = @(
    'src/omakase/__init__.py', 'src/omakase/plus/__init__.py',
    'src/omakase/plus/codex_bridge.py', 'src/omakase/plus/codex_jobs.py',
    'src/omakase/plus/codex_worker.py', 'pyproject.toml', 'uv.lock',
    'scripts/run_codex_discovery_bridge.ps1', 'scripts/configure_codex_discovery_task.ps1',
    'scripts/run_codex_discovery_hidden.py'
)
$bridgeDirty = & git -C $bridgeRoot status --porcelain -- $bridgeRuntime
if ($LASTEXITCODE -ne 0 -or $bridgeDirty) { throw 'Runtime source must be committed.' }
$bridgeTask = Get-ScheduledTask -TaskName $bridgeName -ErrorAction Stop
if (@($bridgeTask.Actions).Count -ne 1 -or $bridgeTask.Principal.LogonType -ne 'Interactive' -or $bridgeTask.Principal.RunLevel -ne 'Limited') {
    throw 'Unexpected owner task contract.'
}
$bridgeLegacyScript = Join-Path $bridgeRoot 'scripts\run_codex_discovery_bridge.ps1'
$bridgeLegacyArguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "' + $bridgeLegacyScript + '" -SshAlias ' + $SshAlias + ' -ExpectedRevision ' + $PriorRevision
$bridgePriorArguments = '-m omakase.plus.codex_bridge --ssh-alias ' + $SshAlias + ' --expected-revision ' + $PriorRevision + ' --repository "' + $bridgeRoot + '"'
$bridgePriorGuiArguments = '"' + $bridgeBootstrap + '" --ssh-alias ' + $SshAlias + ' --expected-revision ' + $PriorRevision + ' --repository "' + $bridgeRoot + '"'
if ($bridgeTask.Actions[0].WorkingDirectory -ne $bridgeRoot -or (
    ($bridgeTask.Actions[0].Arguments -ne $bridgeLegacyArguments -or [IO.Path]::GetFileName($bridgeTask.Actions[0].Execute) -ne 'pwsh.exe') -and
    ($bridgeTask.Actions[0].Arguments -ne $bridgePriorArguments -or $bridgeTask.Actions[0].Execute -ne $bridgeRedirector) -and
    ($bridgeTask.Actions[0].Arguments -ne $bridgePriorGuiArguments -or $bridgeTask.Actions[0].Execute -ne $bridgePythonw)
)) { throw 'Existing action does not match the reviewed source and alias.' }
$bridgePrincipal = $bridgeTask.Principal | ConvertTo-Json -Depth 12 -Compress
$bridgeDescription = $bridgeTask.Description
$bridgeOriginalTriggers = @($bridgeTask.Triggers | Where-Object { $_.Id -ne 'OmakaseCodexRecovery' })
$bridgeOriginalTriggerJson = $bridgeOriginalTriggers | ConvertTo-Json -Depth 12 -Compress
$bridgeRecovery = @($bridgeTask.Triggers | Where-Object { $_.Id -eq 'OmakaseCodexRecovery' })
if ($bridgeRecovery.Count -gt 1) { throw 'Duplicate recovery triggers.' }
if ($bridgeRecovery.Count -eq 0) {
    $bridgeRecovery = @(New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval ([TimeSpan]::FromMinutes(1)))
    $bridgeRecovery[0].Id = 'OmakaseCodexRecovery'
} elseif ($bridgeRecovery[0].Repetition.Interval -ne 'PT1M' -or -not $bridgeRecovery[0].Enabled) {
    throw 'Unexpected existing recovery policy.'
}
$bridgeSettings = $bridgeTask.Settings
$bridgeSettings.RestartCount = 3
$bridgeSettings.RestartInterval = 'PT1M'
$bridgeSettings.Hidden = $true
$bridgeArguments = '"' + $bridgeBootstrap + '" --ssh-alias ' + $SshAlias + ' --expected-revision ' + $Revision + ' --repository "' + $bridgeRoot + '"'
$bridgeAction = New-ScheduledTaskAction -Execute $bridgePythonw -Argument $bridgeArguments -WorkingDirectory $bridgeRoot
if ($Apply) {
    if (-not $BackupPath -or (Test-Path -LiteralPath $BackupPath)) { throw 'A new backup path is required.' }
    $bridgeStatusText = & $bridgePython -m omakase.plus.codex_worker --ssh-alias $SshAlias --status
    if ($LASTEXITCODE -ne 0) { throw 'Queue status is unavailable.' }
    $bridgeStatus = $bridgeStatusText | ConvertFrom-Json
    if (-not $bridgeStatus.ok -or $bridgeStatus.active_jobs -ne 0 -or $bridgeStatus.queued_jobs -ne 0) { throw 'Wait for the current owner request before replacing its worker.' }
    Export-ScheduledTask -TaskName $bridgeName | Set-Content -LiteralPath $BackupPath -Encoding utf8
    Set-ScheduledTask -TaskName $bridgeName -Action $bridgeAction -Trigger @($bridgeOriginalTriggers + $bridgeRecovery) -Settings $bridgeSettings | Out-Null
    $bridgeReadback = Get-ScheduledTask -TaskName $bridgeName
    $bridgeReadbackTriggers = @($bridgeReadback.Triggers | Where-Object { $_.Id -ne 'OmakaseCodexRecovery' }) | ConvertTo-Json -Depth 12 -Compress
    if ($bridgeReadback.Actions[0].Execute -ne $bridgePythonw -or $bridgeReadback.Actions[0].Arguments -ne $bridgeArguments -or
        ($bridgeReadback.Principal | ConvertTo-Json -Depth 12 -Compress) -ne $bridgePrincipal -or $bridgeReadback.Description -ne $bridgeDescription -or
        $bridgeReadbackTriggers -ne $bridgeOriginalTriggerJson -or $bridgeReadback.Settings.RestartCount -ne 3 -or $bridgeReadback.Settings.RestartInterval -ne 'PT1M') {
        throw 'Installed task did not converge.'
    }
    # The supervisor's Windows Job ends its descendants when the task stops.
    # No process-name kill, terminal/profile setting, or other user's window is touched.
    Stop-ScheduledTask -TaskName $bridgeName
    if ($StartImmediately) { Start-ScheduledTask -TaskName $bridgeName }
}
[ordered]@{
    outcome = $(if ($Apply) { 'console_free_task_installed' } else { 'console_free_task_preview' })
    revision = $Revision
    prior_revision = $PriorRevision
    task = $bridgeName
    gui_interpreter = $true
    no_console_children = $true
    recovery_interval_seconds = 60
    recovery_start = $bridgeRecovery[0].StartBoundary
    automatic_failure_restarts = 3
    original_principal_and_triggers_preserved = $true
    inference_when_idle = $false
    start_immediately = [bool]$StartImmediately
} | ConvertTo-Json -Compress
