#!/bin/bash
L=/opt/trading-bot/bot/logs/testnet
echo "=== raw ban-related lines (last 20 distinct) ==="
grep -h -iE "banned until|-1003|429|418|too many" $L/api_*.log 2>/dev/null | grep -viE "gate blocked|deferred=" | tail -15
echo
echo "=== distinct ban_until timestamps seen ==="
grep -hoE "banned until [0-9]+" $L/api_*.log 2>/dev/null | awk '{print $3}' | sort -u | tail -20
echo
echo "=== convert last 10 to UTC ==="
grep -hoE "banned until [0-9]+" $L/api_*.log 2>/dev/null | awk '{print $3}' | sort -u | tail -10 | while read t; do python3 -c "import datetime,sys;print(sys.argv[1], datetime.datetime.fromtimestamp(int(sys.argv[1])/1000, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))" "$t"; done
echo
echo "=== last IP ban log lines with context (any file) ==="
grep -l -iE "banned until" $L/api_*.log 2>/dev/null | head -3 | while read f; do echo "--- $f"; grep -n -iE "banned until" "$f" | tail -3; done
echo
echo "=== gate files: ban_until > now ==="
python3 - <<'PY'
import glob,json,time
now=time.time()*1000; act=[]
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f))
        if d.get("ban_until_ms",0)>now: act.append((f,d["ban_until_ms"],d.get("used_weight_1m")))
    except Exception: pass
print("active bans:",len(act))
for a in act[:5]: print(a)
PY
