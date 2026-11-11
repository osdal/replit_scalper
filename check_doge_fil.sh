#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== lock files ==="
ls -la data/bot.lock.* 2>/dev/null
echo
echo "=== check DOGE and FIL logs ==="
tail -30 bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null || echo "no log"
echo "---"
tail -30 bot/logs/testnet/api_filusdt_stdout.log 2>/dev/null || echo "no log"