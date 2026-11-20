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

/**
 * Удаляет gate-файлы <prefix>.* старше maxAgeHours.
 *
 * Живые процессы обновляют свой файл каждую секунду (пишут каждую итерацию
 * цикла), поэтому файл старше 24ч — это мусор от завершённого процесса.
 * Без очистки старый файл с ban_until_ms > 0 мог бы заблокировать весь флот
 * после рестарта: readBanFromDir агрегирует максимум ban_until_ms по ВСЕМ
 * <prefix>.* файлам, независимо от возраста.
 */
export function cleanupStaleGateFiles(dir: string, prefix: string, maxAgeHours = 24): number {
  let removed = 0;
  let names: string[];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return removed;
  }
  const cutoff = Date.now() - maxAgeHours * 3600 * 1000;
  for (const name of names) {
    if (!name.startsWith(prefix)) continue;
    try {
      const st = fs.statSync(path.join(dir, name));
      if (st.mtimeMs < cutoff) {
        fs.unlinkSync(path.join(dir, name));
        removed++;
      }
    } catch {
      /* битый/закрытый файл — пропускаем */
    }
  }
  return removed;
}
