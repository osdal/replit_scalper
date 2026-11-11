import fs from "fs";
import path from "path";
import { BOT_DIR, BOT_ENV } from "../botPaths";
import { GridOrderError } from "./gridErrors";
import { parseBanUntilMs, readBanFromDir, type BanState } from "./banFiles";

/**
 * Единый барьер «идёт бан IP» для ВСЕХ REST-запросов API-сервера к Binance.
 *
 * Зачем: python-боты уже умеют блокировать себя через файлы
 * `bot/data/binance_gate_<env>.json.<pid>` (см. bot/binance_gate.py), но
 * api-server (grid-engine, /drawdown, дневной риск) ходил на биржу напрямую.
 * Каждый такой запрос во время бана возвращал 418/-1003 и ПРОДЛЕВАЛ бан
 * (наблюдали +6 минут за один цикл), поэтому флот не мог дождаться снятия.
 *
 * Решение: перед каждым запросом читаем те же gate-файлы; при активном бане
 * бросаем ошибку БЕЗ сети. Пойманный -1003 записываем в свой gate-файл, чтобы
 * python-боты тоже узнали о продлении (они агрегируют все `<prefix>.*`).
 *
 * Файлы env-специфичные (`_testnet` / `_live`): оба стека монтируют один ./data,
 * и тестнет-бан не должен блокировать live-торговлю.
 */

export type { BanState } from "./banFiles";
export { parseBanUntilMs, readBanFromDir } from "./banFiles";

/** Каталог, где python-боты пишут gate-файлы (cwd бота = BOT_DIR). */
export const GATE_DIR = path.join(BOT_DIR, "data");
export const GATE_PREFIX = `binance_gate_${BOT_ENV}.json`;

/** Файл этого процесса: `.ts` не совпадёт с `<pid>`, но матчится python-глобом. */
const MY_FILE = path.join(GATE_DIR, `${GATE_PREFIX}.ts`);

/** Фолбэк, если -1003 пришёл без «banned until» (типично для testnet). */
const FALLBACK_BAN_SEC = Number(process.env.BINANCE_BAN_FALLBACK_SEC || 30) || 30;
/** Кэш чтения каталога: gate обновляется не чаще раза в секунду. */
const READ_CACHE_MS = 2000;

let cache: { ts: number; state: BanState } = {
  ts: 0,
  state: { banUntilMs: 0, source: null },
};

/** Активный бан (с кэшем 2с). */
export function currentBan(): BanState {
  const now = Date.now();
  if (cache.ts && now - cache.ts < READ_CACHE_MS) {
    // Кэш не должен «залипать» после истечения бана.
    if (cache.state.banUntilMs <= now) return { banUntilMs: 0, source: null };
    return cache.state;
  }
  const state = readBanFromDir(GATE_DIR, GATE_PREFIX, now);
  cache = { ts: now, state };
  return state.banUntilMs > now ? state : { banUntilMs: 0, source: null };
}

export function isBanned(now: number = Date.now()): boolean {
  return currentBan().banUntilMs > now;
}

/**
 * Пишем бан в общий gate-файл, чтобы его увидели python-боты (и следующий
 * preflight этого процесса). `ts` в секундах — формат binance_gate.py.
 */
export function recordBan(banUntilMs: number): void {
  try {
    fs.mkdirSync(GATE_DIR, { recursive: true });
    const state = {
      ban_until_ms: Math.max(0, Math.floor(banUntilMs)),
      used_weight_1m: 0,
      local_weight_1m: 0,
      ts: Date.now() / 1000,
    };
    fs.writeFileSync(MY_FILE, JSON.stringify(state));
    cache = { ts: Date.now(), state: { banUntilMs: state.ban_until_ms, source: path.basename(MY_FILE) } };
  } catch {
    /* fail-open: не смогли записать — не блокируем торговлю из-за этого */
  }
}

/**
 * Единая точка: вызывается перед каждым HTTP-запросом к Binance.
 * Бросает GridOrderError без сети, если бан активен.
 */
export function assertNotBanned(what: string): void {
  const ban = currentBan();
  if (ban.banUntilMs <= Date.now()) return;
  const left = Math.max(1, Math.ceil((ban.banUntilMs - Date.now()) / 1000));
  throw new GridOrderError(
    503,
    `Binance ban active (${left}s left, until ${ban.banUntilMs}); ${what} skipped to avoid extending it`,
  );
}

/** База Binance текущего стека — для диагностики в /status. */
export function binanceBaseUrl(): string {
  const testnet = String(process.env.BINANCE_TESTNET || "").toLowerCase() === "true";
  return testnet ? "https://testnet.binancefuture.com" : "https://fapi.binance.com";
}

/** Сбрасывает кэш (тесты/ручной сброс). */
export function resetBanCache(): void {
  cache = { ts: 0, state: { banUntilMs: 0, source: null } };
}

/** Фолбэк-бан для ответов без явного «banned until». */
export function fallbackBanUntil(now: number = Date.now()): number {
  return now + FALLBACK_BAN_SEC * 1000;
}
