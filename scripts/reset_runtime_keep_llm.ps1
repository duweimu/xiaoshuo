param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DatabasePath = "",
    [switch]$StopServices,
    [switch]$SkipServiceCheck,
    [switch]$NoArtifactCleanup,
    [switch]$NoVacuum
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-ExistingOrParentPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (Test-Path -LiteralPath $Path) {
        return (Resolve-Path -LiteralPath $Path).Path
    }

    $parent = Split-Path -Parent $Path
    if ([string]::IsNullOrWhiteSpace($parent)) {
        $parent = "."
    }
    $resolvedParent = (Resolve-Path -LiteralPath $parent).Path
    return (Join-Path $resolvedParent (Split-Path -Leaf $Path))
}

function Assert-PathInsideRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $resolvedPath = Resolve-ExistingOrParentPath -Path $Path
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
    if (-not ($resolvedPath -eq $resolvedRoot -or $resolvedPath.StartsWith($resolvedRoot + [System.IO.Path]::DirectorySeparatorChar))) {
        throw ("Refusing to operate outside repo root: {0}" -f $resolvedPath)
    }
    return $resolvedPath
}

function Get-RecordedRootProcessIds {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RunDir
    )

    $recorded = New-Object System.Collections.Generic.List[int]
    foreach ($pidFileName in @("backend.pid", "frontend.pid")) {
        $pidFile = Join-Path $RunDir $pidFileName
        if (-not (Test-Path -LiteralPath $pidFile)) {
            continue
        }

        foreach ($line in (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue)) {
            $value = 0
            if ([int]::TryParse(($line | Out-String).Trim(), [ref]$value)) {
                [void]$recorded.Add($value)
            }
        }
    }

    return @($recorded | Select-Object -Unique)
}

function Get-LiveProcessIds {
    param(
        [Parameter(Mandatory = $true)]
        [int[]]$ProcessIds
    )

    return @(
        $ProcessIds | Where-Object {
            $null -ne (Get-Process -Id $_ -ErrorAction SilentlyContinue)
        }
    )
}

function Get-DescendantProcessIds {
    param(
        [Parameter(Mandatory = $true)]
        [int[]]$RootProcessIds
    )

    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    $allIds = New-Object System.Collections.Generic.HashSet[int]
    $queue = [System.Collections.Generic.Queue[int]]::new()

    foreach ($rootId in $RootProcessIds) {
        if ($allIds.Add($rootId)) {
            $queue.Enqueue($rootId)
        }
    }

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        $children = @($processes | Where-Object { $_.ParentProcessId -eq $current })
        foreach ($child in $children) {
            $childId = [int]$child.ProcessId
            if ($allIds.Add($childId)) {
                $queue.Enqueue($childId)
            }
        }
    }

    return @($allIds)
}

function Get-RepoServiceProcessIds {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $processIds = New-Object System.Collections.Generic.HashSet[int]
    $portOwners = @(
        Get-NetTCPConnection -State Listen -LocalPort 8000, 5174 -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
    if ($portOwners.Count -eq 0) {
        return @()
    }

    $normalizedRoot = $Root.ToLowerInvariant()
    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    foreach ($processId in $portOwners) {
        $process = $processes | Where-Object { [int]$_.ProcessId -eq [int]$processId } | Select-Object -First 1
        if ($null -eq $process) {
            continue
        }
        $commandLine = [string]($process.CommandLine)
        if ($commandLine.ToLowerInvariant().Contains($normalizedRoot)) {
            [void]$processIds.Add([int]$process.ProcessId)
        }
    }

    return @($processIds)
}

function Stop-TrackedServicesIfRequested {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$RunDir
    )

    if ($script:SkipServiceCheck) {
        return
    }

    $recordedIds = @(Get-RecordedRootProcessIds -RunDir $RunDir)
    $repoServiceIds = @(Get-RepoServiceProcessIds -Root $Root)
    $liveIds = @(Get-LiveProcessIds -ProcessIds @($recordedIds + $repoServiceIds) | Select-Object -Unique)
    if ($liveIds.Count -eq 0) {
        return
    }

    if (-not $script:StopServices) {
        throw ("Tracked dev services are still running ({0}). Re-run with -StopServices or stop them first." -f ($liveIds -join ", "))
    }

    $stopWrapper = Join-Path $Root "stop-dev.cmd"
    if (-not (Test-Path -LiteralPath $stopWrapper)) {
        throw "Tracked dev services are running, but stop-dev.cmd is missing."
    }

    Write-Step -Message "Stopping tracked dev services"
    & $stopWrapper | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw ("stop-dev.cmd failed with exit code {0}." -f $LASTEXITCODE)
    }

    $remainingIds = @(Get-LiveProcessIds -ProcessIds @($liveIds))
    if ($remainingIds.Count -gt 0) {
        $allIds = @(Get-DescendantProcessIds -RootProcessIds $remainingIds)
        foreach ($processId in ($allIds | Sort-Object -Descending)) {
            try {
                Stop-Process -Id $processId -Force -ErrorAction Stop
            }
            catch {
            }
        }
        Start-Sleep -Seconds 2
    }
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Script,
        [string[]]$ArgumentList = @()
    )

    $Script | python - @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw ("Python command failed with exit code {0}." -f $LASTEXITCODE)
    }
}

