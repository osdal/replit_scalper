#!/bin/bash
cd /opt/trading-bot/bot || exit 1
date -u
python3 - <<'PY'
import glob, json, time
now = time.time()*1000
files = glob.glob("data/binance_gate_testnet.json*")
print("files:", len(files))
act = []
fresh = []
for f in files:
    try:
        d = json.load(open(f))
    except Exception:
        continue
    b = int(d.get("ban_until_ms", 0) or 0)
    ts = float(d.get("ts", 0) or 0)
    if b > now:
        act.append((round((b-now)/1000), f, b, d.get("used_weight_1m"), d.get("local_weight_1m")))
    fresh.append((ts, f, d))
print("active bans:", len(act))
for a in sorted(act)[-10:]:
    print(a)
fresh.sort(reverse=True)
print("--- freshest 10 ---")
for ts, f, d in fresh[:10]:
    print(f, "ban", d.get("ban_until_ms"), "used", d.get("used_weight_1m"), "local", d.get("local_weight_1m"), "age_s", round(time.time()-ts))
PY
