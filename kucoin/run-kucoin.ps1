param()

$scriptDir = $PSScriptRoot
Set-Location -LiteralPath $scriptDir

# Load .env into environment and pass to child processes
$envFile = Join-Path $scriptDir ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match "^([A-Z_]+)=(.*)$") {
            $envVarName = $matches[1]
            $envVarValue = $matches[2]
            $env:$envVarName = $envVarValue
        }
    }
}

# Start bots via cmd with env vars inline
Start-Process -FilePath "cmd" -ArgumentList "/c `"$($env:KUCOIN_API_KEY)`" && python main_kucoin.py BTCUSDT 5m" -WindowStyle Normal -WorkingDirectory $scriptDir
Start-Process -FilePath "cmd" -ArgumentList "/c python main_kucoin.py ETHUSDT 5m" -WindowStyle Normal -WorkingDirectory $scriptDir
Start-Process -FilePath "cmd" -ArgumentList "/c python main_kucoin.py SOLUSDT 5m" -WindowStyle Normal -WorkingDirectory $scriptDir

Write-Host "KuCoin bots started in new windows"