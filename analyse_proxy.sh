#!/bin/bash
echo "=== env proxies in api-testnet ==="
docker exec replit_scalper-api-testnet bash -lc 'env | grep -iE "proxy|no_proxy" || echo "no proxy vars"'
echo
echo "=== ping from INSIDE container ==="
docker exec replit_scalper-api-testnet bash -lc 'curl -s -m 8 -D - -o /dev/null https://testnet.binancefuture.com/fapi/v1/ping | tr -d "\r" | grep -iE "^HTTP|x-mbx-used-weight"' 2>&1
echo
echo "=== ping from HOST ==="
curl -s -m 8 -D - -o /dev/null https://testnet.binancefuture.com/fapi/v1/ping | tr -d '\r' | grep -iE "^HTTP|x-mbx-used-weight"
echo
echo "=== external IP host ==="
curl -s -m 8 https://api.ipify.org; echo
echo "=== external IP container ==="
docker exec replit_scalper-api-testnet bash -lc 'curl -s -m 8 https://api.ipify.org' 2>&1; echo
echo
echo "=== signed ping? use bot key from env (headers only) ==="
docker exec replit_scalper-api-testnet bash -lc 'env | grep -iE "BINANCE_API|BINANCE_TESTNET|BINANCE_BASE|BINANCE_TLD" | sed "s/=.*/=<set>/"' 2>&1
