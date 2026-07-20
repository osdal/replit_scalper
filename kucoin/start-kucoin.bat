@echo off
cd /d "%~dp0"

REM Load .env variables
for /f "tokens=1,2 delims==" %%a in ('type .env ^| findstr /v "^#"') do set %%a=%%b

REM Start KuCoin bots
start "KuCoin BTC" python main_kucoin.py BTCUSDT 5m
start "KuCoin ETH" python main_kucoin.py ETHUSDT 5m
start "KuCoin SOL" python main_kucoin.py SOLUSDT 5m

echo KuCoin bots started in separate windows