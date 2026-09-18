param(
    [switch]$NoBrowser
)

$scriptDir = $PSScriptRoot
Set-Location -LiteralPath $scriptDir

Write-Host "============================================"
Write-Host "  Starting Trading Bot Environment"
Write-Host "============================================"
Write-Host ""

# 0. Stop only this script's own dashboard (never the grid UI on 5175, never python)
function Kill-ProcessOnPort($port) {
    $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | Where-Object { $_.State -eq 'Listen' }
    foreach ($conn in $connections) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) { Write-Host "Stopping $($proc.ProcessName) PID $($proc.Id) on port $port"; Stop-Process -Id $proc.Id -Force }
    }
}

Write-Host "[0/6] Stopping existing processes..."
Kill-ProcessOnPort 5173
$apiAlreadyRunning = [bool](Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue)
if ($apiAlreadyRunning) {
    Write-Host "      API server already running on 5000 - reusing it (grid engine stays up)"
} else {
    Write-Host "      API server not listening on 5000 - will start it"
}
Write-Host "      OK"
Write-Host ""

# 0b. Build dashboard
Write-Host "[0b/6] Building dashboard..."
Set-Location -LiteralPath $scriptDir
pnpm --filter @workspace/dashboard run build
if ($LASTEXITCODE -ne 0) {
    Write-Warning "      Dashboard build failed, continuing anyway..."
}
Write-Host "      OK"
Write-Host ""

# 1. Init database
Write-Host "[1/6] Initializing database..."
if (Test-Path "$scriptDir\data\bot.db") {
    Copy-Item -Path "$scriptDir\data\bot.db" -Destination "$scriptDir\data\bot.db.bak" -Force
    Write-Host "      Backed up data/bot.db"
}
Set-Location -LiteralPath $scriptDir
pnpm run init-db 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "[FAIL] init-db error!"
    exit 1
}
Write-Host "      OK"
Write-Host ""

# 2. Start API server in background (reuse if already listening on 5000)
Write-Host "[2/6] Starting API server..."
Set-Location -LiteralPath $scriptDir
if ($apiAlreadyRunning) {
    Write-Host "      Reusing existing API server on 5000"
} else {
    New-Item -ItemType Directory -Force -Path "$scriptDir\logs" | Out-Null
    $logStamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $apiLog = "logs\api-server_$logStamp.log"
    $env:BOT_DIR = "bot"; $env:DATABASE_PATH = ".\data\bot.db"; $env:PORT = "5000"
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c pnpm --filter @workspace/api-server run dev > $apiLog 2>&1" -WindowStyle Minimized -WorkingDirectory $scriptDir
    Write-Host "      API log: $apiLog"
}
Start-Sleep -Seconds 3

# Smoke check API (up to 30 seconds)
$apiOK = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-RestMethod "http://localhost:5000/api/bots" -ErrorAction Stop | Out-Null
        $apiOK = $true
        break
    } catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $apiOK) {
    Write-Warning "      API health check failed"
}
Write-Host "      OK - http://localhost:5000"
Write-Host ""

# 3. Start Dashboard in background
Write-Host "[3/6] Starting Dashboard..."
Set-Location -LiteralPath $scriptDir
Start-Process powershell -ArgumentList "-NoProfile -Command Set-Location '$scriptDir'; pnpm --filter @workspace/dashboard run dev" -WindowStyle Hidden -WorkingDirectory $scriptDir
Start-Sleep -Seconds 2

# Smoke check Dashboard (up to 30 seconds)
$dbOK = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-WebRequest "http://localhost:5173" -UseBasicParsing -ErrorAction Stop | Out-Null
        $dbOK = $true
        break
    } catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $dbOK) {
    Write-Warning "      Dashboard health check failed"
}
Write-Host "      OK - http://localhost:5173"
Write-Host ""

# 4. Bots are started manually (via the dashboard Start button / API)
#    This script intentionally does NOT auto-start bots.
$botConfigs = Get-ChildItem -Path "$scriptDir\bot" -Filter "config_*.yaml"
Write-Host "[4/6] Bots: manual start ($($botConfigs.Count) configs found) - use dashboard 'Start' button"
Write-Host ""

# 5. Final status
Write-Host "[5/6] Startup complete!"
Write-Host "============================================"
Write-Host "  All services started!"
Write-Host "  API:       http://localhost:5000"
Write-Host "  Dashboard: http://localhost:5173"
Write-Host "  Grid UI:   http://localhost:5175 (runs separately via start_grid.ps1)"
Write-Host "  Bots:      start manually via dashboard ($($botConfigs.Count) configs)"
Write-Host "============================================"
Write-Host ""

if (-not $NoBrowser) {
    Write-Host "Opening dashboard in browser..."
    Start-Process "http://localhost:5173"
}

Write-Host "To stop: stop the bot(s) via the dashboard, then close the API/dashboard windows (or Kill-ProcessOnPort 5000/5173)."
