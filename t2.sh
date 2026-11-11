#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== SL rows with close details ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
q = """
SELECT id, symbol, direction, ROUND(pnl,4), ROUND(leg_pnl,4), ROUND(cycle_pnl,4),
       chain_depth, cycle_close_reason, exit_reason, entry_time, exit_time, preset
FROM trades
WHERE status!='rejected' AND exit_time >= '2026-11-05'
  AND exit_reason IN ('SL','REVERSE_CHAIN_STOP','stale_close')
ORDER BY id
"""
for r in con.execute(q):
    print(r)
print()
print("=== any chain_close_reason NULL on SL? ===")
for r in con.execute("SELECT cycle_close_reason, COUNT(*) FROM trades WHERE exit_reason='SL' GROUP BY cycle_close_reason"):
    print(r)
PY
echo
echo "=== reverse settings in testnet configs ==="
for f in bot/configs/testnet/config_ada.yaml bot/configs/testnet/config_doge.yaml bot/configs/testnet/config_lsk.yaml bot/configs/testnet/config_fil.yaml; do
  echo "--- $f"
  grep -nE 'reverse|backstop|stale|expired|sl_|tp_|chain|recovery' "$f" | head -30
done
