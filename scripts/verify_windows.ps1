param(
    [switch]$BackendOnly,
    [switch]$FrontendOnly,
    [ValidateRange(1, 16)]
    [int]$BackendShardCount = 4
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-NativeStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$ArgumentList = @()
    )

    Write-Host "==> $Label" -ForegroundColor Cyan
    Push-Location $WorkingDirectory
    try {
        & $FilePath @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            throw ("Native command failed with exit code {0}: {1} {2}" -f $LASTEXITCODE, $FilePath, ($ArgumentList -join " "))
        }
    }
    finally {
        Pop-Location
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot "backend"
$backendPython = Join-Path $backendDir ".venv\Scripts\python.exe"
$reactDir = Join-Path $repoRoot "frontend-react"

if (-not $FrontendOnly) {
    if (-not (Test-Path -LiteralPath $backendPython -PathType Leaf)) {
        throw "Locked backend Python is missing: $backendPython. Run: cd backend; uv sync --locked --extra dev"
    }
    Invoke-NativeStep -Label "Backend correctness lint" -WorkingDirectory $backendDir -FilePath $backendPython -ArgumentList @("-m", "ruff", "check", "src", "tests")
    Invoke-NativeStep -Label "Backend dependency audit" -WorkingDirectory $backendDir -FilePath $backendPython -ArgumentList @("-m", "pip_audit", "-r", "requirements.lock", "--disable-pip", "--require-hashes")
    $backendResultsDir = Join-Path $backendDir ".test-results"
    New-Item -ItemType Directory -Force -Path $backendResultsDir | Out-Null
    for ($shardIndex = 0; $shardIndex -lt $BackendShardCount; $shardIndex++) {
        $junitPath = Join-Path $backendResultsDir ("backend-windows-shard-{0}.xml" -f $shardIndex)
        Invoke-NativeStep `
            -Label ("Backend pytest non-Chroma shard {0}/{1}" -f ($shardIndex + 1), $BackendShardCount) `
            -WorkingDirectory $backendDir `
            -FilePath $backendPython `
            -ArgumentList @(
                "scripts\pytest_shard.py",
                "--shard-index", "$shardIndex",
                "--shard-count", "$BackendShardCount",
                "--",
                "-q",
                "-m", "not chroma_integration",
                "--junitxml=$junitPath"
            )
    }
}

if (-not $BackendOnly) {

    # React mainline (frontend-react) is the default frontend gate: vitest unit tests + build.
    Invoke-NativeStep -Label "React frontend tests" -WorkingDirectory $reactDir -FilePath "npm.cmd" -ArgumentList @("test")
    Invoke-NativeStep -Label "React frontend build" -WorkingDirectory $reactDir -FilePath "npm.cmd" -ArgumentList @("run", "build")
}
