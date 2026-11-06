#!/bin/bash
echo "=== used_weight lines in bot logs ==="
grep -h "RATE_LIMIT] used_weight" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -6
echo
echo "=== env keys (masked) ==="
docker exec replit_scalper-api-testnet bash -lc 'env | grep -iE "BINANCE" | sed "s/=.*/=<set>/"' 2>&1
echo
echo "=== signed vs unsigned header test inside container ==="
docker exec replit_scalper-api-testnet python3 - <<'PY' 2>&1
import os, time, hmac, hashlib, urllib.parse, urllib.request

base = "https://testnet.binancefuture.com"
key = os.getenv("BINANCE_API_KEY","")
sec = os.getenv("BINANCE_API_SECRET","")
print("key set:", bool(key), "secret set:", bool(sec))

def get(path, signed=False):
    q = ""
    if signed:
        q = f"timestamp={int(time.time()*1000)}&recvWindow=5000"
        sig = hmac.new(sec.encode(), q.encode(), hashlib.sha256).hexdigest()
        q = q + "&signature=" + sig
    req = urllib.request.Request(base+path+("?"+q if q else ""), headers={"X-MBX-APIKEY": key})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        h = {k.lower():v for k,v in r.headers.items()}
        print(path, "signed" if signed else "unsigned", "status", r.status,
              "used1m", h.get("x-mbx-used-weight-1m"),
              "order1m", h.get("x-mbx-order-count-1m"))
    except Exception as e:
        print(path, "ERR", e)

for _ in range(2):
    get("/fapi/v1/ping")
    get("/fapi/v2/positionRisk", True)
    get("/fapi/v3/positionRisk", True)
    time.sleep(2)
PY
echo
echo "=== host unsigned moment later ==="
curl -s -m 8 -D - -o /dev/null https://testnet.binancefuture.com/fapi/v1/ping | tr -d '\r' | grep -iE "x-mbx-used-weight"
