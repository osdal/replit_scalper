#!/usr/bin/env python3
"""Опрос Binance apiTradingStatus (без symbol) с записью в CSV.

Запускать раз в 10 минут. Пропуск при used_weight >= 1000 или активном бане.
Вес одного запроса — 10. Пустой ответ пишется одной строкой `none`.
"""
import csv
import glob
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_GLOB = os.path.join(ROOT, "bot", "data", "binance_gate_live.json.*")
CSV_PATH = os.path.join(ROOT, "data", "quant_status_live.csv")
WEIGHT_SKIP_AT = 1000
HEADER = ["time", "symbol", "indicator", "value", "triggerValue", "isLocked", "plannedRecoverTime"]


def gate_blocked() -> bool:
    """True если общий вес >=1000 или активен бан по gate-файлам."""
    now = time.time()
    for p in glob.glob(GATE_GLOB):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if now - float(d.get("ts", 0)) > 30:
            continue
        if int(d.get("used_weight_1m", 0) or 0) >= WEIGHT_SKIP_AT:
            return True
        if float(d.get("ban_until_ms", 0) or 0) > now * 1000:
            return True
    return False


def signed_get(path: str) -> dict:
    env = {}
    for line in open(os.path.join(ROOT, ".env.live"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(env["BINANCE_API_SECRET"].encode(), qs.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"https://fapi.binance.com{path}?{qs}&signature={sig}",
        headers={"X-MBX-APIKEY": env["BINANCE_API_KEY"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def build_rows(data: dict, now: datetime) -> list:
    """Строки CSV из ответа apiTradingStatus; пустой ответ -> строка none."""
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    symbols = data.get("symbols") or []
    top = data.get("indicators") or {}
    for sym in symbols:
        name = sym.get("symbol") or "none"
        locked = sym.get("isLocked")
        recover = sym.get("plannedRecoverTime")
        inds = sym.get("indicators") or []
        if not inds:
            rows.append(dict(time=stamp, symbol=name, indicator="none", value="",
                             triggerValue="", isLocked=locked, plannedRecoverTime=recover))
        for ind in inds:
            rows.append(dict(
                time=stamp, symbol=name,
                indicator=ind.get("indicator", ""),
                value=ind.get("value", ""),
                triggerValue=ind.get("triggerValue", ""),
                isLocked=locked, plannedRecoverTime=recover,
            ))
    if not rows and top:
        for name, value in (top.items() if isinstance(top, dict) else []):
            rows.append(dict(time=stamp, symbol="ACCOUNT", indicator=name,
                             value=value, triggerValue="", isLocked=data.get("accountUnlocked"),
                             plannedRecoverTime=data.get("lockStartTime")))
    if not rows:
        rows.append(dict(time=stamp, symbol="none", indicator="none", value="",
                         triggerValue="", isLocked="", plannedRecoverTime=""))
    return rows


def append_rows(rows: list, path: str = CSV_PATH) -> None:
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    if gate_blocked():
        print("skip: gate>=1000 или бан")
        return 0
    data = signed_get("/fapi/v1/apiTradingStatus")
    rows = build_rows(data, datetime.now(timezone.utc))
    append_rows(rows)
    print(f"weight=10 rows={len(rows)} csv={CSV_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
