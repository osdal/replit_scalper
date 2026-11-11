#!/bin/bash
python3 << 'EOF'
import glob,json,time
now=time.time()
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f))
        age=now-d.get("ts",0)
        lw=d.get("local_weight_1m",0)
        uw=d.get("used_weight_1m",0)
        ban=d.get("ban_until_ms",0)
        print(f"{f.split('.')[-1]}: age={age:.0f}s local={lw} header={uw} ban_active={ban>now*1000}")
    except:
        pass
EOF