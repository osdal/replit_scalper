#!/bin/bash
echo "=== env files on server ==="
ls -la /opt/trading-bot/.env* /opt/trading-bot/bot/.env* 2>/dev/null
echo
python3 - <<'PY'
import glob, hashlib, re, os
for f in glob.glob("/opt/trading-bot/.env*") + glob.glob("/opt/trading-bot/bot/.env*"):
    try: txt=open(f,errors="ignore").read()
    except Exception: continue
    for ln in txt.splitlines():
        if ln.startswith("BINANCE_API_KEY=") or ln.startswith("BINANCE_TESTNET=") or ln.startswith("BOT_ENV="):
            k,v=ln.split("=",1)
            v=v.strip().strip('"').strip("'")
            if k=="BINANCE_API_KEY":
                print(f, k, "sha1="+hashlib.sha1(v.encode()).hexdigest()[:8], "len", len(v))
            else:
                print(f, k, v)
PY
echo
echo "=== all python/node processes on host (non-main.py) ==="
ps -eo pid,ppid,etime,cmd | grep -E "python|node" | grep -vE "grep|main.py" | head -20
echo
echo "=== api-live container: processes + binance env ==="
docker exec -i replit_scalper-api-live bash -lc 'ps -eo pid,args | grep -E "python|node" | grep -v grep | head -5; echo ---; env | grep -iE "BINANCE_TESTNET|BOT_ENV"' 2>&1
