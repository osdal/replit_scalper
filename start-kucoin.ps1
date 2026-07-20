# Start KuCoin bots only - isolated from Binance
$env:KUCOIN_API_KEY = Get-Content "kucoin\.env" | Where-Object { $_ -match "^KUCOIN_API_KEY=" } | ForEach-Object { ($_ -split "=", 2)[1] }
$env:KUCOIN_API_SECRET = Get-Content "kucoin\.env" | Where-Object { $_ -match "^KUCOIN_API_SECRET=" } | ForEach-Object { ($_ -split "=", 2)[1] }
$env:KUCOIN_API_PASSPHRASE = Get-Content "kucoin\.env" | Where-Object { $_ -match "^KUCOIN_API_PASSPHRASE=" } | ForEach-Object { ($_ -split "=", 2)[1] }

# Start KuCoin bots
Write-Host "Starting KuCoin bots..."
Start-Process -FilePath "python" -ArgumentList "main_kucoin.py BTCUSDT 5m" -WorkingDirectory "$PWD\kucoin" -WindowStyle Hidden
Start-Process -FilePath "python" -ArgumentList "main_kucoin.py ETHUSDT 5m" -WorkingDirectory "$PWD\kucoin" -WindowStyle Hidden

Write-Host "KuCoin bots started (check kucoin/logs/ for output)"