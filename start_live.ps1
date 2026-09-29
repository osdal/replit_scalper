# LIVE-лаунчер: отдельный API (5001) + live-дашборд (5176) на РЕАЛЬНОЙ бирже.
# Запускает те же приложения, но с BOT_ENV=live (ключи берутся из .env.live).
Set-Location -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)

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

Kill-ProcessOnPort 5001
Kill-ProcessOnPort 5176

New-Item -ItemType Directory -Force -Path logs | Out-Null

# BOT_ENV=live -> env.ts читает .env.live (BINANCE_TESTNET=false, реальные ключи),
# боты получают это окружение и торгуют на реальной бирже.
$env:BOT_ENV = "live"

Write-Host "Starting LIVE API server on port 5001 (real exchange)..."
$logStamp = Get-Date -Format "yyyyMMdd_HHmmss"
$apiLog = "logs\api-server-live_$logStamp.log"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c pnpm start:api > $apiLog 2>&1" -NoNewWindow
Write-Host "LIVE API log: $apiLog"
Start-Sleep -Seconds 6

Write-Host "Starting LIVE dashboard on port 5176..."
Start-Process -FilePath "cmd.exe" -ArgumentList "/c pnpm --filter @workspace/dashboard exec vite --host 0.0.0.0 --port 5176 --mode live" -NoNewWindow
Start-Sleep -Seconds 3

Start-Process "http://localhost:5176"
