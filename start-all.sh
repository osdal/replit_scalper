#!/bin/bash

# Trading Bot Environment Startup Script for Linux
# Usage: ./start-all.sh [--no-browser]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================"
echo "  Starting Trading Bot Environment"
echo "============================================"
echo ""

# 0. Stop existing processes before starting new ones
echo "[0/5] Stopping existing processes..."
pkill -f "node.*api-server" 2>/dev/null
pkill -f "node.*dashboard" 2>/dev/null
pkill -f "python.*bot/main.py" 2>/dev/null
sleep 2
echo "      OK"
echo ""

# Set environment variables for correct path resolution
export BOT_DIR="$SCRIPT_DIR"
export DATABASE_PATH="$SCRIPT_DIR/data/bot.db"

# 1. Init database
echo "[1/5] Initializing database..."

# Ensure data directory exists
mkdir -p "$SCRIPT_DIR/data"
mkdir -p "$SCRIPT_DIR/logs"

# Backup existing database
if [ -f "$SCRIPT_DIR/data/bot.db" ]; then
    cp -f "$SCRIPT_DIR/data/bot.db" "$SCRIPT_DIR/data/bot.db.bak"
    echo "      Backed up data/bot.db"
fi

pnpm run init-db
if [ $? -ne 0 ]; then
    echo "[FAIL] init-db error!"
    exit 1
fi
echo "      OK"
echo ""

# 2. Start API server in background
echo "[2/5] Starting API server..."
nohup pnpm --filter @workspace/api-server run dev > logs/api-server.log 2>&1 &
API_PID=$!
echo "      API PID: $API_PID"
sleep 2

# Smoke check API
API_OK=false
for i in {1..5}; do
    if curl -s "http://localhost:5000/api/bots" > /dev/null 2>&1; then
        API_OK=true
        break
    fi
    sleep 1
done
if [ "$API_OK" = false ]; then
    echo "      WARNING: API health check failed"
fi
echo "      OK - http://localhost:5000"
echo ""

# 3. Start Dashboard in background
echo "[3/5] Starting Dashboard..."
nohup pnpm --filter @workspace/dashboard run dev > logs/dashboard.log 2>&1 &
DASHBOARD_PID=$!
echo "      Dashboard PID: $DASHBOARD_PID"
sleep 2

# Smoke check Dashboard
DASHBOARD_OK=false
for i in {1..5}; do
    if curl -s "http://localhost:5173" > /dev/null 2>&1; then
        DASHBOARD_OK=true
        break
    fi
    sleep 1
done
if [ "$DASHBOARD_OK" = false ]; then
    echo "      WARNING: Dashboard health check failed"
fi
echo "      OK - http://localhost:5173"
echo ""

# 4. Start Bots (auto-detect all config files)
echo "[4/5] Starting bots..."
BOT_COUNT=0
for config in "$SCRIPT_DIR"/bot/config_*.yaml; do
    if [ -f "$config" ]; then
        BOT_NAME=$(basename "$config" | sed 's/config_//;s/.yaml$//')
        echo "      Starting $BOT_NAME Bot..."
        nohup python bot/main.py "$config" > "logs/${BOT_NAME,,}.log" 2>&1 &
        BOT_COUNT=$((BOT_COUNT + 1))
    fi
done
echo "      OK - $BOT_COUNT bots started"
echo ""

# 5. Final status
echo "[5/5] Startup complete!"
echo "============================================"
echo "  All services started!"
echo "  API:       http://localhost:5000"
echo "  Dashboard: http://localhost:5173"
echo "  Bots:      $BOT_COUNT running"
echo "============================================"
echo ""

# Check for --no-browser flag
NO_BROWSER=false
if [ "$1" = "--no-browser" ]; then
    NO_BROWSER=true
fi

if [ "$NO_BROWSER" = false ]; then
    echo "Opening dashboard in browser..."
    if command -v xdg-open &> /dev/null; then
        xdg-open "http://localhost:5173" 2>/dev/null
    elif command -v open &> /dev/null; then
        open "http://localhost:5173" 2>/dev/null
    fi
fi

echo ""
echo "To stop all: pkill -f 'api-server|dashboard|bot/main.py'"