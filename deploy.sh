#!/bin/bash
set -e

echo "=== Trading Bot Setup ==="

# Install dependencies
sudo apt update && sudo apt install -y python3 python3-pip python3-venv nodejs npm sqlite3

# Setup Python venv
cd /root/bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Setup API server
cd /root/api-server
npm install
npm run init-db

echo "=== Starting API server (port 5000) ==="
nohup npm run start:api > /tmp/api.log 2>&1 &
echo "API PID: $!"

echo "=== Starting Dashboard (port 5173) ==="
cd /root/dashboard
npm install
nohup npm run dev > /tmp/dashboard.log 2>&1 &
echo "Dashboard PID: $!"

echo ""
echo "=== DONE ==="
echo "  API:      http://localhost:5000"
echo "  Dashboard: http://localhost:5173"
echo ""
echo "To stop: kill \$(cat /tmp/api.pid) \$(cat /tmp/dashboard.pid)"
echo "To view logs: tail -f /tmp/api.log /tmp/dashboard.log"
