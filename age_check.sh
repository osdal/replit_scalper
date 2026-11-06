#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
for r in con.execute("""SELECT id, symbol, entry_time,
   ROUND((julianday('now')-julianday(entry_time))*24,2) jd_hours,
   ROUND((julianday('now'))*24,2) now_jd_h,
   ROUND(julianday(entry_time)*24,2) entry_jd_h
FROM trades WHERE is_open=1 AND status!='rejected'"""):
    print(r)
now = datetime.datetime.now(datetime.timezone.utc)
print('py now utc:', now.isoformat())
for r in con.execute("SELECT id, symbol, entry_time FROM trades WHERE is_open=1 AND status!='rejected'"):
    et = datetime.datetime.fromisoformat(r[2].replace('Z','+00:00'))
    if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
    print(r[1], 'age_h=', round((now-et).total_seconds()/3600, 2))
PY
echo
echo "=== open positions on exchange (container) ==="
docker exec -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 -c "import asyncio,os;from dotenv import load_dotenv;load_dotenv('/app/.env',override=True);from binance import AsyncClient;
async def m():
 c=await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,api_secret=os.getenv('BINANCE_API_SECRET') or None,testnet=True)
 try:
  [print(p['symbol'],p['positionAmt'],p.get('entryPrice'),p.get('unRealizedProfit')) for p in await c.futures_position_information() if abs(float(p.get('positionAmt',0) or 0))>0]
 finally:
  await c.close_connection()
asyncio.run(m())" 2>&1 | tail -10
