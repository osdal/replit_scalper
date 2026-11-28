"""chain_sim: стохастический бэктест механики reverse-цепочки на 5m свечах.

Отдельно от backtester.py: входы случайные (без сигналов/слотов), но механика
цикла взята из live-кода (_reverse_sizing, кап/флор, BE, cum-cap, expired).
Funding и проскальзывание не моделируются. Inside-bar: mode A = SL первым,
mode B = TP первым.
"""
import argparse
import csv
import io
import json
import math
import os
import random
import re
import statistics
import time
import zipfile
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CACHE = os.path.join(ROOT, "data", "klines_cache")
BASE_URL = "https://data.binance.vision/data/futures/um/monthly/klines"

DEFAULTS = dict(tp1_pct=1.0, sl_pct=0.5, reverse_breakeven_pct=1.5,
                reverse_fee_buffer_pct=0.1, reverse_profit_pct=0.5,
                reverse_sl_pct=0.5, taker_fee_pct=0.05, maker_fee_pct=0.02,
                expired_after_minutes=120, max_position_notional_usd=50.0,
                reverse_chain_max=7, tp1_close_pct=100.0,
                position_size_pct=0.25, leverage=75.0)
MIN_NOTIONAL = 5.0
WALLET = 58.5
CUM_CAP_PCT = 30.0
EXPIRED_BPS = 50.0
EXPIRED_MIN = 0.04
EXPIRED_MAX = 0.10
CYCLES_PER_DAY = 2.7
SHORT_PROB = 0.55


def load_cfg(symbol):
    cfg = dict(DEFAULTS)
    p = os.path.join(ROOT, "bot", "configs", "live", f"config_{symbol.lower()}.yaml")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            m = re.match(r"^([a-z_0-9]+):\s*(\S+)", line)
            if m and m.group(1) in cfg:
                try: cfg[m.group(1)] = float(m.group(2))
                except ValueError: pass
    return cfg


def fetch_day(symbol, day, cache_dir):
    """Скачать дневной zip (для текущего месяца, когда месячный ещё не выложен)."""
    name = f"{symbol}-5m-{day}.zip"
    os.makedirs(cache_dir, exist_ok=True)
    zpath = os.path.join(cache_dir, name)
    if not os.path.exists(zpath):
        url = f"{BASE_URL.replace('/monthly/', '/daily/')}/{symbol}/5m/{name}"
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []  # ещё не выложенный день
            raise
        with open(zpath, "wb") as fh:
            fh.write(data)
    with zipfile.ZipFile(zpath) as z:
        inner = [n for n in z.namelist() if n.endswith(".csv")][0]
        return list(csv.reader(io.StringIO(z.read(inner).decode("utf-8"))))


def fetch_month(symbol, month, cache_dir):
    """Скачать месячный zip с data.binance.vision, вернуть список строк CSV."""
    name = f"{symbol}-5m-{month}.zip"
    os.makedirs(cache_dir, exist_ok=True)
    zpath = os.path.join(cache_dir, name)
    if not os.path.exists(zpath):
        url = f"{BASE_URL}/{symbol}/5m/{name}"
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        with open(zpath, "wb") as fh:
            fh.write(data)
    with zipfile.ZipFile(zpath) as z:
        inner = [n for n in z.namelist() if n.endswith(".csv")][0]
        text = z.read(inner).decode("utf-8")
    return list(csv.reader(io.StringIO(text)))


def load_candles(symbol, start, end, cache_dir=DEFAULT_CACHE):
    out = []
    months = sorted({f"{d.year:04d}-{d.month:02d}" for d in (start, end)})
    # если период через границу месяца — добавить все месяцы между
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        mm = f"{y:04d}-{m:02d}"
        if mm not in months: months.append(mm)
        m += 1
        if m == 13: y, m = y + 1, 1
    for mm in months:
        try:
            arch = fetch_month(symbol, mm, cache_dir)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            arch = []
            y, m = int(mm[:4]), int(mm[5:])
            month_start = date(y, m, 1)
            month_end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
            d = max(month_start, start.date())
            last = min(month_end, end.date())
            while d <= last:
                arch.extend(fetch_day(symbol, d.isoformat(), cache_dir))
                d += timedelta(days=1)
        for row in arch:
            if len(row) < 6: continue
            try: ts = int(row[0])
            except ValueError: continue
            t = datetime.fromtimestamp(ts / 1000, timezone.utc)
            if start <= t <= end:
                out.append((t, float(row[2]), float(row[3]), float(row[4]), float(row[1])))
    out.sort(key=lambda x: x[0])
    return out


@dataclass
class Leg:
    depth: int
    direction: str
    entry: float
    qty: float
    tp: float
    sl: float
    entry_i: int


