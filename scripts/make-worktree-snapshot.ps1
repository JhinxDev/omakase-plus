# Create an exact, verifiable source snapshot of the WORKING TREE.
#
# The tvOS build used to archive HEAD with `git archive`, so an uncommitted tree
# could not be built at all: the build silently represented the base commit
# instead of the code under review. This snapshots the paths the tvOS build
# consumes and records the identity of the whole worktree (base commit, base
# tree, full diff digest) inside the archive, so `build-tvos.sh
# --worktree-snapshot` can verify exactly what it built.
#
# Usage: pwsh -File scripts/make-worktree-snapshot.ps1 [-OutputDir build/snapshot]

param(
    [string]$OutputDir = "build/snapshot"
)

$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$out = Join-Path $repo $OutputDir
$snapshot = Join-Path $out "source.tar"
$stagedMeta = Join-Path $repo "SNAPSHOT-META.json"

Push-Location $repo
try {
    $commit = (& git rev-parse HEAD).Trim()
    $tree = (& git rev-parse 'HEAD^{tree}').Trim()
    if ($commit -notmatch '^[0-9a-f]{40}$' -or $tree -notmatch '^[0-9a-f]{40}$') {
        throw "Git identity is not a lowercase 40-character hash."
    }

    # The full diff binds the snapshot to every uncommitted change, including
    # files the tvOS build itself does not consume.
    $diffPath = Join-Path ([System.IO.Path]::GetTempPath()) ("omakase-diff-" + [guid]::NewGuid() + ".bin")
    & git diff --binary > $diffPath
    $diffDigest = (Get-FileHash $diffPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $diffBytes = (Get-Item $diffPath).Length
    Remove-Item $diffPath -Force

    $status = @(& git status --porcelain=v1 | Where-Object { $_ })
    $trackedDirty = @($status | Where-Object { -not $_.StartsWith("??") }).Count
    $untracked = @($status | Where-Object { $_.StartsWith("??") }).Count

    New-Item -ItemType Directory -Force -Path $out | Out-Null
    if (Test-Path $snapshot) { Remove-Item $snapshot -Force }

    $meta = [ordered]@{
        schema              = "omakase-worktree-snapshot-v1"
        base_commit         = $commit
        base_tree           = $tree
        dirty               = [bool]($trackedDirty + $untracked)
        tracked_dirty_count = $trackedDirty
        untracked_count     = $untracked
        full_diff_sha256    = $diffDigest
        full_diff_bytes     = $diffBytes
        paths               = @("tv-source", "scripts")
        created_by          = "scripts/make-worktree-snapshot.ps1"
    }
    ($meta | ConvertTo-Json -Depth 4) + "`n" |
        Set-Content -Path $stagedMeta -Encoding utf8 -NoNewline

    $exclude = @(
        "--exclude=tv-source/QualificationCore/.build",
        "--exclude=*__pycache__*",
        "--exclude=*.pyc",
        "--exclude=*/DerivedData*",
        "--exclude=build/*"
    )
    try {
        & tar -cf $snapshot @exclude "SNAPSHOT-META.json" "tv-source" "scripts"
        if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }
    }
    finally {
        Remove-Item $stagedMeta -Force -ErrorAction SilentlyContinue
    }

    $sha = (Get-FileHash $snapshot -Algorithm SHA256).Hash.ToLowerInvariant()
    $bytes = (Get-Item $snapshot).Length
    "$sha  source.tar" |
        Set-Content -Path (Join-Path $out "source.tar.sha256") -Encoding utf8 -NoNewline

    "snapshot   $snapshot"
    "sha256     $sha"
    "bytes      $bytes"
    "base       $commit"
    "base_tree  $tree"
    "dirty      $($meta.dirty) (tracked $trackedDirty, untracked $untracked)"
    "diff       $diffDigest ($diffBytes bytes)"
}
finally {
    Pop-Location
}
