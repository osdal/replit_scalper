#!/bin/bash
echo "=== python ping from INSIDE container ==="
docker exec replit_scalper-api-testnet python3 - <<'PY' 2>&1
import urllib.request
for url in ("https://testnet.binancefuture.com/fapi/v1/ping",):
    try:
        r = urllib.request.urlopen(url, timeout=10)
        print(url, r.status)
        for k,v in r.headers.items():
            if "weight" in k.lower(): print(" ", k, "=", v)
    except Exception as e:
        print(url, "ERR", e)
PY
echo
echo "=== external ip from INSIDE container (python) ==="
docker exec replit_scalper-api-testnet python3 -c "import urllib.request;print(urllib.request.urlopen('https://api.ipify.org',timeout=10).read().decode())" 2>&1
echo
echo "=== does inside-container ping hit same counter? repeat 3x host+container ==="
for i in 1 2 3; do
  H=$(curl -s -m 8 -D - -o /dev/null https://testnet.binancefuture.com/fapi/v1/ping | tr -d '\r' | awk -F': *' 'tolower($1)=="x-mbx-used-weight-1m"{print $2;exit}')
  C=$(docker exec replit_scalper-api-testnet python3 -c "
import urllib.request
r=urllib.request.urlopen('https://testnet.binancefuture.com/fapi/v1/ping',timeout=10)
print(r.headers.get('x-mbx-used-weight-1m'))
" 2>&1)
  echo "host=$H container=$C"
  sleep 3
done
echo
echo "=== binance client base url used by bots ==="
docker exec replit_scalper-api-testnet bash -lc 'cd /app/bot && grep -rn "testnet.binancefuture\|fapi.binance" --include=*.py . | head -10' 2>&1
