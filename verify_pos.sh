#!/bin/bash
echo "=== host db open trades ==="
python3 - <<'PY'
import sqlite3
con=sqlite3.connect("file:/opt/trading-bot/data/bot.db?mode=ro", uri=True)
rows=list(con.execute("select id,symbol,direction,status,is_open,exit_reason,exit_time,pnl from trades order by id desc limit 8"))
for r in rows: print(r)
print("open now:", list(con.execute("select id,symbol,direction,is_open from trades where is_open=1")))
PY
echo
echo "=== bans after 06:37 ==="
grep -h -E "^2026-11-06 06:(3[7-9]|[45][0-9]):[0-9]{2}.*IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | wc -l
echo
echo "=== time now ==="
date -u
echo
echo "=== exchange positions (signed) ==="
docker exec -i replit_scalper-api-testnet python3 - <<'PY' 2>&1
import glob, time, hmac, hashlib, urllib.request, json
key=sec=""
for p in glob.glob("/proc/[0-9]*/cmdline"):
    try: cmd=open(p,"rb").read().decode(errors="ignore")
    except Exception: continue
    if "main.py" not in cmd: continue
    pid=p.split("/")[2]
    try: env=dict(kv.split(b"=",1) for kv in open(f"/proc/{pid}/environ","rb").read().split(b"\x00") if b"=" in kv)
    except Exception: continue
    key=env.get(b"BINANCE_API_KEY",b"").decode(); sec=env.get(b"BINANCE_API_SECRET",b"").decode()
    if key: break
q=f"timestamp={int(time.time()*1000)}&recvWindow=5000"
q+="&signature="+hmac.new(sec.encode(),q.encode(),hashlib.sha256).hexdigest()
req=urllib.request.Request("https://testnet.binancefuture.com/fapi/v2/positionRisk?"+q, headers={"X-MBX-APIKEY":key})
r=urllib.request.urlopen(req,timeout=10)
data=json.loads(r.read())
print("non-zero positions:", [(p["symbol"],p["positionAmt"],p.get("unRealizedProfit")) for p in data if abs(float(p["positionAmt"]))>0])
PY
