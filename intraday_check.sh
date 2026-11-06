#!/bin/bash
cd /opt/trading-bot
echo "=== 1. Sizing новых входов 03.11 ==="
grep -hE "Size by position_size_pct|qty raised|Market order placed|CAP\]" bot/logs/testnet/api_aptusdt_stdout.log bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null | grep "2026-11-03" | tail -20
echo
echo "=== 2. Отказы после разблокировки (11:06+) ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
for r in con.execute("""SELECT reject_reason, COUNT(*) n FROM trades
WHERE status='rejected' AND entry_time >= '2026-11-03T11:06:00' GROUP BY reject_reason ORDER BY n DESC"""):
    print(dict(r))
PY
echo
echo "=== 3. Открытые ордера на бирже по DOGE/APT/MTL ==="
python3 - <<'PY'
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/opt/trading-bot/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for s in ('DOGEUSDT','APTUSDT','MTLUSDT'):
            try:
                orders = await c.futures_get_open_orders(symbol=s)
            except Exception as e:
                print(s, 'ERR', e); continue
            print(s, 'regular:', [(o['type'], o['side'], o['price'], o['stopPrice'], o['closePosition']) for o in orders])
            try:
                algos = await c.futures_get_open_algo_orders(symbol=s)
                print(s, 'algo:', [(a.get('algoType'), a.get('side'), a.get('triggerPrice'), a.get('closePosition')) for a in (algos or [])])
            except Exception as e:
                print(s, 'algo ERR', e)
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== 4. ERROR после 11:06 ==="
grep -h "ERROR" bot/logs/testnet/api_*.log 2>/dev/null | grep -E "2026-11-03 (11|12):" | tail -10
echo "(всего: $(grep -h "ERROR" bot/logs/testnet/api_*.log 2>/dev/null | grep -cE "2026-11-03 (11|12):"))"
echo
echo "=== 5. /trading/check ETH ==="
curl -s -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"ETHUSDT"}'
echo
