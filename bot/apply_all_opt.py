import glob
import re
import os

pair_map = {
    "ATOMUSDT": "atom", "DOGEUSDT": "doge", "ETHUSDT": "eth",
    "SOLUSDT": "sol", "INJUSDT": "inj", "OPUSDT": "op",
    "POLUSDT": "pol", "ONTUSDT": "ont", "HBARUSDT": "hbar",
    "NEARUSDT": "near", "SUIUSDT": "sui", "FILUSDT": "fil",
    "KASUSDT": "kas", "XRPUSDT": "xrp", "LINKUSDT": "link",
    "DOTUSDT": "dot", "TRXUSDT": "trx", "BTCUSDT": "btc",
    "BNBUSDT": "bnb", "AVAXUSDT": "avax", "ADAUSDT": "ada",
    "1000PEPEUSDT": "1000pepe", "ARBUSDT": "arb", "APTUSDT": "apt"
}

pairs = list(pair_map.keys())
results = {}
for pair in pairs:
    files = sorted(
        glob.glob("logs/optimization_{}_*.csv".format(pair)),
        key=os.path.getmtime,
        reverse=True
    )[:1]
    if files:
        with open(files[0]) as f:
            lines = f.readlines()
        best = lines[1].strip().split(",")
        results[pair] = {
            "ema_fast": best[8],
            "ema_slow": best[9],
            "sl_pct": best[10],
            "tp1_pct": best[11],
            "tp2_pct": best[12],
            "vol_mult": best[13],
            "tp1cl": best[14],
            "risk": best[15],
            "htf_f": best[16],
            "htf_s": best[17],
        }

for pair in pairs:
    cfg_path = "config_{}.yaml".format(pair_map[pair])
    if pair not in results or not os.path.exists(cfg_path):
        continue
    with open(cfg_path) as f:
        content = f.read()
    p = results[pair]
    content = re.sub(r"ema_fast: \d+", "ema_fast: {}".format(p["ema_fast"]), content)
    content = re.sub(r"ema_slow: \d+", "ema_slow: {}".format(p["ema_slow"]), content)
    content = re.sub(r"sl_pct: [\d.]+", "sl_pct: {}".format(p["sl_pct"]), content)
    content = re.sub(r"tp1_pct: [\d.]+", "tp1_pct: {}".format(p["tp1_pct"]), content)
    content = re.sub(r"tp2_pct: [\d.]+", "tp2_pct: {}".format(p["tp2_pct"]), content)
    content = re.sub(r"volume_multiplier: [\d.]+", "volume_multiplier: {}".format(p["vol_mult"]), content)
    content = re.sub(r"tp1_close_pct: \d+", "tp1_close_pct: {}".format(p["tp1cl"]), content)
    content = re.sub(r"risk_pct: [\d.]+", "risk_pct: {}".format(p["risk"]), content)
    if p["htf_f"] not in ("", "None"):
        content = re.sub(r"htf_ema_fast: \d+", "htf_ema_fast: {}".format(p["htf_f"]), content)
        content = re.sub(r"htf_ema_slow: \d+", "htf_ema_slow: {}".format(p["htf_s"]), content)
    with open(cfg_path, "w") as f:
        f.write(content)
    print("Updated", pair)
