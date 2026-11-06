#!/bin/bash
date -u
echo "=== last ban lines with time ==="
grep -h -E "^2026-11-06 (06:5[0-9]|07:[0-9]{2}):[0-9]{2}.*IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | sort | tail -4
echo
echo "=== last Gate ACTIVE lines (any) ==="
grep -h "Gate ACTIVE" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | sort | tail -3
echo
echo "=== last demand lines ==="
grep -h "RATE_LIMIT] demand" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -4
echo
echo "=== gate file sample ==="
cd /opt/trading-bot/bot/data
for f in $(ls -t binance_gate_testnet.json.* 2>/dev/null | head -2); do echo -n "$f: "; cat "$f"; echo; done
echo
echo "=== last 5 log lines of one bot ==="
tail -5 /opt/trading-bot/bot/logs/testnet/api_taousdt_stdout.log
echo
echo "=== container ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
