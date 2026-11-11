#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== set max_positions=3 for the 8 testnet bots ==="
python3 - <<'PY'
import sqlite3
syms = ['ADAUSDT','FILUSDT','LSKUSDT','NEARUSDT','1000PEPEUSDT','TRXUSDT','DOGEUSDT','INJUSDT']
con = sqlite3.connect('data/bot.db')
con.execute("UPDATE bots SET max_positions=3 WHERE symbol IN (%s)" % ",".join("?"*len(syms)), syms)
con.commit()
for r in con.execute("SELECT symbol,max_positions,is_running FROM bots WHERE is_running=1 ORDER BY symbol"):
    print(r)
PY
echo
echo "=== recreate api-testnet (env change requires recreate, restart is not enough) ==="
docker compose up -d --force-recreate api-testnet
sleep 15
echo
echo "=== container env ==="
docker inspect replit_scalper-api-testnet --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'POST_BAN|SLOTS_FULL|MAX_POSITIONS=|STARTUP_STAGGER'
echo
echo "=== status ==="
curl -s -m 10 http://localhost:5000/api/trading/status
echo
