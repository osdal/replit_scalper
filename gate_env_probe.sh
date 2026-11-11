#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== env-specific gate files ==="
ls -la bot/data/binance_gate_*.json* 2>/dev/null | tail -20
echo
echo "=== newest testnet gate content ==="
for f in $(ls -t bot/data/binance_gate_testnet.json* 2>/dev/null | head -5); do echo "--- $f"; cat "$f"; echo; done
echo
echo "=== BOT_ENV inside containers ==="
docker exec replit_scalper-api-testnet sh -lc 'echo testnet_container BOT_ENV=$BOT_ENV; ls -la /app/bot/data/binance_gate_* 2>/dev/null | tail -5'
docker exec replit_scalper-api-live sh -lc 'echo live_container BOT_ENV=$BOT_ENV; ls -la /app/bot/data/binance_gate_* 2>/dev/null | tail -5'
