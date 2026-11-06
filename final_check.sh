#!/bin/bash
cd /opt/trading-bot
date -u
echo "=== LINK: строка сделки + остатки на бирже ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,ROUND(exit_price,6) exitp,
ROUND(pnl,4) pnl,ROUND(cycle_pnl,4) cycle,ROUND(commission,6) comm,is_open,exit_reason,ROUND(chain_depth,0) chain,exit_time
FROM trades WHERE symbol='LINKUSDT' ORDER BY id DESC LIMIT 3"""):
    print(dict(r))
PY
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -8
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for p in await c.futures_position_information(symbol='LINKUSDT'):
            print('POS', p['positionAmt'])
        print('ORD', len(await c.futures_get_open_orders(symbol='LINKUSDT')))
        print('ALG', len(await c.futures_get_open_algo_orders(symbol='LINKUSDT') or []))
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== -2022 по символам за 04.11 ==="
grep -h "\-2022" bot/logs/testnet/api_*.log | grep "2026-11-04" | sed -E 's/.*\[LIVE ([A-Z]+)\].*/\1/' | sort | uniq -c | sort -rn
echo
echo "=== -4164 за 04.11 (строки) ==="
grep -h "\-4164" bot/logs/testnet/api_*.log | grep "2026-11-04" | tail -4
echo
echo "=== Боты: состояние ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
bad=[]
for r in con.execute("SELECT symbol,is_running,armed,last_heartbeat FROM bots"):
    try:
        et = datetime.datetime.fromisoformat(str(r['last_heartbeat']).replace('Z','+00:00'))
        if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
        age=(now-et).total_seconds()/60
    except Exception:
        age=999
    if not r['is_running'] or not r['armed'] or age>15: bad.append((r['symbol'],r['is_running'],r['armed'],round(age,1)))
print("проблемных:", len(bad), bad[:8])
PY
echo
echo "=== Итог с 03.11 11:00 (все закрытые сделки \$10-режима) ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
rows = con.execute("""SELECT symbol,exit_reason,ROUND(pnl,4) pnl,ROUND(commission,6) comm,exit_time
FROM trades WHERE is_open=0 AND status!='rejected' AND exit_time >= '2026-11-03T11:00:00' ORDER BY exit_time""").fetchall()
for r in rows: print(f"{r['exit_time'][:19]} {r['symbol']:<10} {r['exit_reason']:<24} {r['pnl']:>9}")
print("сделок:", len(rows), "| pnl=", round(sum(r['pnl'] or 0 for r in rows),4),
      "| wins", sum(1 for r in rows if (r['pnl'] or 0)>0),
      "| losses", sum(1 for r in rows if (r['pnl'] or 0)<0),
      "| комиссии", round(sum(r['comm'] or 0 for r in rows),4))
PY
