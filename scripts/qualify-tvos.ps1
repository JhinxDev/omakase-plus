#Requires -Version 7.0
<#
.SYNOPSIS
    Run the portable Apple TV qualification gates on this host and write a receipt.

.DESCRIPTION
    Runs, in order, stopping at the first failure:

      1. phase-probe-self-test    the saved-start phase reader accepts good lines and
                                  rejects the known-bad shapes (no server needed)
      2. tvos-fixture-self-test   the connected fixture's pure state machine: plan
                                  expiry stays authorized, a stale close leaves the
                                  successor live (no TLS, no simulator)
      3. pytest-native-tv         the off-device gates named in the audit
      4. swift-qualification      swift test in tv-source/QualificationCore

    Every child has a finite deadline, hidden window, captured stdout/stderr and a
    recorded exit status. Nothing here builds, signs, installs or contacts NoMac:
    those are separate authorized actions. The receipt names the exact next build
    command instead of running it.

    The receipt never overstates what ran:

      result=passed    every planned gate executed and passed      exit 0
      result=partial   some gates executed, some skipped           exit 3
      result=failed    an executed gate failed                     exit 1
      result=skipped   no gate executed at all: not a pass         exit 2

    It also records `checks_run` (gates that actually executed), a SHA-256 snapshot
    digest binding the tested source, and the paths each executed gate read.

    Bounded and non-interactive. No prompts, no windows, no network.

.PARAMETER OutputPath
    Receipt destination. Default: build/qualification/qualification-receipt.json

.PARAMETER Skip
    Gate ids to skip. A run with skipped gates is `partial`, never `passed`.

.PARAMETER BaseCommit
    Commit the source snapshot digest is diffed against. Default 6c44a99fc835.

.EXAMPLE
    pwsh -File scripts/qualify-tvos.ps1

.EXAMPLE
    pwsh -File scripts/qualify-tvos.ps1 -OutputPath build/qualification/receipt.json -Skip swift-qualification
#>
[CmdletBinding()]
param(
    [string]$OutputPath = '',
    [string[]]$Skip = @(),
    [string]$PythonPath = '',
    [string]$SwiftPath = '',
    [string]$BaseCommit = '6c44a99fc8356278892a0bb51d213845f561ca08',
    [int]$PytestTimeoutSeconds = 1800,
    [int]$SwiftTimeoutSeconds = 1200
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# `pwsh -File script.ps1 -Skip a,b` arrives as one comma-joined string, so split
# it: a caller who thinks they skipped a gate must not silently execute it.
$Skip = @($Skip | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } |
    Where-Object { -not [string]::IsNullOrWhiteSpace($_) })

$Repo = (Split-Path -Parent (Split-Path -Parent $PSCommandPath))
if (-not (Test-Path -LiteralPath (Join-Path $Repo 'pyproject.toml'))) {
    throw "qualify-tvos.ps1 must live in <repo>/scripts and run against this checkout; resolved $Repo"
}
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $Repo 'build/qualification/qualification-receipt.json'
}
elseif (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $Repo $OutputPath
}
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $Repo '.venv/Scripts/python.exe'
}

$LogDir = Join-Path (Split-Path -Parent $OutputPath) 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$StartedAt = [DateTime]::UtcNow
$TotalWatch = [System.Diagnostics.Stopwatch]::StartNew()

