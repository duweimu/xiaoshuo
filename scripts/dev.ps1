param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "stop", "restart")]
    [string]$Action,
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

    Write-Step -Message $Label
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

function Assert-CommandAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw ("Required command not found: {0}" -f $Name)
    }
}

function Wait-Until {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Condition,
        [int]$TimeoutSeconds = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) {
            return
        }
        Start-Sleep -Seconds 1
    }

    throw ("Timed out waiting for {0}." -f $Label)
}

function Test-UrlHealthy {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url
    )

    try {
        $response = Invoke-WebRequest -UseBasicParsing $Url -TimeoutSec 5
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Test-PortBindable {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $listener = $null
    try {
        $address = [System.Net.IPAddress]::Parse("127.0.0.1")
        $listener = [System.Net.Sockets.TcpListener]::new($address, $Port)
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $listener) {
            $listener.Stop()
        }
    }
}

function Resolve-AvailablePort {
    param(
        [Parameter(Mandatory = $true)]
        [int]$PreferredPort,
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [int[]]$ReservedPorts = @(),
        [int]$ScanLimit = 200
    )

    for ($port = $PreferredPort; $port -le ($PreferredPort + $ScanLimit); $port++) {
        if (($ReservedPorts -notcontains $port) -and (Test-PortBindable -Port $port)) {
            if ($port -ne $PreferredPort) {
                Write-Step -Message ("{0} preferred port {1} is unavailable; using {2}." -f $Label, $PreferredPort, $port)
            }
            return $port
        }
    }

    throw ("No available {0} port found in range {1}-{2}." -f $Label, $PreferredPort, ($PreferredPort + $ScanLimit))
}

function Get-RecordedRootProcessIds {
    $recorded = New-Object System.Collections.Generic.List[int]
    foreach ($pidFile in @($script:BackendPidFile, $script:FrontendPidFile, $script:ReactPidFile)) {
        if (-not (Test-Path $pidFile)) {
            continue
        }

        foreach ($line in (Get-Content -Path $pidFile -ErrorAction SilentlyContinue)) {
            $value = 0
            if ([int]::TryParse(($line | Out-String).Trim(), [ref]$value)) {
                [void]$recorded.Add($value)
            }
        }
    }

    return @($recorded | Select-Object -Unique)
}

