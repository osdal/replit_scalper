#!/bin/bash
date -u
echo ""
echo "################ TESTNET: P2 VERIFY ################"
bash /tmp/verify_p2b.sh 2>&1
echo ""
echo "################ LIVE STACK ################"
echo "=== live api container ==="
docker ps --filter name=replit_scalper-api-live --format '{{.Names}} {{.Status}}' 2>&1
echo ""
echo "=== live bot processes ==="
docker exec replit_scalper-api-live bash -lc "ps aux | grep -c 'main.py'" 2>&1 || echo "no live container"
echo ""
echo "=== live env: endpoints/testnet flag ==="
docker exec replit_scalper-api-live bash -lc 'env | grep -E "BINANCE_TESTNET|DAILY_LOSS|MAX_POSITIONS"' 2>&1 || true
echo ""
echo "=== last live bot log line ==="
ls -t /opt/trading-bot/bot/logs/live/*_stdout.log 2>/dev/null | head -1 | xargs -r tail -2
echo ""
echo "=== testnet host vs mainnet host in logs (sample) ==="
grep -h -o "https://testnet.binancefuture.com" /opt/trading-bot/bot/logs/testnet/api_btcusdt_stdout.log 2>/dev/null | head -1
grep -h -o "https://fapi.binance.com" /opt/trading-bot/bot/logs/live/*_stdout.log 2>/dev/null | head -1
echo ""
echo "=== dated IPs in recent ban messages (testnet) ==="
grep -h -o "IP([0-9.]*)" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | sort | uniq -c | tail -5
