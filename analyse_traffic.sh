#!/bin/bash
echo "=== all containers ==="
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
echo
echo "=== live bot process count ==="
docker exec replit_scalper-api-live bash -lc 'ps aux | grep "main.py" | grep -v grep | wc -l' 2>&1
echo "=== live demand sums ==="
python3 - <<'PY'
import glob, re, os
L="/opt/trading-bot/bot/logs"
def sumdir(d):
    tot_r=tot_w=0; n=0
    for f in glob.glob(os.path.join(d,"api_*_stdout.log")):
        try: lines=open(f,errors="ignore").read().splitlines()
        except Exception: continue
        for ln in reversed(lines[-600:]):
            if "[RATE_LIMIT] demand" in ln:
                m=re.search(r"req=(\d+) w=(\d+)", ln)
                if m:
                    tot_r+=int(m.group(1)); tot_w+=int(m.group(2)); n+=1
                break
    return n,tot_r,tot_w
for d in (L, L+"/testnet", L+"/live"):
    print(d, sumdir(d))
PY
echo
echo "=== socket traffic snapshot (30s), grouped by process ==="
python3 - <<'PY'
import subprocess, time, re, collections

def snap():
    out = subprocess.run(["ss","-tni","-p"], capture_output=True, text=True).stdout
    # entries separated by blank line
    entries = [e for e in out.split("\n\n") if "bytes_sent" in e]
    res = {}
    for e in entries:
        m = re.search(r'bytes_sent:(\d+)', e)
        if not m: continue
        bs = int(m.group(1))
        p = re.search(r'users:\(\("([^"]+)"', e)
        proc = p.group(1) if p else "?"
        peer = e.split()[4] if len(e.split())>4 else "?"
        key = (proc, peer.split(":")[0])
        res[key] = bs
    return res

a = snap(); time.sleep(30); b = snap()
agg = collections.Counter()
for k, v in b.items():
    if k in a and v >= a[k]:
        agg[k[0]] += v - a[k]
print(f"{'process':<24}{'peer':<18}{'sent/30s':>12}{'bytes/s':>10}")
for (proc, peer), d in agg.most_common(15):
    print(f"{proc:<24}{peer:<18}{d:>12}{d/30:>10.0f}")
print("total bytes/30s:", sum(agg.values()))
PY
