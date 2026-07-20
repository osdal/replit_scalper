param(
    [switch]$NoBrowser
)

$scriptDir = $PSScriptRoot
Set-Location -LiteralPath $scriptDir

Write-Host "============================================"
Write-Host "  Starting Trading Bot Environment"
Write-Host "============================================"
Write-Host ""

# 0. Stop existing processes before starting new ones
Write-Host "[0/5] Stopping existing processes..."
Stop-Process -Name node -ErrorAction SilentlyContinue -Force
Stop-Process -Name python -ErrorAction SilentlyContinue -Force
# Принудительно освобождаем порт 5000 (старый API может не отпустить
# сокет сразу после Stop-Process), иначе новый сервер упадёт с EADDRINUSE
$portPid = (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue).OwningProcess
if ($portPid) {
    Stop-Process -Id $portPid -Force -ErrorAction SilentlyContinue
}
# Ждём реального освобождения порта, а не фиксированные 2 секунды
$waited = 0
while ((Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue) -and $waited -lt 10) {
    Start-Sleep -Seconds 1
    $waited++
}
Write-Host "      OK"
Write-Host ""

# 1. Init database
Write-Host "[1/5] Initializing database..."

# Backup existing database
if (Test-Path "$scriptDir\data\bot.db") {
    Copy-Item -Path "$scriptDir\data\bot.db" -Destination "$scriptDir\data\bot.db.bak" -Force
    Write-Host "      Backed up data/bot.db"
}

pnpm run init-db
if ($LASTEXITCODE -ne 0) {
    Write-Error "[FAIL] init-db error!"
    exit 1
}
Write-Host "      OK"
Write-Host ""

# 2. Start API server in background
Write-Host "[2/5] Starting API server..."
$env:BOT_DIR = "bot"
$env:DATABASE_PATH = ".\data\bot.db"
$env:PORT = "5000"
Start-Process -FilePath "cmd" -ArgumentList "/c pnpm --filter @workspace/api-server run dev" -WindowStyle Hidden -WorkingDirectory $scriptDir
Start-Sleep -Seconds 3

# Smoke check API
$apiOK = $false
for ($i = 0; $i -lt 5; $i++) {
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
Write-Host "[3/5] Starting Dashboard..."
Start-Process -FilePath "cmd" -ArgumentList "/c pnpm --filter @workspace/dashboard run dev" -WindowStyle Hidden -WorkingDirectory $scriptDir
Start-Sleep -Seconds 2

# Smoke check Dashboard
$dbOK = $false
for ($i = 0; $i -lt 5; $i++) {
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

# 4. Start Bots (auto-detect all config files)
Write-Host "[4/5] Starting bots..."
$botConfigs = Get-ChildItem -Path "$scriptDir\bot" -Filter "config_*.yaml"
foreach ($config in $botConfigs) {
    $botName = $config.BaseName -replace '^config_', ''
    Write-Host "      Starting $botName Bot..."
    Start-Process -FilePath "python" -ArgumentList "bot\main.py $($config.Name)" -WindowStyle Hidden -WorkingDirectory $scriptDir
}
Write-Host "      OK - $($botConfigs.Count) bots started"
Write-Host ""

# 5. Final status
Write-Host "[5/5] Startup complete!"
Write-Host "============================================"
Write-Host "  All services started!"
Write-Host "  API:       http://localhost:5000"
Write-Host "  Dashboard: http://localhost:5173"
Write-Host "  Bots:      $($botConfigs.Length) running"
Write-Host "============================================"
Write-Host ""

if (-not $NoBrowser) {
    Write-Host "Opening dashboard in browser..."
    Start-Process "http://localhost:5173"
}

Write-Host "To stop all: Stop-Process -Name node,python -Force"
