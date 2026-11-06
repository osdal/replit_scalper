#!/bin/bash
echo "=== tools ==="
which ss tcpdump 2>&1
echo
echo "=== ss sample raw ==="
ss -tni 2>&1 | head -30
echo
echo "=== connections to 443 by process ==="
ss -tnp 2>/dev/null | awk 'NR>1 {print $5, $6}' | grep ':443' | sed 's/.*users:((//' | cut -d'"' -f2 | sort | uniq -c | sort -rn | head -10
echo "count 443 conns:"; ss -tn 2>/dev/null | grep -c ':443'
echo
echo "=== who connects to binance hosts (reverse dns of peers) ==="
ss -tnp 2>/dev/null | grep ':443' | awk '{print $5}' | cut -d: -f1 | sort | uniq -c | sort -rn | head -10
echo
echo "=== grid engine mentions in api-testnet logs (last) ==="
docker logs replit_scalper-api-testnet --since 30m 2>&1 | grep -iE "grid|engine|binance" | tail -20
echo
echo "=== grid bots in DBs ==="
for db in /opt/trading-bot/data/bot.db /opt/trading-bot/data/*.db; do
  [ -f "$db" ] || continue
  echo "--- $db"
  sqlite3 "$db" ".tables" 2>/dev/null | tr ' ' '\n' | grep -i grid
  sqlite3 "$db" "select count(*) from grid_bots;" 2>/dev/null
done
echo
echo "=== sqlite tables list ==="
sqlite3 /opt/trading-bot/data/bot.db ".tables" 2>&1 | head -5
