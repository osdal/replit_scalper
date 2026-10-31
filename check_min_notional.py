import json, urllib.request, glob, os, re

# mainnet public endpoint
url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
data = json.load(urllib.request.urlopen(url, timeout=30))
info = {s["symbol"]: s for s in data["symbols"]}

syms = set()
for f in glob.glob("/opt/trading-bot/bot/configs/live/config_*.yaml"):
    m = re.search(r"symbol:\s*([A-Z0-9]+)", open(f, encoding="utf-8").read())
    if m:
        syms.add(m.group(1))

rows = []
for s in sorted(syms):
    si = info.get(s)
    if not si:
        rows.append((s, "NOT_FOUND", 0, 0))
        continue
    min_notional = 0.0
    min_qty = 0.0
    for f in si["filters"]:
        if f["filterType"] == "MIN_NOTIONAL":
            min_notional = float(f.get("notional", 0) or 0)
        elif f["filterType"] == "LOT_SIZE":
            min_qty = float(f.get("minQty", 0) or 0)
    rows.append((s, si["status"], min_notional, min_qty))

print("symbols:", len(rows))
print(f"{'symbol':14}{'status':10}{'minNotional':>12}{'minQty':>14}")
for s, st, mn, mq in sorted(rows, key=lambda r: -r[2]):
    print(f"{s:14}{st:10}{mn:>12.2f}{mq:>14}")
