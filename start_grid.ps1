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

New-Item -ItemType Directory -Force -Path logs

Write-Host "Starting API server on port 5000..."
$logStamp = Get-Date -Format "yyyyMMdd_HHmmss"
$apiLog = "logs\api-server_$logStamp.log"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c pnpm start:api > $apiLog 2>&1" -NoNewWindow
Write-Host "API log: $apiLog"
Start-Sleep -Seconds 5

Write-Host "Triggering history download in background (non-blocking)..."
Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile","-Command","try { Invoke-RestMethod -Uri 'http://localhost:5000/api/history/download' -Method Post -TimeoutSec 600 -UseBasicParsing | Out-Null } catch { }" -NoNewWindow
Start-Sleep -Seconds 1

Write-Host "Starting dashboard on port 5175..."
Start-Process -FilePath "cmd.exe" -ArgumentList "/c pnpm start:dashboard-v2" -NoNewWindow
Start-Sleep -Seconds 3

Start-Process "http://localhost:5175"