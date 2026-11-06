#!/bin/bash
for P in 3058867 3058866 3058820 3058819; do
  echo "=== PID $P ==="
  echo -n "cmdline: "; tr '\0' ' ' < /proc/$P/cmdline 2>/dev/null; echo
  echo -n "cwd: "; readlink /proc/$P/cwd 2>/dev/null
  echo "--- start/parent ---"
  ps -o pid,ppid,etime,user,args -p $P 2>/dev/null
  echo "--- key hash / env ---"
  python3 - "$P" <<'PY'
import sys, hashlib
pid=sys.argv[1]
try: env=open(f"/proc/{pid}/environ","rb").read().split(b"\x00")
except Exception as e: print("no env", e); raise SystemExit
d=dict(kv.split(b"=",1) for kv in env if b"=" in kv)
k=d.get(b"BINANCE_API_KEY",b"").decode()
print("key sha1:", hashlib.sha1(k.encode()).hexdigest()[:8] if k else "NONE",
      "testnet:", d.get(b"BINANCE_TESTNET",b"?").decode(),
      "bot_env:", d.get(b"BOT_ENV",b"?").decode(),
      "symbol:", d.get(b"SYMBOL",b"?").decode())
PY
  echo
done
echo "=== all 'bot.main' / 'web.backend' processes ==="
ps -eo pid,ppid,etime,args | grep -E "bot\.main|web\.backend" | grep -v grep
echo
echo "=== parent tree ==="
ps -o pid,ppid,etime,args -p 3058819,3058820,3058866,3058867 2>/dev/null
