#!/bin/bash
cd /opt/trading-bot
date -u
echo "=== git ==="
git log --oneline -1
git status --short | head -5
echo
echo "=== контейнер ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
echo
echo "=== открытые (БД) ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
rows = con.execute("SELECT id,symbol,direction,qty,entry_price,entry_time FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time").fetchall()
for r in rows:
    et = datetime.datetime.fromisoformat(r['entry_time']).replace(tzinfo=datetime.timezone.utc)
    print(f"{r['symbol']:<10} {r['direction']:<5} id={r['id']} qty={r['qty']} entry={r['entry_price']} "
          f"n=${r['qty']*r['entry_price']:.2f} age={(now-et).total_seconds()/3600:.1f}h")
print("открытых:", len(rows))
PY
echo
echo "=== боты ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
bad = []
for r in con.execute("SELECT symbol,is_running,armed,last_heartbeat FROM bots"):
    try:
        et = datetime.datetime.fromisoformat(str(r['last_heartbeat']).replace('Z','+00:00'))
        if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
        age = (now-et).total_seconds()/60
    except Exception:
        age = 999
    if not r['is_running'] or not r['armed'] or age > 15:
        bad.append((r['symbol'], r['is_running'], r['armed'], round(age,1)))
print("проблемных:", len(bad), bad[:6])
PY
echo
echo "=== daily_loss сегодня ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
day = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
print("day:", day, "loss:", round(con.execute("SELECT COALESCE(SUM(-pnl),0) FROM trades WHERE is_open=0 AND pnl<0 AND exit_time >= ?", (day,)).fetchone()[0], 4))
PY
