#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== compose lines with new env ==="
grep -n -E 'POST_BAN|SLOTS_FULL|MAX_POSITIONS=|STARTUP_STAGGER' docker-compose.yml
echo
echo "=== container config env ==="
docker inspect replit_scalper-api-testnet --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'POST_BAN|SLOTS_FULL|MAX_POSITIONS|STARTUP_STAGGER|BINANCE_WEIGHT'
echo
echo "=== container compose project/config files ==="
docker inspect replit_scalper-api-testnet --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
echo
echo "=== .env / .env.testnet overrides ==="
grep -n -E 'POST_BAN|SLOTS_FULL|MAX_POSITIONS|STARTUP_STAGGER' .env .env.testnet 2>/dev/null || echo "none in env files"
echo
echo "=== deploy.sh ==="
cat /tmp/deploy.sh
echo
echo "=== git status ==="
git status --short | head -10
