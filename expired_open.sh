#!/bin/bash
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY'
import sqlite3, datetime, os, asyncio
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient

con = sqlite3.connect('/app/data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)

async def fetch():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        return {p['symbol']: (float(p.get('unRealizedProfit',0) or 0), float(p.get('positionAmt',0) or 0))
                for p in await c.futures_position_information()}
    finally:
        await c.close_connection()
ex = asyncio.run(fetch())

print("=== открытые: возраст, notional, порог EXPIRED, upnl ===")
rows = con.execute("""SELECT id,symbol,direction,qty,entry_price,entry_time
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time""").fetchall()
for r in rows:
    et = datetime.datetime.fromisoformat(str(r['entry_time']).replace('Z','+00:00'))
    if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
    age = (now-et).total_seconds()/3600
    notional = abs(float(r['qty'])) * float(r['entry_price'])
    req = notional * 0.0005 + 0.02
    u, amt = ex.get(r['symbol'], (0.0, 0.0))
    if age >= 2:
        state = "ЗАКРЫТЬ" if u >= req else f"ждёт +{req-u:.4f}"
    else:
        state = "молодая"
    print(f"{r['symbol']:<9} {r['direction']:<5} age={age:5.1f}h notional=${notional:6.2f} "
          f"required=${req:.4f} upnl={u:+.4f} amt={amt} -> {state}")
PY
