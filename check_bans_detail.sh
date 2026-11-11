#!/bin/bash
echo "=== all 'banned until' with IP, since 12:28 UTC ==="
grep -h -E "Way too many requests|banned until 17940" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | grep -E "2026-11-07 1[2-9]:" | tail -15
echo ""
echo "=== first ban after deploy (12:28) ==="
grep -h -E "^2026-11-07 (12:(2[89]|[3-9][0-9])|1[3-9]:)[0-9]{2}:[0-9]{2}.*IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | sort | head -10
echo ""
echo "=== count ban lines per minute since 12:28 ==="
grep -h -E "^2026-11-07 (12:(2[89]|[3-9][0-9])|1[3-9]:)[0-9]{2}:[0-9]{2}.*IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | cut -c1-16 | sort | uniq -c
echo ""
echo "=== container/process start time ==="
docker exec replit_scalper-api-testnet bash -lc "ps -eo pid,lstart,args | grep main.py | head -2"
