/**
 * Единый дневной риск-учёт (общий для гейта входов `/api/trading/check`
 * и для kill-switch `/api/live/*`).
 *
 * Зачем модуль: раньше дневной лимит считался в ДВУХ местах по-разному:
 *  - `trading.ts` — только реализованный net, 5% от ТЕКУЩЕГО баланса;
 *  - `live.ts`    — realized + unrealized, 10% от ТЕКУЩЕГО баланса.
 * Два порога на один и тот же риск: входы блокировались раньше, чем срабатывал
 * kill-switch, а база «уезжала» вниз вместе с убытком.
 *
 * Здесь одна формула:
 *   net(день)  = SUM(pnl закрытых сегодня) + unrealized открытых (real-time)
 *   база(день) = баланс кошелька на начало дня (фиксируется один раз)
 *   лимит      = база × total_loss_pct / 100
 *   стоп       = net <= -лимит  (прибыль компенсирует убыток — считаем NET)
 *
 * Граница дня — Europe/Kyiv (см. DAILY_RESET_TZ), а не UTC: иначе «следующий
 * день» наступал бы в 02:00–03:00 по местному времени.
 *
 * База фиксируется в `data/daily_baseline.json` и переживает рестарт контейнера.
 * При первом запуске ВНУТРИ дня база реконструируется как `wallet - realized`:
 * wallet уже включает сегодняшние реализованные убытки/прибыль, поэтому разница
 * даёт состояние счёта на 00:00 по Киеву (а не «сейчас»).
 */
import fs from "fs";
import path from "path";
import { db, botsTable, tradesTable } from "@workspace/db";
import { and, eq, gte, sql } from "drizzle-orm";
import { BOT_DIR, BOT_ENV } from "../botPaths";
import { binanceRequest } from "../grid-orders-lib";
import { fallbackDepositUsd, getWalletBalanceUsd } from "./account-balance";
import { DAILY_TZ, dayKey, dayStartUtcIso, tzOffsetMinutes } from "./dayBoundary";

export { DAILY_TZ, dayKey, dayStartUtcIso, tzOffsetMinutes };

// ВАЖНО: файл базы — per-env. Оба стека монтируют один ./data, поэтому общий
// файл приводил к тому, что live читал базу TESTNET (101.98 вместо 9.54) и
// дневной лимит на реальном счёте фактически отключался.
const BASELINE_FILE = path.resolve(
  BOT_DIR, "..", "data", `daily_baseline_${BOT_ENV}.json`,
);

export type BaselineSource = "auto" | "manual";

export type DailyBaseline = {
  day: string;
  balance: number;
  source: BaselineSource;
  ts: string;
};

function readBaselineFile(): DailyBaseline | null {
  try {
    const raw = JSON.parse(fs.readFileSync(BASELINE_FILE, "utf8")) as Partial<DailyBaseline>;
    const day = String(raw?.day ?? "");
    const balance = Number(raw?.balance);
    if (!day || !Number.isFinite(balance) || balance < 0) return null;
    return {
      day,
      balance,
      source: raw?.source === "manual" ? "manual" : "auto",
      ts: String(raw?.ts ?? ""),
    };
  } catch {
    return null;
  }
}

function writeBaseline(state: DailyBaseline): void {
  try {
    fs.mkdirSync(path.dirname(BASELINE_FILE), { recursive: true });
    fs.writeFileSync(BASELINE_FILE, JSON.stringify(state, null, 2));
  } catch (e) {
    console.error("[dailyRisk] baseline save failed:", String(e));
  }
}

/** Уже зафиксированная база текущего дня (или null). */
export function currentBaseline(): DailyBaseline | null {
  const cur = readBaselineFile();
  return cur && cur.day === dayKey() ? cur : null;
}

/**
 * База (баланс) на начало дня.
 *
 * Если для сегодняшнего дня базы ещё нет — фиксируем её как
 * `wallet - realizedToday` и сохраняем на диск. `force` — перезаписать
 * (ручной сброс лимита из UI).
 */
