@echo off
chcp 65001 >nul 2>&1
titleTrading Bot Startup

echo ============================================
echo   Starting Trading Bot Environment
echo ============================================
echo.

echo [1/3] Init database...
cd /d C:\DATA\bots\replit_scalper
call pnpm run init-db
if %ERRORLEVEL% neq 0 (
    echo [FAIL] init-db error!
    pause
    exit /b 1
)
echo       OK
echo.

echo [2/3] Starting API server...
start "Trading Bot API" /min cmd /c "cd /d C:\DATA\bots\replit_scalper && call pnpm run start:api && pause"
call timeout /t 3 /nobreak >nul 2>&1
echo       OK - http://localhost:5000
echo.

echo [3/3] Starting Dashboard...
start "Trading Bot Dashboard" /min cmd /c "cd /d C:\DATA\bots\replit_scalper && call pnpm run start:dashboard && pause"
call timeout /t 3 /nobreak >nul 2>&1
echo       OK - http://localhost:5173
echo.

echo ============================================
echo   All services started!
echo   Open browser: http://localhost:5173
echo ============================================
