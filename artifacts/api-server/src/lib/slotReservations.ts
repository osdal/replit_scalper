/**
 * Атомарное резервирование слотов глобального лимита позиций (max_positions).
 *
 * Проблема, которую решает модуль: раньше /trading/check только ЧИТАЛ счётчик
 * открытых позиций из БД, а строка в БД появляется лишь после того, как бот
 * разместил ордер на бирже (секунды). Сигналы приходят синхронно (закрытие
 * свечи), поэтому десятки ботов одновременно видели «свободно» и лимит
 * превышался многократно (check-then-act race).
 *
 * Здесь проверка и занятие слота выполняются под одной блокировкой:
 *   занято = символы с открытой принятой позицией в БД
 *            ∪ символы с живым резервом (ещё не попавшие в БД)
 *            [∪ символы с позицией на бирже, если включена сверка]
 *
 * Правила:
 *  - Слот привязан к СИМВОЛУ. Если символ уже занимает слот (открытая позиция
 *    или резерв) — запрос «own» и лимитом не блокируется. Это нужно для
 *    reverse-цепочек: добор ноги в уже открытую позицию не новый слот.
 *  - Резерв гасится сам, когда символ появляется в БД («поглощён»), либо по TTL
 *    (бот упал), либо явным release (ордер не прошёл).
 *  - Всё состояние — в памяти одного Node-процесса (по процессу на стек), поэтому
 *    мьютекс на promise-цепочке даёт настоящую атомарность.
 */

export interface SlotResult {
  allowed: boolean;
  /** Символ уже занимает слот (открытая позиция или собственный резерв). */
  own: boolean;
  /** id резерва, созданного/продлённого этим вызовом (иначе null). */
  reservationId: string | null;
  /** Сколько слотов занято всеми символами (БД + резервы), включая own. */
  occupied: number;
  /** Из них — резервы, которых ещё нет в БД. */
  reserved: number;
}

interface Reservation {
  id: string;
  expiresAt: number;
}

let seq = 0;
function newId(now: number): string {
  seq = (seq + 1) % 1_000_000;
  return `${now.toString(36)}-${seq.toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export class SlotReservations {
  private readonly ttlMs: number;
  private readonly now: () => number;
  private readonly res = new Map<string, Reservation>();
  private tail: Promise<unknown> = Promise.resolve();

  constructor(opts: { ttlMs?: number; now?: () => number } = {}) {
    this.ttlMs = opts.ttlMs && opts.ttlMs > 0 ? opts.ttlMs : 60_000;
    this.now = opts.now ?? Date.now;
  }

  /** Сериализует асинхронные критические секции (простой мьютекс). */
  exclusive<T>(fn: () => Promise<T>): Promise<T> {
    const run = this.tail.then(() => fn());
    this.tail = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  /** Убирает просроченные резервы и поглощённые (символ уже есть в БД/на бирже). */
  private prune(occupiedSymbols: Set<string>): void {
    const t = this.now();
    for (const [sym, r] of this.res) {
      if (r.expiresAt <= t || occupiedSymbols.has(sym)) this.res.delete(sym);
    }
  }

  /**
   * Проверить лимит и (при reserve=true) занять слот под одной блокировкой.
   * loadOccupied — свежий список символов, реально занимающих слот (БД [+ биржа]).
   */
  check(
    symbol: string,
    max: number,
    loadOccupied: () => Promise<Iterable<string>>,
    reserve: boolean,
  ): Promise<SlotResult> {
    return this.exclusive(async () => {
      const occupiedSet = new Set(await loadOccupied());
      this.prune(occupiedSet);
      const reservedCount = this.res.size;
      const occupied = occupiedSet.size + reservedCount;

      const mine = this.res.get(symbol);
      if (occupiedSet.has(symbol) || mine) {
        if (mine) mine.expiresAt = this.now() + this.ttlMs; // повторный запрос — продлеваем
        return {
          allowed: true,
          own: true,
          reservationId: mine ? mine.id : null,
          occupied,
          reserved: reservedCount,
        };
      }

      if (occupied >= max) {
        return { allowed: false, own: false, reservationId: null, occupied, reserved: reservedCount };
      }

      if (!reserve) {
        return { allowed: true, own: false, reservationId: null, occupied, reserved: reservedCount };
      }

      const t = this.now();
      const id = newId(t);
      this.res.set(symbol, { id, expiresAt: t + this.ttlMs });
      return {
        allowed: true,
        own: false,
        reservationId: id,
        occupied: occupied + 1,
        reserved: reservedCount + 1,
      };
    });
  }

  /** Освободить резерв символа (ордер не прошёл / сигнал отменён). id — защита от чужого release. */
  release(symbol: string, reservationId?: string | null): Promise<boolean> {
    return this.exclusive(async () => {
      const r = this.res.get(symbol);
      if (!r) return false;
      if (reservationId && r.id !== reservationId) return false;
      this.res.delete(symbol);
      return true;
    });
  }

  /** Для /status: сколько занято и сколько из этого — резервы. */
  inspect(loadOccupied: () => Promise<Iterable<string>>): Promise<{ open: number; reserved: number; occupied: number }> {
    return this.exclusive(async () => {
      const occupiedSet = new Set(await loadOccupied());
      this.prune(occupiedSet);
      return {
        open: occupiedSet.size,
        reserved: this.res.size,
        occupied: occupiedSet.size + this.res.size,
      };
    });
  }
}

/**
 * Защита от «тихого отключения» лимита: Number("abc") = NaN, а `x >= NaN` всегда
 * false — то есть опечатка в MAX_POSITIONS превращала бы лимит в бесконечный.
 * Возвращает валидное целое >= 1 либо fallback.
 */
export function sanitizeMaxPositions(value: unknown, fallback: number): number {
  const n = Number(value);
  if (Number.isFinite(n) && n >= 1) return Math.floor(n);
  return fallback;
}
