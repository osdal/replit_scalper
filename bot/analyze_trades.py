"""
Analysis script for replit_scalper bot.
Reads trades from SQLite database and generates comprehensive statistics.
Adapted from ClawStreet bot analytics.
"""

import sys
import os
import argparse
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlite3
from preset_config import PRESET_CONFIG

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "bot.db")


def parse_timestamp(ts_str):
    if not ts_str:
        return None
    try:
        ts_str = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        return dt.replace(tzinfo=None)
    except Exception:
        return None


def load_trades(db_path=None):
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    trades = []
    for row in conn.execute("SELECT * FROM trades ORDER BY entry_time ASC").fetchall():
        trades.append(dict(row))
    conn.close()
    return trades


def analyze_trades(trades=None, excluded_presets=None, start_time=None, end_time=None):
    if trades is None:
        trades = load_trades()
    if excluded_presets is None:
        excluded_presets = set()

    if start_time is not None or end_time is not None:
        filtered = []
        for t in trades:
            ts = parse_timestamp(t.get("entry_time", ""))
            if ts is None:
                continue
            if start_time is not None and ts < start_time:
                continue
            if end_time is not None and ts > end_time:
                continue
            filtered.append(t)
        trades = filtered

    opens = [t for t in trades if t.get("status") == "open" or (t.get("is_open") == 1 and t.get("status") != "rejected")]
    closes = [t for t in trades if t.get("status") == "closed"]
    rejected = [t for t in trades if t.get("status") == "rejected"]

    total_opens = len(trades)
    total_closes = len(closes)
    total_rejected = len(rejected)

    # By symbol
    opens_by_sym = {}
    closes_by_sym = defaultdict(list)
    rejected_by_sym = defaultdict(list)
    for t in opens:
        opens_by_sym[t["symbol"]] = t
    for t in closes:
        closes_by_sym[t["symbol"]].append(t)
    for t in rejected:
        rejected_by_sym[t["symbol"]].append(t)

    # Direction stats (за всё время, открыто+закрыто+отклонено)
    long_opens = len([t for t in trades if t.get("direction") == "LONG"])
    short_opens = len([t for t in trades if t.get("direction") == "SHORT"])
    long_closes = len([t for t in closes if t.get("direction") == "LONG"])
    short_closes = len([t for t in closes if t.get("direction") == "SHORT"])

    # Preset stats
    preset_stats = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "commission": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
        "wins_list": [], "losses_list": [], "hold_times": [],
        "hold_count": 0, "hold_times_tp": [], "hold_times_sl": [],
    })

    total_commission = 0.0
    for t in closes:
        preset = t.get("preset") or "unknown"
        pnl = float(t.get("pnl") or 0.0)
        commission = float(t.get("commission") or 0.0)
        total_commission += commission
        s = preset_stats[preset]
        s["trades"] += 1
        s["pnl"] += pnl
        s["commission"] += commission
        if pnl > 0:
            s["wins"] += 1
            s["wins_list"].append(pnl)
        else:
            s["losses"] += 1
            s["losses_list"].append(pnl)
        if pnl > s["best_trade"]:
            s["best_trade"] = pnl
        if pnl < s["worst_trade"]:
            s["worst_trade"] = pnl

    for preset, s in preset_stats.items():
        s["avg_win"] = sum(s["wins_list"]) / len(s["wins_list"]) if s["wins_list"] else 0.0
        s["avg_loss"] = sum(s["losses_list"]) / len(s["losses_list"]) if s["losses_list"] else 0.0

    # Hold times — для каждой закрытой сделки: exit_time - entry_time из одной записи
    avg_hold_time = 0.0
    avg_hold_tp = 0.0
    avg_hold_sl = 0.0
    hold_tp_count = 0
    hold_sl_count = 0
    hold_time_count = 0

    for close_t in closes:
        try:
            ot = parse_timestamp(close_t.get("entry_time", ""))
            ct = parse_timestamp(close_t.get("exit_time", ""))
            if not (ot and ct):
                continue
            hold_minutes = (ct - ot).total_seconds() / 60.0
            preset = close_t.get("preset") or "unknown"
            pstats = preset_stats.get(preset)
            if pstats is not None:
                pstats["hold_times"].append(hold_minutes)
                pstats["hold_count"] += 1
                pstats["avg_hold_time"] = sum(pstats["hold_times"]) / len(pstats["hold_times"])
            avg_hold_time += hold_minutes
            hold_time_count += 1
            reason = close_t.get("exit_reason", "")
            if pstats is not None:
                if "TP" in reason:
                    pstats["hold_times_tp"].append(hold_minutes)
                elif "SL" in reason:
                    pstats["hold_times_sl"].append(hold_minutes)
            if "TP" in reason:
                avg_hold_tp += hold_minutes
                hold_tp_count += 1
            elif "SL" in reason:
                avg_hold_sl += hold_minutes
                hold_sl_count += 1
        except Exception:
            continue

    avg_hold_time = avg_hold_time / hold_time_count if hold_time_count else 0.0
    avg_hold_tp = avg_hold_tp / hold_tp_count if hold_tp_count else 0.0
    avg_hold_sl = avg_hold_sl / hold_sl_count if hold_sl_count else 0.0

    for preset, s in preset_stats.items():
        s["avg_hold_time"] = sum(s["hold_times"]) / len(s["hold_times"]) if s["hold_times"] else 0.0
        s["avg_hold_tp"] = sum(s["hold_times_tp"]) / len(s["hold_times_tp"]) if s["hold_times_tp"] else 0.0
        s["avg_hold_sl"] = sum(s["hold_times_sl"]) / len(s["hold_times_sl"]) if s["hold_times_sl"] else 0.0

    # Overall stats
    included_closes = [t for t in closes if (t.get("preset") or "unknown") not in excluded_presets]
    total_pnl = sum(float(t.get("pnl") or 0.0) for t in included_closes)
    avg_pnl_per_trade = total_pnl / len(included_closes) if included_closes else 0.0
    win_rate = (len([t for t in included_closes if float(t.get("pnl") or 0.0) > 0]) / len(included_closes) * 100) if included_closes else 0.0

    gross_profit = sum(float(t.get("pnl") or 0.0) for t in included_closes if float(t.get("pnl") or 0.0) > 0)
    gross_loss = sum(abs(float(t.get("pnl") or 0.0)) for t in included_closes if float(t.get("pnl") or 0.0) < 0)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Consecutive wins/losses
    max_consecutive_losses = 0
    current_consecutive = 0
    max_consecutive_wins = 0
    consecutive_wins = 0
    for t in sorted(included_closes, key=lambda x: x.get("entry_time", "")):
        pnl = float(t.get("pnl") or 0.0)
        if pnl < 0:
            current_consecutive += 1
            max_consecutive_losses = max(max_consecutive_losses, current_consecutive)
            consecutive_wins = 0
        elif pnl > 0:
            consecutive_wins += 1
            max_consecutive_wins = max(max_consecutive_wins, consecutive_wins)
            current_consecutive = 0
        else:
            current_consecutive = 0
            consecutive_wins = 0

    loss_streaks = []
    current_streak = None
    sorted_for_streaks = sorted(included_closes, key=lambda x: x.get("entry_time", ""))
    for t in sorted_for_streaks:
        pnl = float(t.get("pnl") or 0.0)
        ts = parse_timestamp(t.get("entry_time", ""))
        if pnl < 0:
            if current_streak is None:
                current_streak = {
                    "start": ts,
                    "end": ts,
                    "count": 1,
                    "pnl": pnl,
                    "symbols": {t.get("symbol", "?")},
                    "presets": {t.get("preset") or "unknown"},
                    "start_hour": ts.hour if ts else None,
                }
            else:
                current_streak["count"] += 1
                current_streak["pnl"] += pnl
                current_streak["end"] = ts
                current_streak["symbols"].add(t.get("symbol", "?"))
                current_streak["presets"].add(t.get("preset") or "unknown")
        else:
            if current_streak is not None:
                loss_streaks.append(current_streak)
                current_streak = None
    if current_streak is not None:
        loss_streaks.append(current_streak)

    streak_lengths = defaultdict(int)
    streak_pnl = defaultdict(float)
    streak_coins = defaultdict(int)
    streak_presets = defaultdict(int)
    streak_hours = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for s in loss_streaks:
        streak_lengths[s["count"]] += 1
        streak_pnl[s["count"]] += s["pnl"]
        for sym in s["symbols"]:
            streak_coins[sym] += 1
        for pr in s["presets"]:
            streak_presets[pr] += 1
        h = s.get("start_hour")
        if h is not None:
            streak_hours[h]["count"] += 1
            streak_hours[h]["pnl"] += s["pnl"]

    # Drawdown
    equity_curve = []
    cumulative = 0.0
    for t in sorted(included_closes, key=lambda x: x.get("entry_time", "")):
        cumulative += float(t.get("pnl") or 0.0)
        equity_curve.append(cumulative)
    max_drawdown = 0.0
    peak = equity_curve[0] if equity_curve else 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
        drawdown = peak - value
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    # LLM stats (if present in reject_reason)
    llm_rejected = [t for t in rejected if t.get("reject_reason") == "llm_reject"]
    llm_rejected_by_preset = defaultdict(int)
    for t in llm_rejected:
        preset = t.get("preset") or "unknown"
        llm_rejected_by_preset[preset] += 1

    # Signal funnel: opened vs risk-rejected vs llm-rejected vs throttled skips
    risk_rejected = [t for t in rejected if (t.get("reject_reason") or "").startswith("risk:")]
    skipped_signals = [t for t in rejected if (t.get("reject_reason") or "").startswith("skip:")]
    signal_funnel = {
        "opened": len(trades) - len(rejected),  # все открытые сделки (open + closed) = не отклонённые
        "risk_rejected": len(risk_rejected),
        "llm_rejected": len(llm_rejected),
        "skipped": len(skipped_signals),
    }
    skipped_by_preset = defaultdict(int)
    for t in skipped_signals:
        skipped_by_preset[t.get("preset") or "unknown"] += 1
    risk_rejected_by_preset = defaultdict(int)
    for t in risk_rejected:
        risk_rejected_by_preset[t.get("preset") or "unknown"] += 1

    # Exit reasons
    tp_closes = [t for t in closes if "TP" in (t.get("exit_reason") or "")]
    sl_closes = [t for t in closes if "SL" in (t.get("exit_reason") or "")]
    time_closes = [t for t in closes if "TIME_PROFIT" in (t.get("exit_reason") or "")]
    other_closes = [t for t in closes if not t.get("exit_reason")]

    # Long/short win rates
    long_wins = len([t for t in closes if t.get("direction") == "LONG" and float(t.get("pnl") or 0.0) > 0])
    long_losses = len([t for t in closes if t.get("direction") == "LONG" and float(t.get("pnl") or 0.0) <= 0])
    short_wins = len([t for t in closes if t.get("direction") == "SHORT" and float(t.get("pnl") or 0.0) > 0])
    short_losses = len([t for t in closes if t.get("direction") == "SHORT" and float(t.get("pnl") or 0.0) <= 0])
    long_total = long_wins + long_losses
    short_total = short_wins + short_losses
    long_win_rate = (long_wins / long_total * 100) if long_total else 0.0
    short_win_rate = (short_wins / short_total * 100) if short_total else 0.0

    # Hourly stats
    hourly_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    preset_hourly_stats = defaultdict(lambda: defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0}))

    for t in closes:
        ts = parse_timestamp(t.get("exit_time", ""))
        if not ts:
            ts = parse_timestamp(t.get("entry_time", ""))
        if not ts:
            continue
        hour = ts.hour
        hourly_stats[hour]["trades"] += 1
        hourly_stats[hour]["pnl"] += float(t.get("pnl") or 0.0)
        if float(t.get("pnl") or 0.0) > 0:
            hourly_stats[hour]["wins"] += 1
        preset = t.get("preset") or "unknown"
        preset_hourly_stats[preset][hour]["trades"] += 1
        preset_hourly_stats[preset][hour]["pnl"] += float(t.get("pnl") or 0.0)
        if float(t.get("pnl") or 0.0) > 0:
            preset_hourly_stats[preset][hour]["wins"] += 1

    # Per-coin stats
    coin_stats = {}
    for sym in set(list(opens_by_sym.keys()) + list(closes_by_sym.keys())):
        sym_closes = closes_by_sym.get(sym, [])
        sym_opens = opens_by_sym.get(sym, [])
        sym_rejected = rejected_by_sym.get(sym, [])
        sym_pnl = sum(float(t.get("pnl") or 0.0) for t in sym_closes)
        sym_wins = len([t for t in sym_closes if float(t.get("pnl") or 0.0) > 0])
        sym_losses = len([t for t in sym_closes if float(t.get("pnl") or 0.0) <= 0])
        sym_trades = len(sym_closes)
        sym_wr = (sym_wins / sym_trades * 100) if sym_trades else 0.0
        coin_stats[sym] = {
            "trades": sym_trades,
            "wins": sym_wins,
            "losses": sym_losses,
            "win_rate": sym_wr,
            "pnl": sym_pnl,
            "rejected": len(sym_rejected),
            "open_preset": (sym_opens.get("preset") if isinstance(sym_opens, dict) else "unknown"),
        }

    # Position sizing stats — рассчитывается по закрытым сделкам (qty × entry_price)
    pos_notionals = []
    pos_qtys = []
    pos_by_sym = defaultdict(list)
    for t in closes:
        try:
            qty = float(t.get("qty") or 0.0)
            entry = float(t.get("entry_price") or 0.0)
            notional = abs(qty) * abs(entry)
        except Exception:
            continue
        if notional <= 0:
            continue
        pos_notionals.append(notional)
        pos_qtys.append(abs(qty))
        pos_by_sym[t.get("symbol", "?")].append(notional)
    pos_stats = {
        "count": len(pos_notionals),
        "avg_notional": (sum(pos_notionals) / len(pos_notionals)) if pos_notionals else 0.0,
        "min_notional": min(pos_notionals) if pos_notionals else 0.0,
        "max_notional": max(pos_notionals) if pos_notionals else 0.0,
        "avg_qty": (sum(pos_qtys) / len(pos_qtys)) if pos_qtys else 0.0,
        "min_qty": min(pos_qtys) if pos_qtys else 0.0,
        "max_qty": max(pos_qtys) if pos_qtys else 0.0,
        "by_symbol": {s: (round(sum(ns) / len(ns), 2), len(ns)) for s, ns in pos_by_sym.items()},
    }

    # Day-of-week PnL distribution
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_stats = {}
    for t in closes:
        ts = parse_timestamp(t.get("exit_time", "")) or parse_timestamp(t.get("entry_time", ""))
        if not ts:
            continue
        d = dow_names[ts.weekday()]
        s = dow_stats.setdefault(d, {"trades": 0, "wins": 0, "pnl": 0.0})
        s["trades"] += 1
        p = float(t.get("pnl") or 0.0)
        s["pnl"] += p
        if p > 0:
            s["wins"] += 1
    for d in dow_names:
        dow_stats.setdefault(d, {"trades": 0, "wins": 0, "pnl": 0.0})

    # ATR vs PnL correlation (волатильность на входе)
    atr_pairs = []
    for t in closes:
        try:
            atr = float(t.get("atr") or 0.0)
            pnl = float(t.get("pnl") or 0.0)
            entry = float(t.get("entry_price") or 0.0)
            if atr > 0 and entry > 0:
                atr_pairs.append((atr / entry, pnl))  # относительный ATR в %
        except Exception:
            continue
    atr_corr = _pearson([a for a, _ in atr_pairs], [p for _, p in atr_pairs]) if len(atr_pairs) >= 2 else None
    atr_buckets = {}
    if atr_pairs:
        for rel, pnl in atr_pairs:
            pct = round(rel * 100, 3)
            bucket = (pct // 0.1) * 0.1
            b = atr_buckets.setdefault(bucket, {"trades": 0, "pnl": 0.0})
            b["trades"] += 1
            b["pnl"] += pnl

    # Commission % по сделкам (commission / notional)
    comm_pcts = []
    for t in closes:
        try:
            comm = float(t.get("commission") or 0.0)
            notional = abs(float(t.get("qty") or 0.0)) * abs(float(t.get("entry_price") or 0.0))
            if notional > 0:
                comm_pcts.append((comm / notional) * 100)
        except Exception:
            continue
    avg_comm_pct = (sum(comm_pcts) / len(comm_pcts)) if comm_pcts else 0.0

    # Одновременно открытые позиции: sweep по событиям open/close
    events = []
    for t in trades:
        try:
            ot = parse_timestamp(t.get("entry_time", ""))
            if ot:
                events.append((ot.timestamp(), 1))
            ct = parse_timestamp(t.get("exit_time", ""))
            if ct:
                events.append((ct.timestamp(), -1))
        except Exception:
            continue
    events.sort(key=lambda x: (x[0], x[1]))
    cur_open = 0
    max_open = 0
    total_open_time = 0.0
    prev_ts = None
    concurrency_entries = []
    for ts, delta in events:
        if prev_ts is not None:
            span = ts - prev_ts
            if span > 0:
                total_open_time += span * max(cur_open, 0)
                concurrency_entries.append((cur_open, span))
        cur_open += delta
        prev_ts = ts
        if cur_open > max_open:
            max_open = cur_open
    total_span = (prev_ts - events[0][0]) if events and prev_ts else 0.0
    avg_open = total_open_time / total_span if total_span > 0 else 0.0

    metrics = {
        "dow_stats": dow_stats,
        "atr_corr": atr_corr,
        "atr_buckets": atr_buckets,
        "avg_comm_pct": avg_comm_pct,
        "concurrency": {"max": max_open, "avg": avg_open, "samples": len(events)},
    }

    return {
        "total_opens": total_opens,
        "total_closes": total_closes,
        "total_rejected": total_rejected,
        "long_opens": long_opens,
        "short_opens": short_opens,
        "long_closes": long_closes,
        "short_closes": short_closes,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "total_commission": total_commission,
        "avg_pnl_per_trade": avg_pnl_per_trade,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
        "max_consecutive_losses": max_consecutive_losses,
        "max_consecutive_wins": max_consecutive_wins,
        "long_win_rate": long_win_rate,
        "short_win_rate": short_win_rate,
        "preset_stats": dict(preset_stats),
        "llm_rejected": len(llm_rejected),
        "llm_rejected_by_preset": dict(llm_rejected_by_preset),
        "signal_funnel": signal_funnel,
        "skipped_by_preset": dict(skipped_by_preset),
        "risk_rejected_by_preset": dict(risk_rejected_by_preset),
        "time_closes": len(time_closes),
        "tp_closes": len(tp_closes),
        "sl_closes": len(sl_closes),
        "avg_hold_time": avg_hold_time,
        "avg_hold_tp": avg_hold_tp,
        "avg_hold_sl": avg_hold_sl,
        "hourly_stats": dict(hourly_stats),
        "preset_hourly_stats": {p: dict(hours) for p, hours in preset_hourly_stats.items()},
        "coin_stats": coin_stats,
        "pos_stats": pos_stats,
        "metrics": metrics,
        "excluded_presets": sorted(excluded_presets),
        "loss_streaks": {
            "count": len(loss_streaks),
            "by_length": {k: {"count": v, "pnl": streak_pnl[k]} for k, v in sorted(streak_lengths.items())},
            "coins": dict(sorted(streak_coins.items(), key=lambda x: x[1], reverse=True)),
            "presets": dict(sorted(streak_presets.items(), key=lambda x: x[1], reverse=True)),
            "hours": dict(sorted(streak_hours.items())),
            "raw": [
                {
                    "start": s["start"].isoformat() if s["start"] else None,
                    "end": s["end"].isoformat() if s["end"] else None,
                    "count": s["count"],
                    "pnl": s["pnl"],
                    "symbols": sorted(s["symbols"]),
                    "presets": sorted(s["presets"]),
                    "start_hour": s.get("start_hour"),
                }
                for s in loss_streaks
            ],
        },
    }


def format_number(value, decimals=2):
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}"


