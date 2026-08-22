import sqlite3
import yaml
import os

db_path = r'C:\DATA\bots\replit_scalper\data\bot.db'
bot_dir = r'C:\DATA\bots\replit_scalper\bot'

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

symbols = ['LTCUSDT','TONUSDT','TIAUSDT','BCHUSDT','AAVEUSDT','RENDERUSDT','IMXUSDT','WIFUSDT','FETUSDT','TAOUSDT','UNIUSDT']
rows = conn.execute("SELECT symbol, is_running FROM bots WHERE symbol IN (" + ",".join(["?"]*len(symbols)) + ")", symbols).fetchall()
print('Existing in DB:', [dict(r) for r in rows])

new_bots = [
    ('LTCUSDT', 'config_ltc.yaml'),
    ('TONUSDT', 'config_ton.yaml'),
    ('TIAUSDT', 'config_tia.yaml'),
    ('BCHUSDT', 'config_bch.yaml'),
    ('AAVEUSDT', 'config_aave.yaml'),
    ('RENDERUSDT', 'config_render.yaml'),
    ('IMXUSDT', 'config_imx.yaml'),
    ('WIFUSDT', 'config_wif.yaml'),
    ('FETUSDT', 'config_fet.yaml'),
    ('TAOUSDT', 'config_tao.yaml'),
    ('UNIUSDT', 'config_uni.yaml'),
]

existing = {r['symbol'] for r in rows}
added = 0
for symbol, config_file in new_bots:
    if symbol in existing:
        continue
    with open(os.path.join(bot_dir, config_file), 'r') as f:
        cfg = yaml.safe_load(f)
    conn.execute('''
        INSERT INTO bots (symbol, mode, timeframe, leverage, risk_pct, sl_pct, tp1_pct, tp1_close_pct, tp2_pct, ema_fast, ema_slow, volume_ma_period, volume_multiplier, htf_enabled, htf_timeframe, htf_ema_fast, htf_ema_slow, htf2_enabled, htf2_timeframe, htf2_ema_fast, htf2_ema_slow, auto_mode, paper_balance, log_file, is_running)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        symbol,
        cfg.get('mode', 'paper'),
        cfg.get('timeframe', '5m'),
        cfg.get('leverage', 75),
        cfg.get('risk_pct', 3.0),
        cfg.get('sl_pct', 0.5),
        cfg.get('tp1_pct', 1.0),
        cfg.get('tp1_close_pct', 100),
        cfg.get('tp2_pct', 1.0),
        cfg.get('ema_fast', 8),
        cfg.get('ema_slow', 24),
        cfg.get('volume_ma_period', 20),
        cfg.get('volume_multiplier', 1.2),
        cfg.get('htf_enabled', True),
        cfg.get('htf_timeframe', '1h'),
        cfg.get('htf_ema_fast', 8),
        cfg.get('htf_ema_slow', 24),
        cfg.get('htf2_enabled', False),
        cfg.get('htf2_timeframe', '15m'),
        cfg.get('htf2_ema_fast', 12),
        cfg.get('htf2_ema_slow', 26),
        cfg.get('auto_mode', True),
        cfg.get('paper_balance', 1000.0),
        cfg.get('log_file', 'logs/' + symbol.lower() + '.log'),
        True,
    ))
    added += 1

conn.commit()
print(f'Added {added} new bots to DB')

rows = conn.execute("SELECT symbol, is_running FROM bots WHERE symbol IN (" + ",".join(["?"]*len(symbols)) + ")", symbols).fetchall()
print('Now in DB:', [dict(r) for r in rows])
conn.close()
