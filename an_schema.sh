#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("data/bot.db")
con.row_factory = sqlite3.Row
print("=== SCHEMA trades ===")
for r in con.execute("PRAGMA table_info(trades)"):
    print(f"{r['name']:<24} {r['type']}")
print()
print("=== SCHEMA bots ===")
for r in con.execute("PRAGMA table_info(bots)"):
    print(f"{r['name']:<24} {r['type']}")
print()
print("=== TABLES ===")
for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    print(r['name'])
PY
