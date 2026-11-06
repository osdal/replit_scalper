/**
 * Баланс кошелька USDT-M Futures для процентных лимитов риска.
 *
 * Зачем отдельный модуль: и гейт входов (`routes/trading.ts`, дневной лимит),
 * и kill-switch (`routes/live.ts`) считают лимит как процент от ТЕКУЩЕГО
 * баланса счёта. Один кэш на процесс, чтобы не дёргать /fapi/v2/account
 * несколько раз за цикл (вес 5, но при 40 ботах лучше кэшировать).
 *
 * Поведение при сбое: отдаём последнее успешное значение, иначе DEPOSIT_USD
 * (по умолчанию 10). Лимит не должен «исчезать» из-за сетевой ошибки —
 * иначе риск-контроль молча отключается (fail-safe, а не fail-open).
 */
import { binanceRequest, syncBinanceTime } from "../grid-orders-lib";

/** Сколько живёт закэшированный баланс. */
const BALANCE_TTL_MS = 60_000;

type Cache = { value: number; ts: number };
let cache: Cache = { value: 0, ts: 0 };
let inflight: Promise<number> | null = null;
let lastAttemptMs = 0;
/** Минимальный интервал между попытками при сбоях (антишторм). */
const RETRY_COOLDOWN_MS = 15_000;

/** Fallback-депозит из env (USD), если Binance недоступен. */
export function fallbackDepositUsd(): number {
  const n = Number(process.env.DEPOSIT_USD || "10");
  return Number.isFinite(n) && n > 0 ? n : 10;
}

async function fetchWalletBalanceUsd(): Promise<number> {
  await syncBinanceTime();
  const raw = await binanceRequest("GET", "/fapi/v2/account", {});
  const wallet = Number(raw?.totalWalletBalance ?? 0) || 0;
  return wallet > 0 ? wallet : 0;
}

/**
 * Текущий `totalWalletBalance` (USDT) с кэшем 60с.
 *
 * @param fresh — игнорировать кэш (например, после закрытия сделок).
 */
export async function getWalletBalanceUsd(opts?: { fresh?: boolean }): Promise<number> {
  const now = Date.now();
  if (!opts?.fresh && cache.value > 0 && now - cache.ts < BALANCE_TTL_MS) {
    return cache.value;
  }
  // Не долбим Binance при сбоях: не чаще раза в 15с, отдаём последнее/депозит.
  if (!opts?.fresh && now - lastAttemptMs < RETRY_COOLDOWN_MS) {
    return cache.value > 0 ? cache.value : fallbackDepositUsd();
  }
  if (inflight) return inflight;

  lastAttemptMs = now;
  inflight = (async () => {
    try {
      const value = await fetchWalletBalanceUsd();
      if (value > 0) {
        cache = { value, ts: Date.now() };
        return value;
      }
    } catch (e) {
      console.error("[balance] wallet balance fetch failed:", String(e));
    }
    // Fail-safe: последнее известное значение, иначе депозит из env.
    return cache.value > 0 ? cache.value : fallbackDepositUsd();
  })();

  try {
    return await inflight;
  } finally {
    inflight = null;
  }
}

/** Сбросить кэш (тесты/ручной сброс после крупных операций). */
export function resetWalletBalanceCache(): void {
  cache = { value: 0, ts: 0 };
}
