"""Анализ распределения результатов сделок (не только винрейт).

Печатает: винрейт, распределение PnL (min/p5/median/p95/max), гистограмму,
худшие/лучшие циклы, долю комиссий (reverse vs обычные), max drawdown по
кумулятивной кривой, разбивку по exit_reason и по символам.

Использование:
    python cycle_stats.py [--db data/bot.db] [--symbol DOTUSDT]
"""
import argparse
import sqlite3
import statistics
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = r"C:\DATA\bots\replit_scalper"


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=ROOT + r"\data\bot.db")
    ap.add_argument("--symbol", default=None)
    return ap.parse_args()


def pct(vals, q):
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = (len(vals) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def main():
    args = parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cur = con.cursor()
    where = "is_open=0 AND pnl IS NOT NULL AND status != 'rejected'"
    params = ()
    if args.symbol:
        where += " AND symbol=?"
        params = (args.symbol.upper(),)
    rows = cur.execute(
        f"SELECT symbol, pnl, commission, exit_reason, entry_time, exit_time "
        f"FROM trades WHERE {where} ORDER BY exit_time", params
    ).fetchall()

    print(f"DB: {args.db}" + (f"  symbol={args.symbol}" if args.symbol else ""))
    if not rows:
        print("нет закрытых сделок")
        return

    pnls = [float(r[1]) for r in rows]
    comm = [float(r[2] or 0.0) for r in rows]
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    gross = sum(pnls) + sum(comm)

    print(f"\nсделок={n}  винрейт={wins}/{n} ({100*wins/n:.1f}%)")
    print(f"net={sum(pnls):+.4f}  gross={gross:+.4f}  комиссии={sum(comm):.4f}")
    print(f"pnl: min={min(pnls):+.4f} p5={pct(pnls,0.05):+.4f} median={statistics.median(pnls):+.4f} "
          f"p95={pct(pnls,0.95):+.4f} max={max(pnls):+.4f} mean={statistics.mean(pnls):+.4f}")

    print("\nраспределение pnl (гистограмма):")
    buckets = [(-1e9, -5), (-5, -2), (-2, -1), (-1, -0.5), (-0.5, -0.1), (-0.1, 0),
               (0, 0.1), (0.1, 0.5), (0.5, 1), (1, 2), (2, 5), (5, 1e9)]
    for lo, hi in buckets:
        c = sum(1 for p in pnls if lo <= p < hi)
        if c:
            bar = "#" * min(60, c)
            print(f"  [{lo:>7.2f}, {hi:>7.2f}) {c:>4} {bar}")

    print("\nхудшие 10 циклов:")
    for r in sorted(rows, key=lambda r: r[1])[:10]:
        print(f"  {r[0]:12} {r[1]:+9.4f}  {r[3]}  {r[5]}")

    print("\nлучшие 10 циклов:")
    for r in sorted(rows, key=lambda r: r[1], reverse=True)[:10]:
        print(f"  {r[0]:12} {r[1]:+9.4f}  {r[3]}  {r[5]}")

    print("\nпо exit_reason:")
    agg = {}
    for r in rows:
        a = agg.setdefault(r[3] or "(none)", [0, 0.0, 0.0, 0])
        a[0] += 1
        a[1] += float(r[1])
        a[2] += float(r[2] or 0.0)
        a[3] += 1 if r[1] > 0 else 0
    for reason, (c, s, cm, w) in sorted(agg.items(), key=lambda kv: kv[1][1]):
        print(f"  {reason:20} n={c:<4} sum={s:+10.4f} win={w}/{c} comm={cm:.4f}")

    rev_comm = sum(float(r[2] or 0.0) for r in rows if (r[3] or "").startswith("REVERSE"))
    print(f"\nкомиссии: reverse={rev_comm:.4f} ({100*rev_comm/max(sum(comm),1e-9):.1f}%)  "
          f"обычные={sum(comm)-rev_comm:.4f}")

    # Кумулятивная кривая и max drawdown
    cum = 0.0
    peak = 0.0
    maxdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        maxdd = max(maxdd, peak - cum)
    print(f"итог по кривой={cum:+.4f}  max drawdown={maxdd:.4f}")

    print("\nпо символам (сортировка по net):")
    per = {}
    for r in rows:
        a = per.setdefault(r[0], [0, 0.0, 0])
        a[0] += 1
        a[1] += float(r[1])
        a[2] += 1 if r[1] > 0 else 0
    for sym, (c, s, w) in sorted(per.items(), key=lambda kv: kv[1][1]):
        print(f"  {sym:12} n={c:<4} net={s:+10.4f} win={w}/{c} ({100*w/c:.0f}%)")
    con.close()


if __name__ == "__main__":
    main()
