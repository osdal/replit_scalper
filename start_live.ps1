# ============================================================
#  LIVE-стек: отдельный API (5001) + live-дашборд (5176) на РЕАЛЬНОЙ бирже.
#  Тестнет-стек живёт отдельно: .\start-all.ps1 (API 5000 + дашборд 5173).
#
#  Оба стека можно держать запущенными одновременно — порты не пересекаются:
#    testnet : API 5000, дашборд 5173, БД data/bot.db
#    live    : API 5001, дашборд 5176, БД data/bot_live.db
#
#  Ключи и BINANCE_TESTNET=false берутся из .env.live (BOT_ENV=live).
# ============================================================
param(
    [switch]$NoBrowser
)

Set-Location -LiteralPath $PSScriptRoot

function Kill-ProcessOnPort($port) {
    $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | Where-Object { $_.State -eq 'Listen' }
    foreach ($conn in $connections) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -eq 'node') {
            Write-Host "Stopping process $($proc.Id) on port $port"
            Stop-Process -Id $proc.Id -Force
        }
    }
}

function Wait-Http($url, $seconds) {
    for ($i = 0; $i -lt $seconds; $i++) {
        try {
            Invoke-WebRequest $url -UseBasicParsing -ErrorAction Stop | Out-Null
            return $true
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    return $false
}

Write-Host "============================================"
Write-Host "  Starting LIVE stack (real money)"
Write-Host "============================================"

# 1. Освобождаем только свои порты (5000/5173 тестнета не трогаем)
Write-Host "[1/5] Freeing live ports 5001 / 5176..."
Kill-ProcessOnPort 5001
Kill-ProcessOnPort 5176
New-Item -ItemType Directory -Force -Path logs | Out-Null

# BOT_ENV=live -> env.ts читает .env.live (реальные ключи, BINANCE_TESTNET=false)
# и отказывается стартовать без них. Боты, запущенные из дашборда, наследуют
# это окружение, поэтому берут конфиги из bot/configs/live и БД bot_live.db.
$env:BOT_ENV = "live"

# 2. Схема БД live (идемпотентно: создаёт таблицы и карточки ботов из
#    bot/configs/live, если их ещё нет)
Write-Host "[2/5] Initializing live database (data/bot_live.db)..."
pnpm run init-db
if ($LASTEXITCODE -ne 0) {
    Write-Error "[FAIL] init-db error!"
    exit 1
}

# 3. API-сервер live
Write-Host "[3/5] Starting LIVE API server on port 5001..."
$logStamp = Get-Date -Format "yyyyMMdd_HHmmss"
$apiLog = "logs\api-server-live_$logStamp.log"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c pnpm start:api > $apiLog 2>&1" -NoNewWindow
Write-Host "      LIVE API log: $apiLog"

if (-not (Wait-Http "http://localhost:5001/api/bots" 30)) {
    Write-Warning "      API health check failed - see $apiLog"
}
Write-Host "      OK - http://localhost:5001"

# 4. Live-дашборд: --mode live читает artifacts/dashboard/.env.live
#    (VITE_API_URL=5001 + VITE_THEME=live -> красная тема и вкладка LIVE)
Write-Host "[4/5] Starting LIVE dashboard on port 5176..."
Start-Process -FilePath "cmd.exe" -ArgumentList "/c pnpm --filter @workspace/dashboard exec vite --host 0.0.0.0 --port 5176 --mode live" -NoNewWindow

if (-not (Wait-Http "http://localhost:5176" 30)) {
    Write-Warning "      Dashboard health check failed"
}
Write-Host "      OK - http://localhost:5176"
Write-Host ""

# 5. Итог
Write-Host "[5/5] LIVE stack is up."
Write-Host "============================================"
Write-Host "  LIVE API:       http://localhost:5001"
Write-Host "  LIVE dashboard: http://localhost:5176"
Write-Host "  Live bots:      start with the 'Start' button (arm first)"
Write-Host ""
Write-Host "  Testnet stack (separate, may run at the same time):"
Write-Host "    .\start-all.ps1  ->  API 5000, dashboard 5173"
Write-Host "============================================"

if (-not $NoBrowser) {
    Start-Process "http://localhost:5176"
}