function Invoke-Bounded {
    <#
        Run one child process with a deadline, a hidden window and captured
        streams. Never throws on a non-zero exit: the caller decides.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [Parameter(Mandatory)][string]$LogPath,
        [Parameter(Mandatory)][int]$TimeoutSeconds,
        [string]$Label = ''
    )
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FilePath
    foreach ($argument in $Arguments) { $psi.ArgumentList.Add($argument) }
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $psi.Environment['DOTNET_CLI_TELEMETRY_OPTOUT'] = '1'
    $psi.Environment['PYTHONUNBUFFERED'] = '1'

    $started = [DateTime]::UtcNow
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $timedOut = $false
    try {
        $process = [System.Diagnostics.Process]::Start($psi)
    }
    catch {
        return [pscustomobject]@{
            StartedAt = $started; ElapsedSeconds = 0.0; ExitCode = 127; TimedOut = $false
            StdOut = ''; StdErr = "$($_.Exception.Message)"; Log = $LogPath
            LaunchError = $_.Exception.Message
        }
    }
    $outTask = $process.StandardOutput.ReadToEndAsync()
    $errTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $timedOut = $true
        try { $process.Kill($true) } catch { }
        $null = $process.WaitForExit(15000)
    }
    $stdout = $outTask.GetAwaiter().GetResult()
    $stderr = $errTask.GetAwaiter().GetResult()
    $watch.Stop()
    $exitCode = if ($timedOut) { -1 } else { $process.ExitCode }

    $rendered = @(
        "# $Label"
        "# command: $FilePath $($Arguments -join ' ')"
        "# cwd: $WorkingDirectory"
        "# started_utc: $($started.ToString('o'))"
        "# elapsed_seconds: $([math]::Round($watch.Elapsed.TotalSeconds, 3))"
        "# exit_code: $exitCode"
        "# timed_out: $timedOut"
        ''
        '--- stdout ---'
        $stdout
        '--- stderr ---'
        $stderr
    ) -join [Environment]::NewLine
    Set-Content -LiteralPath $LogPath -Value $rendered -Encoding utf8

    [pscustomobject]@{
        StartedAt = $started
        ElapsedSeconds = [math]::Round($watch.Elapsed.TotalSeconds, 3)
        ExitCode = $exitCode
        TimedOut = $timedOut
        StdOut = $stdout
        StdErr = $stderr
        Log = $LogPath
        LaunchError = $null
    }
}

function Get-Version {
    param([string]$FilePath, [string[]]$Arguments = @('--version'))
    try {
        $result = Invoke-Bounded -FilePath $FilePath -Arguments $Arguments `
            -WorkingDirectory $Repo -LogPath (Join-Path $LogDir 'toolchain-version.log') `
            -TimeoutSeconds 60 -Label 'toolchain-version'
        $text = ($result.StdOut + ' ' + $result.StdErr).Trim()
        if ($result.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($text)) { return $null }
        return ($text -split "`r?`n" | Where-Object { $_ -match '\S' } | Select-Object -First 1).Trim()
    }
    catch { return $null }
}

function Get-Tail {
    param([string]$Text, [int]$Count = 40)
    if ([string]::IsNullOrWhiteSpace($Text)) { return @() }
    # Force an array: a one-line tail is a scalar string, which has no .Count
    # under Set-StrictMode and would abort the run instead of reporting it.
    $lines = @($Text -split "`r?`n" | Where-Object { $_ -match '\S' })
    if ($lines.Count -le $Count) { return $lines }
    return @($lines | Select-Object -Last $Count)
}

