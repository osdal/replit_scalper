#!/usr/bin/env python3
"""Реконсилер PnL: сверяет закрытые live-сделки с фактическими филлами Binance.

Зачем: при закрытии «извне» (SIGTERM / close_all, ветка «reverse не открыт»)
бот пишет локальный PnL по трекеру, не дожидаясь филлов, а fallback
OrderManager.get_realized_pnl() умеет вернуть -entry_commission, если в окне
нет закрывающих филлов. В итоге trades.pnl расходится с биржей на доллары.

Модель БД: одна строка trades = один цикл (исходная нога + все реверсы);
pnl/cycle_pnl строки — итог всего цикла, entry_time — вход исходной ноги.
Поэтому сверяем не «окно одной ноги», а сумму всех round-trip'ов
(позиция 0 -> ... -> 0) на символе между закрытием предыдущего цикла и
закрытием текущего.

Строка патчится, только если последний round-trip попадает в ±3 мин от
exit_time (однозначная привязка). Иначе — skip, ничего не пишем.

pnl = sum(realizedPnl) + sum(commission) + funding за цикл.
По умолчанию dry-run: в БД ничего не пишет, только лог. Запись — с --apply.

Запуск (в контейнере api-live, где есть binance и .env.live):

    python3 /app/bot/reconcile_pnl.py --hours 48            # посмотреть
    python3 /app/bot/reconcile_pnl.py --hours 48 --apply    # применить
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

_this = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_this)

BOT_ENV = (os.getenv("BOT_ENV") or "testnet").strip().lower() or "testnet"
DEFAULT_DB = os.path.join(_ROOT, "data", f"bot_{BOT_ENV}.db")
DEFAULT_LOG = os.path.join(_ROOT, "data", "pnl_reconcile.log")

# --- Нагрузка на IP-вес Binance (вариант, experiment: fix/reconcile-weight) ---
# X-MBX-USED-WEIGHT-1M — суммарный вес ВСЕХ 60 ботов на IP. Он годится только
# для паузы и для порога gate; свой расход считаем локально по весам эндпоинтов.
WEIGHT_PAUSE_ABOVE = 600      # пауза 10 с, если суммарный вес выше
REQ_PAUSE_SEC = 0.35          # пауза перед каждым запросом
BUDGET_WEIGHT = 300           # бюджет СВОЕГО расхода на запуск
SHED_THRESHOLD = 1400         # не запускать, если суммарный вес выше
W_TRADES = 5                  # weight futures_account_trades
W_INCOME = 30                 # weight futures_income_history
INCOME_CHUNK_MS = 6 * 3_600_000   # потолок одного куска окна income (6 ч)
INCOME_MAX_PAGES = 20         # потолок страниц income на прогон

# Минимальное расхождение, при котором вообще трогаем строку.
TOLERANCE_USD = 0.01
# Строка считается «закрывающей цикл», если cycle_pnl совпадал с pnl.
CYCLE_MATCH_USD = 0.005
# Допуск привязки последнего round-trip'а к exit_time строки.
DEFAULT_DRIFT_SEC = 180
# Допуск fallback-привязки одиночной ноги по entry/exit (закрытие «зависло»,
# exit_time записан в момент SL, а филл случился позже).
FALLBACK_DRIFT_MS = 15 * 60 * 1000
# Допуск на «нулевую» позицию: сумма qty из userTrades не бинарно точна
# (-3.84 + 3.84 = 6.7e-16). Без нормализации такая позиция считалась
# переворотом и порождала фантомную ногу на стыке циклов (#48714 ATOMUSDT:
# len(cycle)=3 при chain_depth=2 -> skip).
QTY_EPS = 1e-9
# Насколько раньше entry_time мог фактически начаться цикл (entry_time — время
# сигнала/первого филла; бывает на несколько минут раньше фактического входа).
FIRST_ENTRY_LOOKBACK_MIN = 60


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(os.path.join(_ROOT, f".env.{BOT_ENV}"), override=True)
    load_dotenv(os.path.join(_ROOT, ".env"), override=False)
    load_dotenv(override=False)


def _parse_ms(value: str) -> int:
    """ISO-время из БД -> epoch ms. Наивное время трактуем как UTC."""
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _log(path: str, line: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    msg = f"{stamp} {line}"
    print(msg, flush=True)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass


def _fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%m-%d %H:%M:%S")


def _resp_weight(cli) -> int:
    """Текущий вес из X-MBX-USED-WEIGHT-1M последнего ответа клиента (0 если нет)."""
    resp = getattr(cli, "response", None)
    if resp is None:
        return 0
    v = resp.headers.get("X-MBX-USED-WEIGHT-1M") or resp.headers.get("x-mbx-used-weight-1m")
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _maybe_pause(cli, log, force: bool = False) -> None:
    """Пауза между запросами + ждём, если вес выше WEIGHT_PAUSE_ABOVE."""
    w = _resp_weight(cli)
    if force or w > WEIGHT_PAUSE_ABOVE:
        time.sleep(10.0 if not force else REQ_PAUSE_SEC)
    else:
        time.sleep(REQ_PAUSE_SEC)


def _raise_rate(e: Exception) -> bool:
    """True для BinanceAPIException по -1003 / -418 / -429."""
    if not isinstance(e, Exception):
        return False
    try:
        from binance.exceptions import BinanceAPIException
        if isinstance(e, BinanceAPIException):
            return int(getattr(e, "code", 0) or 0) in (-1003, -418, -429)
    except Exception:
        pass
    return False


def _gate_skips(log_path: str) -> bool:
    """True, если прогон надо пропустить.

    Fail-closed: любая ошибка чтения gate (импорт, get_status) — пропуск с
    причиной в логе. Никаких запросов в Binance здесь не делается.
    """
    try:
        from binance_gate import AccountGate
    except Exception as e:  # noqa: BLE001
        _log(log_path, f"skip: binance_gate не импортируется ({e!r}) — fail-closed")
        return True
    gate_path = os.path.join(_ROOT, "bot", "data", f"binance_gate_{BOT_ENV}.json")
    try:
        st = AccountGate(path=gate_path).get_status() or {}
        used = int(st.get("used_weight_1m", 0) or 0)
        ban = int(st.get("ban_until_ms", 0) or 0)
    except Exception as e:  # noqa: BLE001
        _log(log_path, f"skip: get_status упал ({e!r}) — fail-closed")
        return True
    _log(log_path, f"gate: used_weight={used} ban_until_ms={ban} path={gate_path}*")
    if ban > int(time.time() * 1000):
        _log(log_path, "skip: gate сигнализирует активный бан, пропускаем прогон")
        return True
    if used >= SHED_THRESHOLD:
        _log(log_path, f"skip: суммарный вес {used} >= {SHED_THRESHOLD}, пропускаем прогон")
        return True
    return False


def _round_trips(fills: list) -> list:
    """Собирает round-trip'ы (позиция 0 -> ... -> 0) из userTrades.

    В one-way режиме филлы идут хронологически; realizedPnl на филле относится
    к закрываемой части позиции. Флип (переворот через ноль) закрывает текущий
    round-trip и начинает новый.
    """
    trips: list = []
    pos = 0.0
    cur = None

    def _new(t: int, direction: str, price: float) -> dict:
        return {"start": t, "end": None, "direction": direction,
                "realized": 0.0, "commission": 0.0, "entry_price": price}

    for f in sorted(fills, key=lambda x: int(x.get("time") or 0)):
        try:
            qty = float(f.get("qty") or 0)
            price = float(f.get("price") or 0)
            realized = float(f.get("realizedPnl") or 0)
            commission = float(f.get("commission") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        if f.get("commissionAsset") != "USDT":
            commission = 0.0
        side = f.get("side")
        signed = qty if side == "BUY" else -qty
        ts = int(f.get("time") or 0)

        if pos == 0.0:
            cur = _new(ts, "LONG" if side == "BUY" else "SHORT", price)

        if cur is None:
            continue
        # userTrades отдаёт commission положительной (это стоимость), а в
        # итоговом net она должна уменьшать результат — храним со знаком минус.
        cur["commission"] -= commission
        cur["realized"] += realized
        new_pos = pos + signed
        if abs(new_pos) <= QTY_EPS:
            new_pos = 0.0

        flipped = (pos > 0 > new_pos) or (pos < 0 < new_pos)
        if flipped:
            cur["end"] = ts
            trips.append(cur)
            cur = _new(ts, "LONG" if new_pos > 0 else "SHORT", price)
            pos = new_pos
            continue

        pos = new_pos
        if abs(pos) <= QTY_EPS:
            pos = 0.0
            cur["end"] = ts
            trips.append(cur)
            cur = None

    return trips


def _align_flat(fills: list) -> list:
    """Отрезает ведущие «закрывающие» филлы, оставшиеся от предыдущего цикла.

    Выборка начинается сразу после записанного закрытия предыдущего цикла, но
    фактический филл мог быть на секунду позже записи. Первый филл с
    realizedPnl != 0 при позиции 0 — это хвост прошлого цикла; без отсечения
    он сдвигает весь учёт позиции.
    """
    pos = 0.0
    out = []
    for f in sorted(fills, key=lambda x: int(x.get("time") or 0)):
        try:
            qty = float(f.get("qty") or 0)
            realized = float(f.get("realizedPnl") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        if pos == 0.0 and abs(realized) > 1e-9:
            continue
        out.append(f)
        pos += qty if f.get("side") == "BUY" else -qty
        if abs(pos) <= QTY_EPS:
            pos = 0.0
    return out


def _all_closed_rows(db: sqlite3.Connection, symbol: str):
    """Все закрытые live-строки символа — чтобы знать границу предыдущего цикла."""
    return db.execute(
        "SELECT id, symbol, direction, pnl, cycle_pnl, leg_pnl, commission, "
        "       entry_price, entry_time, exit_time, chain_depth, exit_reason "
        "FROM trades "
        "WHERE symbol=? AND is_open=0 AND status!='rejected' "
        "  AND mode='live' AND pnl IS NOT NULL AND exit_time IS NOT NULL "
        "ORDER BY exit_time",
        (symbol,),
    ).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description="Сверка PnL закрытых live-сделок с Binance по round-trip")
    ap.add_argument("--hours", type=float, default=3.0,
                    help="глубина проверки, часов (по умолчанию 3; полное окно 48 "
                         "только для ручного запуска или раз в сутки)")
    ap.add_argument("--limit", type=int, default=30,
                    help="максимум сделок с расхождением за прогон (по умолчанию 30)")
    ap.add_argument("--apply", action="store_true",
                    help="записать исправления в БД (без флага — dry-run)")
    ap.add_argument("--symbol", default=None, help="только один символ, напр. DOGEUSDT")
    ap.add_argument("--drift-sec", type=int, default=DEFAULT_DRIFT_SEC,
                    help=f"допуск привязки к exit_time, сек (по умолчанию {DEFAULT_DRIFT_SEC})")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"путь к БД (по умолчанию {DEFAULT_DB})")
    ap.add_argument("--log", default=DEFAULT_LOG, help=f"путь к логу (по умолчанию {DEFAULT_LOG})")
    args = ap.parse_args()

    _load_env()
    if _gate_skips(args.log):
        # Не делаем ни одного запроса при бане/высоком весе (см. _gate_skips).
        return 0

    testnet = (os.getenv("BINANCE_TESTNET", "false").lower() == "true")
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")

    mode_label = "APPLY" if args.apply else "DRY"
    _log(args.log, (
        f"run start env={BOT_ENV} testnet={testnet} mode={mode_label} "
        f"hours={args.hours:g} limit={args.limit} symbol={args.symbol or '-'} "
        f"drift={args.drift_sec}s"
    ))

    if not api_key or not api_secret:
        _log(args.log, "error: BINANCE_API_KEY/SECRET не заданы")
        return 2
    if not os.path.exists(args.db):
        _log(args.log, f"error: БД не найдена: {args.db}")
        return 2

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S")

    db = sqlite3.connect(args.db, timeout=30)
    db.row_factory = sqlite3.Row

    from binance.client import Client  # импорт здесь: --help работает без binance

    cli = Client(api_key=api_key, api_secret=api_secret, testnet=testnet)

    checked = matched = updated = no_trip = errors = 0
    abort_rate = 0
    drift_ms = args.drift_sec * 1000
    weight_spent = 0.0
    try:
        symbols = [r[0] for r in db.execute(
            "SELECT DISTINCT symbol FROM trades "
            "WHERE is_open=0 AND status!='rejected' AND mode='live' "
            "  AND pnl IS NOT NULL AND exit_time >= ?"
            + (" AND symbol = ?" if args.symbol else "") + " ORDER BY symbol",
            ([since_iso, args.symbol.upper()] if args.symbol else [since_iso]),
        )]
        if not symbols:
            _log(args.log, "нет закрытых live-сделок за период")
            return 0

        # Только символы, где есть строки на проверку (чтобы не лезть в income без нужды).
        symbols_need = []
        fetch_bounds = {}  # symbol -> (start, end)
        for symbol in symbols:
            sequence = _all_closed_rows(db, symbol)
            selected_idx = [i for i, r in enumerate(sequence) if r["exit_time"] >= since_iso]
            if not selected_idx:
                continue
            symbols_need.append(symbol)
            first_i = selected_idx[0]
            if first_i > 0:
                window_start = _parse_ms(sequence[first_i - 1]["exit_time"])
            else:
                window_start = _parse_ms(sequence[first_i]["entry_time"]) - FIRST_ENTRY_LOOKBACK_MIN * 60_000
            window_end = _parse_ms(sequence[selected_idx[-1]]["exit_time"]) + drift_ms
            fetch_bounds[symbol] = (window_start + 1_000, window_end + 10 * 60_000)
        if not symbols_need:
            _log(args.log, "нет символов с открытыми строками в указанном окне")
            return 0

        # Один общий income-запрос без symbol (фильтр по symbol локально).
        # Окно > INCOME_CHUNK_MS режем на куски; внутри куска — пагинация по
        # времени последней строки (+1 мс), чтобы не дублировать запросы.
        g_start = min(v[0] for v in fetch_bounds.values())
        g_end = max(v[1] for v in fetch_bounds.values())
        chunks = (g_end - g_start) // INCOME_CHUNK_MS + 1
        if chunks > 1:
            _log(args.log, (
                f"income: окно {(g_end - g_start) / 3_600_000:.1f} ч > "
                f"{INCOME_CHUNK_MS // 3_600_000} ч — дробим на {chunks} кусков"
            ))
        income: List[dict] = []
        pages = 0
        stop_income = False
        chunk_start = g_start
        while chunk_start <= g_end and not abort_rate and not stop_income:
            chunk_end = min(g_end, chunk_start + INCOME_CHUNK_MS - 1)
            s = chunk_start
            while s <= chunk_end:
                if pages >= INCOME_MAX_PAGES:
                    _log(args.log, f"income: потолок страниц ({INCOME_MAX_PAGES}) — останов")
                    stop_income = True
                    break
                if weight_spent + W_INCOME > BUDGET_WEIGHT:
                    _log(args.log, (
                        f"income: бюджет исчерпан (свой расход {weight_spent}, "
                        f"страница {W_INCOME}) — останов"
                    ))
                    stop_income = True
                    break
                _maybe_pause(cli, args.log)
                pages += 1
                weight_spent += W_INCOME
                try:
                    rows = cli.futures_income_history(
                        startTime=s, endTime=chunk_end, limit=1000,
                    ) or []
                except Exception as e:  # noqa: BLE001
                    if _raise_rate(e):
                        errors += 1
                        _log(args.log, f"abort: rate-limit от Binance: {e!r}")
                        abort_rate += 1
                        break
                    errors += 1
                    _log(args.log, f"error: не удалось получить income: {e!r}")
                    break
                income.extend(rows)
                if len(rows) < 1000:
                    break
                try:
                    last_t = max(int(x.get("time") or 0) for x in rows)
                except ValueError:
                    last_t = 0
                if last_t < s:
                    _log(args.log, "income: время последней строки не продвинулось — останов")
                    break
                s = last_t + 1
            chunk_start = chunk_end + 1

        for symbol in symbols_need:
            if abort_rate:
                break
            if weight_spent + W_TRADES > BUDGET_WEIGHT:
                _log(args.log, (
                    f"trades: бюджет исчерпан (свой расход {weight_spent}, "
                    f"запрос {W_TRADES}) — останов"
                ))
                break
            fetch_start, fetch_end = fetch_bounds[symbol]
            fills: List[dict] = []
            time.sleep(REQ_PAUSE_SEC)
            if _resp_weight(cli) > WEIGHT_PAUSE_ABOVE:
                time.sleep(10.0)
            weight_spent += W_TRADES
            try:
                fills = cli.futures_account_trades(
                    symbol=symbol, startTime=fetch_start, endTime=fetch_end, limit=1000,
                ) or []
            except Exception as e:  # noqa: BLE001
                if _raise_rate(e):
                    errors += 1
                    _log(args.log, f"abort: rate-limit от Binance ({symbol}): {e!r}")
                    abort_rate += 1
                    continue
                errors += 1
                _log(args.log, f"{symbol} error: не удалось получить филлы: {e!r}")
                continue
            if len(fills) >= 1000:
                errors += 1
                _log(args.log, f"{symbol} skip: 1000 филлов — история обрезана, атрибуция ненадёжна")
                continue

            sequence = _all_closed_rows(db, symbol)
            selected_idx = [i for i, r in enumerate(sequence) if r["exit_time"] >= since_iso]
            if not selected_idx:
                continue
            income_for_symbol = [x for x in income if x.get("symbol") == symbol]

            fills = _align_flat(fills)
            trips = [t for t in _round_trips(fills) if t["end"] is not None]
            funding = []
            for x in income_for_symbol:
                if x.get("incomeType") != "FUNDING_FEE":
                    continue
                try:
                    funding.append((int(x.get("time") or 0), float(x.get("income") or 0)))
                except (TypeError, ValueError):
                    continue

            for idx in selected_idx:
                row = sequence[idx]
                checked += 1
                tid = row["id"]
                if matched >= args.limit:
                    _log(args.log, f"limit reached ({args.limit}) — остальное в следующий прогон")
                    break

                lower = (
                    _parse_ms(sequence[idx - 1]["exit_time"]) - 5_000
                    if idx > 0 else
                    _parse_ms(row["entry_time"]) - FIRST_ENTRY_LOOKBACK_MIN * 60_000
                )
                exit_ms = _parse_ms(row["exit_time"])
                entry_ms = _parse_ms(row["entry_time"])
                depth = row["chain_depth"]
                cycle = [
                    t for t in trips
                    if t["start"] >= lower and t["end"] <= exit_ms + drift_ms
                ]
                via = "цикл"
                ok = (
                    bool(cycle)
                    and abs(cycle[-1]["end"] - exit_ms) <= drift_ms
                    and cycle[-1].get("direction") == row["direction"]
                    and (depth is None or len(cycle) == int(depth))
                )
                # Строгая привязка не сработала. Для одиночной ноги пробуем
                # найти trip по entry/exit с допуском: при «зависшем» закрытии
                # бот пишет exit_time в момент SL, а филл случается позже.
                if not ok and (depth is None or int(depth) == 1):
                    entry_price = float(row["entry_price"] or 0)
                    fb = [
                        t for t in trips
                        if t.get("direction") == row["direction"]
                        and abs(t["start"] - entry_ms) <= FALLBACK_DRIFT_MS
                        and abs(t["end"] - exit_ms) <= FALLBACK_DRIFT_MS
                        and entry_price > 0
                        and abs(float(t.get("entry_price") or 0) - entry_price)
                        <= 0.005 * entry_price
                    ]
                    if len(fb) == 1:
                        cycle = fb
                        ok = True
                        via = "fallback entry/exit"
                if not ok:
                    no_trip += 1
                    _log(args.log, f"#{tid} {symbol} skip: привязка к round-trip'у неоднозначна (граница цикла не собрана)")
                    continue

                fees = sum(t["commission"] for t in cycle)
                fund = sum(
                    v for ts, v in funding
                    if cycle[0]["start"] <= ts <= cycle[-1]["end"]
                )
                net = sum(t["realized"] for t in cycle) + fees + fund

                old_pnl = float(row["pnl"])
                diff = net - old_pnl
                if abs(diff) <= TOLERANCE_USD:
                    continue

                matched += 1
                old_fee = row["commission"]
                new_fee = round(abs(fees), 6)
                _log(args.log, (
                    f"#{tid} {symbol} {row['direction']} d={row['chain_depth']} "
                    f"{row['exit_reason']} pnl {old_pnl:+.4f} -> {net:+.4f} (diff {diff:+.4f}) "
                    f"fee {old_fee} -> {new_fee} "
                    f"[{via} {_fmt_ms(cycle[0]['start'])}..{_fmt_ms(cycle[-1]['end'])} "
                    f"trips={len(cycle)} rp={sum(t['realized'] for t in cycle):+.4f} "
                    f"fee={fees:+.4f} funding={fund:+.4f}]"
                ))
                if not args.apply:
                    continue

                sets = ["pnl=?", "commission=?"]
                params: list = [round(net, 4), new_fee]
                if row["cycle_pnl"] is not None and abs(float(row["cycle_pnl"]) - old_pnl) <= CYCLE_MATCH_USD:
                    sets.append("cycle_pnl=?")
                    params.append(round(net, 4))
                params.extend([tid, round(old_pnl, 4)])
                sql = (
                    f"UPDATE trades SET {', '.join(sets)} "
                    "WHERE id=? AND ROUND(pnl, 4) = ?"
                )
                try:
                    with db:
                        n = db.execute(sql, params).rowcount
                    if n == 1:
                        updated += 1
                    else:
                        _log(args.log, f"#{tid} skip write: строку изменил другой процесс (guard pnl={old_pnl:+.4f})")
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    _log(args.log, f"#{tid} write error: {e!r}")
    finally:
        try:
            cli.close_connection()
        except Exception:
            pass
        db.close()

    _log(args.log, (
        f"run done: checked={checked} mismatched={matched} updated={updated} "
        f"no_trip={no_trip} errors={errors} weight={weight_spent} mode={mode_label}"
    ))
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
