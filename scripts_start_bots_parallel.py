"""Параллельный запуск всех ботов (без остановки). Только старт по текущему списку.

Остановку выполняет PowerShell-обёртка scripts_restart_all.ps1 (не этот скрипт),
т.к. stop-all делает taskkill python.exe, который убил бы и сам этот процесс.
"""
import asyncio
import requests

API = "http://localhost:5000/api"


def get_symbols():
    return sorted(b["symbol"] for b in requests.get(f"{API}/bots", timeout=15).json())


async def start_one(sym):
    try:
        r = requests.post(f"{API}/bots/{sym}/start", timeout=30)
        return (sym, True, "")
    except Exception as e:
        return (sym, False, str(e)[:80])


async def main():
    syms = get_symbols()
    print(f"Запускаю {len(syms)} ботов параллельно...", flush=True)
    results = await asyncio.gather(*(start_one(s) for s in syms))
    ok = sum(1 for _, o, _ in results if o)
    print(f"Запущено: {ok}/{len(syms)}", flush=True)
    for sym, o, m in results:
        if not o:
            print(f"  FAIL {sym}: {m}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
