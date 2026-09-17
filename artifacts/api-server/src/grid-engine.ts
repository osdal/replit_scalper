import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import yaml from "js-yaml";
import {
  db,
  gridsTable,
  gridHistoryTable,
  botsTable,
  GRID_CREATE_SQL,
  GRID_MIGRATIONS,
  GRID_HISTORY_CREATE_SQL,
  GRID_HISTORY_MIGRATIONS,
  type GridRow,
} from "@workspace/db";
import { desc, eq, sql } from "drizzle-orm";
import { logger } from "./lib/logger";
import { computeAdxFor, getKlines } from "./adx-lib";
import { getMarkPrice } from "./routes/ticker";
import {
  MAX_LEVERAGE,
  createGridOrders,
  cancelOrderIds,
  cancelAlgoOrderIds,
  placeGridStop,
  syncGridTp,
  fetchGridFills,
  fetchOpenAlgoOrders,
  fetchAlgoStatus,
  closeGridPosition,
  getExchangeFilters,
  roundStep,
  GridOrderError,
  numOr0,
  type CreateOrderResult,
  type CreateStops,
  type LevelSide,
  type OpenAlgoOrder,
  type OrderPlanItem,
} from "./grid-orders-lib";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const DEFAULT_INTERVAL_MS = 15000;
const DEFAULT_GATE = 15;
const DEFAULT_ORDER_SIZE_USD = 100;
const DEFAULT_LEVERAGE = 50;
// Допуск касания середины: |price - mid| <= 0.05% от mid.
const TOUCH_TOLERANCE_PCT = 0.05;
// Кулдаун на мутации ордеров по одной сетке.
const ORDER_MUTATION_COOLDOWN_MS = 10_000;
// Относительный допуск расхождения выставленного триггера и целевого.
const TRIGGER_REL_TOLERANCE = 1e-4;
// Auto-mode: те же параметры, что и у браузерного AUTO-режима дашборда.
const AUTO_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "12h", "1d"] as const;
const AUTO_GRID_LEVELS = 10;
const AUTO_TP_PCT = 10;
const AUTO_SL_PCT = 2;
const AUTO_EDGE_PCT = 2;
const DEFAULT_AUTO_MAX = 3;
const DEFAULT_AUTO_TOTAL_MAX = 10;
const DEFAULT_AUTO_ORDER_USD = 10;
const DEFAULT_AUTO_LEVERAGE = 50;
// Комиссии Binance USDⓈ-M (maker/taker), в долях от номинала: 0.02% / 0.05%.
const MAKER_FEE_RATE = 0.0002;
const TAKER_FEE_RATE = 0.0005;
// Свечей в сутках по таймфрейму — как в computeGridBounds дашборда.
const CANDLES_PER_DAY: Record<string, number> = {
  "5m": 288,
  "15m": 96,
  "30m": 48,
  "1h": 24,
  "4h": 6,
  "12h": 2,
  "1d": 1,
};
// Лимиты klines для границ сетки — те же, что limitForInterval в routes/history.ts.
const BOUNDS_KLINES_LIMIT: Record<string, number> = {
  "5m": 1000,
  "15m": 1000,
  "30m": 1000,
  "1h": 720,
  "4h": 180,
  "12h": 60,
  "1d": 30,
};

/**
 * Серверный grid-движок: phase 2a (активация waiting-сеток по касанию середины,
 * опрос филлов, синхронизация защитных STOP/TP) + phase 2b (TP/SL trigger и
 * market close, финализация/история, авто-создание сеток). Движок INERT, пока
 * GRID_ENGINE_ENABLED != true, и трогает только строки grids с engine='server'
 * (браузерные сетки engine='browser' не затрагиваются).
 */

function envFlag(name: string, fallback = false): boolean {
  const raw = process.env[name];
  if (raw === undefined || String(raw).trim() === "") return fallback;
  return String(raw).trim().toLowerCase() === "true";
}

function envIntervalMs(name: string, fallback: number): number {
  const raw = Number(process.env[name]);
  return Number.isFinite(raw) && raw >= 1000 ? raw : fallback;
}

function envNumber(name: string, fallback: number): number {
  const raw = Number(process.env[name]);
  return Number.isFinite(raw) ? raw : fallback;
}

function envInt(name: string, fallback: number): number {
  const raw = Number(process.env[name]);
  return Number.isFinite(raw) ? Math.trunc(raw) : fallback;
}

