#!/bin/bash
cd /opt/trading-bot
echo "=== 1. Открытые accepted (БД) ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,symbol,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,
ROUND(qty*entry_price,2) notional,ROUND(pnl,4) pnl,
ROUND((julianday('now')-julianday(entry_time))*60,0) age_min,status
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time"""):
    print(dict(r))
print("--- всего открытых:", con.execute("SELECT COUNT(*) FROM trades WHERE is_open=1 AND status!='rejected'").fetchone()[0])
PY
echo
echo "=== 2. Позиции на бирже (testnet) ==="
python3 - <<'PY'
import asyncio, os, re
env = {}
for line in open('/opt/trading-bot/.env.testnet', encoding='utf-8', errors='ignore'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, v = line.split('=', 1)
    env[k.strip()] = v.strip().strip('"').strip("'")
for line in open('/opt/trading-bot/.env', encoding='utf-8', errors='ignore'):
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, v = line.split('=', 1)
    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=env.get('BINANCE_API_KEY') or None,
                                 api_secret=env.get('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for p in await c.futures_position_information():
            amt = float(p.get('positionAmt', 0) or 0)
            if abs(amt) > 0:
                print(p['symbol'], amt, 'entry', p.get('entryPrice'), 'upnl', p.get('unRealizedProfit'))
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== 3. /trading/check ETH ==="
curl -s -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"ETHUSDT"}'
echo
echo "=== 4. Новые входы 03.11 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,symbol,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,
ROUND(qty*entry_price,2) notional,status,entry_time FROM trades
WHERE entry_time >= '2026-11-03' AND status!='rejected' ORDER BY id"""):
    print(dict(r))
PY
echo
echo "=== 5. Закрытия 03.11 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
rows = con.execute("""SELECT symbol,exit_reason,ROUND(pnl,4) pnl,ROUND(qty*entry_price,2) notional,exit_time
FROM trades WHERE exit_time >= '2026-11-03' AND is_open=0 AND status!='rejected' ORDER BY exit_time""").fetchall()
for r in rows: print(dict(r))
print("итого:", len(rows), "pnl=", round(sum(r['pnl'] or 0 for r in rows), 4))
PY
echo
echo "=== 6. ERROR 03.11 (шаблоны) ==="
grep -h "ERROR" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-03" | sed -E 's/.*ERROR //' | sed -E 's/[0-9]+/N/g' | sort | uniq -c | sort -rn | head -10