export async function ensureBaseline(opts?: { force?: boolean }): Promise<DailyBaseline> {
  const day = dayKey();
  const cur = readBaselineFile();
  if (!opts?.force && cur && cur.day === day && cur.balance > 0) return cur;

  let balance = fallbackDepositUsd();
  try {
    const wallet = await getWalletBalanceUsd({ fresh: true });
    const realized = await todayRealizedNet();
    const reconstructed = wallet - realized;
    if (Number.isFinite(reconstructed) && reconstructed > 0) balance = reconstructed;
  } catch (e) {
    console.error("[dailyRisk] baseline reconstruct failed:", String(e));
  }

  const state: DailyBaseline = {
    day,
    balance: Number(balance.toFixed(4)),
    source: opts?.force ? "manual" : "auto",
    ts: new Date().toISOString(),
  };
  writeBaseline(state);
  return state;
}

/** Ручная перезапись базы (кнопка «сбросить дневной лимит»). */
export async function resetBaselineTo(balance: number, source: BaselineSource = "manual"): Promise<DailyBaseline> {
  const state: DailyBaseline = {
    day: dayKey(),
    balance: Number(Math.max(0, balance).toFixed(4)),
    source,
    ts: new Date().toISOString(),
  };
  writeBaseline(state);
  return state;
}

/** Реализованный net закрытых сегодня сделок (pnl в БД уже net of fees). */
export async function todayRealizedNet(now: Date = new Date()): Promise<number> {
  const start = dayStartUtcIso(now);
  const [row] = await db
    .select({ net: sql<number>`COALESCE(SUM(pnl),0)` })
    .from(tradesTable)
    .where(and(eq(tradesTable.is_open, false), gte(tradesTable.exit_time, start)));
  return Number(row?.net ?? 0);
}

/**
 * Суммарный НЕреализованный PnL открытых позиций с биржи.
 *
 * `ok=false` означает, что биржа не ответила (бан/сеть). Вызывающий не должен
 * трактовать это как «убытка нет»: для kill-switch ошибка — повод пропустить
 * проверку, а не разрешить торговлю (см. dailyRiskSnapshot.degraded).
 */
const UN_CACHE_TTL_MS = 5_000;
let unCache: { value: number; ok: boolean; ts: number } | null = null;
export async function exchangeUnrealized(opts?: { fresh?: boolean }): Promise<{ value: number; ok: boolean }> {
  const now = Date.now();
  if (!opts?.fresh && unCache && now - unCache.ts < UN_CACHE_TTL_MS) {
    return { value: unCache.value, ok: unCache.ok };
  }
  try {
    const raw = await binanceRequest("GET", "/fapi/v2/positionRisk", {});
    const list: Array<Record<string, unknown>> = Array.isArray(raw) ? raw : raw ? [raw as Record<string, unknown>] : [];
    let sum = 0;
    for (const p of list) {
      const amt = Number(p?.positionAmt ?? 0) || 0;
      if (Math.abs(amt) <= 0) continue;
      sum += Number(p?.unRealizedProfit ?? 0) || 0;
    }
    unCache = { value: sum, ok: true, ts: now };
    return { value: sum, ok: true };
  } catch (e) {
    console.error("[dailyRisk] unrealized fetch failed:", String(e));
    // Ошибку не кэшируем надолго: 5с хватает, чтобы синхронные /check не
    // повторяли один и тот же упавший запрос.
    unCache = { value: 0, ok: false, ts: now };
    return { value: 0, ok: false };
  }
}

export type DailyLimitMode = "pct" | "usd";

/**
 * Как считать дневной лимит.
 *
 * `pct` — процент от базы на начало дня (live). `usd` — фиксированная сумма
 * (testnet: DAILY_LOSS_LIMIT_USD=10, т.к. тестовый баланс 10 000 USDT и процент
 * от него ничего не ограничивал бы).
 */
export function dailyLimitMode(): DailyLimitMode {
  const explicit = String(process.env.DAILY_LOSS_MODE || "").trim().toLowerCase();
  if (explicit === "pct" || explicit === "usd") return explicit;
  return BOT_ENV === "live" ? "pct" : "usd";
}

