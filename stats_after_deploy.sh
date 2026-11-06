#!/bin/bash
cd /opt/trading-bot
echo "=== маркеры нового кода в логах (с 04.11 22:00) ==="
echo "DUST offset applied: $(grep -h 'DUST\] offset applied' bot/logs/testnet/api_*.log | grep -cE '2026-11-0[45]')"
echo "DUST skip new part : $(grep -h 'new part below minNotional' bot/logs/testnet/api_*.log | grep -cE '2026-11-0[45]')"
echo "DUST covered skip  : $(grep -h 'already covered by exchange' bot/logs/testnet/api_*.log | grep -cE '2026-11-0[45]')"
echo
echo "=== примеры DUST (последние 10) ==="
grep -hE "DUST\]|GUARD\] stale dust" bot/logs/testnet/api_*.log | grep -E "2026-11-0[45]" | tail -10
echo
echo "=== ошибки биржи за 05.11 ==="
echo "-2022: $(grep -h '\-2022' bot/logs/testnet/api_*.log | grep -c '2026-11-05')"
echo "-4164: $(grep -h '\-4164' bot/logs/testnet/api_*.log | grep -c '2026-11-05')"
echo "-1003: $(grep -h '\-1003' bot/logs/testnet/api_*.log | grep -c '2026-11-05')"
echo
echo "=== закрытые сделки с 04.11 22:00 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
rows = con.execute("""SELECT id,symbol,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,
ROUND(exit_price,6) exit,ROUND(pnl,4) pnl,exit_reason,exit_time
FROM trades WHERE is_open=0 AND exit_time >= '2026-11-04T22:00:00'
ORDER BY exit_time""").fetchall()
tot = 0.0; wins = 0
for r in rows:
    p = float(r['pnl'] or 0); tot += p; wins += 1 if p > 0 else 0
    print(f"{r['exit_time'][:16]} {r['symbol']:<9} {r['direction']:<5} pnl={p:+.4f} {r['exit_reason']}")
print(f"\nитог: {len(rows)} сделок, {wins} в плюс, PnL={tot:+.4f}")
PY
echo
echo "=== пнл/комиссии по дням ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT substr(exit_time,1,10) d, COUNT(*) n,
SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) w, ROUND(SUM(pnl),4) pnl, ROUND(SUM(fees),4) fees
FROM trades WHERE is_open=0 AND exit_time >= '2026-11-02' GROUP BY d ORDER BY d"""):
    print(dict(r))
PY
echo
echo "=== daily_loss + лимит ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
day = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
loss = con.execute("SELECT COALESCE(SUM(-pnl),0) FROM trades WHERE is_open=0 AND pnl<0 AND exit_time >= ?", (day,)).fetchone()[0]
print("day:", day, "loss:", round(loss,4))
PY
grep -E "daily_loss_limit_usd|max_free_debt_usd|max_positions" bot/recovery_config.yaml
