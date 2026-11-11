#!/bin/bash
python3 - <<'PY'
import glob,json,time
now=time.time(); tot=0; n=0; hdr=[]
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f))
        if now-d.get("ts",0)<=60:
            n+=1
            tot+=int(d.get("local_weight_1m",0) or 0)
            hdr.append(int(d.get("used_weight_1m",0) or 0))
    except Exception: pass
print(f"fresh files={n} local_sum={tot}  (shed threshold={int(2400*0.5)})")
if hdr: print("header values min/med/max:", min(hdr), sorted(hdr)[len(hdr)//2], max(hdr))
PY