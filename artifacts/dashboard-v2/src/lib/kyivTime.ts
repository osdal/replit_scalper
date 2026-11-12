/**
 * Время в UI — всегда по Киеву, независимо от таймзоны браузера/сервера.
 *
 * Почему нужен парсер: боты пишут `entry_time`/`exit_time` как
 * `datetime.utcnow().isoformat()` — это UTC БЕЗ суффикса `Z`
 * (например `2026-11-12T11:47:08.123456`). `new Date(...)` в браузере считает
 * такую строку локальным временем, из-за чего 11:47 UTC показывалось как
 * 11:47 по Киеву (реально это было 13:47). Часть записей приходит уже с `Z`
 * (binance-sync, API-патчи), поэтому парсер поддерживает оба варианта.
 */

export const KYIV_TZ = "Europe/Kyiv";

/** ISO (UTC с Z без Z или с зоной) → Date. Naive-строки трактуются как UTC. */
export function parseUtc(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  let s = String(iso).trim();
  if (!s) return null;
  // "2026-11-12 11:47:08" → "2026-11-12T11:47:08"
  if (s.includes(" ") && !s.includes("T")) s = s.replace(" ", "T");
  // Нет явной зоны → это UTC от бота.
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(s)) s += "Z";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

const PARTS_FMT = new Intl.DateTimeFormat("ru-RU", {
  timeZone: KYIV_TZ,
  day: "2-digit",
  month: "2-digit",
  year: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** `12.11.26 13:47` — дата/время по Киеву. */
export function fmtKyiv(iso: string | null | undefined): string {
  const d = parseUtc(iso);
  if (!d) return "—";
  const parts = PARTS_FMT.formatToParts(d);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("day")}.${get("month")}.${get("year")} ${get("hour")}:${get("minute")}`;
}

/** `12.11 13:47` — дата без года и время по Киеву. */
export function fmtKyivShort(iso: string | null | undefined): string {
  const d = parseUtc(iso);
  if (!d) return "—";
  const parts = new Intl.DateTimeFormat("ru-RU", {
    timeZone: KYIV_TZ,
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(d);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("day")}.${get("month")} ${get("hour")}:${get("minute")}`;
}

/** `13:47` — только время по Киеву. */export function fmtKyivTime(iso: string | null | undefined): string {
  const d = parseUtc(iso);
  if (!d) return "—";
  const parts = new Intl.DateTimeFormat("ru-RU", {
    timeZone: KYIV_TZ,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(d);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("hour")}:${get("minute")}`;
}

/** Полная дата-время по Киеву: `12.11.2026 13:47:08`. */
export function fmtKyivFull(iso: string | null | undefined): string {
  const d = parseUtc(iso);
  if (!d) return "—";
  const parts = new Intl.DateTimeFormat("ru-RU", {
    timeZone: KYIV_TZ,
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(d);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("day")}.${get("month")}.${get("year")} ${get("hour")}:${get("minute")}:${get("second")}`;
}
