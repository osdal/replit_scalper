#!/bin/bash
cd /opt/trading-bot || exit 1
rm -f data/live_daily_loss_state.json && echo "removed old shared state file"
echo
echo "=== spawn/exit events in api log (last 90 lines) ==="
docker logs --tail 400 replit_scalper-api-testnet 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -aE "spawn|started|exited|restart|BOT " | tail -40
echo
echo "=== bot log timestamps: startup markers (doge) ==="
grep -aE "Bot starting \|" bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null | tail -8
echo
echo "=== bot log: exit/error markers (doge) ==="
grep -aE "exited|Failed to fetch positions|FATAL|startup" bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null | tail -8 | sed 's/\x1b\[[0-9;]*m//g'
