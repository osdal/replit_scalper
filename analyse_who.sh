#!/bin/bash
echo "=== host python bot processes ==="
ps aux | grep -E "bot/main\.py|main\.py" | grep -v grep | wc -l
ps aux | grep -E "bot/main\.py" | grep -v grep | head -5
echo
echo "=== container bot processes ==="
docker exec replit_scalper-api-testnet bash -lc 'ps aux | grep "main.py" | grep -v grep | wc -l' 2>&1
echo
echo "=== other log dirs with recent writes ==="
find /opt/trading-bot -maxdepth 3 -type d -name "logs" 2>/dev/null
for d in $(find /opt/trading-bot -maxdepth 3 -type d -name "logs" 2>/dev/null); do
  echo "--- $d"; find "$d" -name "*stdout.log" -mmin -10 2>/dev/null | wc -l
done
echo
echo "=== files modified in last 5 min under /opt/trading-bot (top) ==="
find /opt/trading-bot -maxdepth 3 -type f -mmin -5 2>/dev/null | grep -v node_modules | head -20
echo
echo "=== api-live logs tail ==="
docker logs replit_scalper-api-live --tail 20 2>&1
echo
echo "=== established connections to binance from host ==="
ss -tnp 2>/dev/null | grep -c ":443" || echo "ss unavailable"
echo
echo "=== gate py processes in api-server container (node only?) ==="
docker exec replit_scalper-api-testnet bash -lc 'ps aux | grep -E "node|python" | grep -v grep | head -5' 2>&1