/** Дневной порог в процентах: явный > минимальный положительный из БД. */
export async function resolveTotalLossPct(explicit?: number): Promise<number> {
  if (explicit !== undefined && explicit !== null) {
    const n = Number(explicit);
    if (Number.isFinite(n) && n >= 0) return n;
  }
  try {
    const rows = await db.select({ pct: botsTable.total_loss_pct }).from(botsTable);
    if (rows.length > 0) {
      const vals = rows
        .map((r) => Number(r.pct))
        .filter((v) => Number.isFinite(v) && v >= 0);
      if (vals.length > 0) {
        // min положительных — самый консервативный порог; если у всех 0 —
        // дневной стоп выключен (0 = выкл).
        const positive = vals.filter((v) => v > 0);
        return positive.length > 0 ? Math.min(...positive) : 0;
      }
    }
  } catch (e) {
    console.error("[dailyRisk] total_loss_pct read failed:", String(e));
  }
  const envPct = Number(process.env.LIVE_DAILY_LOSS_LIMIT_PCT || "0");
  if (Number.isFinite(envPct) && envPct > 0) return envPct;
  return 10;
}

/** Порог просадки ОДНОЙ позиции (в % от депозита) для конкретного символа. */
export async function resolvePositionLossPct(symbol?: string): Promise<number> {
  try {
    if (symbol) {
      const [bot] = await db.select({ pct: botsTable.position_loss_pct })
        .from(botsTable).where(eq(botsTable.symbol, symbol.toUpperCase()));
      const n = Number(bot?.pct);
      // 0 = выкл (проверяющий пропускает порог <= 0), поэтому возвращаем как есть.
      if (Number.isFinite(n) && n >= 0) return n;
    } else {
      const rows = await db.select({ pct: botsTable.position_loss_pct }).from(botsTable);
      const vals = rows.map((r) => Number(r.pct)).filter((v) => Number.isFinite(v) && v > 0);
      if (vals.length) return Math.min(...vals);
    }
  } catch (e) {
    console.error("[dailyRisk] position_loss_pct read failed:", String(e));
  }
  const envPct = Number(process.env.LIVE_MAX_DRAWDOWN_PCT || "0");
  if (Number.isFinite(envPct) && envPct > 0) return envPct;
  return 5;
}

export type DailyRiskSnapshot = {
  day: string;
  mode: DailyLimitMode;
  pct: number;
  baseline: number;
  baseline_source: BaselineSource;
  limit_usd: number;
  realized: number;
  unrealized: number;
  /** net за день = realized + unrealized (прибыль минус убыток). */
  total: number;
  /** Сколько ещё можно потерять до стопа (>=0). */
  remaining_usd: number;
  /** net <= -limit_usd. */
  tripped: boolean;
  /** Биржа не ответила — unrealized не учтён. */
  degraded: boolean;
};

/**
 * Полный снимок дневного риска.
 *
 * @param opts.pct           явный процент (per-bot total_loss_pct для /check)
 * @param opts.configuredUsd фиксированный лимит в USD (yaml/env) для usd-режима
 */
export async function dailyRiskSnapshot(opts?: {
  pct?: number;
  configuredUsd?: number;
  now?: Date;
}): Promise<DailyRiskSnapshot> {
  const mode = dailyLimitMode();
  const pct = mode === "pct"
    ? await resolveTotalLossPct(opts?.pct)
    : 0;
  const baseline = await ensureBaseline();
  const configuredUsd = Number(opts?.configuredUsd ?? process.env.DAILY_LOSS_LIMIT_USD ?? 0);

  const realized = await todayRealizedNet(opts?.now);
  const un = await exchangeUnrealized();
  const total = realized + un.value;

  const limitUsd = mode === "pct"
    ? (baseline.balance * pct) / 100
    : (Number.isFinite(configuredUsd) && configuredUsd > 0 ? configuredUsd : 0);

  return {
    day: baseline.day,
    mode,
    pct,
    baseline: baseline.balance,
    baseline_source: baseline.source,
    limit_usd: Number(limitUsd.toFixed(2)),
    realized: Number(realized.toFixed(2)),
    unrealized: Number(un.value.toFixed(2)),
    total: Number(total.toFixed(2)),
    remaining_usd: Number((limitUsd > 0 ? Math.max(0, limitUsd + total) : 0).toFixed(2)),
    tripped: limitUsd > 0 && total <= -limitUsd,
    degraded: !un.ok,
  };
}
