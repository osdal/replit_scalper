#!/bin/bash
cd /opt/trading-bot/bot/data
echo "=== newest gate files ==="
ls -t binance_gate_testnet.json.* 2>/dev/null | head -6
echo
echo "=== contents (newest 8) ==="
for f in $(ls -t binance_gate_testnet.json.* 2>/dev/null | head -8); do echo -n "$f: "; cat "$f"; echo; done
echo
echo "=== weight distribution (all gate files) ==="
python3 - <<'PY'
import glob, json
ws=[]; bans=[]
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f))
        ws.append(int(d.get("used_weight_1m") or 0))
        if d.get("ban_until_ms",0) > 1793937660000: bans.append((f,d.get("ban_until_ms")))
    except Exception: pass
ws.sort()
if ws:
    print("files:", len(ws))
    print("weight min/med/p90/max:", ws[0], ws[len(ws)//2], ws[int(len(ws)*0.9)], ws[-1])
print("files with ban_until > 04:01 (active-ish):", bans[:5], "count=", len(bans))
PY
echo
echo "=== sample 20 weight values ==="
python3 - <<'PY'
import glob, json
out=[]
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f)); out.append(int(d.get("used_weight_1m") or 0))
    except Exception: pass
out.sort()
print(out[-20:])
PY
