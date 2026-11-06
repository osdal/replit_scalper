#!/bin/bash
echo "=== bot pids & key identity (hashed) ==="
docker exec -i replit_scalper-api-testnet python3 - <<'PY' 2>&1
import glob, hashlib, os, re
pids = []
for p in glob.glob("/proc/[0-9]*"):
    try:
        cmd = open(p+"/cmdline","rb").read().replace(b"\x00",b" ").decode(errors="ignore")
    except Exception: continue
    if "main.py" in cmd:
        pids.append(int(p.split("/")[-1]))
print("bot pids:", len(pids), pids[:5])
keys = {}
for pid in pids:
    try:
        env = open(f"/proc/{pid}/environ","rb").read().split(b"\x00")
    except Exception: continue
    d = {}
    for kv in env:
        if b"=" in kv:
            k,v = kv.split(b"=",1); d[k.decode(errors="ignore")] = v.decode(errors="ignore")
    k = d.get("BINANCE_API_KEY","")
    t = d.get("BINANCE_TESTNET","?")
    h = hashlib.sha1(k.encode()).hexdigest()[:8] if k else "NONE"
    keys.setdefault(h, []).append(t)
print("distinct api keys:", len(keys))
for h, ts in keys.items():
    print("  key", h, "bots:", len(ts), "testnet flags:", set(ts))
PY
echo
echo "=== signed header test using bot's own key ==="
docker exec -i replit_scalper-api-testnet python3 - <<'PY' 2>&1
import glob, time, hmac, hashlib, urllib.request, urllib.parse

pids=[]
for p in glob.glob("/proc/[0-9]*"):
    try: cmd=open(p+"/cmdline","rb").read().replace(b"\x00",b" ").decode(errors="ignore")
    except Exception: continue
    if "main.py" in cmd: pids.append(p)
key=sec=""
for p in pids:
    try: env=open(p+"/environ","rb").read().split(b"\x00")
    except Exception: continue
    d=dict(kv.split(b"=",1) for kv in env if b"=" in kv)
    key=d.get(b"BINANCE_API_KEY",b"").decode(); sec=d.get(b"BINANCE_API_SECRET",b"").decode()
    if key: break
print("key found:", bool(key))
base="https://testnet.binancefuture.com"

def call(path, signed=False):
    q=""
    if signed:
        q=f"timestamp={int(time.time()*1000)}&recvWindow=5000"
        q+="&signature="+hmac.new(sec.encode(),q.encode(),hashlib.sha256).hexdigest()
    req=urllib.request.Request(base+path+("?"+q if q else ""), headers={"X-MBX-APIKEY":key})
    try:
        r=urllib.request.urlopen(req,timeout=10)
        h={k.lower():v for k,v in r.headers.items()}
        print(path,"signed" if signed else "unsigned","status",r.status,
              "| used1m",h.get("x-mbx-used-weight-1m"),
              "| order1m",h.get("x-mbx-order-count-1m"))
    except Exception as e:
        body=""
        try: body=e.read().decode()[:200]
        except Exception: pass
        print(path,"ERR",e,body)

for i in range(3):
    call("/fapi/v1/ping")
    call("/fapi/v2/positionRisk", True)
    call("/fapi/v3/positionRisk", True)
    time.sleep(2)
PY