function parseArray(raw: string | null | undefined): unknown[] {
  if (raw == null) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function parseObject(raw: string | null | undefined): Record<string, unknown> | null {
  if (raw == null) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function parseIntArray(raw: string | null | undefined): number[] {
  return parseArray(raw)
    .map((v) => Number(v))
    .filter((n): n is number => Number.isInteger(n) && n > 0);
}

function tpIdsOf(raw: string | null | undefined): { long?: number; short?: number } {
  const obj = parseObject(raw);
  const out: { long?: number; short?: number } = {};
  for (const key of ["long", "short"] as const) {
    const n = Number(obj?.[key]);
    if (Number.isInteger(n) && n > 0) out[key] = n;
  }
  return out;
}

function tpPricesOf(raw: string | null | undefined): { long?: number; short?: number } {
  const obj = parseObject(raw);
  const out: { long?: number; short?: number } = {};
  for (const key of ["long", "short"] as const) {
    const n = Number(obj?.[key]);
    if (Number.isFinite(n) && n > 0) out[key] = n;
  }
  return out;
}

/** Epoch ms из created_at (ISO-строка или число); null, если распарсить нельзя. */
function createdAtMs(raw: string | null | undefined): number | null {
  if (raw == null || String(raw).trim() === "") return null;
  const n = Number(raw);
  if (Number.isFinite(n) && n > 0) return n;
  const t = new Date(String(raw)).getTime();
  return Number.isFinite(t) ? t : null;
}

interface FillEntry {
  level: number;
  side: LevelSide;
  qty: number;
  entry: number;
  cumQuote: number;
  time?: number;
}

function parseFills(raw: string | null | undefined): FillEntry[] {
  const out: FillEntry[] = [];
  for (const value of parseArray(raw)) {
    const o = value as any;
    const level = Number(o?.level);
    if (!Number.isFinite(level) || level <= 0) continue;
    const rawSide = String(o?.side ?? "").toUpperCase();
    const side: LevelSide = rawSide === "SELL" ? "SELL" : "BUY";
    const time = Number(o?.time);
    out.push({
      level,
      side,
      qty: numOr0(o?.qty),
      entry: numOr0(o?.entry),
      cumQuote: numOr0(o?.cumQuote),
      ...(Number.isFinite(time) ? { time } : {}),
    });
  }
  return out;
}

function sideFills(fills: FillEntry[], side: LevelSide): FillEntry[] {
  return fills.filter((f) => f.side === side);
}

/** Σ cumQuote / Σ qty по филлам стороны; 0 при отсутствии данных. */
function avgEntry(fills: FillEntry[]): number {
  let qty = 0;
  let quote = 0;
  for (const f of fills) {
    if (Number.isFinite(f.qty) && Number.isFinite(f.cumQuote)) {
      qty += f.qty;
      quote += f.cumQuote;
    }
  }
  return qty > 0 ? quote / qty : 0;
}

/** Σ qty по филлам стороны; 0 при отсутствии данных. */
function sumQty(fills: FillEntry[]): number {
  let qty = 0;
  for (const f of fills) {
    if (Number.isFinite(f.qty)) qty += f.qty;
  }
  return qty;
}

/** Algo-ордер считается исполненным по этим статусам Binance Algo API. */
function isTriggeredAlgo(status: unknown): boolean {
  const s = String(status ?? "").toUpperCase();
  return s === "TRIGGERED" || s === "FINISHED";
}

const lastMutationAt = new Map<string, number>();
const inFlightSides = new Set<string>();
// Per-grid guard: сетка не может быть закрыта дважды (trigger + reconcile).
const exitInFlight = new Set<string>();

function canMutate(uid: string): boolean {
  const last = lastMutationAt.get(uid) ?? 0;
  return Date.now() - last >= ORDER_MUTATION_COOLDOWN_MS;
}

function markMutation(uid: string): void {
  lastMutationAt.set(uid, Date.now());
}

/** Per-grid/per-side in-flight guard: не даёт одному tick выставить дважды. */
async function guardedAttempt(
  uid: string,
  kind: "stop" | "tp",
  sideName: "long" | "short",
  action: () => Promise<void>,
): Promise<string | null> {
  const key = `${uid}:${kind}:${sideName}`;
  if (inFlightSides.has(key)) return null;
  inFlightSides.add(key);
  try {
    await action();
    return null;
  } catch (e) {
    return e instanceof GridOrderError ? e.message : e instanceof Error ? e.message : String(e);
  } finally {
    inFlightSides.delete(key);
  }
}

/**
 * Допуск сравнения триггера: минимум один tickSize (плюс эпсилон), чтобы
 * округлённый биржей живой триггер не считался устаревшим. Относительный допуск
 * используется только как фолбэк, когда tickSize неизвестен.
 */
function triggerTolerance(want: number, tick: number | null): number {
  if (tick != null && tick > 0) {
    return Math.max(tick * 1.0000001, Math.abs(want) * 1e-6);
  }
  return Math.abs(want) * TRIGGER_REL_TOLERANCE;
}

function needsReplace(live: number | undefined, want: number, tick: number | null): boolean {
  if (live == null) return true;
  if (!Number.isFinite(live) || live <= 0) return false;
  return Math.abs(live - want) >= triggerTolerance(want, tick);
}

/** Округление цены до tickSize; без tick возвращает значение как есть. */
function roundPrice(value: number, tick: number | null): number {
  if (!(Number.isFinite(value) && value > 0)) return value;
  if (tick == null || !(tick > 0)) return value;
  const rounded = roundStep(value, tick);
  return rounded > 0 ? rounded : value;
}

// Кэш tickSize по символу в пределах одного tick движка.
const tickSizeCache = new Map<string, number | null>();

function clearTickSizeCache(): void {
  tickSizeCache.clear();
}

/** tickSize символа из exchangeInfo (кэш на tick); null — фильтры недоступны. */
async function getSymbolTickSize(symbol: string): Promise<number | null> {
  if (tickSizeCache.has(symbol)) return tickSizeCache.get(symbol) ?? null;
  let tick: number | null = null;
  try {
    const filters = await getExchangeFilters(symbol);
    const t = Number(filters?.tTick);
    tick = Number.isFinite(t) && t > 0 ? t : null;
  } catch (e) {
    logger.warn(
      { symbol, err: (e as Error).message },
      "[grid-engine] exchange filters fetch failed, using relative tolerance",
    );
    tick = null;
  }
  tickSizeCache.set(symbol, tick);
  return tick;
}

// Последняя успешно отправленная (округлённая) цель по uid+kind+side: не даёт
// повторно отправить тот же триггер, даже если живой ордер не виден в open algo.
const lastSentTargets = new Map<string, number>();

function targetKey(uid: string, kind: "stop" | "tp", side: "long" | "short"): string {
  return `${uid}:${kind}:${side}`;
}

function memoHit(
  uid: string,
  kind: "stop" | "tp",
  side: "long" | "short",
  rounded: number,
  tick: number | null,
): boolean {
  const prev = lastSentTargets.get(targetKey(uid, kind, side));
  if (prev == null || !Number.isFinite(prev)) return false;
  const tol = tick != null && tick > 0 ? tick * 1e-6 : Math.max(Math.abs(rounded), 1) * 1e-12;
  return Math.abs(prev - rounded) <= tol;
}

function clearSentTargets(uid: string, kind?: "stop" | "tp", side?: "long" | "short"): void {
  if (kind != null && side != null) {
    lastSentTargets.delete(targetKey(uid, kind, side));
    return;
  }
  for (const key of [...lastSentTargets.keys()]) {
    if (key.startsWith(`${uid}:`) && (kind == null || key.startsWith(`${uid}:${kind}:`))) {
      lastSentTargets.delete(key);
    }
  }
}

// Ленивое создание таблиц (grids/grid_history) — как в роутах, чтобы движок был
// самодостаточен при включении без HTTP-трафика.
const ensureTables = (async () => {
  await db.run(sql.raw(GRID_CREATE_SQL));
  for (const migration of GRID_MIGRATIONS) {
    await db.run(sql.raw(migration)).catch(() => {});
  }
  await db.run(sql.raw(GRID_HISTORY_CREATE_SQL));
  for (const migration of GRID_HISTORY_MIGRATIONS) {
    await db.run(sql.raw(migration)).catch(() => {});
  }
})().catch((e) => {
  logger.error({ err: e }, "[grid-engine] table ensure failed");
});

/** Цены уровней сетки: nLevels делений дают nLevels-1 внутренних уровней. */
function gridLevelPrices(lo: number, hi: number, nLevels: number): number[] {
  if (!(hi > lo) || nLevels < 2) return [];
  const step = (hi - lo) / nLevels;
  const out: number[] = [];
  for (let k = 1; k < nLevels; k++) out.push(Number((lo + step * k).toFixed(8)));
  return out;
}

/**
 * Границы сетки по klines: 8-дневный lookback и лимиты свечей как у дашборда
 * (Dashboard.tsx computeGridBounds + routes/history.ts limitForInterval).
 * Возвращает null, если данных недостаточно/таймфрейм неизвестен.
 */
async function computeGridBounds(
  symbol: string,
  timeframe: string,
): Promise<{ lo: number; hi: number; mid: number } | null> {
  const tf = String(timeframe ?? "").trim();
  const candlesPerDay = CANDLES_PER_DAY[tf];
  if (!candlesPerDay) return null;
  const limit = BOUNDS_KLINES_LIMIT[tf] ?? 100;
  const klines = await getKlines(symbol, tf, limit);
  if (!Array.isArray(klines) || klines.length < 20) return null;
  const rows = klines
    .map((k: any) => ({ low: parseFloat(k?.[3]), high: parseFloat(k?.[2]) }))
    .filter((r) => Number.isFinite(r.low) && Number.isFinite(r.high));
  if (rows.length < 20) return null;
  const tail = rows.slice(-(8 * candlesPerDay));
  if (tail.length < 10) return null;
  let lo = Infinity;
  let hi = -Infinity;
  for (const r of tail) {
    if (r.low < lo) lo = r.low;
    if (r.high > hi) hi = r.high;
  }
  if (!(hi > lo)) return null;
  return { lo, hi, mid: (lo + hi) / 2 };
}

/** Символы ботов из таблицы bots (фолбэк — config_*.yaml), как в routes/pairs.ts. */
async function getBotSymbols(): Promise<{ symbols: string[]; running: Set<string> }> {
  try {
    const rows = await db.select().from(botsTable);
    if (rows.length > 0) {
      const symbols = [
        ...new Set(rows.map((r) => String(r.symbol ?? "").toUpperCase()).filter(Boolean)),
      ].sort();
      const running = new Set(
        rows.filter((r) => r.is_running).map((r) => String(r.symbol ?? "").toUpperCase()),
      );
      return { symbols, running };
    }
  } catch (e) {
    logger.warn(
      { err: (e as Error).message },
      "[grid-engine] bot symbols query failed, using yaml fallback",
    );
  }

  const configDir = path.resolve(__dirname, "../../../bot");
  const symbols: string[] = [];
  try {
    const files = fs
      .readdirSync(configDir)
      .filter((f) => f.startsWith("config_") && f.endsWith(".yaml"));
    for (const file of files) {
      try {
        const parsed = yaml.load(
          fs.readFileSync(path.join(configDir, file), "utf8"),
        ) as { symbol?: string };
        if (parsed?.symbol) symbols.push(String(parsed.symbol).toUpperCase());
      } catch {
        // ignore unreadable file
      }
    }
  } catch {
    // ignore missing config dir
  }
  return { symbols: [...new Set(symbols)].sort(), running: new Set<string>() };
}

type GridPatch = Partial<typeof gridsTable.$inferInsert>;

async function updateGrid(uid: string, patch: GridPatch): Promise<void> {
  await db
    .update(gridsTable)
    .set({ ...patch, updated_at: new Date().toISOString() })
    .where(eq(gridsTable.uid, uid));
}

/** ADX >= gate: снимаем отслеживаемые ордера и помечаем сетку stopped. */
async function annulGrid(
  row: GridRow,
  symbol: string,
  price: number,
  adx: number,
  gate: number,
): Promise<void> {
  const limitIds = parseIntArray(row.testnetOrderIds);
  const protectiveIds = [
    ...parseIntArray(row.stopOrderIds),
    ...Object.values(tpIdsOf(row.tpOrderIds)),
  ].filter((n): n is number => typeof n === "number" && Number.isFinite(n) && n > 0);

  let error: string | null = null;
  if (limitIds.length > 0) {
    try {
      await cancelOrderIds(symbol, limitIds);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }
  if (protectiveIds.length > 0) {
    try {
      const res = await cancelAlgoOrderIds(symbol, protectiveIds);
      if (res.errors.length > 0) error = res.errors.map((x) => x.error).join("; ");
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  clearSentTargets(row.uid);
  markMutation(row.uid);
  await updateGrid(row.uid, {
    phase: "stopped",
    finished_at: new Date().toISOString(),
    testnetOrderIds: JSON.stringify([]),
    stopOrderIds: JSON.stringify([]),
    tpOrderIds: JSON.stringify({}),
    tpOrderPrices: JSON.stringify({}),
    lastPrice: price,
    lastPlacementError: error,
  });
  logger.info(
    {
      uid: row.uid,
      symbol,
      adx,
      gate,
      canceled: limitIds.length + protectiveIds.length,
      error,
    },
    "[grid-engine] annul (gate)",
  );
}

/** Касание середины: выставляет двустороннюю сетку и переводит в active. */
async function activateGrid(row: GridRow, symbol: string, price: number): Promise<void> {
  const mid = numOr0(row.midPrice);
  const levelPrices = parseArray(row.levelPrices)
    .map((v) => Number(v))
    .filter((n) => Number.isFinite(n) && n > 0);

  // > mid — SELL, < mid — BUY, ровно mid — пропускаем.
  const plan: OrderPlanItem[] = [];
  for (const level of levelPrices) {
    if (level > mid) plan.push({ level, side: "SELL" });
    else if (level < mid) plan.push({ level, side: "BUY" });
  }

  const orderSizeUsd = numOr0(row.orderSizeUsd) || DEFAULT_ORDER_SIZE_USD;
  const leverageRaw = Math.trunc(numOr0(row.leverage)) || DEFAULT_LEVERAGE;
  const leverage = Math.min(Math.max(leverageRaw, 1), MAX_LEVERAGE);
  const edgePct = numOr0(row.edgePct);

  let results: CreateOrderResult[] = [];
  let stops: CreateStops = { errors: [] };
  let placementError: string | null = null;
  try {
    const out = await createGridOrders({
      symbol,
      plan,
      orderSizeUsd,
      leverage,
      lo: numOr0(row.lo),
      hi: numOr0(row.hi),
      slBufferPct: edgePct,
      placeStops: true,
    });
    results = out.results;
    stops = out.stops;
  } catch (e) {
    placementError =
      e instanceof GridOrderError ? e.message : e instanceof Error ? e.message : String(e);
    logger.warn(
      { uid: row.uid, symbol, err: placementError },
      "[grid-engine] activation placement failed",
    );
  }

  const ids = results
    .filter((r) => !r.error && !r.skipped && Number.isFinite(Number(r.orderId)))
    .map((r) => Number(r.orderId));
  const stopIds = [stops.long?.algoId, stops.short?.algoId].filter(
    (n): n is number => Number.isFinite(n as number) && (n as number) > 0,
  );
  const failed = results.filter((r) => r.error).map((r) => r.level);
  if (!placementError && failed.length > 0) placementError = "partial failure";

  // Мемо уже отправленных (округлённых биржей) триггеров — чтобы следующий tick
  // сравнивал цель с фактически выставленным значением, а не переставлял ордер.
  if (stops.long) lastSentTargets.set(targetKey(row.uid, "stop", "long"), stops.long.triggerPrice);
  if (stops.short) {
    lastSentTargets.set(targetKey(row.uid, "stop", "short"), stops.short.triggerPrice);
  }

  markMutation(row.uid);
  await updateGrid(row.uid, {
    phase: "active",
    activated_at: new Date().toISOString(),
    testnetOrderIds: JSON.stringify(ids),
    stopOrderIds: JSON.stringify(stopIds),
    tpOrderIds: JSON.stringify({}),
    tpOrderPrices: JSON.stringify({}),
    fillEntries: JSON.stringify([]),
    openLots: JSON.stringify([]),
    positionAmt: 0,
    lastPrice: price,
    lastPlacementError: placementError,
  });
  logger.info(
    {
      uid: row.uid,
      symbol,
      levels: plan.length,
      placed: ids.length,
      failed: failed.length,
      stopIds,
      error: placementError,
    },
    "[grid-engine] activated",
  );
}

async function handleWaiting(row: GridRow, symbol: string, price: number): Promise<void> {
  const timeframe = String(row.timeframe ?? "").trim();
  if (!timeframe) return;

  const gate = numOr0(row.gate) || DEFAULT_GATE;
  const adx = await computeAdxFor(symbol, timeframe);
  if (adx == null) {
    logger.debug(
      { uid: row.uid, symbol, timeframe },
      "[grid-engine] waiting: ADX unavailable, skip",
    );
    return;
  }

  if (adx >= gate) {
    if (!canMutate(row.uid)) return;
    await annulGrid(row, symbol, price, adx, gate);
    return;
  }

  const mid = numOr0(row.midPrice);
  if (!(mid > 0)) return;
  const tolerance = (mid * TOUCH_TOLERANCE_PCT) / 100;
  if (Math.abs(price - mid) > tolerance) return;

  if (!canMutate(row.uid)) return;
  await activateGrid(row, symbol, price);
}

interface FinalizeCloseParams {
  closedSide: "BUY" | "SELL" | null;
  reason: "tp" | "sl" | "closed";
  exitAvgPrice: number;
  exitQty: number;
  fundingUsd: number;
  price: number;
}

/**
 * Финализация после закрытия: side-aware realized PnL, обновление строки grids
 * (phase/realized/finished_at) и запись grid_history. Если вторая сторона ещё
 * держит лоты — сетка остаётся active (closedSide != null и otherFills непусты).
 */
async function finalizeGrid(
  row: GridRow,
  symbol: string,
  params: FinalizeCloseParams,
): Promise<void> {
  const fills = parseFills(row.fillEntries);
  const closedSide = params.closedSide;
  const closedFills = closedSide ? sideFills(fills, closedSide) : fills;
  const otherFills = closedSide ? fills.filter((f) => f.side !== closedSide) : [];

  let entryNotional = 0;
  let netUsd = 0;
  if (closedSide) {
    entryNotional = closedFills.reduce(
      (sum, f) => sum + (Number.isFinite(f.cumQuote) ? f.cumQuote : 0),
      0,
    );
    const exitNotional = params.exitAvgPrice * params.exitQty;
    const gross = closedSide === "SELL" ? entryNotional - exitNotional : exitNotional - entryNotional;
    const fees = entryNotional * MAKER_FEE_RATE + exitNotional * TAKER_FEE_RATE;
    netUsd = gross - fees + params.fundingUsd;
  } else {
    // Полное закрытие (обе стороны): считаем каждую сторону отдельно.
    for (const side of ["BUY", "SELL"] as const) {
      const sideFillSet = sideFills(fills, side);
      if (sideFillSet.length === 0) continue;
      const sideEntry = sideFillSet.reduce(
        (sum, f) => sum + (Number.isFinite(f.cumQuote) ? f.cumQuote : 0),
        0,
      );
      const sideExit = params.exitAvgPrice * sumQty(sideFillSet);
      const gross = side === "SELL" ? sideEntry - sideExit : sideExit - sideEntry;
      const fees = sideEntry * MAKER_FEE_RATE + sideExit * TAKER_FEE_RATE;
      netUsd += gross - fees;
      entryNotional += sideEntry;
    }
    netUsd += params.fundingUsd;
  }

  const pct = entryNotional > 0 ? (netUsd / entryNotional) * 100 : 0;
  const totalPnl = numOr0(row.realizedPnl) + pct;
  const totalUsd = numOr0(row.realizedUsd) + netUsd;

  const keepActive = closedSide != null && otherFills.length > 0;
  const phase = params.reason === "tp" ? "done" : "stopped";

  if (keepActive) {
    const sideKey = closedSide === "BUY" ? "long" : "short";
    const tpIds = tpIdsOf(row.tpOrderIds);
    const tpPrices = tpPricesOf(row.tpOrderPrices);
    delete tpIds[sideKey];
    delete tpPrices[sideKey];
    // Защита обеих сторон снята при закрытии — мемо триггеров неактуально.
    clearSentTargets(row.uid);
    markMutation(row.uid);
    await updateGrid(row.uid, {
      phase: "active",
      realizedPnl: totalPnl,
      realizedUsd: totalUsd,
      fillEntries: JSON.stringify(otherFills),
      openLots: JSON.stringify(otherFills.map((f) => f.level).sort((a, b) => a - b)),
      // Защита закрытой стороны снята; остаток пере-синхронизируется следующим tick.
      stopOrderIds: JSON.stringify([]),
      tpOrderIds: JSON.stringify(tpIds),
      tpOrderPrices: JSON.stringify(tpPrices),
      lastPrice: params.price,
    });
    logger.info(
      {
        uid: row.uid,
        symbol,
        phase: "active",
        closedSide,
        reason: params.reason,
        exit: params.exitAvgPrice,
        netUsd,
        totalPnl,
        remaining: otherFills.length,
      },
      "[grid-engine] side closed, grid stays active",
    );
    return;
  }

  const finishedAt = new Date().toISOString();
  clearSentTargets(row.uid);
  markMutation(row.uid);
  await updateGrid(row.uid, {
    phase,
    realizedPnl: totalPnl,
    realizedUsd: totalUsd,
    finished_at: finishedAt,
    testnetOrderIds: JSON.stringify([]),
    stopOrderIds: JSON.stringify([]),
    tpOrderIds: JSON.stringify({}),
    tpOrderPrices: JSON.stringify({}),
    fillEntries: JSON.stringify([]),
    openLots: JSON.stringify([]),
    positionAmt: 0,
    lastPrice: params.exitAvgPrice > 0 ? params.exitAvgPrice : params.price,
    lastPlacementError: null,
  });

  const entry = avgEntry(closedFills) || numOr0(row.lastPrice) || params.price;
  try {
    await db
      .insert(gridHistoryTable)
      .values({
        uid: `${row.uid}-${Date.now()}`,
        symbol,
        timeframe: String(row.timeframe ?? ""),
        phase,
        exit_reason: params.reason,
        tp_pct: numOr0(row.tpPct),
        gate: numOr0(row.gate),
        levels: Math.trunc(numOr0(row.levels)),
        lo: numOr0(row.lo),
        hi: numOr0(row.hi),
        mid: numOr0(row.midPrice),
        entry,
        exit: params.exitAvgPrice > 0 ? params.exitAvgPrice : null,
        pnl: totalPnl,
        pnl_usd: totalUsd,
        positions: closedFills.length,
        created_at: String(row.created_at ?? finishedAt),
        finished_at: finishedAt,
        order_size_usd: numOr0(row.orderSizeUsd) || null,
      })
      .onConflictDoNothing();
  } catch (e) {
    logger.warn(
      { uid: row.uid, symbol, err: (e as Error).message },
      "[grid-engine] grid_history insert failed",
    );
  }

  // Отдельного shared notifier в api-server нет — новый не добавляем.
  logger.debug(
    {
      uid: row.uid,
      symbol,
      phase,
      exitReason: params.reason,
      entry,
      exit: params.exitAvgPrice,
      pnl: totalPnl,
      pnlUsd: totalUsd,
    },
    "[grid-engine] telegram notify skipped (no shared notifier)",
  );

  logger.info(
    {
      uid: row.uid,
      symbol,
      phase,
      reason: params.reason,
      closedSide,
      entry,
      exit: params.exitAvgPrice > 0 ? params.exitAvgPrice : null,
      netUsd,
      totalPnl,
      totalUsd,
      positions: closedFills.length,
    },
    "[grid-engine] finalized",
  );
}

/**
 * Закрытие стороны по TP/SL по рынку: снимает resting/защитные ордера, закрывает
 * сторону общим close-хелпером, разрешает фактическую цену выхода (fill ->
 * actualPrice -> last price) и финализирует сетку. true — строка обновлена
 * (финализация/keepActive); false — закрыть не удалось, вызывающий продолжает
 * обычный persist.
 */
async function triggerClose(
  row: GridRow,
  symbol: string,
  side: "long" | "short",
  reason: "tp" | "sl",
  price: number,
  ctx: { buyFills: FillEntry[]; sellFills: FillEntry[] },
): Promise<boolean> {
  if (exitInFlight.has(row.uid)) return false;
  exitInFlight.add(row.uid);
  try {
    const orderIds = parseIntArray(row.testnetOrderIds);
    const protectiveIds = [
      ...parseIntArray(row.stopOrderIds),
      ...Object.values(tpIdsOf(row.tpOrderIds)),
    ].filter((n): n is number => Number.isFinite(n) && n > 0);
    if (orderIds.length > 0) {
      try {
        await cancelOrderIds(symbol, orderIds);
      } catch (e) {
        logger.warn(
          { uid: row.uid, symbol, err: (e as Error).message },
          "[grid-engine] close: resting cancel failed",
        );
      }
    }
    if (protectiveIds.length > 0) {
      try {
        await cancelAlgoOrderIds(symbol, protectiveIds);
      } catch (e) {
        logger.warn(
          { uid: row.uid, symbol, err: (e as Error).message },
          "[grid-engine] close: protective cancel failed",
        );
      }
    }
    clearSentTargets(row.uid);

    const sideQty = sumQty(side === "long" ? ctx.buyFills : ctx.sellFills);
    let closeRes: Awaited<ReturnType<typeof closeGridPosition>> | null = null;
    try {
      closeRes = await closeGridPosition({
        symbol,
        direction: side,
        quantity: sideQty > 0 ? sideQty : undefined,
        sinceMs: createdAtMs(row.created_at) ?? undefined,
      });
    } catch (e) {
      const message =
        e instanceof GridOrderError ? e.message : e instanceof Error ? e.message : String(e);
      logger.warn(
        {
          uid: row.uid,
          symbol,
          side,
          reason,
          err: message,
          nothingToClose: /nothing toclose|position is 0/i.test(message),
        },
        "[grid-engine] close failed",
      );
      return false; // следующий tick повторит закрытие / реконсиляцию
    }
    if (!closeRes) return false;

    let exitAvgPrice = closeRes.avgPrice;
    let exitQty = closeRes.executedQty > 0 ? closeRes.executedQty : sideQty;
    if (!(exitAvgPrice > 0) && closeRes.cumQuote > 0 && exitQty > 0) {
      exitAvgPrice = closeRes.cumQuote / exitQty;
    }
    // Фолбэк: actualPrice защитного algo-ордера этой стороны.
    if (!(exitAvgPrice > 0)) {
      const tpMap = tpIdsOf(row.tpOrderIds);
      const algoIds = [
        side === "long" ? tpMap.long : tpMap.short,
        ...parseIntArray(row.stopOrderIds),
      ].filter((n): n is number => Number.isFinite(n as number) && (n as number) > 0);
      if (algoIds.length > 0) {
        try {
          const statuses = await fetchAlgoStatus(symbol, algoIds);
          for (const st of statuses) {
            if ("error" in st) continue;
            const px = numOr0((st as any).actualPrice);
            if (px > 0) {
              exitAvgPrice = px;
              break;
            }
          }
        } catch {
          // ignore: останется last price
        }
      }
    }
    if (!(exitAvgPrice > 0)) exitAvgPrice = price;

    await finalizeGrid(row, symbol, {
      closedSide: side === "long" ? "BUY" : "SELL",
      reason,
      exitAvgPrice,
      exitQty,
      fundingUsd: closeRes.fundingUsd,
      price,
    });
    return true;
  } finally {
    exitInFlight.delete(row.uid);
  }
}

/**
 * Реконсиляция: позиция обнулилась, а филлы есть — значит защитный ордер сработал
 * на бирже (клиент/api-server мог быть выключен). Определяем причину по
 * сработавшему algo, разрешаем цену выхода (userTrades -> actualPrice ->
 * lastPrice), снимаем остатки и финализируем. true — строка обработана.
 */
async function reconcileFlatGrid(
  row: GridRow,
  symbol: string,
  price: number,
  fillEntries: FillEntry[],
): Promise<boolean> {
  if (exitInFlight.has(row.uid)) return false;
  if (fillEntries.length === 0) return false;

  const buyFills = sideFills(fillEntries, "BUY");
  const sellFills = sideFills(fillEntries, "SELL");
  let closedSide: "BUY" | "SELL" | null;
  if (buyFills.length > 0 && sellFills.length === 0) closedSide = "BUY";
  else if (sellFills.length > 0 && buyFills.length === 0) closedSide = "SELL";
  else closedSide = null; // обе стороны -> полное закрытие

  const stopIds = parseIntArray(row.stopOrderIds);
  const tpIdMap = tpIdsOf(row.tpOrderIds);
  const tpIds = Object.values(tpIdMap).filter(
    (n): n is number => Number.isFinite(n as number) && (n as number) > 0,
  );

  let reason: "tp" | "sl" | "closed" = "closed";
  let exitAvgPrice = numOr0(row.lastPrice) || price;
  let exitQty = sumQty(fillEntries);

  const algoIds = Array.from(new Set([...tpIds, ...stopIds]));
  if (algoIds.length > 0) {
    try {
      const statuses = await fetchAlgoStatus(symbol, algoIds);
      const findTriggered = (ids: number[]) => {
        for (const id of ids) {
          const st = statuses.find((r) => Number((r as any).algoId) === id);
          if (st && !("error" in st) && isTriggeredAlgo((st as any).algoStatus)) return st;
        }
        return undefined;
      };
      const tpHit = findTriggered(tpIds);
      const hit = tpHit ?? findTriggered(stopIds);
      if (hit) {
        const h = hit as any;
        reason = tpHit ? "tp" : "sl";
        if (numOr0(h.actualQty) > 0) exitQty = numOr0(h.actualQty);
        if (numOr0(h.actualPrice) > 0) exitAvgPrice = numOr0(h.actualPrice);
        const hitOrderId = numOr0(h.actualOrderId);
        if (hitOrderId > 0) {
          try {
            const fillRes = await fetchGridFills(symbol, [hitOrderId]);
            let qty = 0;
            let quote = 0;
            for (const r of fillRes.results) {
              if ("error" in r) continue;
              qty += numOr0((r as any).executedQty);
              quote += numOr0((r as any).cumQuote);
            }
            if (qty > 0) {
              exitQty = qty;
              if (quote > 0) exitAvgPrice = quote / qty;
            }
          } catch {
            // ignore: actualPrice остаётся фолбэком
          }
        }
      }
    } catch (e) {
      logger.warn(
        { uid: row.uid, symbol, err: (e as Error).message },
        "[grid-engine] reconcile algo status failed",
      );
    }
  }
  if (!(exitAvgPrice > 0)) exitAvgPrice = price;
  if (!(exitQty > 0)) exitQty = sumQty(fillEntries);

  // Снимаем остатки: resting-ордера и неисполненную защиту.
  const orderIds = parseIntArray(row.testnetOrderIds);
  const protectiveIds = [...stopIds, ...tpIds];
  if (orderIds.length > 0) {
    try {
      await cancelOrderIds(symbol, orderIds);
    } catch (e) {
      logger.warn(
        { uid: row.uid, symbol, err: (e as Error).message },
        "[grid-engine] reconcile resting cancel failed",
      );
    }
  }
  if (protectiveIds.length > 0) {
    try {
      await cancelAlgoOrderIds(symbol, protectiveIds);
    } catch (e) {
      logger.warn(
        { uid: row.uid, symbol, err: (e as Error).message },
        "[grid-engine] reconcile protective cancel failed",
      );
    }
  }
  clearSentTargets(row.uid);

  exitInFlight.add(row.uid);
  try {
    await finalizeGrid(row, symbol, {
      closedSide,
      reason,
      exitAvgPrice,
      exitQty,
      fundingUsd: 0,
      price,
    });
  } finally {
    exitInFlight.delete(row.uid);
  }
  return true;
}

async function handleActive(row: GridRow, symbol: string, price: number): Promise<void> {
  const patch: GridPatch = {};
  if (row.lastPrice !== price) patch.lastPrice = price;

  const mid = numOr0(row.midPrice);
  const levelPrices = parseArray(row.levelPrices)
    .map((v) => Number(v))
    .filter((n) => Number.isFinite(n) && n > 0);
  const orderIds = parseIntArray(row.testnetOrderIds);

  let fillEntries = parseFills(row.fillEntries);
  let positionAmt: number | null = typeof row.positionAmt === "number" ? row.positionAmt : null;

  // 1. Опрос филлов и позиции через общий батч.
  if (orderIds.length > 0) {
    try {
      const fills = await fetchGridFills(symbol, orderIds, createdAtMs(row.created_at));
      const nextFills: FillEntry[] = [];
      for (const r of fills.results) {
        if ("error" in r) continue;
        const executedQty = numOr0(r.executedQty);
        if (!(executedQty > 0)) continue;
        const px = numOr0(r.price);
        const rawSide = String(r.side ?? "").toUpperCase();
        const side: LevelSide =
          rawSide === "BUY" || rawSide === "SELL" ? rawSide : px > mid ? "SELL" : "BUY";
        nextFills.push({
          level: px,
          side,
          qty: executedQty,
          entry: numOr0(r.avgPrice) || px,
          cumQuote: numOr0(r.cumQuote),
        });
      }
      const nextOpenLots = nextFills.map((f) => f.level).sort((a, b) => a - b);
      if (JSON.stringify(nextFills) !== JSON.stringify(fillEntries)) {
        patch.fillEntries = JSON.stringify(nextFills);
      }
      const prevLots = parseArray(row.openLots).map((v) => Number(v));
      if (JSON.stringify(nextOpenLots) !== JSON.stringify(prevLots)) {
        patch.openLots = JSON.stringify(nextOpenLots);
      }
      fillEntries = nextFills;

      const resPos = fills.positionAmt;
      if (resPos != null && Number.isFinite(resPos)) {
        if (row.positionAmt !== resPos) patch.positionAmt = resPos;
        positionAmt = resPos;
      } else if (resPos === null) {
        if (row.positionAmt !== null) patch.positionAmt = null;
        positionAmt = null;
      }
    } catch (e) {
      logger.warn(
        { uid: row.uid, symbol, err: (e as Error).message },
        "[grid-engine] fills poll failed",
      );
    }
  }

  // 2. Реконсиляция (phase 2b): позиция обнулилась при наличии филлов — значит
  // защитный ордер сработал на бирже. Финализируем и прекращаем обработку строки.
  if (positionAmt === 0 && fillEntries.length > 0) {
    const handled = await reconcileFlatGrid(row, symbol, price, fillEntries);
    if (handled) return;
  }

  // 3. Средние по сторонам и целевые TP/SL по формулам клиента. Цены сразу
  // округляются до tickSize символа: биржа хранит живой триггер уже округлённым,
  // поэтому сравнение «как есть» с неокруглённым float даёт ложное устаревание.
  const tTick = await getSymbolTickSize(symbol);

  const buyFills = sideFills(fillEntries, "BUY");
  const sellFills = sideFills(fillEntries, "SELL");
  const avgLong = avgEntry(buyFills);
  const avgShort = avgEntry(sellFills);
  const hasLong = buyFills.length > 0;
  const hasShort = sellFills.length > 0;

  const longLevels = levelPrices.filter((l) => l < mid);
  const shortLevels = levelPrices.filter((l) => l > mid);
  const outerLong = longLevels.length ? Math.min(...longLevels) : numOr0(row.lo);
  const outerShort = shortLevels.length ? Math.max(...shortLevels) : numOr0(row.hi);

  const slPct = numOr0(row.slPct);
  const edgePct = numOr0(row.edgePct);
  const tpPct = numOr0(row.tpPct);

  const longSl = roundPrice(
    avgLong > 0
      ? Math.max(avgLong * (1 - slPct / 100), outerLong * (1 - edgePct / 100))
      : outerLong * (1 - edgePct / 100),
    tTick,
  );
  const shortSl = roundPrice(
    avgShort > 0
      ? Math.min(avgShort * (1 + slPct / 100), outerShort * (1 + edgePct / 100))
      : outerShort * (1 + edgePct / 100),
    tTick,
  );
  const longTpRaw = avgLong > 0 ? Math.min(avgLong * (1 + tpPct / 100), mid) : null;
  const shortTpRaw = avgShort > 0 ? Math.max(avgShort * (1 - tpPct / 100), mid) : null;
  const longTp = longTpRaw != null ? roundPrice(longTpRaw, tTick) : null;
  const shortTp = shortTpRaw != null ? roundPrice(shortTpRaw, tTick) : null;

  // 4. Синхронизация защитных STOP/TP (place / replace / cancel).
  let mutated = false;
  let placementError: string | null = null;
  let stopIds = parseIntArray(row.stopOrderIds);
  let tpIds = tpIdsOf(row.tpOrderIds);
  let tpPrices = tpPricesOf(row.tpOrderPrices);

  if (canMutate(row.uid)) {
    let openAlgo: OpenAlgoOrder[] | null = null;
    try {
      openAlgo = await fetchOpenAlgoOrders(symbol);
    } catch (e) {
      logger.warn(
        { uid: row.uid, symbol, err: (e as Error).message },
        "[grid-engine] open algo fetch failed, skipping protective sync",
      );
    }

    if (openAlgo) {
      const stopBy: { long?: number; short?: number } = {};
      const stopPriceBy: { long?: number; short?: number } = {};
      const tpBy: { long?: number; short?: number } = {};
      const tpPriceBy: { long?: number; short?: number } = {};
      for (const o of openAlgo) {
        const cid = String(o.clientAlgoId ?? "");
        const id = Number(o.algoId);
        if (!Number.isFinite(id) || id <= 0) continue;
        const sideName = cid.endsWith("_long") ? "long" : cid.endsWith("_short") ? "short" : null;
        if (!sideName) continue;
        if (cid.startsWith("gridsl_")) {
          stopBy[sideName] = id;
          if (o.triggerPrice > 0) stopPriceBy[sideName] = o.triggerPrice;
        } else if (cid.startsWith("gridtp_")) {
          tpBy[sideName] = id;
          if (o.triggerPrice > 0) tpPriceBy[sideName] = o.triggerPrice;
        }
      }

      const posKnown = positionAmt != null && Number.isFinite(positionAmt);
      const hasPosLong = posKnown && (positionAmt as number) > 0;
      const hasPosShort = posKnown && (positionAmt as number) < 0;
      const stopSet = new Set<number>(stopIds);

      // STOP: по фактической позиции; при нулевой стороне — снимаем.
      for (const sideName of ["long", "short"] as const) {
        const want = sideName === "long" ? longSl : shortSl;
        const liveId = stopBy[sideName];
        const hasPos = sideName === "long" ? hasPosLong : hasPosShort;
        if (hasPos) {
          const memo = liveId == null && memoHit(row.uid, "stop", sideName, want, tTick);
          if (!memo && (liveId == null || needsReplace(stopPriceBy[sideName], want, tTick))) {
            let applied = false;
            const err = await guardedAttempt(row.uid, "stop", sideName, async () => {
              const res = await placeGridStop({
                symbol,
                direction: sideName,
                triggerPrice: want,
                algoId: liveId ?? null,
              });
              if (liveId != null) stopSet.delete(liveId);
              stopSet.add(res.algoId);
              // Сохраняем фактически выставленный (округлённый) триггер.
              lastSentTargets.set(targetKey(row.uid, "stop", sideName), res.triggerPrice);
              applied = true;
            });
            if (err) placementError = err;
            if (applied) {
              markMutation(row.uid);
              mutated = true;
            }
          }
        } else if (posKnown && liveId != null) {
          let applied = false;
          const err = await guardedAttempt(row.uid, "stop", sideName, async () => {
            const res = await cancelAlgoOrderIds(symbol, [liveId]);
            if (res.errors.length > 0) throw new Error(res.errors[0].error);
            stopSet.delete(liveId);
            clearSentTargets(row.uid, "stop", sideName);
            applied = true;
          });
          if (err) placementError = err;
          if (applied) {
            markMutation(row.uid);
            mutated = true;
          }
        }
      }
      stopIds = Array.from(stopSet);

      // TP: по наличию филлов стороны; при их отсутствии — снимаем.
      const nextTpIds: { long?: number; short?: number } = { ...tpIds };
      const nextTpPrices: { long?: number; short?: number } = { ...tpPrices };
      for (const sideName of ["long", "short"] as const) {
        const hasFills = sideName === "long" ? hasLong : hasShort;
        const target = sideName === "long" ? longTp : shortTp;
        const liveId = tpBy[sideName];
        if (hasFills && target != null) {
          const memo = liveId == null && memoHit(row.uid, "tp", sideName, target, tTick);
          if (!memo && (liveId == null || needsReplace(tpPriceBy[sideName], target, tTick))) {
            let applied = false;
            const err = await guardedAttempt(row.uid, "tp", sideName, async () => {
              const res = await syncGridTp({
                symbol,
                direction: sideName,
                tpPrice: target,
                tpOrderId: liveId ?? null,
              });
              if (res.tpOrderId != null) {
                const stored = res.tpPrice ?? target;
                nextTpIds[sideName] = res.tpOrderId;
                nextTpPrices[sideName] = stored;
                // Сохраняем фактически выставленный (округлённый) TP.
                lastSentTargets.set(targetKey(row.uid, "tp", sideName), stored);
                applied = true;
              }
            });
            if (err) placementError = err;
            if (applied) {
              markMutation(row.uid);
              mutated = true;
            }
          }
        } else {
          const tracked = nextTpIds[sideName];
          const id = liveId ?? (typeof tracked === "number" ? tracked : undefined);
          if (id != null) {
            let applied = false;
            const err = await guardedAttempt(row.uid, "tp", sideName, async () => {
              const res = await cancelAlgoOrderIds(symbol, [id]);
              if (res.errors.length > 0) throw new Error(res.errors[0].error);
              delete nextTpIds[sideName];
              delete nextTpPrices[sideName];
              clearSentTargets(row.uid, "tp", sideName);
              applied = true;
            });
            if (err) placementError = err;
            if (applied) {
              markMutation(row.uid);
              mutated = true;
            }
          }
        }
      }
      tpIds = nextTpIds;
      tpPrices = nextTpPrices;
    }
  }

  // 5. Триггеры TP/SL (phase 2b): по фактической позиции и текущей марк-цене
  // закрываем сторону по рынку общим close-хелпером и финализируем.
  if (
    !exitInFlight.has(row.uid) &&
    positionAmt != null &&
    Number.isFinite(positionAmt) &&
    positionAmt !== 0
  ) {
    let exitSide: "long" | "short" | null = null;
    let exitReason: "tp" | "sl" | null = null;
    if (positionAmt > 0) {
      if (price <= longSl) {
        exitSide = "long";
        exitReason = "sl";
      } else if (longTp != null && price >= longTp) {
        exitSide = "long";
        exitReason = "tp";
      }
    } else {
      if (price >= shortSl) {
        exitSide = "short";
        exitReason = "sl";
      } else if (shortTp != null && price <= shortTp) {
        exitSide = "short";
        exitReason = "tp";
      }
    }
    if (exitSide && exitReason) {
      const handled = await triggerClose(row, symbol, exitSide, exitReason, price, {
        buyFills,
        sellFills,
      });
      if (handled) return;
    }
  }

  // 6. Персист изменений.
  const nextStopIdsJson = JSON.stringify(stopIds);
  if (nextStopIdsJson !== JSON.stringify(parseIntArray(row.stopOrderIds))) {
    patch.stopOrderIds = nextStopIdsJson;
  }
  const nextTpIdsJson = JSON.stringify(tpIds);
  if (nextTpIdsJson !== JSON.stringify(tpIdsOf(row.tpOrderIds))) {
    patch.tpOrderIds = nextTpIdsJson;
  }
  const nextTpPricesJson = JSON.stringify(tpPrices);
  if (nextTpPricesJson !== JSON.stringify(tpPricesOf(row.tpOrderPrices))) {
    patch.tpOrderPrices = nextTpPricesJson;
  }
  if (mutated || placementError) {
    patch.lastPlacementError = placementError;
  }

  const meaningful = Object.keys(patch).some((k) => k !== "lastPrice");
  if (Object.keys(patch).length > 0) {
    await updateGrid(row.uid, patch);
  }
  if (meaningful) {
    logger.info(
      {
        uid: row.uid,
        symbol,
        phase: "active",
        positionAmt,
        fills: fillEntries.length,
        stopIds,
        tpIds,
        mutated,
        error: placementError,
      },
      "[grid-engine] active update",
    );
  }
}

/**
 * Auto-mode (GRID_AUTO_ENABLED=true): создаёт waiting-сетки engine='server' по
 * символам ботов, у которых ADX любого из таймфреймов < gate. Пропускает символы
 * с запущенным ботом и уже существующей waiting/active сеткой; ограничивает число
 * новых сеток за tick (GRID_AUTO_MAX) и общее число active/waiting
 * (GRID_AUTO_TOTAL_MAX). Ошибка одного кандидата не прерывает остальные.
 */
async function runAutoMode(): Promise<void> {
  if (!envFlag("GRID_AUTO_ENABLED", false)) return;

  const maxNewPerTick = Math.max(0, envInt("GRID_AUTO_MAX", DEFAULT_AUTO_MAX));
  const totalCap = Math.max(0, envInt("GRID_AUTO_TOTAL_MAX", DEFAULT_AUTO_TOTAL_MAX));
  const orderSizeUsd = envNumber("GRID_AUTO_ORDER_USD", DEFAULT_AUTO_ORDER_USD);
  const leverageRaw = envInt("GRID_AUTO_LEVERAGE", DEFAULT_AUTO_LEVERAGE);
  const leverage = Math.min(Math.max(leverageRaw, 1), MAX_LEVERAGE);
  if (maxNewPerTick <= 0 || totalCap <= 0 || !(orderSizeUsd > 0)) return;

  // Все waiting/active сетки (любого engine) — busy-индикатор: не создаём вторую
  // сетку по символу, которым уже управляет браузер или сервер.
  const allGrids = await db.select().from(gridsTable);
  const activeWaiting = allGrids.filter((g) => g.phase === "waiting" || g.phase === "active");
  if (activeWaiting.length >= totalCap) return;

  const busySymbols = new Set(activeWaiting.map((g) => String(g.symbol ?? "").toUpperCase()));
  const { symbols, running } = await getBotSymbols();
  const slots = Math.min(maxNewPerTick, totalCap - activeWaiting.length);
  if (slots <= 0) return;

  const created: Array<{ uid: string; symbol: string; timeframe: string }> = [];
  for (const rawSymbol of symbols) {
    if (created.length >= slots) break;
    const symbol = String(rawSymbol ?? "").toUpperCase();
    if (!symbol || busySymbols.has(symbol) || running.has(symbol)) continue;

    try {
      let timeframe: string | null = null;
      for (const tf of AUTO_TIMEFRAMES) {
        const adx = await computeAdxFor(symbol, tf);
        if (adx != null && adx < DEFAULT_GATE) {
          timeframe = tf;
          break;
        }
      }
      if (!timeframe) continue;

      const bounds = await computeGridBounds(symbol, timeframe);
      if (!bounds) continue;
      const levelPrices = gridLevelPrices(bounds.lo, bounds.hi, AUTO_GRID_LEVELS);
      if (levelPrices.length === 0) continue;

      const now = new Date().toISOString();
      const uid = `srv_${symbol}_${timeframe}_${Date.now().toString(36)}_${Math.random()
        .toString(36)
        .slice(2, 7)}`.slice(0, 64);
      await db.insert(gridsTable).values({
        uid,
        symbol,
        timeframe,
        phase: "waiting",
        direction: "both",
        lo: bounds.lo,
        hi: bounds.hi,
        midPrice: bounds.mid,
        gate: DEFAULT_GATE,
        levels: levelPrices.length + 1,
        levelPrices: JSON.stringify(levelPrices),
        tpPct: AUTO_TP_PCT,
        slPct: AUTO_SL_PCT,
        edgePct: AUTO_EDGE_PCT,
        orderSizeUsd,
        leverage,
        testnetOrderIds: JSON.stringify([]),
        stopOrderIds: JSON.stringify([]),
        tpOrderIds: JSON.stringify({}),
        tpOrderPrices: JSON.stringify({}),
        fillEntries: JSON.stringify([]),
        openLots: JSON.stringify([]),
        positionAmt: 0,
        realizedPnl: 0,
        realizedUsd: 0,
        lastPrice: getMarkPrice(symbol),
        lastPlacementError: null,
        engine: "server",
        created_at: now,
        updated_at: now,
      });
      busySymbols.add(symbol);
      created.push({ uid, symbol, timeframe });
    } catch (e) {
      logger.warn(
        { symbol, err: (e as Error).message },
        "[grid-engine] auto-mode candidate failed",
      );
    }
  }

  if (created.length > 0) {
    logger.info(
      {
        created,
        count: created.length,
        active: activeWaiting.length + created.length,
        totalCap,
      },
      "[grid-engine] auto-mode summary",
    );
  }
}

async function processGrid(row: GridRow): Promise<void> {
  const symbol = String(row.symbol ?? "").trim().toUpperCase();
  if (!symbol) return;

  const price = getMarkPrice(symbol);
  if (price == null) {
    logger.debug({ uid: row.uid, symbol }, "[grid-engine] no mark price, skip");
    return;
  }

  const phase = String(row.phase ?? "");
  if (phase === "waiting") {
    await handleWaiting(row, symbol, price);
  } else if (phase === "active") {
    await handleActive(row, symbol, price);
  }
}

/**
 * Запускает фоновый движок сеток. INERT по умолчанию: без
 * GRID_ENGINE_ENABLED=true не стартует и ничего не трогает. Работает только со
 * строками grids.engine='server'.
 *
 * @returns stop-функцию (если таймер запущен) либо null (если движок выключен).
 */
export function startGridEngine(): (() => void) | null {
  const enabled = envFlag("GRID_ENGINE_ENABLED", false);
  if (!enabled) {
    logger.info(
      "Grid engine disabled (GRID_ENGINE_ENABLED != true); engine not started",
    );
    return null;
  }

  const intervalMs = envIntervalMs("GRID_ENGINE_INTERVAL_MS", DEFAULT_INTERVAL_MS);
  logger.info(
    { intervalMs, auto: envFlag("GRID_AUTO_ENABLED", false) },
    "Grid engine started (phase 2b: activate/fills/protection/close/finalize/auto)",
  );

  let tickInFlight = false;

  const tick = async (): Promise<void> => {
    if (tickInFlight) return;
    tickInFlight = true;
    try {
      clearTickSizeCache();
      await ensureTables;
      const rows: GridRow[] = await db
        .select()
        .from(gridsTable)
        .where(eq(gridsTable.engine, "server"))
        .orderBy(desc(gridsTable.id));

      for (const row of rows) {
        // Ошибка одной сетки не должна прерывать общий tick.
        try {
          await processGrid(row);
        } catch (e) {
          const message = e instanceof Error ? e.message : String(e);
          logger.warn({ uid: row.uid, err: message }, "[grid-engine] grid tick failed");
          try {
            await updateGrid(row.uid, { lastPlacementError: message });
          } catch {
            // игнорируем вторичную ошибку записи
          }
        }
      }

      // Auto-mode: создание новых waiting-сеток. Ошибка не должна ронять tick.
      try {
        await runAutoMode();
      } catch (e) {
        logger.warn({ err: (e as Error).message }, "[grid-engine] auto-mode failed");
      }
    } catch (e) {
      logger.warn({ err: e }, "[grid-engine] tick failed");
    } finally {
      tickInFlight = false;
    }
  };

  void tick();
  const timer = setInterval(() => {
    void tick();
  }, intervalMs);

  return () => clearInterval(timer);
}
