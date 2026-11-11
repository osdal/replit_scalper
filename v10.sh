#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
ls -la data/binance_gate_testnet.json* 2>/dev/null | head -20
echo
sudo python3 - <<'PY'
import glob, json, time
now = time.time()*1000
for f in sorted(glob.glob('data/binance_gate_testnet.json*')):
    try:
        d = json.load(open(f))
    except Exception as e:
        print(f, "unreadable", e); continue
    b = int(d.get('ban_until_ms', 0) or 0)
    print(f, "ban", b, "ACTIVE" if b > now else "expired",
          "left_s", round((b-now)/1000) if b > now else 0,
          "used", d.get('used_weight_1m'), "local", d.get('local_weight_1m'),
          "age_s", round((time.time()-float(d.get('ts',0) or 0))))
PY
