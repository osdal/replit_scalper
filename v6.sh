#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== container ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Names}} {{.Status}}'
echo
echo "=== env ==="
docker inspect replit_scalper-api-testnet --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'POST_BAN|SLOTS_FULL|MAX_POSITIONS=|STARTUP_STAGGER' || echo "NO NEW ENV"
echo
echo "=== db perms ==="
ls -l data/bot.db data/ | head -5
id
sudo -n true 2>/dev/null && echo "sudo: yes" || echo "sudo: no/needs password"
echo
echo "=== status ==="
curl -s -m 10 http://localhost:5000/api/trading/status
echo
