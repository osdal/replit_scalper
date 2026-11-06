#!/bin/bash
echo "=== host: find gate files ==="
find /opt/trading-bot -maxdepth 4 -name "binance_gate*" 2>/dev/null | head -20
echo "=== container: find gate files ==="
docker exec replit_scalper-api-testnet bash -lc 'find / -maxdepth 4 -name "binance_gate*" 2>/dev/null | head -20' 2>&1
echo
echo "=== container pwd / workdir ==="
docker exec replit_scalper-api-testnet bash -lc 'pwd; ls -la data 2>/dev/null | head' 2>&1
echo
echo "=== sample gate state ==="
docker exec replit_scalper-api-testnet bash -lc 'for f in $(find / -maxdepth 4 -name "binance_gate*.json" 2>/dev/null | head -5); do echo "--- $f"; cat "$f"; echo; done' 2>&1
echo
echo "=== ban timestamps -> durations ==="
python3 - <<'PY'
import datetime
ts=[1793879460801,1793884979989,1793887260611,1793887860697,1793888161020,1793894157965,1793894639738,1793898060688,1793898360983,1793898661082,1793905736717,1793905976971,1793906760451,1793907060863,1793907361066,1793907660335,1793907960312,1793908260397,1793908860957,1793916899618,1793917019794,1793918760221,1793927279249,1793933760449,1793937660547]
for t in ts[-12:]:
    print(t, datetime.datetime.fromtimestamp(t/1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
PY
