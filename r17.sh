#!/bin/bash
date -u
echo "=== host public IP ==="
curl -s -m 10 https://api.ipify.org; echo
curl -s -m 10 https://ifconfig.me; echo
echo
echo "=== container public IP ==="
docker exec replit_scalper-api-testnet bash -lc "curl -s -m 10 https://api.ipify.org; echo"
echo
echo "=== proxy env (container) ==="
docker exec replit_scalper-api-testnet bash -lc 'env | grep -i proxy' || echo "no proxy vars"
echo
echo "=== proxy in .env files ==="
grep -ri "proxy" /opt/trading-bot/.env /opt/trading-bot/.env.live /opt/trading-bot/docker-compose.yml 2>/dev/null | head -10 || echo "none"
echo
echo "=== docker daemon proxy ==="
cat /etc/systemd/system/docker.service.d/*.conf 2>/dev/null | head -20 || echo "no daemon proxy config"
cat ~/.docker/config.json 2>/dev/null | head -20
echo
echo "=== testnet ping headers (weight) ==="
curl -s -m 10 -D - -o /dev/null https://testnet.binancefuture.com/fapi/v1/ping 2>&1 | head -20
echo
echo "=== testnet DNS ==="
getent hosts testnet.binancefuture.com
echo
echo "=== mainnet ping headers ==="
curl -s -m 10 -D - -o /dev/null https://fapi.binance.com/fapi/v1/ping 2>&1 | head -20
