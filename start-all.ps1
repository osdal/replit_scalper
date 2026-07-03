param(
    [switch]$NoBrowser
)

$scriptDir = $PSScriptRoot
Set-Location -LiteralPath $scriptDir

Write-Host "============================================"
Write-Host "  Starting Trading Bot Environment"
Write-Host "============================================"
Write-Host ""

# 1. Init database
Write-Host "[1/3] Initializing database..."
pnpm run init-db
if ($LASTEXITCODE -ne 0) {
    Write-Error "[FAIL] init-db error!"
    exit 1
}
Write-Host "      OK"
Write-Host ""

# 2. Start API server in background
Write-Host "[2/4] Starting API server..."
Start-Process -FilePath "cmd" -ArgumentList "/c pnpm --filter @workspace/api-server run dev" -WindowStyle Hidden -WorkingDirectory $scriptDir
Write-Host "      OK - http://localhost:5000"
Write-Host ""

# 3. Start Dashboard in background
Write-Host "[3/4] Starting Dashboard..."
Start-Process -FilePath "cmd" -ArgumentList "/c pnpm --filter @workspace/dashboard run dev" -WindowStyle Hidden -WorkingDirectory $scriptDir
Start-Sleep -Seconds 2
Write-Host "      OK - http://localhost:5173"
Write-Host ""

# 4. Start Bots
Write-Host "[4/4] Starting bots..."

$botConfigs = @(
    "bot/config_btc.yaml",
    "bot/config_eth.yaml",
    "bot/config_bnb.yaml",
    "bot/config_sol.yaml",
    "bot/config_xrp.yaml",
    "bot/config_trx.yaml",
    "bot/config_doge.yaml",
    "bot/config_ont.yaml"
)

foreach ($config in $botConfigs) {
    $botName = ($config -split '/')[1] -replace 'config_|.yaml', ''
    Write-Host "      Starting $botName Bot..."
    Start-Process -FilePath "python" -ArgumentList "bot/main.py $config" -WindowStyle Hidden -WorkingDirectory $scriptDir
}
Write-Host "      OK - $($botConfigs.Length) bots started"
Write-Host ""

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

Write-Host "To stop bots: Stop-Process -Name python"
Write-Host "To stop all: Stop-Process -Name node,python -Force"