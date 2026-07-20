@echo off
cd /d "%~dp0"

REM Read and set env vars from .env
for /f "tokens=1,2 delims==" %%a in ('type .env ^| findstr /v "^#"') do (
    set "%%a=%%b"
)

REM Start bots
echo Starting KuCoin bots...
start "KuCoin BTC" cmd /k python main_kucoin.py BTCUSDT 5m
start "KuCoin ETH" cmd /k python main_kucoin.py ETHUSDT 5m
start "KuCoin SOL" cmd /k python main_kucoin.py SOLUSDT 5m

echo Bots starting in separate windows