@dataclass
class Cycle:
    symbol: str
    entry_i: int
    exit_i: int
    depth: int
    net: float
    reason: str
    tp1_first: bool
    duration_min: float
    legs: list = field(default_factory=list)


def reverse_size(P, N, nr, U_eff, new_dir, cfg):
    """Порт _reverse_sizing (order_manager.py:1215-1266) + floor/cap."""
    w = (cfg["reverse_breakeven_pct"] + cfg["reverse_fee_buffer_pct"]) / 100.0
    T = P * (1 + w) if new_dir == "LONG" else P * (1 - w)
    f_in = cfg["taker_fee_pct"] / 100.0
    f_out = f_in
    p = cfg["reverse_profit_pct"] / 100.0
    denom = abs(T - P) - f_in * P - (f_out + p) * T
    if P <= 0 or denom <= 0: return None, T
    s_abs = (-(nr + U_eff) + f_in * abs(N) * P) / denom
    if s_abs <= 0: return None, T
    S = s_abs if new_dir == "LONG" else -s_abs
    q = S - N
    # floor minNotional
    if q * P < MIN_NOTIONAL:
        q = MIN_NOTIONAL / P
        S = N + q if N != 0 else q
    cap = cfg["max_position_notional_usd"]
    if cap > 0 and abs(q) * P > cap:
        q = cap / P
    return (abs(q), T) if q > 0 else (None, T)


def simulate_cycle(symbol, candles, start_i, direction, cfg, seed, mode):
    """Один цикл; возвращает Cycle или None."""
    rng = random.Random(seed)
    e = candles[start_i][1]  # open входной свечи
    sign = 1 if direction == "LONG" else -1
    margin = WALLET * cfg["position_size_pct"] / 100.0
    qty0 = (margin * cfg["leverage"]) / e
    first = Leg(1, direction, e, qty0, e*(1+sign*cfg["tp1_pct"]/100),
                e*(1-sign*cfg["sl_pct"]/100), start_i)
    legs = [first]
    net = 0.0
    i = start_i
    tp1_first = False
    reason = ""
    # commission на вход
    net -= first.qty * e * cfg["taker_fee_pct"] / 100
    depth = 1
    while i < len(candles) - 1:
        i += 1
        t, o, h, l, cl = candles[i]
        leg = legs[-1]
        sg = 1 if leg.direction == "LONG" else -1
        tp_hit = (h >= leg.tp) if sg > 0 else (l <= leg.tp)
        sl_hit = (l <= leg.sl) if sg > 0 else (h >= leg.sl)
        if tp_hit and sl_hit:
            tp_hit, sl_hit = ((False, True) if mode == "A" else (True, False))
        if tp_hit:
            net += leg.qty * leg.tp * cfg["maker_fee_pct"] / 100
            net += leg.qty * abs(leg.tp - leg.entry) * sg
            reason = "TP1" if depth == 1 else "REVERSE_EXPIRED_PROFIT"
            break
        if sl_hit:
            px = leg.sl
            net += leg.qty * px * cfg["taker_fee_pct"] / 100
            net += leg.qty * abs(px - leg.entry) * (-sg)
            if depth == 1:
                reason = "SL"
                break
            reason = "REVERSE_BE"
            break
        # proтухшие / cum-cap / лимит глубины — упрощённо проверяем на каждой свече
        if (t - candles[leg.entry_i][0]).total_seconds() / 60 >= cfg["expired_after_minutes"]:
            addon = min(max(abs(leg.qty*leg.entry)*EXPIRED_BPS/10000, EXPIRED_MIN), EXPIRED_MAX)
            fee = leg.qty * cl * cfg["taker_fee_pct"] / 100
            realized = net + leg.qty * (cl - leg.entry) * sg - fee
            if realized >= fee + addon:
                net = realized
                reason = "EXPIRED_CLOSE"
                break
        if net <= -WALLET * CUM_CAP_PCT / 100:
            reason = "REVERSE_CUM_LOSS_CAP"
            break
        if depth >= int(cfg["reverse_chain_max"]):
            reason = "REVERSE_CHAIN_STOP"
            break
        # SL первой ноги -> разворот
        if depth == 1 and False:  # разворот открывается только на SL (обработано выше)
            pass
    # если вышли по SL первой ноги — открываем разворот и продолжаем
    if reason == "SL":
        reason = ""
        while depth < int(cfg["reverse_chain_max"]) and i < len(candles) - 1:
            i += 1
            t, o, h, l, cl = candles[i]
            prev = legs[-1]
            P = o
            N = prev.qty * (1 if prev.direction == "LONG" else -1)
            nr = net
            U_eff = N * (P - prev.entry)
            new_dir = "SHORT" if N > 0 else "LONG"
            q, T = reverse_size(P, N, nr, U_eff, new_dir, cfg)
            if not q or q <= 0:
                reason = "REVERSE_BE"
                break
            sg2 = 1 if new_dir == "LONG" else -1
            leg = Leg(depth+1, new_dir, P, q, T, P*(1-sg2*cfg["reverse_sl_pct"]/100), i)
            legs.append(leg); depth += 1
            net -= q * P * cfg["taker_fee_pct"] / 100
            while i < len(candles) - 1:
                i += 1
                _, o2, h2, l2, cl2 = candles[i]
                tp_hit = (h2 >= leg.tp) if sg2 > 0 else (l2 <= leg.tp)
                sl_hit = (l2 <= leg.sl) if sg2 > 0 else (h2 >= leg.sl)
                if tp_hit and sl_hit:
                    tp_hit, sl_hit = ((False, True) if mode == "A" else (True, False))
                if tp_hit:
                    net += leg.qty * leg.tp * cfg["maker_fee_pct"] / 100
                    net += leg.qty * abs(leg.tp - leg.entry) * sg2
                    # Как в live: TP ноги при отрицательном net цикла не завершает
                    # цепочку — открываем следующую ногу (main.py:2829).
                    reason = "" if net < 0 else "REVERSE_EXPIRED_PROFIT"
                    break
                if sl_hit:
                    net += leg.qty * leg.sl * cfg["taker_fee_pct"] / 100
                    net += leg.qty * abs(leg.sl - leg.entry) * (-sg2)
                    reason = "REVERSE_BE"
                    break
            if reason in ("REVERSE_EXPIRED_PROFIT", "REVERSE_BE"):
                break
            if net <= -WALLET * CUM_CAP_PCT / 100:
                reason = "REVERSE_CUM_LOSS_CAP"; break
            # next reverse iteration
    if not reason:
        reason = "REVERSE_CHAIN_STOP"
    tp1_first = (reason == "TP1")
    return Cycle(symbol, start_i, i, depth, net, reason, tp1_first,
                 (candles[i][0] - candles[start_i][0]).total_seconds()/60, legs)


