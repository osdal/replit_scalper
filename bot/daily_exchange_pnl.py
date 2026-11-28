#!/usr/bin/env python3
"""Ежедневный PnL с Binance (income) с upsert в CSV.

Зачем: `trades.pnl` — уровень цикла и не совпадает с биржевыми филлами по
датам. Здесь источник истины — `/fapi/v1/income`; TRANSFER хранится отдельной
колонкой и НЕ входит в `net` (торговый результат).

Запуск:
    python3 bot/daily_exchange_pnl.py --cache /tmp/income.json --dry-run
    python3 bot/daily_exchange_pnl.py                # живой запрос + upsert CSV

Бюджет: не более 3 страниц income по 1000 строк (≤90 weight по бюджету
reconcile_pnl.py); запросы пропускаются при used_weight ≥ 1000 или бане.
"""
import argparse
import csv
import glob
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GATE_GLOB = os.path.join(HERE, "data", "binance_gate_live.json.*")
CSV_PATH = os.path.join(ROOT, "data", "daily_exchange_pnl.csv")
WEIGHT_PER_PAGE = 30
MAX_PAGES = 3
WEIGHT_SKIP_AT = 1000
HEADER = ["date", "realized", "commission", "funding", "transfer", "net", "wallet"]
BALANCE_TYPES = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "TRANSFER"}


def gate_blocked() -> bool:
    """True если общий вес ≥1000 или активен бан."""
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


def fetch_income(client, start_ms: int, end_ms: int, verbose: bool = True,
                 max_pages: int = MAX_PAGES) -> tuple:
    """Вернуть (строки, вес, страницы). Один поток без incomeType, пагинация по времени."""
    rows = []
    cursor = start_ms
    pages = 0
    weight = 0
    while cursor <= end_ms and pages < max_pages:
        if gate_blocked():
            if verbose:
                print("skip: gate weight>=1000 или бан")
            break
        if pages:
            time.sleep(1)
        try:
            page = client.futures_income_history(
                startTime=cursor, endTime=end_ms, limit=1000,
            ) or []
        except Exception as e:
            if verbose:
                print(f"error: income: {e!r}")
            break
        pages += 1
        weight += WEIGHT_PER_PAGE
        rows.extend(page)
        if len(page) < 1000:
            break
        last = max(int(x.get("time") or 0) for x in page)
        if last <= cursor:
            break
        cursor = last + 1
    return rows, weight, pages


def aggregate(rows, wallet: float, today: str = None):
    """wallet пишем только в строку сегодняшнего дня (баланс на сейчас)."""
    today = today or datetime.now(KYIV).date().isoformat()
    buckets = {}
    for x in rows:
        typ = x.get("incomeType")
        if typ not in BALANCE_TYPES:
            continue
        d = datetime.fromtimestamp(int(x["time"]) / 1000, KYIV).date().isoformat()
        b = buckets.setdefault(d, dict(realized=0.0, commission=0.0, funding=0.0, transfer=0.0))
        v = float(x.get("income") or 0)
        if typ == "REALIZED_PNL":
            b["realized"] += v
        elif typ == "COMMISSION":
            b["commission"] += v
        elif typ == "FUNDING_FEE":
            b["funding"] += v
        else:
            b["transfer"] += v
    out = []
    for d in sorted(buckets):
        b = buckets[d]
        net = b["realized"] + b["commission"] + b["funding"]
        out.append({
            "date": d,
            "realized": round(b["realized"], 6),
            "commission": round(b["commission"], 6),
            "funding": round(b["funding"], 6),
            "transfer": round(b["transfer"], 6),
            "net": round(net, 6),
            "wallet": round(wallet, 4) if d == today else "",
        })
    return out


def upsert(path: str, rows: list) -> None:
    by_date = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                by_date[r["date"]] = r
    for r in rows:
        by_date[r["date"]] = {k: str(r[k]) for k in HEADER}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        w.writeheader()
        for d in sorted(by_date):
            w.writerow(by_date[d])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", help="JSON со строками income (dry-run без Binance)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=17, help="глубина, дней")
    ap.add_argument("--wallet", type=float, default=0.0)
    ap.add_argument("--budget", type=int, default=90,
                    help="бюджет weight на прогон; 30 weight = 1 страница income")
    ap.add_argument("--csv", default=CSV_PATH)
    args = ap.parse_args()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    if args.cache:
        rows = json.load(open(args.cache))
        weight = pages = 0
        rows = [x for x in rows if start.timestamp()*1000 <= int(x["time"]) <= end.timestamp()*1000]
    else:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, f".env.{os.getenv('BOT_ENV', 'live')}"), override=True)
        from binance.client import Client
        client = Client(api_key=os.getenv("BINANCE_API_KEY"), api_secret=os.getenv("BINANCE_API_SECRET"))
        max_pages = max(1, args.budget // WEIGHT_PER_PAGE)
        rows, weight, pages = fetch_income(
            client, int(start.timestamp()*1000), int(end.timestamp()*1000),
            max_pages=max_pages,
        )
    out = aggregate(rows, args.wallet)
    print(f"rows={len(rows)} pages={pages} weight={weight} days={len(out)} "
          f"wallet={args.wallet} mode={'DRY' if args.dry_run else 'WRITE'}")
    print("date | realized | commission | funding | transfer | net | wallet")
    for r in out:
        print(f"{r['date']} | {r['realized']:+.4f} | {r['commission']:+.4f} | "
              f"{r['funding']:+.5f} | {r['transfer']:+.4f} | {r['net']:+.4f} | {r['wallet']}")
    if not args.dry_run:
        upsert(args.csv, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
