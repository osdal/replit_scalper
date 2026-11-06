#!/bin/bash
docker exec -i replit_scalper-api-testnet python3 - <<'PY' 2>&1
import time, hmac, hashlib, urllib.request, os, glob

base="https://testnet.binancefuture.com"

def signed_call(key, sec, path="/fapi/v2/positionRisk"):
    q=f"timestamp={int(time.time()*1000)}&recvWindow=5000"
    q+="&signature="+hmac.new(sec.encode(),q.encode(),hashlib.sha256).hexdigest()
    req=urllib.request.Request(base+path+"?"+q, headers={"X-MBX-APIKEY":key})
    try:
        r=urllib.request.urlopen(req,timeout=10)
        h={k.lower():v for k,v in r.headers.items()}
        return f"OK status={r.status} used1m={h.get('x-mbx-used-weight-1m')}"
    except Exception as e:
        h={}
        try: h={k.lower():v for k,v in e.headers.items()}
        except Exception: pass
        body=""
        try: body=e.read().decode()[:120]
        except Exception: pass
        return f"ERR {e} used1m={h.get('x-mbx-used-weight-1m')} body={body}"

# 1) bogus key
print("BOGUS KEY:", signed_call("a"*64, "b"*64))

# 2) current bot key
pids=[p for p in glob.glob("/proc/[0-9]*") if "main.py" in open(p+"/cmdline","rb").read().decode(errors="ignore")] if False else []
for p in glob.glob("/proc/[0-9]*"):
    try: cmd=open(p+"/cmdline","rb").read().decode(errors="ignore")
    except Exception: continue
    if "main.py" in cmd:
        try: env=dict(kv.split(b"=",1) for kv in open(p+"/environ","rb").read().split(b"\x00") if b"=" in kv)
        except Exception: continue
        k=env.get(b"BINANCE_API_KEY",b"").decode(); s=env.get(b"BINANCE_API_SECRET",b"").decode()
        if k:
            print("CURRENT KEY:", signed_call(k,s)); break

# 3) other key from bot/.env
txt=open("/app/bot/.env",errors="ignore").read() if os.path.exists("/app/bot/.env") else ""
d={}
for ln in txt.splitlines():
    if "=" in ln and not ln.startswith("#"):
        a,b=ln.split("=",1); d[a.strip()]=b.strip().strip('"').strip("'")
k=d.get("BINANCE_API_KEY",""); s=d.get("BINANCE_API_SECRET","")
print("ALT KEY present:", bool(k), "same as current:", k == os.getenv("BINANCE_API_KEY",""))
if k: print("ALT KEY:", signed_call(k,s))
PY
