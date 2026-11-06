#!/bin/bash
docker exec -i replit_scalper-api-live python3 - <<'PY' 2>&1
import os, time, hmac, hashlib, urllib.request
key=os.getenv("BINANCE_API_KEY",""); sec=os.getenv("BINANCE_API_SECRET","")
print("live key set:", bool(key), "testnet:", os.getenv("BINANCE_TESTNET"))
base="https://fapi.binance.com"

def call(path, signed=False, k=None, s=None):
    k = k if k is not None else key; s = s if s is not None else sec
    q=""
    if signed:
        q=f"timestamp={int(time.time()*1000)}&recvWindow=5000"
        q+="&signature="+hmac.new(s.encode(),q.encode(),hashlib.sha256).hexdigest()
    req=urllib.request.Request(base+path+("?"+q if q else ""), headers={"X-MBX-APIKEY":k})
    try:
        r=urllib.request.urlopen(req,timeout=10)
        h={kk.lower():vv for kk,vv in r.headers.items()}
        print(path, "signed" if signed else "unsigned", "status",r.status,"used1m",h.get("x-mbx-used-weight-1m"))
    except Exception as e:
        h={}
        try: h={kk.lower():vv for kk,vv in e.headers.items()}
        except Exception: pass
        print(path,"ERR",e,"used1m",h.get("x-mbx-used-weight-1m"))

for i in range(2):
    call("/fapi/v1/ping")
    call("/fapi/v2/positionRisk", True)
    time.sleep(2)
PY