function Test-ProcessAlive {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Get-LiveRecordedRootProcessIds {
    return @(
        Get-RecordedRootProcessIds | Where-Object { Test-ProcessAlive -ProcessId $_ }
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

function Remove-RunState {
    Remove-Item $script:BackendPidFile, $script:FrontendPidFile, $script:ReactPidFile, $script:BackendUrlFile, $script:FrontendUrlFile, $script:ReactUrlFile -ErrorAction SilentlyContinue
}

function ConvertTo-SingleQuotedPowerShellLiteral {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    return "'{0}'" -f ($Value -replace "'", "''")
}

function Resolve-DevConfigSecret {
    if ($env:NOVEL_SYSTEM_CONFIG_SECRET) {
        return $env:NOVEL_SYSTEM_CONFIG_SECRET
    }

    New-Item -ItemType Directory -Path $script:RunDir -Force | Out-Null
    if (Test-Path -LiteralPath $script:ConfigSecretFile) {
        $existing = (Get-Content -LiteralPath $script:ConfigSecretFile -Raw -ErrorAction SilentlyContinue).Trim()
        if ($existing) {
            return $existing
        }
    }

    $secret = "{0}{1}" -f ([guid]::NewGuid().ToString("N")), ([guid]::NewGuid().ToString("N"))
    Set-Content -LiteralPath $script:ConfigSecretFile -Value $secret -NoNewline
    return $secret
}

function Clear-PreviousLogs {
    Remove-Item $script:BackendOutLog, $script:BackendErrLog, $script:FrontendOutLog, $script:FrontendErrLog, $script:ReactOutLog, $script:ReactErrLog -ErrorAction SilentlyContinue
}

function Stop-TrackedServices {
    $recordedIds = @(Get-RecordedRootProcessIds)
    if ($recordedIds.Count -eq 0) {
        Write-Step -Message "No tracked dev services are running."
        Remove-RunState
        return
    }

    Write-Step -Message "Stopping tracked dev services"
    $allIds = @(Get-DescendantProcessIds -RootProcessIds $recordedIds)
    foreach ($processId in ($allIds | Sort-Object -Descending)) {
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
        }
        catch {
        }
    }

    Start-Sleep -Seconds 2
    Remove-RunState
}

function Invoke-BackendBootstrap {
    $previousPythonPath = $env:PYTHONPATH
    $previousVectorBackend = $env:NOVEL_SYSTEM_VECTOR_BACKEND
    $previousConfigSecret = $env:NOVEL_SYSTEM_CONFIG_SECRET

    try {
        $env:PYTHONPATH = "src"
        $env:NOVEL_SYSTEM_VECTOR_BACKEND = "memory"
        $env:NOVEL_SYSTEM_CONFIG_SECRET = Resolve-DevConfigSecret
        Invoke-NativeStep -Label "Backend migration" -WorkingDirectory $script:BackendDir -FilePath $script:PythonExe -ArgumentList @("-m", "alembic", "upgrade", "head")
        # Production startup runs migrations only; it never seeds demo projects.
    }
    finally {
        if ($null -eq $previousPythonPath) {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        }
        else {
            $env:PYTHONPATH = $previousPythonPath
        }

        if ($null -eq $previousVectorBackend) {
            Remove-Item Env:NOVEL_SYSTEM_VECTOR_BACKEND -ErrorAction SilentlyContinue
        }
        else {
            $env:NOVEL_SYSTEM_VECTOR_BACKEND = $previousVectorBackend
        }

        if ($null -eq $previousConfigSecret) {
            Remove-Item Env:NOVEL_SYSTEM_CONFIG_SECRET -ErrorAction SilentlyContinue
        }
        else {
            $env:NOVEL_SYSTEM_CONFIG_SECRET = $previousConfigSecret
        }
    }
}

function Show-StartupFailureDiagnostics {
    param(
        [string]$FailureMessage
    )

    Write-Host ""
    Write-Host "==> Startup failed before all services became healthy." -ForegroundColor Red
    if ($FailureMessage) {
        Write-Host ("    {0}" -f $FailureMessage) -ForegroundColor Red
    }

    # Surface the backend's actual HTTP status + body. The most common failure is the
    # backend answering with a non-200 (e.g. a 500 from a stale DB schema) — a plain
    # "Timed out waiting for backend health" message hides that root cause.
    try {
        $resp = Invoke-WebRequest -UseBasicParsing $script:BackendHealthUrl -TimeoutSec 5
        Write-Host ("Backend {0}: HTTP {1}" -f $script:BackendHealthUrl, $resp.StatusCode) -ForegroundColor Yellow
    }
    catch {
        $probeError = $_
        $statusCode = $null
        $body = $null
        $exception = $probeError.Exception
        if (($exception -is [System.Net.WebException]) -and ($null -ne $exception.Response)) {
            try { $statusCode = [int]([System.Net.HttpWebResponse]$exception.Response).StatusCode } catch {}
            try {
                $reader = New-Object System.IO.StreamReader($exception.Response.GetResponseStream())
                $body = $reader.ReadToEnd()
                $reader.Close()
            }
            catch {}
        }
        if (-not $body) {
            $errorDetails = $probeError.ErrorDetails
            if ($null -ne $errorDetails) { $body = $errorDetails.Message }
        }
        if ($statusCode) {
            Write-Host ("Backend {0}: HTTP {1}" -f $script:BackendHealthUrl, $statusCode) -ForegroundColor Red
        }
        else {
            Write-Host ("Backend {0} not reachable: {1}" -f $script:BackendHealthUrl, $exception.Message) -ForegroundColor Red
        }
        if ($body) { Write-Host ("Backend response: {0}" -f $body.Trim()) -ForegroundColor Red }
    }

    # Dump the tail of each service log so the underlying error is visible inline.
    $logTargets = @(
        @{ Label = "backend.err.log"; Path = $script:BackendErrLog },
        @{ Label = "backend.out.log"; Path = $script:BackendOutLog },
        @{ Label = "frontend-react.err.log"; Path = $script:ReactErrLog },
        @{ Label = "frontend.err.log"; Path = $script:FrontendErrLog }
    )
    foreach ($target in $logTargets) {
        if (Test-Path $target.Path) {
            $tail = Get-Content -Path $target.Path -Tail 20 -ErrorAction SilentlyContinue
            if ($tail) {
                Write-Host ("----- {0} (last 20 lines) -----" -f $target.Label) -ForegroundColor Yellow
                foreach ($line in $tail) { Write-Host $line }
            }
        }
    }
    Write-Host ("Full logs: {0}" -f $script:RunDir) -ForegroundColor Yellow
    Write-Host ("Tip: a stale schema (`"no such column ...`") means migrations are behind — run: cd backend; python -m alembic upgrade head") -ForegroundColor Yellow
    Write-Host ""
}

function Start-TrackedServices {
    Assert-CommandAvailable -Name "npm.cmd"
    if (-not (Test-Path -LiteralPath $script:PythonExe -PathType Leaf)) {
        throw "Locked backend Python is missing: $script:PythonExe. Run: cd backend; uv sync --locked --extra dev"
    }

    $artifactRetentionDays = if ($env:NOVEL_SYSTEM_ARTIFACT_RETENTION_DAYS) {
        $env:NOVEL_SYSTEM_ARTIFACT_RETENTION_DAYS
    } else {
        "14"
    }
    Invoke-NativeStep -Label "Pruning expired reproducible runtime artifacts" -WorkingDirectory $repoRoot -FilePath $script:PythonExe -ArgumentList @(
        (Join-Path $PSScriptRoot "cleanup_runtime_artifacts.py"),
        "--run-dir",
        $script:RunDir,
        "--retention-days",
        $artifactRetentionDays,
        "--apply"
    )

    $liveTrackedIds = @(Get-LiveRecordedRootProcessIds)
    if ($liveTrackedIds.Count -gt 0) {
        throw "Tracked dev services already appear to be running. Use .\\restart-dev.cmd or .\\stop-dev.cmd first."
    }

    Remove-RunState
    $script:BackendPort = Resolve-AvailablePort -PreferredPort $script:BackendPreferredPort -Label "Backend"
    $script:BackendUrl = "http://127.0.0.1:$script:BackendPort"
    $script:BackendHealthUrl = "$script:BackendUrl/ready"
    $reservedPorts = @($script:BackendPort)
    $script:ReactPort = Resolve-AvailablePort -PreferredPort $script:ReactPreferredPort -Label "React frontend" -ReservedPorts $reservedPorts
    $script:ReactUrl = "http://127.0.0.1:$script:ReactPort"

    New-Item -ItemType Directory -Path $script:RunDir -Force | Out-Null
    Clear-PreviousLogs
    Invoke-BackendBootstrap

    try {
        Write-Step -Message "Starting backend on $script:BackendUrl"
        $configSecretLiteral = ConvertTo-SingleQuotedPowerShellLiteral -Value (Resolve-DevConfigSecret)
        $pythonLiteral = ConvertTo-SingleQuotedPowerShellLiteral -Value $script:PythonExe
        $backendCommand = '$env:PYTHONPATH = ''src''; $env:NOVEL_SYSTEM_VECTOR_BACKEND = ''memory''; $env:NOVEL_SYSTEM_CONFIG_SECRET = {0}; & {1} -m uvicorn novel_system.api.app:create_app --factory --reload --host 127.0.0.1 --port {2} --app-dir src' -f $configSecretLiteral, $pythonLiteral, $script:BackendPort
        $backendProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $backendCommand) -WorkingDirectory $script:BackendDir -RedirectStandardOutput $script:BackendOutLog -RedirectStandardError $script:BackendErrLog -PassThru
        Set-Content -Path $script:BackendPidFile -Value $backendProcess.Id
        Set-Content -Path $script:BackendUrlFile -Value $script:BackendUrl

        Write-Step -Message "Starting React frontend on $script:ReactUrl"
        $reactCommand = '$env:VITE_NOVEL_SYSTEM_API_BASE = ''{0}''; npm.cmd run dev -- --host 127.0.0.1 --port {1}' -f $script:BackendUrl, $script:ReactPort
        $reactProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $reactCommand) -WorkingDirectory $script:ReactDir -RedirectStandardOutput $script:ReactOutLog -RedirectStandardError $script:ReactErrLog -PassThru
        Set-Content -Path $script:ReactPidFile -Value $reactProcess.Id
        Set-Content -Path $script:ReactUrlFile -Value $script:ReactUrl

        Wait-Until -Label "backend health" -Condition { Test-UrlHealthy -Url $script:BackendHealthUrl } -TimeoutSeconds 90
        Wait-Until -Label "react frontend home" -Condition { Test-UrlHealthy -Url $script:ReactUrl } -TimeoutSeconds 60
    }
    catch {
        try { Show-StartupFailureDiagnostics -FailureMessage $_.Exception.Message } catch {}
        Stop-TrackedServices
        throw
    }

    Write-Host ("Backend:  {0}" -f $script:BackendUrl) -ForegroundColor Green
    Write-Host ("React:    {0}  (default)" -f $script:ReactUrl) -ForegroundColor Green
    Write-Host ("Logs:     {0}" -f $script:RunDir) -ForegroundColor Green

    try {
        Start-Process $script:ReactUrl
    }
    catch {
        Write-Host ("Could not auto-open a browser; open {0} manually." -f $script:ReactUrl) -ForegroundColor Yellow
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$script:BackendDir = Join-Path $repoRoot "backend"
$script:ReactDir = Join-Path $repoRoot "frontend-react"
$script:RunDir = Join-Path $repoRoot ".codex-run"
$script:BackendPidFile = Join-Path $script:RunDir "backend.pid"
$script:FrontendPidFile = Join-Path $script:RunDir "frontend.pid"
$script:ReactPidFile = Join-Path $script:RunDir "frontend-react.pid"
$script:BackendUrlFile = Join-Path $script:RunDir "backend.url"
$script:FrontendUrlFile = Join-Path $script:RunDir "frontend.url"
$script:ReactUrlFile = Join-Path $script:RunDir "frontend-react.url"
$script:ConfigSecretFile = Join-Path $script:RunDir "config.secret"
$script:BackendOutLog = Join-Path $script:RunDir "backend.out.log"
$script:BackendErrLog = Join-Path $script:RunDir "backend.err.log"
$script:FrontendOutLog = Join-Path $script:RunDir "frontend.out.log"
$script:FrontendErrLog = Join-Path $script:RunDir "frontend.err.log"
$script:ReactOutLog = Join-Path $script:RunDir "frontend-react.out.log"
$script:ReactErrLog = Join-Path $script:RunDir "frontend-react.err.log"
$script:BackendPreferredPort = 8000
$script:ReactPreferredPort = 5174
$script:PythonExe = Join-Path $script:BackendDir ".venv\Scripts\python.exe"
$script:BackendPort = $script:BackendPreferredPort
$script:ReactPort = $script:ReactPreferredPort
$script:BackendUrl = "http://127.0.0.1:$script:BackendPort"
$script:BackendHealthUrl = "$script:BackendUrl/ready"
$script:ReactUrl = "http://127.0.0.1:$script:ReactPort"

switch ($Action) {
    "start" {
        Start-TrackedServices
    }
    "stop" {
        Stop-TrackedServices
    }
    "restart" {
        Stop-TrackedServices
        Start-TrackedServices
    }
}