function Get-PortableLocation {
    <#
        A tool location that is safe to publish in a receipt: workspace-relative
        when the tool lives in the checkout (that path is already recorded as the
        source root), otherwise only the generic file name. Never this user's home
        directory.
    #>
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    if ([System.IO.Path]::IsPathRooted($Path)) {
        $full = [System.IO.Path]::GetFullPath($Path)
        if ($full.StartsWith($Repo, [System.StringComparison]::OrdinalIgnoreCase)) {
            return ($full.Substring($Repo.Length).TrimStart('\', '/') -replace '\\', '/')
        }
    }
    return [System.IO.Path]::GetFileName($Path)
}

function Get-CommandPath {
    param([string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) { return $null }
    if (-not [string]::IsNullOrWhiteSpace($command.Source)) { return $command.Source }
    if (-not [string]::IsNullOrWhiteSpace($command.Path)) { return $command.Path }
    return $null
}

function Get-PortableArguments {
    param([string[]]$Arguments)
    return @($Arguments | ForEach-Object {
        if (-not [string]::IsNullOrWhiteSpace($_) -and [System.IO.Path]::IsPathRooted($_)) {
            Get-PortableLocation $_
        } else { $_ }
    })
}

function Get-TextSha256 {
    param([string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Get-FileSha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# ---------------------------------------------------------------- source identity
$branch = (git -C $Repo rev-parse --abbrev-ref HEAD 2>$null | Out-String).Trim()
$commit = (git -C $Repo rev-parse HEAD 2>$null | Out-String).Trim()
$tree = (git -C $Repo rev-parse 'HEAD^{tree}' 2>$null | Out-String).Trim()
$subject = (git -C $Repo log -1 --pretty=%s 2>$null | Out-String).Trim()
$dirty = @(git -C $Repo status --porcelain 2>$null)
$dirtyPaths = @($dirty | ForEach-Object { ($_ -replace '^.. ', '').Trim() } | Where-Object { $_ })
$shortCommit = if ($commit.Length -ge 12) { $commit.Substring(0, 12) } else { $commit }

# Exact snapshot digest. `git ls-files -s` names every tracked blob the run could
# read; the diff against the base commit names the uncommitted tracked edits; the
# untracked list is hashed per file because most of this change set is new files.
# Build outputs and bytecode are excluded so the digest identifies source, not a
# previous run's artifacts.
$snapshotExcludes = @(
    ':(exclude)build'
    ':(exclude)build/**'
    ':(exclude)**/__pycache__/**'
    ':(exclude)**/*.pyc'
)
$indexText = (@(git -C $Repo ls-files -s 2>$null) -join "`n")
$baseKnown = -not [string]::IsNullOrWhiteSpace(
    (git -C $Repo rev-parse --verify --quiet "$BaseCommit^{commit}" 2>$null))
$diffText = if ($baseKnown) {
    (@(git -C $Repo diff --binary $BaseCommit -- . @snapshotExcludes 2>$null) -join "`n")
} else { '' }
$untrackedPaths = @(git -C $Repo ls-files -o --exclude-standard -- . @snapshotExcludes 2>$null)
$untrackedLines = @(
    foreach ($path in ($untrackedPaths | Sort-Object)) {
        $full = Join-Path $Repo $path
        if (Test-Path -LiteralPath $full -PathType Leaf) {
            "$path $(Get-FileSha256 -Path $full)"
        }
    }
)
$indexDigest = Get-TextSha256 $indexText
$diffDigest = Get-TextSha256 $diffText
$untrackedDigest = Get-TextSha256 ($untrackedLines -join "`n")
$snapshotDigest = Get-TextSha256 (@(
    'omakase-tvos-qualification-snapshot-v1'
    "base=$BaseCommit"
    "commit=$commit"
    "branch=$branch"
    "index_sha256=$indexDigest"
    "diff_sha256=$diffDigest"
    "untracked_sha256=$untrackedDigest"
) -join "`n")

# ---------------------------------------------------------------- toolchains
$swiftResolved = $null
$swiftSource = $null
if (-not [string]::IsNullOrWhiteSpace($SwiftPath) -and (Test-Path -LiteralPath $SwiftPath)) {
    $swiftResolved = $SwiftPath
    $swiftSource = 'argument'
}
if ($null -eq $swiftResolved) {
    $fromPath = Get-CommandPath 'swift'
    if ($null -ne $fromPath) {
        $swiftResolved = $fromPath
        $swiftSource = 'PATH'
    }
}
if ($null -eq $swiftResolved) {
    # The desktop toolchain installs outside the checkout; only its file name is
    # recorded below, never the home directory it lives in.
    $candidate = Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA 'Programs/Swift/Toolchains') `
        -Filter 'swift.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $candidate) {
        $swiftResolved = $candidate.FullName
        $swiftSource = 'local-toolchain'
    }
}

$toolchains = [ordered]@{
    pwsh          = $PSVersionTable.PSVersion.ToString()
    os            = [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
    git           = Get-Version -FilePath 'git'
    python        = if (Test-Path -LiteralPath $PythonPath) { Get-Version -FilePath $PythonPath } else { $null }
    swift         = if ($null -ne $swiftResolved) { Get-Version -FilePath $swiftResolved } else { $null }
    python_path   = Get-PortableLocation $PythonPath
    python_source = if (Test-Path -LiteralPath $PythonPath) { 'workspace-relative' } else { 'missing' }
    swift_path    = Get-PortableLocation $swiftResolved
    swift_source  = $swiftSource
}

$probe = Join-Path $Repo 'scripts/fixtures/native-startup-phases/native_startup_phases.py'
$fixture = Join-Path $Repo 'scripts/fixtures/tvos-connected/connected_fixture.py'
$swiftPackage = Join-Path $Repo 'tv-source/QualificationCore'
$pytestFiles = @(
    'tests/plus/test_native_tv_test.py'
    'tests/plus/test_native_hls_routes.py'
    'tests/plus/test_pause_recovery_and_gate.py'
    'tests/plus/test_subtitle_playing_gate.py'
)

$gatePlan = @(
    [pscustomobject]@{
        Id = 'phase-probe-self-test'
        Description = 'saved-start phase reader accepts good receipts and rejects known-bad shapes'
        FilePath = $PythonPath
        Arguments = @('-B', $probe, '--self-test')
        WorkingDirectory = $Repo
        TimeoutSeconds = 120
        Requires = 'python'
        TestedPaths = @('scripts/fixtures/native-startup-phases/native_startup_phases.py')
    },
    [pscustomobject]@{
        Id = 'tvos-fixture-self-test'
        Description = 'connected fixture: plan expiry stays authorized, stale close leaves the successor live'
        FilePath = $PythonPath
        Arguments = @('-B', $fixture, '--self-test')
        WorkingDirectory = $Repo
        TimeoutSeconds = 120
        Requires = 'python'
        TestedPaths = @(
            'scripts/fixtures/tvos-connected/connected_fixture.py'
            'scripts/fixtures/tvos-connected/hls'
        )
    },
    [pscustomobject]@{
        Id = 'pytest-native-tv'
        Description = 'off-device native TV, route, pause-recovery and subtitle gates'
        FilePath = $PythonPath
        Arguments = @('-m', 'pytest') + $pytestFiles + @('-q')
        WorkingDirectory = $Repo
        TimeoutSeconds = $PytestTimeoutSeconds
        Requires = 'python'
        TestedPaths = $pytestFiles
    },
    [pscustomobject]@{
        Id = 'swift-qualification'
        Description = 'portable Swift package tests (QualificationCore)'
        FilePath = $swiftResolved
        Arguments = @('test')
        WorkingDirectory = $swiftPackage
        TimeoutSeconds = $SwiftTimeoutSeconds
        Requires = 'swift'
        TestedPaths = @('tv-source/QualificationCore')
    }
)

$toolAvailability = @{
    python = (Test-Path -LiteralPath $PythonPath)
    swift  = ($null -ne $swiftResolved)
}

$gates = @()
$failedGate = $null
foreach ($gate in $gatePlan) {
    $reason = $null
    if ($Skip -contains $gate.Id) {
        $reason = 'requested_skip'
        Write-Host ("SKIP {0}: requested" -f $gate.Id) -ForegroundColor DarkGray
    }
    elseif (-not $toolAvailability[$gate.Requires]) {
        $reason = "toolchain_missing:$($gate.Requires)"
        Write-Host ("SKIP {0}: {1}" -f $gate.Id, $reason) -ForegroundColor DarkGray
    }

    if ($null -ne $reason) {
        $gates += [pscustomobject]@{
            id = $gate.Id; description = $gate.Description; passed = $null
            skipped = $true; reason = $reason; exit_code = $null; elapsed_seconds = 0.0
            started_at = $null; command = @(Get-PortableLocation $gate.FilePath) + (Get-PortableArguments $gate.Arguments)
            cwd = Get-PortableLocation $gate.WorkingDirectory
            log = $null; tail = @(); tested_paths = @($gate.TestedPaths)
        }
        continue
    }

    $logPath = Join-Path $LogDir ("{0}.log" -f $gate.Id)
    Write-Host ("RUN  {0}: {1}" -f $gate.Id, $gate.Description)
    $result = Invoke-Bounded -FilePath $gate.FilePath -Arguments $gate.Arguments `
        -WorkingDirectory $gate.WorkingDirectory -LogPath $logPath `
        -TimeoutSeconds $gate.TimeoutSeconds -Label $gate.Id

    $passed = ($result.ExitCode -eq 0)
    # The receipt is shareable: keep absolute paths out of the captured tail
    # (the log file keeps the unabridged text). The workspace root is the only
    # path the receipt states, in source.root.
    $tail = @(Get-Tail -Text ($result.StdOut + "`n" + $result.StdErr) |
        ForEach-Object { $_.Replace($Repo, '.') })
    $gates += [pscustomobject]@{
        id = $gate.Id; description = $gate.Description; passed = $passed
        skipped = $false; reason = $null; exit_code = $result.ExitCode
        timed_out = $result.TimedOut
        elapsed_seconds = $result.ElapsedSeconds
        started_at = $result.StartedAt.ToString('o')
        command = @(Get-PortableLocation $gate.FilePath) + (Get-PortableArguments $gate.Arguments)
        cwd = Get-PortableLocation $gate.WorkingDirectory
        log = Get-PortableLocation $logPath
        tail = $tail
        tested_paths = @($gate.TestedPaths)
    }
    if (-not $passed) {
        $why = if ($result.TimedOut) {
            "timed out after $($gate.TimeoutSeconds)s"
        } else {
            "exit $($result.ExitCode)"
        }
        Write-Host ("FAIL {0}: {1}" -f $gate.Id, $why) -ForegroundColor Red
        foreach ($line in $tail) { Write-Host ("  | {0}" -f $line) -ForegroundColor DarkYellow }
        Write-Host ("  full log: {0}" -f $logPath)
        $failedGate = $gate.Id
        break
    }
    Write-Host ("PASS {0} in {1}s" -f $gate.Id, $result.ElapsedSeconds) -ForegroundColor Green
}

$TotalWatch.Stop()
$FinishedAt = [DateTime]::UtcNow

# ---------------------------------------------------------------- outcome
$executed = @($gates | Where-Object { -not $_.skipped })
$skipped = @($gates | Where-Object { $_.skipped })
$checksRun = $executed.Count
$checksSkipped = $skipped.Count
$testedPaths = @($executed | ForEach-Object { $_.tested_paths } | Sort-Object -Unique)

if ($null -ne $failedGate) {
    $result = 'failed'
    $exitCode = 1
}
elseif ($checksRun -eq 0) {
    # Zero executed gates is not a pass. The audited runner returned 0 here with
    # {"result":"passed","checksRun":0}.
    $result = 'skipped'
    $exitCode = 2
}
elseif ($checksSkipped -gt 0) {
    $result = 'partial'
    $exitCode = 3
}
else {
    $result = 'passed'
    $exitCode = 0
}

$nextBuild = "bash scripts/build-tvos.sh build/tvos-$shortCommit"
$receipt = [ordered]@{
    schema          = 'omakase_tvos_qualification_receipt_v2'
    kind            = 'portable-gates'
    result          = $result
    failed_gate     = $failedGate
    checks_run      = $checksRun
    checks_skipped  = $checksSkipped
    checks_planned  = $gatePlan.Count
    exit_code       = $exitCode
    started_at      = $StartedAt.ToString('o')
    finished_at     = $FinishedAt.ToString('o')
    elapsed_seconds = [math]::Round($TotalWatch.Elapsed.TotalSeconds, 3)
    host            = $env:COMPUTERNAME
    source          = [ordered]@{
        root              = $Repo
        branch            = $branch
        commit            = $commit
        commit_subject    = $subject
        tree              = $tree
        base_commit       = $BaseCommit
        base_commit_known = $baseKnown
        snapshot_sha256   = $snapshotDigest
        index_sha256      = $indexDigest
        diff_sha256       = $diffDigest
        untracked_sha256  = $untrackedDigest
        untracked_count   = $untrackedPaths.Count
        dirty_count       = $dirtyPaths.Count
        dirty_paths       = $dirtyPaths
        tested_paths      = $testedPaths
        digest_scope      = @(
            'git ls-files -s (tracked blob identities)'
            "git diff --binary $BaseCommit"
            'untracked files hashed per path'
            'excludes: build/, __pycache__, *.pyc'
        )
    }
    toolchains      = $toolchains
    gates           = $gates
    not_run         = @(
        [ordered]@{
            action       = 'tvOS build'
            why          = 'requires macOS and full Xcode; this runner never builds'
            next_command = $nextBuild
        },
        [ordered]@{
            action       = 'simulator UI tests'
            why          = 'requires macOS, the tvOS simulator and the connected fixture'
            next_command = 'bash scripts/fixtures/tvos-connected/start_connected_fixture.sh <udid> <scratch>; xcodebuild test -only-testing:OmakasePlusTVUITests/ConnectedQualification OMAKASE_QUALIFICATION_ORIGIN=https://127.0.0.1:8854'
        },
        [ordered]@{
            action       = 'sign and install'
            why          = 'separate authorized action; must bind the built artifact hash'
            next_command = 'see docs/qualification/APPLE-TV-QUALIFICATION.md step 3 (sign and install)'
        },
        [ordered]@{
            action       = 'physical acceptance'
            why          = 'requires the Apple TV, the remote and a watched session'
            next_command = 'see docs/qualification/APPLE-TV-QUALIFICATION.md step 4'
        }
    )
    next_command    = $nextBuild
    notes           = @(
        'Portable gates prove logic only: they do not prove a tvOS build, a signature, an install or on-device behaviour.'
        'The connected UI fixture compiles in the tvOS UI test target and has not been compiled or run on Windows.'
        'checks_run counts gates that actually executed; a run with zero executed gates is `skipped`, never `passed`.'
        'snapshot_sha256 binds the tracked tree, the diff against base_commit and every untracked file, excluding build outputs.'
    )
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null
$receipt | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $OutputPath -Encoding utf8

# ---------------------------------------------------------------- summary
$color = switch ($result) {
    'passed' { 'Green' }
    'partial' { 'Yellow' }
    default { 'Red' }
}
Write-Host ''
Write-Host '=== qualification summary ===' -ForegroundColor Cyan
Write-Host ("result    : {0} (exit {1})" -f $result.ToUpperInvariant(), $exitCode) -ForegroundColor $color
Write-Host ("gates     : {0} run, {1} skipped of {2}" -f $checksRun, $checksSkipped, $gatePlan.Count)
Write-Host ("source    : {0} @ {1} ({2} dirty path(s), {3} untracked)" -f `
    $branch, $shortCommit, $dirtyPaths.Count, $untrackedPaths.Count)
Write-Host ("snapshot  : {0}" -f $snapshotDigest)
Write-Host ("base      : {0}" -f $BaseCommit)
Write-Host ("elapsed   : {0}s" -f [math]::Round($TotalWatch.Elapsed.TotalSeconds, 1))
foreach ($gate in $gates) {
    $state = if ($gate.skipped) { 'SKIP' } elseif ($gate.passed) { 'PASS' } else { 'FAIL' }
    $suffix = if ($gate.skipped) { "  ($($gate.reason))" } else { '' }
    Write-Host ("  {0,-4} {1,-24} {2,8}s  exit={3}{4}" -f `
        $state, $gate.id, $gate.elapsed_seconds, $gate.exit_code, $suffix)
}
Write-Host ("receipt   : {0}" -f $OutputPath)
Write-Host ''
Write-Host 'Next step is separate and NOT run by this script:' -ForegroundColor Yellow
Write-Host ("  {0}" -f $nextBuild) -ForegroundColor Yellow
Write-Host '  (macOS with full Xcode; then the simulator suite, then sign/install, then physical acceptance)'

exit $exitCode
