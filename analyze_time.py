#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разбор логов testnet по времени: баны, отказы, reverse, ошибки."""
import os, re, glob, collections, datetime, sys

LOGDIR = "/opt/trading-bot/bot/logs/testnet"
TS = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})")

now = datetime.datetime.now()
today = now.strftime("%Y-%m-%d")

files = sorted(glob.glob(os.path.join(LOGDIR, "api_*_stdout.log")))
print("files:", len(files))

# метрики по дням
per_day = collections.defaultdict(lambda: collections.Counter())
hour_bucket = collections.defaultdict(collections.Counter)  # (date,hour) -> counter

patterns = {
    "ban_1003": re.compile(r"-1003"),
    "ban_429": re.compile(r"code -429|code=-429|-429\)"),
    "err_418": re.compile(r"418"),
    "reject_maxpos": re.compile(r"reason=max_positions"),
    "reject_other": re.compile(r"reason=(loss_streak|daily_loss_limit|check_error)"),
    "reverse_open": re.compile(r"REVERSE.*(open|открыт)|\[REVERSE\].*(open|entry)", re.I),
    "chain_stop": re.compile(r"REVERSE_CHAIN_STOP|chain limit"),
    "force_close": re.compile(r"force.close|chain_failed"),
    "notional_4164": re.compile(r"-4164"),
    "unknown_order_2011": re.compile(r"-2011"),
    "reduceonly_2022": re.compile(r"-2022"),
    "would_trigger_2021": re.compile(r"-2021"),
    "traceback": re.compile(r"Traceback"),
    "ws_error": re.compile(r"\[WS\].*(error|disconnect|closed)", re.I),
    "size_by": re.compile(r"Size by position_size_pct"),
    "rate_demand": re.compile(r"\[RATE_LIMIT\] demand"),
}

last_ts = None
for fp in files:
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = TS.match(line)
                if m:
                    day = m.group(1)
                    hour = m.group(2)
                    for name, rx in patterns.items():
                        if rx.search(line):
                            per_day[day][name] += 1
                            hour_bucket[(day, hour)][name] += 1
    except Exception as e:
        print("ERR", fp, e, file=sys.stderr)

days = sorted(per_day)
print("\n=== По дням (только дни с активностью) ===")
cols = ["ban_1003","ban_429","err_418","reject_maxpos","reject_other","reverse_open","chain_stop","force_close","notional_4164","unknown_order_2011","reduceonly_2022","would_trigger_2021","traceback","ws_error","size_by"]
print("date        " + " ".join(f"{c[:11]:>11}" for c in cols))
for d in days[-20:]:
    c = per_day[d]
    print(f"{d}  " + " ".join(f"{c[x]:>11}" for x in cols))

print("\n=== Последние 12 часов по часам ===")
keys = sorted(hour_bucket)[-12:]
for k in keys:
    c = hour_bucket[k]
    line = f"{k[0]} {k[1]}h: "
    parts = [f"{n}={c[n]}" for n in ["ban_1003","ban_429","err_418","reject_maxpos","reverse_open","chain_stop","force_close","size_by","unknown_order_2011"] if c[n]]
    print(line + (" ".join(parts) if parts else "-"))
