import glob
import os
import re
import datetime
from pathlib import Path

BOT_DIR = Path(".")
today = datetime.date.today().strftime("%Y%m%d")

pairs = {
    "ATOMUSDT": "atom", "DOGEUSDT": "doge", "ETHUSDT": "eth", "SOLUSDT": "sol",
    "INJUSDT": "inj", "OPUSDT": "op", "POLUSDT": "pol", "ONTUSDT": "ont",
    "HBARUSDT": "hbar", "NEARUSDT": "near", "SUIUSDT": "sui", "FILUSDT": "fil",
    "KASUSDT": "kas", "XRPUSDT": "xrp", "LINKUSDT": "link", "DOTUSDT": "dot",
    "TRXUSDT": "trx", "BTCUSDT": "btc", "BNBUSDT": "bnb", "AVAXUSDT": "avax",
    "ADAUSDT": "ada", "1000PEPEUSDT": "1000pepe", "ARBUSDT": "arb", "APTUSDT": "apt",
}

def best_row_for(pair):
    """Return the highest-score data row among today's fold CSVs for this pair."""
    cfgs = sorted(
        glob.glob(f"logs/optimization_{pair}_{today}_*.csv"),
        key=os.path.getmtime,
    )
    best = None
    best_score = -1
    for c in cfgs:
        lines = open(c).read().strip().split("\n")
        if len(lines) < 2:
            continue
        h = lines[0].split(",")
        row = lines[1].split(",")
        d = dict(zip(h, row))
        try:
            score = float(d.get("score", -1))
        except (ValueError, TypeError):
            score = -1
        if score > best_score:
            best_score = score
            best = d
    return best

def apply(cfg_path, d):
    content = Path(cfg_path).read_text(encoding="utf-8")
    # Numeric optimizer params (from CSV best row)
    fields = {
        "ema_fast": int(float(d["ema_fast"])),
        "ema_slow": int(float(d["ema_slow"])),
        "sl_pct": float(d["sl_pct"]),
        "tp1_pct": float(d["tp1_pct"]),
        "tp2_pct": float(d["tp2_pct"]),
        "volume_multiplier": float(d["volume_multiplier"]),
        "tp1_close_pct": int(float(d["tp1_close_pct"])),
        "risk_pct": 3.0,
    }
    for key, val in fields.items():
        if key in ("ema_fast", "ema_slow", "tp1_close_pct"):
            content = re.sub(rf"{key}: \d+", f"{key}: {val}", content)
        else:
            content = re.sub(rf"{key}: [\d.]+", f"{key}: {val}", content)
    # Keep margin_pct=10 and fixed_* disabled
    content = re.sub(r"margin_pct: [\d.]+", "margin_pct: 10", content)
    content = re.sub(r"fixed_notional_usd: [\d.]+", "fixed_notional_usd: 0", content)
    content = re.sub(r"fixed_qty: [\d.]+", "fixed_qty: 0.0", content)
    content = re.sub(r"fixed_risk_usd: [\d.]+", "fixed_risk_usd: 0", content)
    Path(cfg_path).write_text(content, encoding="utf-8")
    return fields

applied = []
missing = []
for pair, short in pairs.items():
    cfg_path = f"config_{short}.yaml"
    if not Path(cfg_path).exists():
        missing.append(pair)
        continue
    d = best_row_for(pair)
    if not d:
        missing.append(pair)
        continue
    f = apply(cfg_path, d)
    applied.append((pair, f["ema_fast"], f["ema_slow"], f["sl_pct"]))
    print(f"APPLIED {pair}: ema={f['ema_fast']}/{f['ema_slow']} sl={f['sl_pct']} tp1={f['tp1_pct']} tp2={f['tp2_pct']} vol={f['volume_multiplier']} tp1cl={f['tp1_close_pct']} risk={f['risk_pct']}")

print()
print(f"Applied: {len(applied)}/{len(pairs)}")
if missing:
    print("MISSING (no fold CSV):", missing)
else:
    print("All pairs applied.")
