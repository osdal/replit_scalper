"""
Применяет результаты оптимизации (tp2x_opt_*.csv или symmetric_opt_*.csv)
к соответствующим config_*.yaml.

Usage:
    python apply_opt_to_config.py logs/tp2x_opt_20260731_073918.csv
    python apply_opt_to_config.py --file logs/symmetric_opt_20260731_055017.csv
"""

import argparse
import csv
import os
import sys

import yaml


BOT_DIR = os.path.dirname(__file__)


def apply_to_config(symbol: str, params: dict) -> None:
    sym_lower = symbol.replace("USDT", "").lower()
    cfg_path = os.path.join(BOT_DIR, f"config_{sym_lower}.yaml")
    if not os.path.exists(cfg_path):
        print(f"  [{symbol}] SKIP: config not found ({os.path.basename(cfg_path)})")
        return

    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    cfg["ema_fast"] = int(params["ema_fast"])
    cfg["ema_slow"] = int(params["ema_slow"])
    cfg["volume_multiplier"] = float(params["vol_mult"])
    cfg["risk_pct"] = 3.0
    cfg["fixed_qty"] = 0.0
    cfg["tp1_close_pct"] = 100
    cfg["htf_enabled"] = True
    cfg["htf_ema_fast"] = int(params["htf_f"])
    cfg["htf_ema_slow"] = int(params["htf_s"])

    if "sl_pct" in params and "tp_pct" in params:
        # TP = 2x SL
        cfg["sl_pct"] = float(params["sl_pct"])
        cfg["tp1_pct"] = float(params["tp_pct"])
        cfg["tp2_pct"] = float(params["tp_pct"])
    else:
        # Symmetric SL = TP
        cfg["sl_pct"] = float(params["sl_tp_pct"])
        cfg["tp1_pct"] = float(params["sl_tp_pct"])
        cfg["tp2_pct"] = float(params["sl_tp_pct"])

    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

    sl = cfg["sl_pct"]
    tp = cfg["tp1_pct"]
    print(f"  [{symbol}] OK | SL={sl}% TP={tp}% EMA={cfg['ema_fast']}/{cfg['ema_slow']} Vol={cfg['volume_multiplier']} HTF={cfg['htf_ema_fast']}/{cfg['htf_ema_slow']}")


def main():
    parser = argparse.ArgumentParser(description="Apply optimizer results to config files")
    parser.add_argument("--file", required=True, help="Path to tp2x_opt_*.csv or symmetric_opt_*.csv")
    parser.add_argument("--symbols", default=None, help="Comma-separated subset of symbols to apply")
    args = parser.parse_args()

    file_path = args.file
    if not os.path.isabs(file_path):
        file_path = os.path.join(BOT_DIR, file_path)

    if not os.path.exists(file_path):
        print(f"  File not found: {file_path}")
        sys.exit(1)

    with open(file_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("  CSV is empty")
        return

    # Detect type from header
    is_tp2x = "tp_pct" in (reader.fieldnames or [])
    opt_type = "TP=2xSL" if is_tp2x else "Symmetric SL=TP"
    print(f"  Optimizer type: {opt_type}")
    print(f"  Symbols: {len(rows)}")
    print()

    only = set(s.strip().upper() for s in args.symbols.split(",")) if args.symbols else None

    applied = 0
    for row in rows:
        symbol = row["symbol"].strip()
        if only and symbol not in only:
            continue
        apply_to_config(symbol, row)
        applied += 1

    print(f"\n  Applied to {applied} configs.")
    print("  Restart bots or press Refresh in dashboard to reload.")


if __name__ == "__main__":
    main()
