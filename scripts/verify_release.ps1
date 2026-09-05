param(
    [string]$Distro = "Ubuntu-24.04"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$ArgumentList = @()
    )

    Write-Host "==> $Label" -ForegroundColor Cyan
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw ("Native command failed with exit code {0}: {1} {2}" -f $LASTEXITCODE, $FilePath, ($ArgumentList -join " "))
    }
}

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
$windowsScript = Join-Path $repoRoot "scripts\verify_windows.ps1"
$repoRootForWslPath = $repoRoot -replace "\\", "/"

$windowsArgs = @("-ExecutionPolicy", "Bypass", "-File", $windowsScript)
Invoke-NativeCommand -Label "Windows verification lane" -FilePath "powershell" -ArgumentList $windowsArgs

# React mainline contract E2E (run-smokes.mjs) is the default release gate for the
# production frontend: verify_react_e2e.ps1 spins up an isolated seeded backend on :8009
# + the React app on :5174, runs the smoke suites (reseeding between each), then tears
# everything down. Needs Playwright installed in frontend-react/ (cd frontend-react; npm ci).
$reactE2eScript = Join-Path $repoRoot "scripts\verify_react_e2e.ps1"
Invoke-NativeCommand -Label "React mainline contract E2E (run-smokes)" -FilePath "powershell" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $reactE2eScript)

$repoRootWsl = (& wsl.exe -d $Distro wslpath -a "$repoRootForWslPath" | Out-String).Trim()
if (-not $repoRootWsl) {
    throw "Could not resolve the repository path inside WSL."
}

$bashCommand = "cd '$repoRootWsl' && bash scripts/verify_wsl_strict.sh"

Invoke-NativeCommand -Label "WSL strict Chroma verification lane" -FilePath "wsl.exe" -ArgumentList @("-d", $Distro, "bash", "-lc", $bashCommand)
