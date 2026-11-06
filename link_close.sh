#!/bin/bash
cd /opt/trading-bot
date -u
echo "=== LINK: лог 02:40-сейчас (без rate-limit шума) ==="
grep -hE "2026-11-04 02:(4|5)[0-9]:|2026-11-04 03:" bot/logs/testnet/api_linkusdt_stdout.log | grep -vE "RATE_LIMIT|PRICE\]|Heartbeat|Candle closed" | tail -25
echo
echo "=== LINK: позиция/ордера/строки БД ==="
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -12
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for p in await c.futures_position_information(symbol='LINKUSDT'):
            print(' POS', p['positionAmt'], p['entryPrice'], p.get('markPrice'), p.get('unRealizedProfit'))
        for o in await c.futures_get_open_orders(symbol='LINKUSDT'):
            print(' ORD', o.get('type'), o.get('side'), o.get('price'), o.get('origQty'), o.get('reduceOnly'))
        for a in await c.futures_get_open_algo_orders(symbol='LINKUSDT') or []:
            print(' ALG', a.get('orderType'), a.get('side'), a.get('triggerPrice'), a.get('closePosition'), a.get('algoId'))
    finally:
        await c.close_connection()
asyncio.run(main())
PY
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,ROUND(exit_price,6) exitp,
ROUND(pnl,4) pnl,ROUND(cycle_pnl,4) cycle_pnl,is_open,status,exit_reason,ROUND(chain_depth,0) chain,exit_time
FROM trades WHERE symbol='LINKUSDT' ORDER BY id DESC LIMIT 4"""):
    print(dict(r))
PY
echo
echo "=== Открытые сейчас ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
for r in con.execute("SELECT symbol,direction,qty,entry_price,entry_time FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time"):
    et = datetime.datetime.fromisoformat(r['entry_time']).replace(tzinfo=datetime.timezone.utc)
    print(f"{r['symbol']:<10} {r['direction']:<5} qty={r['qty']} entry={r['entry_price']} age={(now-et).total_seconds()/3600:.1f}h")
print("открытых:", con.execute("SELECT COUNT(*) FROM trades WHERE is_open=1 AND status!='rejected'").fetchone()[0])
PY
echo
echo "=== Баны -1003 за 04.11 (последние) ==="
grep -h "1003" bot/logs/testnet/api_*.log | grep "2026-11-04" | tail -6