function Reset-DatabaseKeepingLlm {
    param(
        [Parameter(Mandatory = $true)]
        [string]$DbPath,
        [bool]$Vacuum
    )

    if (-not (Test-Path -LiteralPath $DbPath)) {
        Write-Step -Message ("Database not found, skipping DB reset: {0}" -f $DbPath)
        return
    }

    Write-Step -Message "Clearing SQLite runtime data while keeping LLM config"
    $python = @'
import json
import sqlite3
import sys

db_path = sys.argv[1]
vacuum = sys.argv[2] == "1"

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
tables = {
    row["name"]
    for row in conn.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
    )
}

preserved_tables = {"alembic_version", "system_config_snapshots", "system_secrets"}
deleted = {}

conn.execute("PRAGMA foreign_keys=OFF")
try:
    with conn:
        for table_name in sorted(tables - preserved_tables):
            quoted = '"' + table_name.replace('"', '""') + '"'
            cursor = conn.execute(f"delete from {quoted}")
            deleted[table_name] = cursor.rowcount

        if "system_config_snapshots" in tables:
            cursor = conn.execute(
                "delete from system_config_snapshots where category not in ('api', 'models')"
            )
            deleted["system_config_snapshots(non_llm)"] = cursor.rowcount

        if "system_secrets" in tables:
            cursor = conn.execute(
                """
                delete from system_secrets
                where not (
                    secret_id = 'llm_api_key'
                    or secret_id like 'llm_provider:%'
                )
                """
            )
            deleted["system_secrets(non_llm)"] = cursor.rowcount

    if vacuum:
        conn.execute("VACUUM")
finally:
    conn.close()

print(json.dumps({"deleted": deleted}, ensure_ascii=False, sort_keys=True))
'@

    Invoke-Python -Script $python -ArgumentList @($DbPath, ($(if ($Vacuum) { "1" } else { "0" })))
}

function Remove-GeneratedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$RelativePath
    )

    $target = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $target)) {
        return
    }

    $resolved = Assert-PathInsideRoot -Path $target -Root $Root
    Remove-Item -LiteralPath $resolved -Recurse -Force
    Write-Host ("removed {0}" -f $resolved)
}

function Remove-GeneratedFilePattern {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$RelativeDirectory,
        [Parameter(Mandatory = $true)]
        [string]$Pattern
    )

    $directory = Join-Path $Root $RelativeDirectory
    if (-not (Test-Path -LiteralPath $directory)) {
        return
    }

    $resolvedDirectory = Assert-PathInsideRoot -Path $directory -Root $Root
    Get-ChildItem -LiteralPath $resolvedDirectory -File -Filter $Pattern -ErrorAction SilentlyContinue |
        ForEach-Object {
            $resolved = Assert-PathInsideRoot -Path $_.FullName -Root $Root
            Remove-Item -LiteralPath $resolved -Force
            Write-Host ("removed {0}" -f $resolved)
        }
}

function Remove-GeneratedArtifacts {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    Write-Step -Message "Removing generated runtime and audit artifacts"
    foreach ($relativePath in @(
        ".codex-run",
        ".playwright-cli",
        "backend\.pytest_cache",
        "backend\.vector_store",
        "backend\src\novel_system.egg-info",
        "frontend\dist",
        "frontend\test-results",
        "frontend\playwright-report",
        "docs\reports"
    )) {
        Remove-GeneratedPath -Root $Root -RelativePath $relativePath
    }

    Remove-GeneratedFilePattern -Root $Root -RelativeDirectory "docs" -Pattern "*-qa-*.md"

    Get-ChildItem -LiteralPath $Root -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.FullName -notmatch "\\frontend\\node_modules(\\|$)" -and
            $_.FullName -notmatch "\\backend\\.venv(\\|$)" -and
            $_.FullName -notmatch "\\backend\\.venv-wsl(\\|$)"
        } |
        ForEach-Object {
            $resolved = Assert-PathInsideRoot -Path $_.FullName -Root $Root
            Remove-Item -LiteralPath $resolved -Recurse -Force
            Write-Host ("removed {0}" -f $resolved)
        }

    $worktreesDir = Join-Path $Root ".worktrees"
    if (Test-Path -LiteralPath $worktreesDir) {
        Get-ChildItem -LiteralPath $worktreesDir -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { @(Get-ChildItem -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue).Count -eq 0 } |
            ForEach-Object {
                $resolved = Assert-PathInsideRoot -Path $_.FullName -Root $Root
                Remove-Item -LiteralPath $resolved -Force -ErrorAction SilentlyContinue
                if (-not (Test-Path -LiteralPath $resolved)) {
                    Write-Host ("removed {0}" -f $resolved)
                }
            }
    }
}

$resolvedRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    $DatabasePath = Join-Path $resolvedRoot "backend\novel_system.db"
}
$resolvedDbPath = Assert-PathInsideRoot -Path $DatabasePath -Root $resolvedRoot
$runDir = Join-Path $resolvedRoot ".codex-run"

Stop-TrackedServicesIfRequested -Root $resolvedRoot -RunDir $runDir
Reset-DatabaseKeepingLlm -DbPath $resolvedDbPath -Vacuum (-not $NoVacuum)

if (-not $NoArtifactCleanup) {
    Remove-GeneratedArtifacts -Root $resolvedRoot
}

Write-Host "Reset complete. LLM api/models config snapshots and llm_* secrets were preserved." -ForegroundColor Green
