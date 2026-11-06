#!/bin/bash
cd /opt/trading-bot/bot/data
echo "=== gate files NOW (newest 8) ==="
for f in $(ls -t binance_gate_testnet.json.* 2>/dev/null | head -8); do echo -n "$f: "; cat "$f"; echo; done
echo
echo "=== header now ==="
curl -s -m 8 -D - -o /dev/null https://testnet.binancefuture.com/fapi/v1/ping 2>/dev/null | tr -d '\r' | grep -iE "^HTTP|x-mbx-used-weight"
echo
echo "=== weight every 5s for 120s ==="
for i in $(seq 1 24); do
  W=$(curl -s -m 8 -D - -o /dev/null https://testnet.binancefuture.com/fapi/v1/ping 2>/dev/null | tr -d '\r' | awk -F': *' 'tolower($1)=="x-mbx-used-weight-1m"{print $2; exit}')
  echo "$(date -u +%H:%M:%S) weight=$W"
  sleep 5
done
echo
echo "=== gate file weight distribution NOW ==="
python3 - <<'PY'
import glob,json,time
ws=[];fresh=0;now=time.time()
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f)); ts=d.get("ts",0)
        if now-ts<180: fresh+=1
        ws.append(int(d.get("used_weight_1m") or 0))
    except Exception: pass
ws.sort()
print("files:",len(ws),"fresh(<3m):",fresh,"min/med/max:",ws[0],ws[len(ws)//2],ws[-1])
PY
