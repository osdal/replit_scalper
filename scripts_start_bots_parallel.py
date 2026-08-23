"""Параллельный запуск всех ботов (без остановки). Только старт по текущему списку.

Остановку выполняет PowerShell-обёртка scripts_restart_all.ps1 (не этот скрипт),
т.к. stop-all делает taskkill python.exe, который убил бы и сам этот процесс.

Запускает ботов ПООЧЕРЕДНО с паузой между стартами (stagger). Это критично:
если запустить все 35 ботов разом, каждый качает klines для прогрева, суммарно
превышается rate-limit Binance и IP уходит в бан (-1003). Пауза между стартами
размазывает запросы и не даёт бану сработать.
"""
import asyncio
import time
import requests

API = "http://localhost:5000/api"

# Пауза между стартами ботов (сек). Чем больше ботов — тем нужнее пауза.
# 3-5 сек достаточно, чтобы не упираться в лимит Binance.
START_STAGGER_SEC = 4.0


def get_symbols():
    return sorted(b["symbol"] for b in requests.get(f"{API}/bots", timeout=15).json())


async def start_one(sym):
    try:
        r = requests.post(f"{API}/bots/{sym}/start", timeout=60)
        return (sym, True, "")
    except Exception as e:
        return (sym, False, str(e)[:80])


async def main():
    syms = get_symbols()
    print(f"Запускаю {len(syms)} ботов поочерёдно с паузой {START_STAGGER_SEC}s...", flush=True)
    ok = 0
    results = []
    for i, sym in enumerate(syms, 1):
        sym_id, ok_flag, msg = await start_one(sym)
        results.append((sym_id, ok_flag, msg))
        if ok_flag:
            ok += 1
        print(f"  [{i}/{len(syms)}] {sym}: {'OK' if ok_flag else 'FAIL ' + msg}", flush=True)
        if i < len(syms):
            await asyncio.sleep(START_STAGGER_SEC)
    print(f"Запущено: {ok}/{len(syms)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
