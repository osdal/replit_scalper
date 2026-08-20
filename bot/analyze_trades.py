"""
Analysis script for replit_scalper bot.
Reads trades from SQLite database and generates comprehensive statistics.
Adapted from ClawStreet bot analytics.
"""

import sys
import os
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlite3
from preset_config import PRESET_CONFIG

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "bot.db")


def parse_timestamp(ts_str):
    if not ts_str:
        return None
    try:
        # База хранит ISO как '2026-08-20T09:41:00[.ffffff]'; приводим 'T' к пробелу.
        ts_str = ts_str.replace("Z", "").replace("T", " ").split(".")[0]
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            ts_str = ts_str.replace("Z", "").replace("T", " ")
            return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
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


def analyze_trades(trades=None, excluded_presets=None):
    if trades is None:
        trades = load_trades()
    if excluded_presets is None:
        excluded_presets = set()

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
        "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
        "wins_list": [], "losses_list": [], "hold_times": [],
    })

    for t in closes:
        preset = t.get("preset") or "unknown"
        pnl = float(t.get("pnl") or 0.0)
        s = preset_stats[preset]
        s["trades"] += 1
        s["pnl"] += pnl
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
        s["avg_hold_time"] = 0.0
        s["hold_count"] = 0

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
        "time_closes": len(time_closes),
        "tp_closes": len(tp_closes),
        "sl_closes": len(sl_closes),
        "avg_hold_time": avg_hold_time,
        "avg_hold_tp": avg_hold_tp,
        "avg_hold_sl": avg_hold_sl,
        "hourly_stats": dict(hourly_stats),
        "preset_hourly_stats": {p: dict(hours) for p, hours in preset_hourly_stats.items()},
        "coin_stats": coin_stats,
        "excluded_presets": sorted(excluded_presets),
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


def generate_report(stats):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = []
    report.append(f"## Analysis Report - {now}")
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
    report.append(f"| Total PnL | {format_number(stats['total_pnl'])} |")
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
    report.append("| Preset | Trades | Wins | Losses | Win% | PnL | AvgWin | AvgLoss | Best | Worst | AvgHold |")
    report.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for preset, data in sorted(stats["preset_stats"].items()):
        win_rate = (data["wins"] / data["trades"] * 100) if data["trades"] > 0 else 0.0
        report.append(
            f"| {preset} | {data['trades']} | {data['wins']} | {data['losses']} | "
            f"{format_number(win_rate)}% | {format_number(data['pnl'])} | "
            f"{format_number(data['avg_win'])} | {format_number(data['avg_loss'])} | "
            f"{format_number(data['best_trade'])} | {format_number(data['worst_trade'])} | "
            f"{format_number(data.get('avg_hold_time', 0), 1)} min |"
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
    trades = load_trades()
    stats = analyze_trades(trades)
    report = generate_report(stats)
    print(report)

    analytics_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ANALYTICS.md")
    with open(analytics_path, "w", encoding="utf-8") as f:
        f.write(report)
        f.write("\n")
    print(f"\nReport written to {analytics_path}")


if __name__ == "__main__":
    main()
