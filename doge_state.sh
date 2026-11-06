#!/bin/bash
cd /opt/trading-bot
echo "=== DOGE: входы за 03.11 ==="
grep -hE "Market order placed|Size by|SYNC|qty raised" bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null | tail -25
echo
echo "=== DB: все строки DOGE за 03.11 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
for r in con.execute("SELECT id,symbol,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,ROUND(pnl,4) pnl,is_open,status,exit_reason,entry_time,exit_time FROM trades WHERE symbol='DOGEUSDT' ORDER BY id DESC LIMIT 6"):
    print(dict(r))
print("--- все открытые:")
for r in con.execute("SELECT id,symbol,direction,ROUND(qty,4),ROUND(entry_price,6),ROUND(qty*entry_price,2),is_open,status,entry_time FROM trades WHERE is_open=1 AND status!='rejected'"):
    print(r)
PY
echo
echo "=== Биржа: все ненулевые позиции (testnet) ==="
docker exec -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -20
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env.testnet', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        ps = await c.futures_position_information()
        for p in ps:
            amt = float(p.get('positionAmt', 0) or 0)
            if abs(amt) > 0:
                print(p['symbol'], amt, p.get('entryPrice'))
    finally:
        await c.close_connection()
asyncio.run(main())
PY
