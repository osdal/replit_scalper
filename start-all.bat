@echo off
chcp 65001 >nul 2>&1
title Trading Bot Startup

echo ============================================
echo   Starting Trading Bot Environment
echo ============================================
echo.

cd /d C:\DATA\bots\replit_scalper

echo [1/3] Init database...
call pnpm run init-db
if %ERRORLEVEL% neq 0 (
    echo [FAIL] init-db error!
    pause
    exit /b 1
)
echo       OK
echo.

echo [2/3] Starting API server...
start "Trading Bot API" cmd /c "cd /d C:\DATA\bots\replit_scalper && pnpm run start:api"
call timeout /t 3 /nobreak >nul 2>&1
echo       OK - http://localhost:5000
echo.

echo [3/3] Starting Dashboard...
start "Trading Bot Dashboard" cmd /c "cd /d C:\DATA\bots\replit_scalper && pnpm run start:dashboard"
call timeout /t 3 /nobreak >nul 2>&1
echo       OK - http://localhost:5173
echo.

echo [4/3] Starting bots...
start "BTC Bot" cmd /c "cd /d C:\DATA\bots\replit_scalper && python bot/main.py bot/config_btc.yaml"
start "ETH Bot" cmd /c "cd /d C:\DATA\bots\replit_scalper && python bot/main.py bot/config_eth.yaml"
start "BNB Bot" cmd /c "cd /d C:\DATA\bots\replit_scalper && python bot/main.py bot/config_bnb.yaml"
start "SOL Bot" cmd /c "cd /d C:\DATA\bots\replit_scalper && python bot/main.py bot/config_sol.yaml"
start "XRP Bot" cmd /c "cd /d C:\DATA\bots\replit_scalper && python bot/main.py bot/config_xrp.yaml"
start "TRX Bot" cmd /c "cd /d C:\DATA\bots\replit_scalper && python bot/main.py bot/config_trx.yaml"
start "DOGE Bot" cmd /c "cd /d C:\DATA\bots\replit_scalper && python bot/main.py bot/config_doge.yaml"
start "ONT Bot" cmd /c "cd /d C:\DATA\bots\replit_scalper && python bot/main.py bot/config_ont.yaml"
echo       OK - 8 bots started
echo.

echo ============================================
echo   All services started!
echo   API:       http://localhost:5000
echo   Dashboard:  http://localhost:5173
echo   Bots:      8 running
echo ============================================
echo.
echo Press any key to close this window (services will keep running)...
pause >nul
