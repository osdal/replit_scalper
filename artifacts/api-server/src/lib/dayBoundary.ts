/**
 * Границы «торгового дня» в заданной таймзоне (по умолчанию Europe/Kyiv).
 *
 * Вынесено отдельным модулем без зависимостей от БД/бинарника бота, чтобы:
 *  - дневной риск (`dailyRisk.ts`) и тесты использовали ОДНУ функцию;
 *  - можно было покрыть тестами переход на летнее/зимнее время.
 *
 * Почему не UTC: пользователь живёт по Киеву, и «следующий день» должен
 * наступать в 00:00 местного времени, а не в 02:00–03:00.
 */

/** Таймзона «дня». Переопределяется через DAILY_RESET_TZ (тесты/нестандарт). */
export const DAILY_TZ = (process.env.DAILY_RESET_TZ || "Europe/Kyiv").trim() || "Europe/Kyiv";

const tzFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: DAILY_TZ,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

export type TzParts = { y: number; m: number; d: number; h: number; mi: number; s: number };

function tzParts(date: Date): TzParts {
  const parts = tzFmt.formatToParts(date);
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? 0);
  return { y: get("year"), m: get("month"), d: get("day"), h: get("hour"), mi: get("minute"), s: get("second") };
}

/** Смещение таймзоны в минутах для конкретного момента (с учётом DST). */
export function tzOffsetMinutes(date: Date): number {
  const p = tzParts(date);
  const asUtc = Date.UTC(p.y, p.m - 1, p.d, p.h, p.mi, p.s);
  const base = Math.floor(date.getTime() / 1000) * 1000; // без миллисекунд
  return Math.round((asUtc - base) / 60000);
}

/** Ключ дня: YYYY-MM-DD в DAILY_TZ. */
export function dayKey(now: Date = new Date()): string {
  const p = tzParts(now);
  const mm = String(p.m).padStart(2, "0");
  const dd = String(p.d).padStart(2, "0");
  return `${p.y}-${mm}-${dd}`;
}

/** Начало текущего дня в DAILY_TZ, выраженное в UTC ISO (сравнимо с exit_time). */
export function dayStartUtcIso(now: Date = new Date()): string {
  const p = tzParts(now);
  const midnightUtcGuess = Date.UTC(p.y, p.m - 1, p.d, 0, 0, 0);
  // Смещение берём на ПОЛНОЧЬ, а не «сейчас»: в день перехода на летнее время
  // смещение после 04:00 уже новое и дало бы 23:00/01:00.
  let off = tzOffsetMinutes(new Date(midnightUtcGuess));
  let start = midnightUtcGuess - off * 60000;
  const offAtStart = tzOffsetMinutes(new Date(start));
  if (offAtStart !== off) {
    off = offAtStart;
    start = midnightUtcGuess - off * 60000;
  }
  return new Date(start).toISOString();
}
