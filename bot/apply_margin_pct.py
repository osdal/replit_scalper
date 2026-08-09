import glob
import re
import os

configs = sorted(glob.glob("config_*.yaml"))
for cfg_path in configs:
    with open(cfg_path) as f:
        content = f.read()
    content = re.sub(r"margin_pct: [\d.]+", "margin_pct: 10", content)
    content = re.sub(r"fixed_notional_usd: [\d.]+", "fixed_notional_usd: 0", content)
    content = re.sub(r"fixed_qty: [\d.]+", "fixed_qty: 0.0", content)
    content = re.sub(r"fixed_risk_usd: [\d.]+", "fixed_risk_usd: 0", content)
    content = re.sub(r"risk_pct: [\d.]+", "risk_pct: 0", content)
    with open(cfg_path, "w") as f:
        f.write(content)
    print("Updated", os.path.basename(cfg_path))
