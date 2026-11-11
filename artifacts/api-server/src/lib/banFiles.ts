import fs from "fs";
import path from "path";

/**
 * Чистые функции чтения gate-файлов бана Binance (без зависимостей от env).
 *
 * Вынесены из `binanceBan.ts`, чтобы тесты могли импортировать их напрямую
 * (`node --experimental-strip-types` не резолвит extensionless-импорты вроде
 * `../botPaths`, которые использует основной модуль).
 */

/** Страховка от мусора/битых таймстампов: баны дальше 24ч игнорируем. */
export const MAX_BAN_AHEAD_MS = 24 * 3600 * 1000;

export type BanState = { banUntilMs: number; source: string | null };

/**
 * Максимальный бан по файлам `<prefix>*` в каталоге `dir`.
 *
 * Бан не зависит от возраста файла: срок задан в `ban_until_ms`. Иначе во
 * время бана процесс не обновляет файл и все «забывали» бы бан, снова били в
 * сеть и продлевали его.
 */
export function readBanFromDir(dir: string, prefix: string, now: number = Date.now()): BanState {
  let best: BanState = { banUntilMs: 0, source: null };
  let names: string[];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return best;
  }
  for (const name of names) {
    if (!name.startsWith(prefix)) continue;
    try {
      const raw = JSON.parse(fs.readFileSync(path.join(dir, name), "utf8")) as Record<string, unknown>;
      const ban = Number(raw?.ban_until_ms ?? 0) || 0;
      if (ban > best.banUntilMs && ban <= now + MAX_BAN_AHEAD_MS) {
        best = { banUntilMs: ban, source: name };
      }
    } catch {
      /* битый/недописанный файл — пропускаем */
    }
  }
  return best;
}

/** Разбирает «banned until <ms>» из текста ответа Binance. */
export function parseBanUntilMs(text: string): number {
  const m = /banned until (\d{10,})/i.exec(text);
  return m ? Number(m[1]) : 0;
}
