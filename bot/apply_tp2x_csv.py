import csv
import glob
import os
import sys

csv_path = sys.argv[1] if len(sys.argv) > 1 else "logs/tp2x_opt_20260801_011323.csv"

import yaml

# Script lives in bot/. Configs are beside it; CSV is under bot/logs/.
BOTBASE = os.path.dirname(os.path.abspath(__file__))
if not os.path.isabs(csv_path):
    csv_path = os.path.join(BOTBASE, csv_path)

applied = 0
missing = 0
with open(csv_path, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        sym = row["symbol"]
        low = sym.replace("USDT", "").lower()
        cfg_file = os.path.join(BOTBASE, f"config_{low}.yaml")
        if not os.path.exists(cfg_file):
            print(f"{sym}: config not found ({cfg_file})")
            missing += 1
            continue
        with open(cfg_file, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        tp = float(row["tp_pct"])
        cfg["ema_fast"] = int(row["ema_fast"])
        cfg["ema_slow"] = int(row["ema_slow"])
        cfg["sl_pct"] = float(row["sl_pct"])
        cfg["tp1_pct"] = tp
        cfg["tp2_pct"] = tp
        cfg["tp1_close_pct"] = 100
        cfg["volume_multiplier"] = float(row["vol_mult"])
        cfg["risk_pct"] = 3.0
        cfg["htf_enabled"] = True
        cfg["htf_ema_fast"] = int(row["htf_f"])
        cfg["htf_ema_slow"] = int(row["htf_s"])
        with open(cfg_file, "w", encoding="utf-8") as fh:
            yaml.dump(cfg, fh, default_flow_style=False, allow_unicode=True)
        print(f"Updated {os.path.basename(cfg_file)} | EMA={cfg['ema_fast']}/{cfg['ema_slow']} "
              f"SL={cfg['sl_pct']} TP={tp} Vol={cfg['volume_multiplier']} HTF={cfg['htf_ema_fast']}/{cfg['htf_ema_slow']} fixed_notional={cfg.get('fixed_notional_usd')}")
        applied += 1

print(f"\nApplied={applied} Missing={missing}")

# verify fixed_notional preserved
import glob
bad = []
for f in glob.glob(os.path.join(BOTBASE, "config_*.yaml")):
    with open(f, encoding="utf-8") as fh:
        c = yaml.safe_load(fh)
    if c.get("fixed_notional_usd") != 2.0:
        bad.append(os.path.basename(f))
print("Configs NOT having fixed_notional_usd=2.0:", bad if bad else "none (all good)")
