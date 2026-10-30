#!/usr/bin/env python3
"""Диагностика ТОЛЬКО живых циклов (открытые позиции на бирже).

Заменяет старую версию с жёстко вписанными датами старта циклов (26-27.10),
которая суммировала уже закрытые циклы и вводила в заблуждение. Здесь цикл
берётся из фактического состояния:

1. открытые позиции — с биржи (`futures_position_information`);
2. начало цикла — `entry_fill_ms` из state-файла трекера
   (`state_<env>_<symbol>.json`), иначе `entry_time` открытой строки в БД;
3. результат — `userTrades` за окно [вход цикла, now]; если торговая история
   недоступна (rate-limit/бан) — fallback на `income` history.

Считается ровно так же, как в боте:
  net = Σ realizedPnl − Σ commission(USDT) + unrealized текущей позиции
(фондирование показывается отдельно, в решение EXPIRED/cap не входит).

Для каждой живой позиции печатается возраст, realized/commission/upnl/net,
оценка комиссии выхода и решение EXPIRED_PROFIT — то есть скрипт отвечает на
вопрос «почему цикл не закрылся сам».

Usage (в контейнере, из каталога бота):
    python diag_cycles.py
    python diag_cycles.py --symbols ATOMUSDT,FETUSDT
    python diag_cycles.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from binance import AsyncClient

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_ENV = (os.getenv("BOT_ENV") or "testnet").strip().lower() or "testnet"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(os.path.join(THIS_DIR, "..", f".env.{BOT_ENV}"), override=True)
    load_dotenv(os.path.join(THIS_DIR, "..", ".env"), override=False)
    load_dotenv(override=False)


_load_env()
TESTNET = (os.getenv("BINANCE_TESTNET", "true").lower() == "true")

# Те же дефолты, что в main.py (должны совпадать, иначе решение разойдётся с ботом).
EXPIRED_HOURS = float(os.getenv("MAX_POSITIONS_IGNORE_AFTER_HOURS") or "0")
EXPIRED_MIN_PROFIT_USD = float(os.getenv("EXPIRED_CLOSE_MIN_PROFIT_USD") or "0.02")
CUM_LOSS_PCT = float(os.getenv("REVERSE_CUM_LOSS_PCT") or "5")
TAKER_FEE = float(os.getenv("TAKER_FEE_RATE") or "0.0005")  # 0.05% futures taker

DEFAULT_DB = os.getenv("BOT_DB_PATH") or "/app/data/bot.db"


def _parse_ms(value: Any) -> int:
    """datetime/ISO/epoch -> ms UTC. Naive-время трактуется как UTC."""
    if value is None:
        return 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return int(v * 1000) if v < 1e12 else int(v)
    s = str(value).strip()
    if not s:
        return 0
    if s.endswith("Z"):
        s = s[:-1]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _state_path(state_dir: str, symbol: str) -> str:
    return os.path.join(state_dir, f"state_{BOT_ENV}_{symbol.lower()}.json")


def _load_state(state_dir: str, symbol: str) -> dict:
    path = _state_path(state_dir, symbol)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _db_open_trade(db_path: str, symbol: str) -> Optional[dict]:
    """Открытая принятая сделка по символу из SQLite (read-only)."""
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id, symbol, direction, qty, entry_price, entry_time, "
                "chain_depth, cycle_pnl, cycle_close_reason "
                "FROM trades WHERE symbol=? AND is_open=1 AND status!='rejected' "
                "ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
    except Exception:
        return None


async def _fetch_fills(client: AsyncClient, symbol: str, start_ms: int, end_ms: int) -> Optional[list]:
    """userTrades за окно с пагинацией. None — если запрос не удался."""
    out: list = []
    cursor = start_ms
    try:
        while cursor <= end_ms:
            batch = await client.futures_account_trades(
                symbol=symbol, startTime=cursor, endTime=end_ms, limit=1000,
            )
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 1000:
                break
            last_time = int(batch[-1].get("time") or 0)
            cursor = last_time + 1
            if last_time <= 0:
                break
    except Exception:
        return None
    return out


async def _fetch_income(client: AsyncClient, symbol: str, start_ms: int, end_ms: int) -> Optional[dict]:
    """Fallback: доход по типам, когда userTrades недоступны."""
    totals = {"realized": 0.0, "commission": 0.0, "funding": 0.0}
    try:
        for itype in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
            rows = await client.futures_income_history(
                symbol=symbol, incomeType=itype, startTime=start_ms, endTime=end_ms, limit=1000,
            )
            total = sum(float(r.get("income") or 0) for r in rows)
            if itype == "REALIZED_PNL":
                totals["realized"] = total
            elif itype == "COMMISSION":
                totals["commission"] = total
            else:
                totals["funding"] = total
    except Exception:
        return None
    return totals


def _fmt(x: float) -> str:
    return f"{x:+.4f}"


async def analyze(client: AsyncClient, symbol: str, pos: dict, args) -> dict:
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    state = _load_state(args.state_dir, symbol)
    db_row = _db_open_trade(args.db, symbol)

    # --- начало цикла: state (entry_fill_ms) > открытая строка БД ---------
    start_ms = int(state.get("entry_fill_ms") or 0)
    source = "state.entry_fill_ms"
    if start_ms <= 0:
        start_ms = _parse_ms(state.get("entry_timestamp"))
        source = "state.entry_timestamp"
    if start_ms <= 0 and db_row:
        start_ms = _parse_ms(db_row.get("entry_time"))
        source = "db.trades.entry_time"
    if start_ms <= 0 or start_ms > now_ms:
        start_ms = now_ms - 24 * 3600 * 1000
        source = "fallback now-24h"

    step = int(state.get("reverse_chain_step") or 0)
    if not step and db_row:
        step = int(db_row.get("chain_depth") or 0)
    is_reverse = bool(state.get("is_reverse")) or step > 0

    amt = float(pos.get("positionAmt") or 0)
    entry_price = float(pos.get("entryPrice") or 0)
    # Binance отдаёт `unRealizedProfit` (заглавная R).
    upnl = float(pos.get("unRealizedProfit") or pos.get("unrealizedProfit") or 0)
    mark_price = float(pos.get("markPrice") or 0) or entry_price
    qty = abs(amt)

    fills = await _fetch_fills(client, symbol, start_ms, now_ms)
    if fills is not None:
        realized = sum(float(f.get("realizedPnl") or 0) for f in fills)
        commission = sum(
            float(f.get("commission") or 0)
            for f in fills
            if (f.get("commissionAsset") or "USDT") == "USDT"
        )
        data_source = f"userTrades({len(fills)})"
        funding = 0.0
        try:
            inc = await _fetch_income(client, symbol, start_ms, now_ms)
            if inc:
                funding = inc.get("funding", 0.0)
        except Exception:
            pass
    else:
        inc = await _fetch_income(client, symbol, start_ms, now_ms)
        if inc is None:
            return {
                "symbol": symbol, "error": "no fills/income (rate-limit or ban)",
                "age_h": round((now_ms - start_ms) / 3_600_000.0, 2),
            }
        realized = inc["realized"]
        commission = inc["commission"]
        funding = inc["funding"]
        data_source = "income(fallback)"

    net = realized - commission + upnl
    # Как в боте (main._expired_cycle_profit_snapshot): комиссия выхода
    # считается от entry_price текущей ноги, а не от mark.
    exit_fee = qty * entry_price * TAKER_FEE
    age_h = (now_ms - start_ms) / 3_600_000.0

    if not is_reverse:
        expired = "n/a (not reverse)"
    elif EXPIRED_HOURS <= 0:
        expired = "off (MAX_POSITIONS_IGNORE_AFTER_HOURS=0)"
    elif age_h < EXPIRED_HOURS:
        expired = f"wait age<{EXPIRED_HOURS:g}h"
    elif net >= exit_fee + EXPIRED_MIN_PROFIT_USD:
        expired = "CLOSE"
    else:
        expired = "wait net<fee+min"

    wallet = 0.0
    try:
        for b in await client.futures_account_balance():
            if b.get("asset") == "USDT":
                wallet = float(b.get("balance") or 0)
                break
    except Exception:
        pass
    cap_threshold = wallet * CUM_LOSS_PCT / 100.0 if CUM_LOSS_PCT > 0 else 0.0

    return {
        "symbol": symbol,
        "dir": "LONG" if amt > 0 else "SHORT",
        "step": step,
        "reverse": is_reverse,
        "age_h": round(age_h, 2),
        "qty": qty,
        "entry": entry_price,
        "mark": mark_price,
        "realized": round(realized, 4),
        "commission": round(commission, 4),
        "funding": round(funding, 4),
        "upnl": round(upnl, 4),
        "net": round(net, 4),
        "exit_fee": round(exit_fee, 4),
        "expired": expired,
        "cap_pct": CUM_LOSS_PCT,
        "cap_threshold_approx": round(cap_threshold, 4),
        "start": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
        "start_source": source,
        "data_source": data_source,
    }


async def main(args) -> int:
    client = await AsyncClient.create(
        api_key=os.getenv("BINANCE_API_KEY") or None,
        api_secret=os.getenv("BINANCE_API_SECRET") or None,
        testnet=TESTNET,
    )
    try:
        positions = await client.futures_position_information()
        wanted = set(args.symbols or [])
        rows = []
        for p in positions:
            symbol = p.get("symbol") or ""
            try:
                amt = float(p.get("positionAmt") or 0)
            except (TypeError, ValueError):
                continue
            if abs(amt) <= 0:
                continue
            if wanted and symbol not in wanted:
                continue
            try:
                rows.append(await analyze(client, symbol, p, args))
            except Exception as e:  # noqa: BLE001
                rows.append({"symbol": symbol, "error": repr(e)[:200]})
    finally:
        await client.close_connection()

    if args.json:
        print(json.dumps({"env": BOT_ENV, "testnet": TESTNET, "cycles": rows}, ensure_ascii=False, indent=2))
        return 0

    print(f"env={BOT_ENV} testnet={TESTNET} | live cycles: {len(rows)}")
    if not rows:
        print("  (нет открытых позиций)")
        return 0
    header = (
        f"{'SYMBOL':<12} {'DIR':<5} {'STEP':>4} {'AGE_H':>6} {'REALIZED':>10} "
        f"{'COMM':>8} {'UPNL':>9} {'NET':>9} {'EXIT_FEE':>9}  EXPIRED"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        if "error" in r:
            print(f"{r['symbol']:<12} ERROR: {r['error']}")
            continue
        print(
            f"{r['symbol']:<12} {r['dir']:<5} {r['step']:>4} {r['age_h']:>6.2f} "
            f"{_fmt(r['realized']):>10} {r['commission']:>8.4f} {_fmt(r['upnl']):>9} "
            f"{_fmt(r['net']):>9} {r['exit_fee']:>9.4f}  {r['expired']}"
        )
    print()
    for r in rows:
        if "error" in r:
            continue
        print(
            f"  {r['symbol']}: start={r['start']} ({r['start_source']}), "
            f"src={r['data_source']}, funding={_fmt(r['funding'])}, "
            f"cap~{r['cap_pct']:g}%≈{r['cap_threshold_approx']:.2f} USD"
        )
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Live cycle diagnostics (open positions only)")
    ap.add_argument("--symbols", default=None, help="CSV: ATOMUSDT,FETUSDT")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--state-dir", default=THIS_DIR)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.symbols:
        a.symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    try:
        sys.exit(asyncio.run(main(a)))
    except KeyboardInterrupt:
        sys.exit(130)