def run(symbols, start, end, seeds=(1,2,3), mode="A", cache_dir=DEFAULT_CACHE):
    results = {}
    for seed in seeds:
        cycles=[]
        for sym in symbols:
            cfg = load_cfg(sym)
            candles = load_candles(sym, start, end, cache_dir)
            if len(candles) < 100:
                continue
            rng = random.Random(seed + abs(hash(sym)) % 100000)
            days = (end-start).days
            n = max(1, int(days * CYCLES_PER_DAY))
            idxs = sorted(rng.sample(range(5, len(candles)-50), min(n, len(candles)-60)))
            for ix in idxs:
                direction = "SHORT" if rng.random() < SHORT_PROB else "LONG"
                c = simulate_cycle(sym, candles, ix, direction, cfg, seed+ix, mode)
                if c: cycles.append(c)
        results[seed]=cycles
    return results


def metrics(cycles):
    n=len(cycles)
    if not n: return {}
    depths=[0]*8
    for c in cycles: depths[min(c.depth,7)]+=1
    losses=[c.net for c in cycles if c.net<0]
    return dict(n=n, tp1_first=round(sum(1 for c in cycles if c.reason=="TP1")/n*100,2),
                depth=[round(d/n*100,1) for d in depths[1:]],
                net=round(sum(c.net for c in cycles),3),
                net_pct=round(sum(c.net for c in cycles)/WALLET*100,2),
                max_loss=round(min(losses) if losses else 0,3),
                p99_loss=round(sorted(losses)[int(0.01*(len(losses)-1))] if losses else 0,3),
                avg_min=round(statistics.mean(c.duration_min for c in cycles),1))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="ZECUSDT,SOLUSDT,XRPUSDT,FETUSDT,HBARUSDT")
    ap.add_argument("--start", default="2026-11-11T00:00:00+00:00")
    ap.add_argument("--end", default="2026-11-28T23:59:59+00:00")
    ap.add_argument("--seeds", default="1,2,3")
    ap.add_argument("--mode", default="A", choices=("A", "B"))
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    a = ap.parse_args()
    start = datetime.fromisoformat(a.start)
    end = datetime.fromisoformat(a.end)
    syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    seeds = tuple(int(s) for s in a.seeds.split(",") if s.strip())
    t0 = time.time()
    res = run(syms, start, end, seeds=seeds, mode=a.mode, cache_dir=a.cache)
    print(f"mode={a.mode} elapsed_s={time.time()-t0:.1f}")
    for seed, cycles in res.items():
        print("seed", seed, metrics(cycles))


if __name__ == "__main__":
    main()
