#!/bin/bash
cd /opt/trading-bot
echo "=== API status (боты должны быть is_running) ==="
curl -s http://localhost:5000/api/bots | python3 -m json.tool 2>/dev/null | head -60
echo
echo "=== trading/status ==="
curl -s http://localhost:5000/api/trading/status | python3 -m json.tool 2>/dev/null