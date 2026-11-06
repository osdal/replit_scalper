#!/bin/bash
cd /opt/trading-bot
echo "=== processes ==="
ps aux | grep -E "main.py|python" | grep -v grep
echo
echo "=== check docker processes ==="
docker exec replit_scalper-api-testnet ps aux | grep -E "main.py|python" | grep -v grep