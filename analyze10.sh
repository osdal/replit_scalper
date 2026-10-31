#!/bin/bash
echo "=== где вообще используется max_positions_with_expired ==="
grep -rn "max_positions_with_expired" /opt/trading-bot/bot /opt/trading-bot/artifacts --include=*.py --include=*.ts --include=*.tsx | grep -v node_modules | head -20
echo
echo "=== expired_after_minutes в коде ==="
grep -rn "expired_after_minutes" /opt/trading-bot/bot /opt/trading-bot/artifacts --include=*.py --include=*.ts | grep -v node_modules | head -20
echo
echo "=== EXCHANGE_CHECK / env ==="
docker exec replit_scalper-api-testnet sh -c 'env | grep -Ei "EXCHANGE_CHECK|MAX_POSITIONS|DASHBOARD_API"' 
echo
echo "=== сколько открытых позиций в БД и их возраст ==="
python3 - <<'PY'
import sqlite3,datetime
con=sqlite3.connect("file:/opt/trading-bot/data/bot.db?mode=ro", uri=True)
c=con.cursor()
now=datetime.datetime.utcnow()
for sym,d,et,q,p in c.execute("SELECT symbol,direction,entry_time,qty,entry_price FROM trades WHERE is_open=1 AND status!='rejected'"):
    try: age=(now-datetime.datetime.fromisoformat(et)).total_seconds()/3600
    except: age=None
    print(f"{sym:12} {d:5} age={age:.1f}h notional={q*p:.2f}")
PY
