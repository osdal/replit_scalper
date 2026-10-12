#!/usr/bin/env python3
"""Смоук-тест API для дашборда: только GET, ничего не мутирует.

Зачем: после деплоя api-server нужно убедиться, что все эндпоинты, которые
дергает дашборд (artifacts/dashboard/src/hooks/useApi.ts), отвечают так же,
как до деплоя. Реальный случай: trades.ts с `eq(col, null)` вернул
`{"trades":[],"total":5240}` — total корректен, массив пуст. Дашборд
молча показал пустые вкладки Trades и PnL Chart. Проверка
"len(trades) > 0 при наличии строк в БД" ловит это за секунду.

ВАЖНО: здесь ТОЛЬКО чтение. Никаких DELETE /trades, /live/close-all,
/trading/close-and-reset, /bots/stop-all и прочих мутаций — тест обязан быть
безопасен для продакшена в любой момент.

Использование:
  python3 smoke_api.py --base http://localhost:5001 --save baseline.json
  python3 smoke_api.py --base http://localhost:5001 --baseline baseline.json
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

# ── Эндпоинты, которые дёргает дашборд (только чтение) ──────────────────
# (name, path, kind, expect)

# Символ, на котором проверяем /trading/check и фильтр /trades?symbol=
# (должен присутствовать среди ботов данного стека).
CHECK_SYMBOL = "HBARUSDT"

PROBES = [
    ("bots",         "/bots",                        "bots",   None),
    ("trades",       "/trades?limit=1000",           "trades", None),
    ("trades_first5","/trades?limit=5",              "trades", None),
    ("stats",        "/trades/stats",                "stats",  None),
    ("recovery_cfg", "/recovery/config",             "object", None),
    ("daily_loss",   "/live/daily-loss",             "object", None),
    ("drawdown",     "/live/drawdown",               "object", None),
    ("relay_stats",  "/signals/stats",               "object", None),
    ("healthz",      "/healthz",                     "any",    None),
    # Информационная проверка лимита (reserve=false — слот НЕ занимается).
    # Именно этот эндпоинт блокировал reverse-ноги старой версией trading.ts.
    ("trade_check",  "/trading/check",               "object", None),
    # Фильтр по символу. Два .where() подряд в drizzle теряли первое условие, и
    # /trades?symbol=X отдавал сделки всех символов. Боты в reconciler'е
    # запрашивали свои сделки, получали чужие и закрывали их ценой своего
    # символа — это и сформировало фиктивный убыток, обнуливший дневной лимит.
    ("trades_sym",   f"/trades?symbol={CHECK_SYMBOL}&limit=20", "trades_sym", None),
]

MUTATING_HINT = (
    "Только GET. POST /trading/check запрашивается с reserve=false — "
    "слот не занимается, состояние не меняется."
)


def fetch(base: str, path: str, method: str = "GET", body=None, timeout: int = 20):
    url = base.rstrip("/") + "/api" + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            ct = r.headers.get("Content-Type", "")
            return r.status, ct, raw
    except urllib.error.HTTPError as e:
        return e.code, "", e.read()
    except Exception as e:
        return None, "", str(e).encode()


def parse(raw: bytes, ct: str):
    if not raw:
        return None
    if "json" in ct or raw[:1] in (b"{", b"["):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def sig_trades(d, name):
    """Сигнатура /trades: главное — НЕПУСТОЙ массив при наличии строк в БД."""
    if not isinstance(d, dict) or "trades" not in d:
        return {"ok": False, "why": "нет ключа trades в ответе"}
    tr = d.get("trades")
    total = d.get("total")
    if not isinstance(tr, list):
        return {"ok": False, "why": "trades не массив"}
    s = {
        "count": len(tr),
        "total": total,
        "statuses": {},
        "rejected_visible": 0,
        "non_null_reject_reason": 0,
        "fields": sorted(tr[0].keys()) if tr else [],
        "run": None,
    }
    if tr:
        s["statuses"] = {}
        for x in tr:
            s["statuses"][str(x.get("status"))] = s["statuses"].get(str(x.get("status")), 0) + 1
        s["rejected_visible"] = s["statuses"].pop("rejected", 0)
        s["non_null_reject_reason"] = sum(1 for x in tr if x.get("reject_reason") is not None)
        s["open"] = sum(1 for x in tr if x.get("is_open"))
        s["closed_with_pnl"] = sum(
            1 for x in tr if not x.get("is_open") and x.get("pnl") is not None
        )
        s["cum_pnl"] = round(
            sum(float(x["pnl"]) for x in tr if x.get("pnl") is not None), 6
        )
    return s


def sig_trades_sym(d, name):
    """Сигнатура /trades?symbol=X: главное — что ВСЕ строки именно этого символа.

    Баг: два .where() подряд в drizzle-orm 0.45.2 теряли ПЕРВОЕ условие, и
    запрос с symbol возвращал сделки всех символов. Дальше боты в reconciler'е
    закрывали чужие открытые сделки ценой своего символа.
    """
    if not isinstance(d, dict) or "trades" not in d:
        return {"ok": False, "why": "нет ключа trades в ответе"}
    tr = d.get("trades")
    if not isinstance(tr, list):
        return {"ok": False, "why": "trades не массив"}
    syms = {}
    for x in tr:
        syms[str(x.get("symbol"))] = syms.get(str(x.get("symbol")), 0) + 1
    other = {k: v for k, v in syms.items() if k != CHECK_SYMBOL}
    return {
        "ok": not other,
        "count": len(tr),
        "symbols": syms,
        "foreign_rows": sum(other.values()),
        "why": None if not other else f"вернулись чужие символы: {other}",
    }


def sig_bots(d, name):
    if not isinstance(d, list):
        return {"ok": False, "why": "/bots не массив"}
    s = {
        "ok": True,
        "count": len(d),
        "running": sum(1 for b in d if b.get("is_running")),
        "with_position": sum(1 for b in d if b.get("position")),
        "fields": sorted(d[0].keys()) if d else [],
        "symbols_hash": None,
    }
    return s


def sig_stats(d, name):
    if not isinstance(d, list):
        return {"ok": False, "why": "/trades/stats не массив"}
    s = {
        "ok": True,
        "symbols": len(d),
        "fields": sorted(d[0].keys()) if d else [],
        "sum_trades": sum(int(x.get("total") or 0) for x in d),
    }
    return s


def sig_object(d, name):
    if d is None:
        return {"ok": False, "why": "не JSON"}
    if isinstance(d, list):
        return {"ok": True, "type": "list", "len": len(d), "keys": None}
    return {"ok": True, "type": "object", "keys": sorted(d.keys())}


def collect(base: str):
    out = {}
    for name, path, kind, _ in PROBES:
        if name == "trade_check":
            status, ct, raw = fetch(base, path, method="POST",
                                    body={"symbol": CHECK_SYMBOL, "reserve": False})
        else:
            status, ct, raw = fetch(base, path)
        d = parse(raw, ct)
        rec = {
            "path": path,
            "http": status,
            "json": d is not None,
        }
        if kind == "trades":
            rec.update(sig_trades(d, name))
        elif kind == "trades_sym":
            rec.update(sig_trades_sym(d, name))
        elif kind == "bots":
            rec.update(sig_bots(d, name))
        elif kind == "stats":
            rec.update(sig_stats(d, name))
        elif kind == "any":
            rec["ok"] = status is not None and 200 <= status < 400
        else:
            rec.update(sig_object(d, name))
        # Троттлинг-независимый снимок значений для глазного сравнения.
        if kind == "any":
            rec["value"] = raw[:200].decode("utf-8", "replace")
        elif isinstance(d, dict):
            rec["value"] = {k: d[k] for k in list(d)[:6]}
        elif isinstance(d, list):
            rec["value"] = f"list[{len(d)}]"
        out[name] = rec
    return out


# ── Инварианты: то, что ДОЛЖНО выполняться всегда ───────────────────────
def invariants(cur):
    """Проверки, которые не зависят от baseline (поймали бы trades-баг)."""
    fails = []
    tr = cur.get("trades", {})
    if tr.get("http") != 200:
        fails.append(f"/trades http={tr.get('http')}")
    elif not tr.get("json"):
        fails.append("/trades: ответ не JSON")
    elif tr.get("count", 0) == 0 and tr.get("total", 0) > 0:
        fails.append(
            f"/trades: массив ПУСТ ({tr.get('count')}), но total={tr.get('total')} "
            f"— фильтр режет все строки (регрессия eq(col,null))"
        )
    elif tr.get("rejected_visible", 0) != 0:
        fails.append(
            f"/trades: API отдаёт {tr.get('rejected_visible')} rejected — "
            f"фильтр на сервере должен был их убрать"
        )
    elif tr.get("non_null_reject_reason", 0) != 0:
        fails.append(
            f"/trades: {tr.get('non_null_reject_reason')} строк с non-null "
            f"reject_reason — фильтр reject_reason не применился"
        )

    st = cur.get("stats", {})
    if st.get("http") != 200 or not st.get("json"):
        fails.append(f"/trades/stats http={st.get('http')} json={st.get('json')}")

    # Фильтр по символу: чужие символы в ответе = регрессия двух .where() подряд,
    # из-за которой боты закрывали чужие сделки ценой своего символа.
    ts = cur.get("trades_sym", {})
    if ts.get("http") != 200 or not ts.get("json"):
        fails.append(f"/trades?symbol=... http={ts.get('http')} json={ts.get('json')}")
    elif ts.get("foreign_rows"):
        fails.append(
            f"/trades?symbol={CHECK_SYMBOL}: вернулись {ts.get('foreign_rows')} строк "
            f"ЧУЖИХ символов {ts.get('symbols')} — фильтр symbol не применяется "
            f"(регрессия: два .where() подряд теряют первое условие)"
        )

    b = cur.get("bots", {})
    if b.get("http") != 200 or not b.get("json"):
        fails.append(f"/bots http={b.get('http')} json={b.get('json')}")
    elif b.get("count", 0) == 0:
        fails.append("/bots: массив пуст")

    tc = cur.get("trade_check", {})
    if tc.get("http") != 200:
        fails.append(f"/trading/check http={tc.get('http')}")
    else:
        v = tc.get("value") or {}
        if isinstance(v, dict) and "allowed" not in v:
            fails.append(f"/trading/check: нет поля allowed -> {list(v)}")
        if isinstance(v, dict) and "own_slot" not in v:
            fails.append(
                "/trading/check: нет поля own_slot — похоже, задеплоена СТАРАЯ "
                "версия trading.ts (без slotReservations)"
            )

    for n in ("healthz", "recovery_cfg", "daily_loss", "drawdown", "relay_stats"):
        r = cur.get(n, {})
        if r.get("http") not in (None, 200):
            fails.append(f"/{r.get('path')} http={r.get('http')}")
        if r.get("json") is False and r.get("http") == 200:
            fails.append(f"/{r.get('path')}: 200 но не JSON")
    return fails


def diff(base, cur):
    """Сравнение с baseline: что изменилось в форме ответов."""
    changes = []
    for name, rec in cur.items():
        b = base.get(name)
        if not b:
            continue
        if b.get("http") != rec.get("http"):
            changes.append(f"{name}: http {b.get('http')} -> {rec.get('http')}")
        for key in ("count", "total", "symbols", "running", "with_position",
                    "rejected_visible", "non_null_reject_reason", "keys", "fields"):
            if key in b and key in rec and b.get(key) != rec.get(key):
                bv, cv = b.get(key), rec.get(key)
                # count/total/running меняются легитимно — помечаем как INFO
                tag = "INFO" if key in ("count", "total", "running", "with_position") else "CHANGED"
                changes.append(f"{name}.{key}: {tag} {bv!r} -> {cv!r}")
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:5001")
    ap.add_argument("--save", help="сохранить сигнатуру как baseline")
    ap.add_argument("--baseline", help="сравнить с baseline json")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    cur = collect(a.base)
    fails = invariants(cur)
    changes = []
    if a.baseline:
        try:
            base = json.load(open(a.baseline, encoding="utf-8"))
            changes = diff(base.get("probes", {}), cur)
        except Exception as e:
            print(f"!! baseline не прочитан: {e}")
            return 2

    if not a.quiet:
        print(f"=== smoke API @ {a.base} ===")
        print(f"({MUTATING_HINT})\n")
        for name, rec in cur.items():
            extra = ""
            for k in ("count", "total", "running", "symbols", "cum_pnl", "keys"):
                if k in rec:
                    extra += f" {k}={rec[k]!r}"
            print(f"  {'OK ' if rec.get('ok', rec.get('http')==200) else 'BAD'} "
                  f"/{rec.get('path'):<28} http={rec.get('http')}{extra}")
        print()

    if changes:
        print("=== diff vs baseline ===")
        for c in changes:
            print("  " + c)
        print()

    if fails:
        # Вывод ASCII-безопасен: консоль Windows может быть в cp1251, где
        # кириллица и спецсимволы (✗, —) роняют print с UnicodeEncodeError.
        def _safe(s: str) -> str:
            try:
                s.encode(sys.stdout.encoding or "ascii")
                return s
            except (UnicodeEncodeError, LookupError):
                return s.encode("ascii", "replace").decode("ascii")
        print("=== FAIL (invariants) ===")
        for f in fails:
            print("  x " + _safe(f))
        print(f"\nRESULT: FAIL ({len(fails)})")
        return 1

    print(f"ИТОГ: PASS" + (f" (diff: {len(changes)} info-изменений)" if changes else ""))
    if a.save:
        json.dump({"base": a.base, "probes": cur}, open(a.save, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"baseline сохранён: {a.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