def format_max_pos(value):
    # max_per_preset=0 означает "без ограничения" (лимиты отключены).
    if value in (0, 0.0, None, "0"):
        return "unlimited"
    return str(value)


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def generate_report(stats, start_time=None, end_time=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = []
    report.append(f"## Analysis Report - {now}")
    if start_time is not None or end_time is not None:
        report.append("")
        report.append(f"_Filtered window: {start_time or '...'} -> {end_time or '...'}_")
    report.append("")
    report.append("### Overall Statistics")
    report.append("")
    report.append("| Metric | Value |")
    report.append("|---|---|")
    report.append(f"| Total Opens | {stats['total_opens']} |")
    report.append(f"| Total Closes | {stats['total_closes']} |")
    report.append(f"| Total Rejected | {stats['total_rejected']} |")
    report.append(f"| Long Opens | {stats['long_opens']} |")
    report.append(f"| Short Opens | {stats['short_opens']} |")
    report.append(f"| Long Closes | {stats['long_closes']} |")
    report.append(f"| Short Closes | {stats['short_closes']} |")
    if stats.get("excluded_presets"):
        report.append(f"| Excluded Presets | {', '.join(stats['excluded_presets'])} |")
    report.append(f"| Win Rate | {format_number(stats['win_rate'])}% |")
    report.append(f"| Total PnL (Net) | {format_number(stats['total_pnl'])} |")
    report.append(f"| Total Commission | {format_number(stats['total_commission'])} |")
    report.append(f"| Total PnL (Gross) | {format_number(stats['total_pnl'] + stats['total_commission'])} |")
    report.append(f"| Avg PnL per Trade | {format_number(stats['avg_pnl_per_trade'])} |")
    report.append(f"| Profit Factor | {format_number(stats['profit_factor'])} |")
    report.append(f"| Max Drawdown | {format_number(stats['max_drawdown'])} |")
    report.append(f"| Max Consecutive Losses | {stats['max_consecutive_losses']} |")
    report.append(f"| Max Consecutive Wins | {stats['max_consecutive_wins']} |")
    report.append(f"| Long Win Rate | {format_number(stats['long_win_rate'])}% |")
    report.append(f"| Short Win Rate | {format_number(stats['short_win_rate'])}% |")
    report.append(f"| TP Closes | {stats['tp_closes']} |")
    report.append(f"| SL Closes | {stats['sl_closes']} |")
    report.append(f"| Time Profit Closes | {stats['time_closes']} |")
    report.append(f"| Avg Hold Time | {format_number(stats['avg_hold_time'], 1)} min |")
    report.append(f"| Avg Hold (TP) | {format_number(stats['avg_hold_tp'], 1)} min |")
    report.append(f"| Avg Hold (SL) | {format_number(stats['avg_hold_sl'], 1)} min |")
    report.append("")
    report.append(f"| Max Consecutive Losses | {stats['max_consecutive_losses']} |")
    report.append(f"| Max Consecutive Wins | {stats['max_consecutive_wins']} |")
    report.append("")
    streak_stats = stats.get("loss_streaks", {})
    report.append("### Loss Streaks")
    report.append("")
    report.append(f"| Total Streaks | {streak_stats.get('count', 0)} |")
    report.append(f"| Max Consecutive Losses | {stats['max_consecutive_losses']} |")
    report.append("")
    report.append("#### Streak Length Distribution")
    report.append("")
    report.append("| Length | Streaks | Total PnL |")
    report.append("|---|---|---|")
    for length, data in sorted(streak_stats.get("by_length", {}).items()):
        report.append(f"| {length} loss | {data['count']} | {format_number(data['pnl'])} |")
    report.append("")
    report.append("#### Streak Coins")
    report.append("")
    report.append("| Symbol | Streaks Involved |")
    report.append("|---|---|")
    for sym, cnt in list(streak_stats.get("coins", {}).items())[:20]:
        report.append(f"| {sym} | {cnt} |")
    report.append("")
    report.append("#### Streak Presets")
    report.append("")
    report.append("| Preset | Streaks Involved |")
    report.append("|---|---|")
    for pr, cnt in list(streak_stats.get("presets", {}).items())[:20]:
        report.append(f"| {pr} | {cnt} |")
    report.append("")
    report.append("#### Loss Streaks by Hour")
    report.append("")
    report.append("| Hour | Streaks | Total PnL |")
    report.append("|---|---|---|")
    for h in sorted(streak_stats.get("hours", {}).keys()):
        d = streak_stats["hours"][h]
        report.append(f"| {h:02d}:00 | {d['count']} | {format_number(d['pnl'])} |")
    report.append("")
    if streak_stats.get("raw"):
        report.append("#### Recent Loss Streaks")
        report.append("")
        report.append("| Start | End | Length | PnL | Symbols | Presets |")
        report.append("|---|---|---|---|---|---|")
        for s in streak_stats["raw"][-20:]:
            syms = ", ".join(s.get("symbols", [])[:5])
            prs = ", ".join(s.get("presets", [])[:5])
            start = (s.get("start") or "")[:19]
            end = (s.get("end") or "")[:19]
            report.append(f"| {start} | {end} | {s['count']} | {format_number(s['pnl'])} | {syms} | {prs} |")
        report.append("")
    pos = stats.get("pos_stats", {})
    report.append("### Position Sizing")
    report.append("")
    report.append("| Metric | Value |")
    report.append("|---|---|")
    report.append(f"| Trades | {pos.get('count', 0)} |")
    report.append(f"| Avg Notional (Entry Value, USDT) | {format_number(pos.get('avg_notional', 0.0))} |")
    report.append(f"| Min Notional (USDT) | {format_number(pos.get('min_notional', 0.0))} |")
    report.append(f"| Max Notional (USDT) | {format_number(pos.get('max_notional', 0.0))} |")
    report.append(f"| Avg Qty | {format_number(pos.get('avg_qty', 0.0))} |")
    report.append(f"| Min Qty | {format_number(pos.get('min_qty', 0.0))} |")
    report.append(f"| Max Qty | {format_number(pos.get('max_qty', 0.0))} |")
    report.append("")
    report.append("| Symbol | Avg Notional (USDT) | Trades |")
    report.append("|---|---|---|")
    for sym in sorted(pos.get("by_symbol", {}).keys()):
        avg_n, cnt = pos["by_symbol"][sym]
        report.append(f"| {sym} | {format_number(avg_n)} | {cnt} |")
    report.append("")

    metr = stats.get("metrics", {})
    report.append("### Day-of-Week Distribution")
    report.append("")
    report.append("| Day | Trades | Wins | Win% | PnL |")
    report.append("|---|---|---|---|---|")
    for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
        s = metr.get("dow_stats", {}).get(d, {"trades": 0, "wins": 0, "pnl": 0.0})
        wr = (s["wins"] / s["trades"] * 100) if s["trades"] > 0 else 0.0
        report.append(f"| {d} | {s['trades']} | {s['wins']} | {format_number(wr)}% | {format_number(s['pnl'])} |")
    report.append("")
    report.append("### ATR (Volatility) vs PnL")
    report.append("")
    report.append("| Metric | Value |")
    report.append("|---|---|")
    corr = metr.get("atr_corr")
    report.append(f"| ATR-PnL Correlation (Pearson) | {format_number(corr) if corr is not None else 'N/A'} |")
    report.append(f"| Avg Commission % of Notional | {format_number(metr.get('avg_comm_pct', 0.0))}% |")
    report.append(f"| Max Concurrent Open Positions | {metr.get('concurrency', {}).get('max', 0)} |")
    report.append(f"| Avg Concurrent Open Positions | {format_number(metr.get('concurrency', {}).get('avg', 0.0), 2)} |")
    report.append("")
    report.append("| ATR% Bucket | Trades | PnL |")
    report.append("|---|---|---|")
    for b in sorted(metr.get("atr_buckets", {}).keys()):
        d = metr["atr_buckets"][b]
        report.append(f"| {format_number(b, 2)}% | {d['trades']} | {format_number(d['pnl'])} |")
    report.append("")
    report.append("### Per Coin Statistics")
    report.append("")
    report.append("| Symbol | Trades | Wins | Losses | Win% | PnL | Rejected |")
    report.append("|---|---|---|---|---|---|---|")
    for sym in sorted(stats["coin_stats"].keys()):
        data = stats["coin_stats"][sym]
        report.append(
            f"| {sym} | {data['trades']} | {data['wins']} | {data['losses']} | "
            f"{format_number(data['win_rate'])}% | {format_number(data['pnl'])} | {data['rejected']} |"
        )
    report.append("")
    report.append("### Per Preset Statistics")
    report.append("")
    report.append("| Preset | Trades | Wins | Losses | Win% | PnL | Commission | AvgWin | AvgLoss | Best | Worst | AvgHold | AvgHold TP | AvgHold SL |")
    report.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for preset, data in sorted(stats["preset_stats"].items()):
        win_rate = (data["wins"] / data["trades"] * 100) if data["trades"] > 0 else 0.0
        report.append(
            f"| {preset} | {data['trades']} | {data['wins']} | {data['losses']} | "
            f"{format_number(win_rate)}% | {format_number(data['pnl'])} | {format_number(data.get('commission', 0.0))} | "
            f"{format_number(data['avg_win'])} | {format_number(data['avg_loss'])} | "
            f"{format_number(data['best_trade'])} | {format_number(data['worst_trade'])} | "
            f"{format_number(data.get('avg_hold_time', 0), 1)} min | "
            f"{format_number(data.get('avg_hold_tp', 0), 1)} min | "
            f"{format_number(data.get('avg_hold_sl', 0), 1)} min |"
        )
    report.append("")
    report.append("### Per Preset Hold Times")
    report.append("")
    report.append("| Preset | Trades | Avg Hold | Avg Hold TP | Avg Hold SL | TP/SL Ratio |")
    report.append("|---|---|---|---|---|---|")
    for preset, data in sorted(stats["preset_stats"].items()):
        if data["trades"] == 0:
            continue
        avg_tp = data.get("avg_hold_tp", 0.0)
        avg_sl = data.get("avg_hold_sl", 0.0)
        ratio = (avg_tp / avg_sl) if avg_sl > 0 else 0.0
        report.append(
            f"| {preset} | {data['trades']} | "
            f"{format_number(data.get('avg_hold_time', 0), 1)} min | "
            f"{format_number(avg_tp, 1)} min | "
            f"{format_number(avg_sl, 1)} min | "
            f"{format_number(ratio, 1)}x |"
        )
    report.append("")
    report.append("### Per Preset Configuration vs Performance")
    report.append("")
    report.append("| Preset | Configured TP | Configured SL | Max Pos | Actual WR | Actual PnL | Status |")
    report.append("|---|---|---|---|---|---|---|")
    for preset in sorted(PRESET_CONFIG.keys()):
        cfg = PRESET_CONFIG[preset]
        tp = cfg.get("tp", 1.0)
        sl = cfg.get("sl", 0.5)
        max_pos = format_max_pos(cfg.get("max_per_preset", "N/A"))
        base_preset = preset.rsplit("_", 1)[0]
        actual = stats["preset_stats"].get(preset, stats["preset_stats"].get(base_preset, {}))
        actual_wr = (actual.get("wins", 0) / actual.get("trades", 1) * 100) if actual.get("trades", 0) > 0 else 0.0
        actual_pnl = actual.get("pnl", 0.0)
        status = "[OK]" if actual_pnl > 0 and actual_wr > 50 else ("[WARN]" if actual_wr > 45 else "[POOR]")
        report.append(
            f"| {preset} | {format_number(tp)}% | {format_number(sl)}% | {max_pos} | "
            f"{format_number(actual_wr)}% | {format_number(actual_pnl)} | {status} |"
        )
    report.append("")

    funnel = stats.get("signal_funnel", {})
    report.append("### Signal Funnel")
    report.append("")
    report.append("| Outcome | Signals |")
    report.append("|---|---|")
    report.append(f"| Opened | {funnel.get('opened', 0)} |")
    report.append(f"| Risk Rejected | {funnel.get('risk_rejected', 0)} |")
    report.append(f"| LLM Rejected | {funnel.get('llm_rejected', 0)} |")
    report.append(f"| Skipped (throttle/limit) | {funnel.get('skipped', 0)} |")
    report.append(f"| **Total Signals** | {funnel.get('opened', 0) + funnel.get('risk_rejected', 0) + funnel.get('llm_rejected', 0) + funnel.get('skipped', 0)} |")
    report.append("")
    skipped_by_p = stats.get("skipped_by_preset", {})
    if skipped_by_p:
        report.append("### Skipped Signals by Preset")
        report.append("")
        report.append("| Preset | Skipped |")
        report.append("|---|---|")
        for preset, count in sorted(skipped_by_p.items()):
            report.append(f"| {preset} | {count} |")
        report.append("")

    report.append("### LLM Statistics")
    report.append("")
    report.append("| Metric | Value |")
    report.append("|---|---|")
    report.append(f"| LLM Rejected | {stats['llm_rejected']} |")
    report.append("")
    if stats.get("llm_rejected_by_preset"):
        report.append("### LLM Rejections by Preset")
        report.append("")
        report.append("| Preset | Rejections |")
        report.append("|---|---|")
        for preset, count in sorted(stats["llm_rejected_by_preset"].items()):
            report.append(f"| {preset} | {count} |")
        report.append("")
    report.append("### Hourly Distribution")
    report.append("")
    report.append("| Hour | Trades | Wins | Win% | PnL |")
    report.append("|---|---|---|---|---|")
    hourly = stats.get("hourly_stats", {})
    for hour in sorted(hourly.keys()):
        data = hourly[hour]
        win_rate = (data["wins"] / data["trades"] * 100) if data["trades"] > 0 else 0.0
        report.append(
            f"| {hour:02d}:00 | {data['trades']} | {data['wins']} | {format_number(win_rate)}% | {format_number(data['pnl'])} |"
        )
    report.append("")
    report.append("### Preset Performance by Hour")
    report.append("")
    preset_hourly = stats.get("preset_hourly_stats", {})
    active_presets = sorted([p for p in preset_hourly.keys() if any(v.get("trades", 0) > 0 for v in preset_hourly[p].values())])
    hours = sorted({h for p in preset_hourly.values() for h in p.keys()})
    if active_presets and hours:
        header = "| Preset |" + "|".join(f" {h:02d}:00" for h in hours) + "|"
        separator = "|---|" + "|".join("---" for _ in hours) + "|"
        report.append(header)
        report.append(separator)
        for preset in active_presets:
            row_cells = []
            for h in hours:
                data = preset_hourly[preset].get(h, {"trades": 0, "wins": 0, "pnl": 0.0})
                trades = data.get("trades", 0)
                wins = data.get("wins", 0)
                pnl = data.get("pnl", 0.0)
                wr = (wins / trades * 100) if trades > 0 else 0.0
                cell = f"{format_number(wr, 0)}%/{format_number(pnl)}%" if trades > 0 else "-"
                row_cells.append(cell)
            report.append("| " + preset + " |" + "|".join(row_cells) + "|")
    else:
        report.append("_No preset-hour data available yet._")
    report.append("")
    report.append("### Preset Configuration Reference")
    report.append("")
    report.append("| Preset | TP | SL | Max Pos |")
    report.append("|---|---|---|---|")
    for preset in sorted(PRESET_CONFIG.keys()):
        cfg = PRESET_CONFIG[preset]
        tp = cfg.get("tp", 1.0)
        sl = cfg.get("sl", 0.5)
        max_pos = format_max_pos(cfg.get("max_per_preset", "N/A"))
        report.append(f"| {preset} | {format_number(tp)}% | {format_number(sl)}% | {max_pos} |")
    report.append("")
    report.append("---")
    report.append("")
    return "\n".join(report)


def main():
    parser = argparse.ArgumentParser(description="Analyze bot trades")
    parser.add_argument("--hours", type=int, default=None, help="Filter trades from last N hours")
    parser.add_argument("--start", type=str, default=None, help="Filter trades from this ISO datetime")
    parser.add_argument("--end", type=str, default=None, help="Filter trades up to this ISO datetime")
    parser.add_argument("--no-write", action="store_true", help="Print report without writing to ANALYTICS.md")
    args = parser.parse_args()

    start_time = None
    end_time = None
    if args.hours is not None:
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=args.hours)
    elif args.start:
        start_time = parse_timestamp(args.start)
    if args.end:
        end_time = parse_timestamp(args.end)

    trades = load_trades()
    stats = analyze_trades(trades, start_time=start_time, end_time=end_time)
    report = generate_report(stats, start_time=start_time, end_time=end_time)
    print(report)

    if not args.no_write:
        analytics_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ANALYTICS.md")
        with open(analytics_path, "w", encoding="utf-8") as f:
            f.write(report)
            f.write("\n")
        print(f"\nReport written to {analytics_path}")


if __name__ == "__main__":
    main()
