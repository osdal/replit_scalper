import sqlite3
import os

db_path = r'C:\DATA\bots\replit_scalper\data\bot.db'
if not os.path.exists(db_path):
    print(f'Database not found: {db_path}')
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute('UPDATE trading_control SET loss_streak = 100 WHERE id = 1')
conn.commit()
rows = cursor.execute('SELECT loss_streak FROM trading_control WHERE id = 1').fetchone()
print(f'Updated loss_streak to: {rows[0]}')
conn.close()
