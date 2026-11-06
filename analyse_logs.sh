#!/bin/bash
cd /opt/trading-bot
echo "=== API STATUS ==="
curl -s http://localhost:5000/api/trading/status 2>&1
echo
echo
LOG=bot/logs/testnet
echo "=== LOG INVENTORY ==="
ls -la $LOG/api_*.log 2>/dev/null | head -3
ls -la $LOG/api_*.log 2>/dev/null | wc -l
echo
echo "=== COUNT: bans (all logs, today+d) ==="
grep -h "IP banned\|banned until" $LOG/api_*.log 2>/dev/null | \
  awk '{print $1" "$2}' | cut -c1-13 | sort | uniq -c | tail -30
echo
echo "=== COUNT: total ban lines by date ==="
grep -h "IP banned" $LOG/api_*.log 2>/dev/null | cut -c1-10 | sort | uniq -c
echo
echo "=== ERRORS by date (top 25 messages) ==="
grep -h " ERROR " $LOG/api_*.log 2>/dev/null | sed -E 's/^[0-9-]+ [0-9:]+ //' | sed -E 's/\[LIVE [A-Z]+\]//' | sed -E 's/0x[0-9a-f]+//g' | cut -c1-110 | sort | uniq -c | sort -rn | head -25
echo
echo "=== Bot starting per hour (last 36h) ==="
grep -h "Bot starting" $LOG/api_*.log 2>/dev/null | cut -c1-13 | sort | uniq -c | tail -36
echo
echo "=== EXPIRED / DEADLINE events last 24h (unique) ==="
grep -h "EXPIRED\|DEADLINE" $LOG/api_*.log 2>/dev/null | grep -E "2026-11-0[56]" | sed -E 's/ (net|required)=.*//' | sort -u | tail -40
echo
echo "=== PHANTOM closed last 24h ==="
grep -h "PHANTOM" $LOG/api_*.log 2>/dev/null | grep -E "2026-11-0[56]" | tail -20
echo
echo "=== GUARD stale flattened last 24h ==="
grep -h "GUARD" $LOG/api_*.log 2>/dev/null | grep -E "2026-11-0[56]" | tail -20
