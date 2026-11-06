#!/bin/bash
echo "=== resolve binance hosts ==="
for h in testnet.binancefuture.com fapi.binance.com fstream.binancefuture.com stream.binancefuture.com; do
  echo -n "$h: "; getent ahostsv4 "$h" | awk '{print $1}' | sort -u | tr '\n' ' '; echo
done
echo
echo "=== traffic by peer ip over 30s ==="
python3 - <<'PY'
import subprocess, time, re, collections

def parse():
    out = subprocess.run(["ss","-tni"], capture_output=True, text=True).stdout
    res = {}
    cur = None
    for ln in out.splitlines():
        if ln.startswith(("ESTAB","TIME-WAIT","CLOSE-WAIT","FIN-WAIT")):
            parts = ln.split()
            try:
                peer = parts[4]
                ip = peer.rsplit(":",1)[0]
            except Exception:
                ip = "?"
            cur = (parts[3], ip)
        else:
            m = re.search(r"bytes_sent:(\d+)", ln)
            if m and cur:
                res[cur] = int(m.group(1))
                cur = None
    return res

a=parse(); time.sleep(30); b=parse()
agg=collections.Counter()
for k,v in b.items():
    if k in a and v>=a[k]:
        agg[k[1]] += v-a[k]
print(f"{'peer_ip':<20}{'bytes/30s':>12}{'KB/s':>10}")
for ip,d in agg.most_common(12):
    print(f"{ip:<20}{d:>12}{d/1024/30:>10.1f}")
print("TOTAL KB/s:", round(sum(agg.values())/1024/30,1))
PY
echo
echo "=== sockets to binance peers with process ==="
ss -tnp 2>/dev/null | grep -E ":443" | sed -E 's/.*(ESTAB|SYN-SENT|TIME-WAIT|FIN-WAIT-1|FIN-WAIT-2)[^0-9]*[0-9.:]+ +([0-9.]+):443.*users:\(\("([^"]+)".*/\2 \3/' | sort | uniq -c | sort -rn | head -20
echo
echo "=== crontab / other python services ==="
crontab -l 2>/dev/null | head -20
systemctl list-units --type=service --state=running 2>/dev/null | grep -iE "bot|trade|scalp|grid" || true
echo
echo "=== grid rows in db (python sqlite) ==="
python3 - <<'PY'
import sqlite3, glob
for db in ["/opt/trading-bot/data/bot.db","/opt/trading-bot/data/bot_live.db"]:
    try:
        con=sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        tabs=[r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        gt=[t for t in tabs if "grid" in t.lower()]
        print(db, "grid tables:", gt)
        for t in gt:
            try:
                n=con.execute(f"select count(*) from {t}").fetchone()[0]
                print("  ", t, "rows:", n)
                if t.endswith("bots") or t=="grid_bots":
                    cols=[d[1] for d in con.execute(f"PRAGMA table_info({t})")]
                    st=[c for c in cols if c in ("status","is_active","is_running","enabled","symbol")]
                    if st:
                        for row in con.execute(f"select {','.join(st)} from {t} limit 20"):
                            print("     ", dict(zip(st,row)))
            except Exception as e:
                print("   err", e)
        con.close()
    except Exception as e:
        print(db, "ERR", e)
PY
