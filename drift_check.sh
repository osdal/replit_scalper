#!/bin/bash
cd /opt/trading-bot
date -u
echo "=== открытые строки БД ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,symbol,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,is_open,status,entry_time
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY symbol,id"""):
    print(dict(r))
PY
echo
echo "=== биржа ==="
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -8
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for p in await c.futures_position_information():
            a = float(p.get('positionAmt', 0) or 0)
            if abs(a) > 0:
                print(f"{p['symbol']:<10} amt={a} entry={p['entryPrice']} upnl={p.get('unRealizedProfit')}")
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== SUI: последние синки/гарды ==="
grep -hE "2026-11-04 21:(2[6-9]|3[0-9]):" bot/logs/testnet/api_suiusdt_stdout.log | grep -E "GUARD|SYNC|DUST|TP|Position|stale" | tail -12
