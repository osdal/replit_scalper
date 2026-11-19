import path from "path";
import fs from "fs";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// artifacts/api-server/src -> корень проекта
export const PROJECT_ROOT = path.resolve(__dirname, "..", "..", "..");

/** Окружение бота: testnet (по умолчанию) или live. Разделяет конфиги/стейт/логи/локи. */
export const BOT_ENV = (process.env.BOT_ENV || "testnet").trim().toLowerCase() || "testnet";

function resolveDir(value: string | undefined, fallback: string): string {
  if (!value) return fallback;
  return path.isAbsolute(value) ? value : path.join(PROJECT_ROOT, value);
}

/** Каталог кода бота (общий для окружений). */
export const BOT_DIR = resolveDir(process.env.BOT_DIR, path.join(PROJECT_ROOT, "bot"));

/** Каталог конфигов текущего окружения. */
export const BOT_CONFIG_DIR = resolveDir(
  process.env.BOT_CONFIG_DIR,
  path.join(BOT_DIR, "configs", BOT_ENV),
);

/** Каталог логов и stdout-логов ботов текущего окружения. */
export const BOT_LOG_DIR = path.join(BOT_DIR, "logs", BOT_ENV);

/** Безопасный stem символа: BTCUSDT -> btc. */
export function symbolStem(symbol: string): string {
  return String(symbol).replace(/USDT$/i, "").replace(/[^A-Za-z0-9]/g, "").toLowerCase();
}

/** Имя файла конфига: config_btc.yaml. */
export function configFileName(symbol: string): string {
  return `config_${symbolStem(symbol)}.yaml`;
}

/** Абсолютный путь к конфигу текущего окружения (уникален для env). */
export function configPath(symbol: string): string {
  return path.join(BOT_CONFIG_DIR, configFileName(symbol));
}

/** Имя файла состояния: state_<env>_<symbol>.json. */
export function stateFileName(symbol: string): string {
  return `state_${BOT_ENV}_${symbolStem(symbol)}.json`;
}

/** Абсолютный путь к файлу состояния текущего окружения. */
export function statePath(symbol: string): string {
  return path.join(BOT_DIR, stateFileName(symbol));
}

/** Префикс lock-файлов текущего окружения. */
export function lockPrefix(): string {
  return `bot.lock.${BOT_ENV}.`;
}

/**
 * Стабильный ключ инстанса бота. В БД это bots.bot_key; если его нет
 * (старые записи) — lower(symbol). Для клиента №1 совпадает с lower(symbol),
 * поэтому имена state/lock-файлов не меняются.
 */
export function botKeyOf(symbol: string, botKey?: string | null): string {
  return String(botKey || symbol).trim().toLowerCase();
}

/**
 * Конфиг инстанса. Приоритет — per-client файл
 * `configs/<env>/clients/<clientId>/<bot_key>.yaml`; если его нет (клиент №1),
 * используется прежний `config_<stem>.yaml`.
 */
export function configPathForBot(bot: {
  symbol: string;
  bot_key?: string | null;
  client_id?: number | null;
}): string {
  if (bot.client_id != null && bot.bot_key) {
    const perClient = path.join(
      BOT_CONFIG_DIR,
      "clients",
      String(bot.client_id),
      `${bot.bot_key}.yaml`,
    );
    if (fs.existsSync(perClient)) return perClient;
  }
  return configPath(bot.symbol);
}

/** State-файл инстанса. Python пишет его по BOT_KEY, поэтому имя должно совпадать. */
export function statePathForBot(symbol: string, botKey?: string | null): string {
  return path.join(BOT_DIR, `state_${BOT_ENV}_${botKeyOf(symbol, botKey)}.json`);
}

/**
 * Проверка согласованности окружения и биржи. Бросает, если BOT_ENV и
 * BINANCE_TESTNET противоречат друг другу — защита от запуска live на тестнет-ключах
 * и наоборот.
 */
export function assertEnvMatchesExchange(): void {
  const testnet = (process.env.BINANCE_TESTNET || "false").toLowerCase() === "true";
  if (BOT_ENV === "live" && testnet) {
    throw new Error("BOT_ENV=live but BINANCE_TESTNET=true — refusing to start (env mismatch)");
  }
  if (BOT_ENV === "testnet" && !testnet) {
    throw new Error("BOT_ENV=testnet but BINANCE_TESTNET!=true — refusing to start (env mismatch)");
  }
}
