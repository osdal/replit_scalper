#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect("data/bot.db"); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
for r in con.execute("""SELECT id FROM trades WHERE symbol='ETHUSDT' AND is_open=1 AND status!='rejected'"""):
    tid = r['id']
    con.execute("""UPDATE trades SET is_open=0, exit_reason='INFRA_BAN_DELAY', exit_time=?, pnl=0, exit_price=0 WHERE id=?""", (now, tid))
    print(f"Closed trade #{tid} as INFRA_BAN_DELAY")
con.commit()
print("Done")
PY