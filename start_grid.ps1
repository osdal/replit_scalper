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

Kill-ProcessOnPort 5000
Kill-ProcessOnPort 5175

Write-Host "Starting API server on port 5000..."
Start-Process -FilePath "cmd.exe" -ArgumentList "/c pnpm start:api" -NoNewWindow
Start-Sleep -Seconds 5

Write-Host "Downloading 1 month of daily candles from Binance..."
try {
    $r = Invoke-RestMethod -Uri "http://localhost:5000/api/history/download" -Method Post -TimeoutSec 120 -UseBasicParsing
    Write-Host "History download result:" ($r | ConvertTo-Json -Compress)
} catch {
    Write-Warning "History download failed: $($_.Exception.Message)"
}

Write-Host "Starting dashboard on port 5175..."
Start-Process -FilePath "cmd.exe" -ArgumentList "/c pnpm start:dashboard-v2" -NoNewWindow
Start-Sleep -Seconds 3

Start-Process "http://localhost:5175"