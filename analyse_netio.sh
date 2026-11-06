#!/bin/bash
PID=$(docker inspect -f '{{.State.Pid}}' replit_scalper-api-testnet)
echo "=== nsenter sanity ==="
nsenter -t "$PID" -n hostname -I 2>&1
echo "--- ss raw (5 lines) ---"
nsenter -t "$PID" -n /usr/bin/ss -tn 2>&1 | head -8
echo "--- ss count ESTAB 443 ---"
nsenter -t "$PID" -n /usr/bin/ss -tn 2>&1 | grep -c ":443" 
echo
echo "=== docker stats net io delta over 60s ==="
python3 - <<'PY'
import subprocess, time, re

def stats():
    out = subprocess.run(["docker","stats","--no-stream","--format",
        "{{.Name}}\t{{.NetIO}}"], capture_output=True, text=True).stdout
    d = {}
    for ln in out.splitlines():
        try: name, io = ln.split("\t")
        except ValueError: continue
        m = re.match(r"([\d.]+)\s*(\w+)\s*/\s*([\d.]+)\s*(\w+)", io)
        if not m: continue
        mult = {"B":1,"kB":1024,"KiB":1024,"MB":1024**2,"MiB":1024**2,"GB":1024**3,"GiB":1024**3}
        rx = float(m.group(1)) * mult.get(m.group(2),1)
        tx = float(m.group(3)) * mult.get(m.group(4),1)
        d[name] = (rx, tx)
    return d

a = stats(); time.sleep(60); b = stats()
rows = []
for k in set(a) & set(b):
    drx = b[k][0]-a[k][0]; dtx = b[k][1]-a[k][1]
    rows.append((dtx, drx, k))
rows.sort(reverse=True)
print(f"{'container':<50}{'tx_KB/60s':>11}{'rx_KB/60s':>11}")
for tx, rx, k in rows[:12]:
    print(f"{k:<50}{tx/1024:>11.1f}{rx/1024:>11.1f}")
PY
