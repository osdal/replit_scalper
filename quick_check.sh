#!/bin/bash
cd /opt/trading-bot
date -u
echo "=== ETH recent ==="
grep -h "STARTUP\|staggered\|warm-up" bot/logs/testnet/api_ethusdt_stdout.log 2>/dev/null | tail -5
echo "=== check other bots starting ==="
grep -h "staggered delay" bot/logs/testnet/api_*.log 2>/dev/null | head -10