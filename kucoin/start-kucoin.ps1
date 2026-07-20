# Load .env file
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match "^([^#=]+)=(.*)$") {
            $name = $matches[1]
            $value = $matches[2]
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}

# Start bots
Start-Process -FilePath "python" -ArgumentList "main_kucoin.py BTCUSDT 5m" -WorkingDirectory $PSScriptRoot
Start-Process -FilePath "python" -ArgumentList "main_kucoin.py ETHUSDT 5m" -WorkingDirectory $PSScriptRoot
Start-Process -FilePath "python" -ArgumentList "main_kucoin.py SOLUSDT 5m" -WorkingDirectory $PSScriptRoot

Write-Host "KuCoin bots started"