#!/bin/bash
PID=$(docker inspect -f '{{.State.Pid}}' replit_scalper-api-testnet)
echo "container pid: $PID"
echo "=== nsenter ss test (first 6 sockets to 443) ==="
nsenter -t "$PID" -n /usr/bin/ss -tnip 2>&1 | grep -A1 ":443" | head -12
echo
echo "monitoring 8 min: weight vs container traffic to testnet ..."
python3 - <<'PY'
import subprocess, time, re, collections, datetime, os

CPID = subprocess.run(["docker","inspect","-f","{{.State.Pid}}","replit_scalper-api-testnet"],
                      capture_output=True, text=True).stdout.strip()

def ss():
    out = subprocess.run(["nsenter","-t",CPID,"-n","/usr/bin/ss","-tni","-p"],
                         capture_output=True, text=True).stdout
    res = {}
    cur = None
    for ln in out.splitlines():
        if re.match(r"^(ESTAB|TIME-WAIT|CLOSE-WAIT|FIN-WAIT|SYN-SENT)", ln):
            p = ln.split()
            peer = p[4] if len(p) > 4 else "?"
            ip = peer.rsplit(":",1)[0].strip("[]").replace("::ffff:","")
            cur = (p[3], ip)
        else:
            m = re.search(r"bytes_sent:(\d+)", ln)
            if m and cur:
                res[cur] = int(m.group(1)); cur = None
    return res

def weight():
    out = subprocess.run(["curl","-s","-m","8","-D","-","-o","/dev/null",
                          "https://testnet.binancefuture.com/fapi/v1/ping"],
                         capture_output=True, text=True).stdout
    m = re.search(r"[Xx]-[Mm][Bb][Xx]-[Uu]sed-[Ww]eight-1m:\s*(\d+)", out)
    return int(m.group(1)) if m else -1

print(f"{'time':<10}{'weight':>8}{'testnet_KB/30s':>16}{'other_KB/30s':>14}")
prev = ss()
start = time.time()
while time.time() - start < 480:
    time.sleep(30)
    cur = ss()
    tn = ot = 0
    for k,v in cur.items():
        if k in prev and v >= prev[k]:
            d = v - prev[k]
            if k[1].startswith("65.8.131."): tn += d
            else: ot += d
    prev = cur
    print(f"{datetime.datetime.utcnow().strftime('%H:%M:%S'):<10}{weight():>8}{tn/1024:>16.1f}{ot/1024:>14.1f}", flush=True)
PY
echo
echo "=== bot demand (last window per bot) ==="
python3 - <<'PY'
import glob, re, os
tot_r=tot_w=0; n=0
for f in glob.glob("/opt/trading-bot/bot/logs/testnet/api_*_stdout.log"):
    try: lines=open(f,errors="ignore").read().splitlines()
    except Exception: continue
    for ln in reversed(lines[-600:]):
        if "[RATE_LIMIT] demand" in ln:
            m=re.search(r"req=(\d+) w=(\d+)", ln)
            if m: tot_r+=int(m.group(1)); tot_w+=int(m.group(2)); n+=1
            break
print(f"bots={n} req/min={tot_r} weight/min={tot_w}")
PY
