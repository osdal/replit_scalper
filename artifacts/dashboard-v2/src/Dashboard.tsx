import { useState, useEffect, useCallback, useRef } from "react";
import { fetchPairs, fetchHistory, fetchLastPrice, fetchAdx, fetchBotsStatus, sendTelegramNotify, sendTelegramStart, saveGridResult, fetchGridHistory, clearGridHistory, createTestnetGridOrders, cancelTestnetGridOrders, cancelGridStops, fetchGridFills, fetchUserTrades, resizeGridOrders, closeGridPosition, resetGridAccount, stopAllBotsAndReset, fetchGridResetStatus, upsertGridTp, placeGridStop, fetchOpenAlgo, fetchAlgoStatus, fetchServerGrids, importServerGrids, patchGrid, deleteAllGrids, fetchGridEngineConfig, updateGridEngineConfig } from "./hooks/useApi";
import type { GridEngineConfig } from "./hooks/useApi";
import { timeframePassesGridGate, getTimeframeAdx } from "./lib/gridGate";
import { Button } from "./components/ui/button";
import * as lightweightCharts from "lightweight-charts";

export interface Candle {
  time: string | number;
  open: number;
  high: number;
  low: number;
  close: number;
}

type GridPhase = "waiting" | "active" | "done" | "stopped";

type GridFill = { level: number; side: "BUY" | "SELL"; qty: number; entry: number; cumQuote: number; time?: number };

type CloseReason = "tp" | "sl" | "stop";

interface GridCloseRequest {
  id: number;
  pair: string;
  reason: CloseReason;
  direction?: "long" | "short";
  quantity?: number;
  tpPct: number;
  sinceMs: number;
}

interface GridTpRequest {
  id: number;
  pair: string;
  direction: "long" | "short";
  tpPrice: number | null;
  tpOrderId?: number | null;
}

interface GridInstance {
  id: number;
  // Phase 3: uid строки в БД /api/grids (для PATCH/DELETE). В browser-режиме не задаётся.
  uid?: string;
  pair: string;
  timeframe: string;
  tpPct: number;
  // Стоп-лосс: slPct% от средней цены входа (ограничивает риск на инвентарь),
  // edgePct% от крайнего ордера сетки (бэкстоп, когда усреднения ещё нет).
  slPct: number;
  edgePct: number;
  gate: number;
  levels: number;
  lo: number;
  hi: number;
  midPrice: number;
  startPrice: number;
  startSide: "above" | "below";
  phase: GridPhase;
  unrealizedPnl: number | null;
  realizedPnl: number;
  realizedUsd: number;
  lastResult: string | null;
  createdAt: number;
  orderSizeUsd: number;
  levelPrices: number[];
  openLots: number[];
  lastPrice: number | null;
  testnetOrderIds: number[];
  stopOrderIds?: number[];
  tpOrderIds?: { long?: number; short?: number };
  tpOrderPrices?: { long?: number; short?: number };
  // Сигнатура набора филлов стороны на момент последнего авто-синка TP:
  // меняется только при новом исполнении, не при правке tpPct.
  tpAutoSig?: { long?: string; short?: string };
  // Ручное выставление TP в процессе (кнопка в строке сетки).
  tpCommitBusy?: boolean;
  lastPlacementError?: string;
  failedLevels?: number[];
  skippedLevels?: number[];
  fillEntries?: GridFill[];
  // Resting-ордера сетки с ценой и стороной: нужны, чтобы изменить размер
  // (resize) по каждому orderId до первого филла.
  entryOrders?: { orderId: number; price: number; side: "BUY" | "SELL" }[];
  positionAmt?: number | null;
  longExitInFlight?: boolean;
  shortExitInFlight?: boolean;
}

// Список orderId защитных TP-ордеров сетки (long/short) для отмены.
export function tpOrderIdList(g: GridInstance): number[] {
  return [g.tpOrderIds?.long, g.tpOrderIds?.short].filter(
    (n): n is number => typeof n === "number" && Number.isFinite(n) && n > 0,
  );
}

export interface GridBounds {
  lo: number;
  hi: number;
  mid: number;
}

export function computeGridBounds(rows: Array<{ low: number; high: number }>, timeframe: string): GridBounds | null {
  if (rows.length < 20) return null;
  const candlesPerDay =
    timeframe === "5m" ? 288 : timeframe === "15m" ? 96 : timeframe === "30m" ? 48 : timeframe === "4h" ? 6 : timeframe === "12h" ? 2 : timeframe === "1h" ? 24 : 1;
  const lookback = 8 * candlesPerDay;
  const tail = rows.slice(-lookback);
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

export function computeGridLevels(rows: Array<{ low: number; high: number }>, timeframe: string, nLevels = 10) {
  const bounds = computeGridBounds(rows, timeframe);
  if (!bounds) return [] as { price: number; type: "grid" }[];
  return gridLevelPrices(bounds.lo, bounds.hi, nLevels).map((price) => ({
    price,
    type: "grid" as const,
  }));
}

// Цены уровней сетки: nLevels делений дают nLevels-1 внутренних уровней.
export function gridLevelPrices(lo: number, hi: number, nLevels: number): number[] {
  if (!(hi > lo) || nLevels < 2) return [];
  const step = (hi - lo) / nLevels;
  const out: number[] = [];
  for (let k = 1; k < nLevels; k++) out.push(Number((lo + step * k).toFixed(8)));
  return out;
}

function nLevelsOf(g: GridInstance): number {
  return g.levelPrices.length > 0 ? g.levelPrices.length + 1 : g.levels + 1;
}

const hadPosition = (g: GridInstance) =>
  (g.fillEntries?.length ?? 0) > 0 || (g.openLots?.length ?? 0) > 0;

// Размер ордера фиксирован в $: qty_i = orderSizeUsd / entry_i, поэтому средняя
// цена входа, дающая ровно tpPct% прибыли на весь инвентарь, — гармоническое
// среднее: n / Σ(1/entry_i).
export function avgEntryByNotional(lots: number[]): number | null {
  if (lots.length === 0) return null;
  let invSum = 0;
  for (const e of lots) {
    if (e > 0) invSum += 1 / e;
  }
  if (!(invSum > 0)) return null;
  return lots.length / invSum;
}

export function tpTargetFromLots(lots: number[], tpPct: number): number | null {
  const avg = avgEntryByNotional(lots);
  return avg == null ? null : avg * (1 + tpPct / 100);
}

// Нереализованный PnL в % от вложенного номинала ($ на лот).
export function openPnlPct(lots: number[], price: number): number | null {
  const avg = avgEntryByNotional(lots);
  if (avg == null || !(price > 0)) return null;
  return (price / avg - 1) * 100;
}

// Реальные комиссии Binance USDⓈ-M (taker/maker), в процентах от номинала.
const MAKER_FEE_PCT = 0.02;
const TAKER_FEE_PCT = 0.05;

// Средняя цена реальных исполнений: сумма quote / сумма qty.
function realEntryAvg(fills: GridFill[] | undefined): number {
  if (!fills || fills.length === 0) return 0;
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

// Реальный номинал входа в USD: сумма cumQuote по исполненным лотам.
function realNotionalUsd(fills: GridFill[] | undefined): number {
  if (!fills) return 0;
  let quote = 0;
  for (const f of fills) {
    if (Number.isFinite(f.cumQuote)) quote += f.cumQuote;
  }
  return quote;
}

// Реальный PnL закрытия: gross = exit*qty - ΣcumQuote, минус maker/taker fee, плюс funding.
function realPnlUsd(fills: GridFill[] | undefined, exitAvgPrice: number, exitQty: number, fundingUsd: number): number {
  const entryNotional = realNotionalUsd(fills);
  const gross = exitAvgPrice * exitQty - entryNotional;
  const fees = (entryNotional * MAKER_FEE_PCT) / 100 + (exitAvgPrice * exitQty * TAKER_FEE_PCT) / 100;
  return gross - fees + fundingUsd;
}

// --- Двусторонняя сетка: helpers по сторонам (BUY = long, SELL = short) ---

export function sideFills(fills: GridFill[] | undefined, side: "BUY" | "SELL"): GridFill[] {
  if (!fills) return [];
  return fills.filter((f) => f.side === side);
}

// Σ cumQuote / Σ qty
export function avgEntry(fills: GridFill[] | undefined): number {
  if (!fills || fills.length === 0) return 0;
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

// Σ cumQuote
export function notional(fills: GridFill[] | undefined): number {
  if (!fills) return 0;
  let quote = 0;
  for (const f of fills) {
    if (Number.isFinite(f.cumQuote)) quote += f.cumQuote;
  }
  return quote;
}

export function sumQty(fills: GridFill[] | undefined): number {
  if (!fills) return 0;
  let qty = 0;
  for (const f of fills) {
    if (Number.isFinite(f.qty)) qty += f.qty;
  }
  return qty;
}

// PnL одной стороны: BUY зарабатывает на росте (exitN - entryN), SELL — на падении.
export function sidePnlUsd(
  side: "BUY" | "SELL",
  fills: GridFill[] | undefined,
  exitAvgPrice: number,
  exitQty: number,
  fundingUsd: number,
): number {
  const entryN = notional(fills);
  const exitN = exitAvgPrice * exitQty;
  const gross = side === "BUY" ? exitN - entryN : entryN - exitN;
  const fees = (entryN * MAKER_FEE_PCT) / 100 + (exitN * TAKER_FEE_PCT) / 100;
  return gross - fees + fundingUsd;
}

// Стоп-лосс сетки по двум параметрам: SL% от средней цены входа (ограничивает
// риск на инвентарь) и Edge% от крайнего ордера сетки (бэкстоп, когда входа
// ещё нет). Из двух значений применяется более близкое к цене.
export function computeGridStops(
  g: GridInstance,
  avgLong: number,
  avgShort: number,
): { long: number; short: number } {
  const longLevelsOf = (x: GridInstance) => x.levelPrices.filter((l) => l < x.midPrice);
  const shortLevelsOf = (x: GridInstance) => x.levelPrices.filter((l) => l > x.midPrice);
  const outerLong = longLevelsOf(g).length ? Math.min(...longLevelsOf(g)) : g.lo;
  const outerShort = shortLevelsOf(g).length ? Math.max(...shortLevelsOf(g)) : g.hi;
  const longSl =
    avgLong > 0
      ? Math.max(avgLong * (1 - g.slPct / 100), outerLong * (1 - g.edgePct / 100))
      : outerLong * (1 - g.edgePct / 100);
  const shortSl =
    avgShort > 0
      ? Math.min(avgShort * (1 + g.slPct / 100), outerShort * (1 + g.edgePct / 100))
      : outerShort * (1 + g.edgePct / 100);
  return { long: longSl, short: shortSl };
}

// Algo-ордер считается исполненным по этим статусам Binance Algo API.
function isTriggeredAlgo(status: unknown): boolean {
  const s = String(status ?? "").toUpperCase();
  return s === "TRIGGERED" || s === "FINISHED";
}

const PERSIST_KEY = "gridsim.state.v1";
// Phase 3: архив мигрированного в БД состояния браузера (данные не теряются).
const PERSIST_MIGRATED_KEY = "gridsim.state.v1.migrated";
const RESET_SEEN_KEY = "gridsim.resetSeenAt";

// Phase 3: режим движка сеток. "browser" (по умолчанию) — всё как раньше;
// "server" — дашборд становится наблюдателем, источник истины — БД /api/grids.
const SERVER_ENGINE = (import.meta.env.VITE_GRID_ENGINE ?? "browser") === "server";

interface PersistedState {
  grids?: GridInstance[];
  timeframe?: string;
  gridGate?: number;
  gridNLevels?: number;
  tpPct?: number | null;
  selectedPair?: string | null;
  activeTab?: "stats" | "history";
  tradeMode?: "manual" | "auto";
  orderSizeUsd?: number;
}

let persistedCache: PersistedState | null = null;

function readPersisted(): PersistedState {
  if (persistedCache) return persistedCache;
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(PERSIST_KEY) : null;
    persistedCache = raw ? (JSON.parse(raw) as PersistedState) : {};
  } catch {
    persistedCache = {};
  }
  return persistedCache;
}

// Читает сохранённую карту orderId/цен TP по сторонам, отбрасывая мусор.
function parseTpMap(raw: any): { long?: number; short?: number } | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const out: { long?: number; short?: number } = {};
  for (const side of ["long", "short"] as const) {
    const v = (raw as any)[side];
    if (v === null || v === undefined) continue;
    const n = Number(v);
    if (Number.isFinite(n) && n > 0) out[side] = n;
  }
  return out.long == null && out.short == null ? undefined : out;
}

// Читает сохранённую карту сигнатур филлов TP по сторонам (та же защита, что
// и у parseTpMap, но значения — строки-сигнатуры `${count}:${avg}`).
function parseTpSigMap(raw: any): { long?: string; short?: string } | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const out: { long?: string; short?: string } = {};
  for (const side of ["long", "short"] as const) {
    const v = (raw as any)[side];
    if (typeof v !== "string" || v.length === 0) continue;
    out[side] = v;
  }
  return out.long == null && out.short == null ? undefined : out;
}

function loadPersistedGrids(): GridInstance[] {
  const raw = readPersisted().grids;
  if (!Array.isArray(raw)) return [];
  const phases: GridPhase[] = ["waiting", "active", "done", "stopped"];
  return raw
    .filter(
      (x: any) =>
        x &&
        typeof x === "object" &&
        Number.isFinite(Number(x.id)) &&
        typeof x.pair === "string",
    )
    .map((x: any) => ({
      id: Number(x.id),
      pair: String(x.pair),
      timeframe: String(x.timeframe ?? "1d"),
      tpPct: Number(x.tpPct ?? 0),
      slPct: (() => {
        const n = Number(x.slPct);
        return Number.isFinite(n) && n > 0 ? n : 2;
      })(),
      edgePct: (() => {
        const n = Number(x.edgePct);
        return Number.isFinite(n) && n > 0 ? n : 2;
      })(),
      gate: Number(x.gate ?? 0),
      levels: Number(x.levels ?? 10),
      lo: Number(x.lo ?? 0),
      hi: Number(x.hi ?? 0),
      midPrice: Number(x.midPrice ?? 0),
      startPrice: Number(x.startPrice ?? x.midPrice ?? 0),
      startSide: x.startSide === "above" ? "above" : "below",
      phase: phases.includes(x.phase) ? x.phase : "waiting",
      unrealizedPnl: x.unrealizedPnl == null ? null : Number(x.unrealizedPnl),
      realizedPnl: Number(x.realizedPnl ?? 0),
      realizedUsd: Number(x.realizedUsd ?? 0),
      lastResult: x.lastResult == null ? null : String(x.lastResult),
      createdAt: Number(x.createdAt ?? Date.now()),
      orderSizeUsd: Number(x.orderSizeUsd ?? 10),
      levelPrices: Array.isArray(x.levelPrices)
        ? x.levelPrices.map(Number).filter((n: number) => Number.isFinite(n))
        : gridLevelPrices(Number(x.lo ?? 0), Number(x.hi ?? 0), Number(x.levels ?? 10) + 1),
      openLots: Array.isArray(x.openLots)
        ? x.openLots.map(Number).filter((n: number) => Number.isFinite(n))
        : [],
      lastPrice: x.lastPrice == null ? null : Number(x.lastPrice),
      testnetOrderIds: Array.isArray(x.testnetOrderIds)
        ? x.testnetOrderIds.map(Number).filter((n: number) => Number.isFinite(n))
        : [],
      stopOrderIds: Array.isArray(x.stopOrderIds)
        ? x.stopOrderIds.map(Number).filter((n: number) => Number.isFinite(n))
        : undefined,
      tpOrderIds: parseTpMap(x.tpOrderIds),
      tpOrderPrices: parseTpMap(x.tpOrderPrices),
      tpAutoSig: parseTpSigMap(x.tpAutoSig),
      lastPlacementError: x.lastPlacementError ?? undefined,
      failedLevels: Array.isArray(x.failedLevels)
        ? x.failedLevels.map(Number).filter((n: number) => Number.isFinite(n))
        : undefined,
      skippedLevels: Array.isArray(x.skippedLevels)
        ? x.skippedLevels.map(Number).filter((n: number) => Number.isFinite(n))
        : undefined,
      fillEntries: Array.isArray(x.fillEntries)
        ? x.fillEntries
            .map((f: any) => ({
              level: Number(f?.level ?? 0),
              side: (f?.side === "SELL" ? "SELL" : "BUY") as "BUY" | "SELL",
              qty: Number(f?.qty ?? 0),
              entry: Number(f?.entry ?? 0),
              cumQuote: Number(f?.cumQuote ?? 0),
              time: Number.isFinite(Number(f?.time)) ? Number(f?.time) : undefined,
            }))
            .filter(
              (f: GridFill) =>
                Number.isFinite(f.level) &&
                Number.isFinite(f.qty) &&
                Number.isFinite(f.entry) &&
                Number.isFinite(f.cumQuote),
            )
        : undefined,
      entryOrders: Array.isArray(x.entryOrders)
        ? x.entryOrders
            .map((o: any) => ({
              orderId: Number(o?.orderId),
              price: Number(o?.price),
              side: (o?.side === "SELL" ? "SELL" : "BUY") as "BUY" | "SELL",
            }))
            .filter((o: any) => Number.isFinite(o.orderId) && Number.isFinite(o.price))
        : undefined,
      positionAmt:
        typeof x.positionAmt === "number" && Number.isFinite(x.positionAmt)
          ? x.positionAmt
          : undefined,
    }));
}

// Phase 3: uid строки серверной сетки (PATCH/DELETE идут по uid; для
// мигрированных браузерных сеток uid = их числовой id).
function gridUid(g: GridInstance): string {
  return g.uid != null && g.uid !== "" ? g.uid : String(g.id);
}

// Phase 3: строка GET /api/grids (JSON-колонки уже распарсены API) -> GridInstance.
function mapServerGrid(row: any): GridInstance {
  const phases: GridPhase[] = ["waiting", "active", "done", "stopped"];
  const numOr = (v: unknown, dflt: number): number => {
    const n = Number(v);
    return Number.isFinite(n) ? n : dflt;
  };
  const numArr = (v: unknown): number[] =>
    Array.isArray(v) ? v.map(Number).filter((n: number) => Number.isFinite(n)) : [];
  const createdAt = (() => {
    const t = row?.created_at ?? row?.createdAt;
    if (typeof t === "number" && Number.isFinite(t)) return t;
    const parsed = Date.parse(String(t ?? ""));
    return Number.isFinite(parsed) ? parsed : Date.now();
  })();
  const midPrice = numOr(row?.midPrice, 0);
  const fillEntries: GridFill[] | undefined = Array.isArray(row?.fillEntries)
    ? (row.fillEntries as any[])
        .map((f: any) => ({
          level: Number(f?.level ?? 0),
          side: (f?.side === "SELL" ? "SELL" : "BUY") as "BUY" | "SELL",
          qty: Number(f?.qty ?? 0),
          entry: Number(f?.entry ?? 0),
          cumQuote: Number(f?.cumQuote ?? 0),
          time: Number.isFinite(Number(f?.time)) ? Number(f?.time) : undefined,
        }))
        .filter(
          (f: GridFill) =>
            Number.isFinite(f.level) &&
            Number.isFinite(f.qty) &&
            Number.isFinite(f.entry) &&
            Number.isFinite(f.cumQuote),
        )
    : undefined;
  return {
    id: numOr(row?.id, 0),
    uid: row?.uid != null ? String(row.uid) : undefined,
    pair: String(row?.symbol ?? ""),
    timeframe: String(row?.timeframe ?? "1d"),
    tpPct: numOr(row?.tpPct, 0),
    slPct: (() => {
      const n = Number(row?.slPct);
      return Number.isFinite(n) && n > 0 ? n : 2;
    })(),
    edgePct: (() => {
      const n = Number(row?.edgePct);
      return Number.isFinite(n) && n > 0 ? n : 2;
    })(),
    gate: numOr(row?.gate, 0),
    levels: numOr(row?.levels, 10),
    lo: numOr(row?.lo, 0),
    hi: numOr(row?.hi, 0),
    midPrice,
    startPrice: midPrice,
    startSide: "below",
    phase: phases.includes(row?.phase) ? row.phase : "waiting",
    unrealizedPnl: null,
    realizedPnl: numOr(row?.realizedPnl, 0),
    realizedUsd: numOr(row?.realizedUsd, 0),
    lastResult: null,
    createdAt,
    orderSizeUsd: numOr(row?.orderSizeUsd, 100),
    levelPrices: numArr(row?.levelPrices),
    openLots: numArr(row?.openLots),
    lastPrice: row?.lastPrice == null ? null : Number(row.lastPrice),
    testnetOrderIds: numArr(row?.testnetOrderIds),
    stopOrderIds: Array.isArray(row?.stopOrderIds) ? numArr(row.stopOrderIds) : undefined,
    tpOrderIds: parseTpMap(row?.tpOrderIds),
    tpOrderPrices: parseTpMap(row?.tpOrderPrices),
    lastPlacementError: row?.lastPlacementError ?? undefined,
    fillEntries,
    positionAmt:
      typeof row?.positionAmt === "number" && Number.isFinite(row.positionAmt)
        ? row.positionAmt
        : undefined,
  };
}

// Phase 3: GridInstance -> тело POST /api/grids/import (поля схемы /api/grids).
function gridToServerImport(g: GridInstance): Record<string, unknown> {
  return {
    uid: String(g.id),
    symbol: g.pair,
    timeframe: g.timeframe,
    phase: g.phase,
    lo: g.lo,
    hi: g.hi,
    midPrice: g.midPrice,
    gate: g.gate,
    levels: g.levels,
    levelPrices: g.levelPrices,
    tpPct: g.tpPct,
    slPct: g.slPct,
    edgePct: g.edgePct,
    orderSizeUsd: g.orderSizeUsd,
    leverage: 50,
    testnetOrderIds: g.testnetOrderIds ?? [],
    stopOrderIds: g.stopOrderIds ?? [],
    tpOrderIds: g.tpOrderIds ?? {},
    tpOrderPrices: g.tpOrderPrices ?? {},
    fillEntries: g.fillEntries ?? [],
    openLots: g.openLots ?? [],
    positionAmt: g.positionAmt ?? null,
    realizedPnl: g.realizedPnl,
    realizedUsd: g.realizedUsd,
    lastPrice: g.lastPrice,
    lastPlacementError: g.lastPlacementError ?? null,
    engine: "server",
    createdAt: g.createdAt,
  };
}

// Человекочитаемые метки/цвета причин выхода. Возвращает null для причин,
// которые отображаются как есть (TP1/TP2/SL и прочие строки бота).
// REVERSE_TP/REVERSE_SL — прежние имена, в БД переименованы в
// REVERSE_BE/REVERSE_BACKSTOP; legacy REVERSE остаётся нейтральным.
export function exitReasonMeta(raw: unknown): { label: string; className: string; title: string } | null {
  const reason = String(raw ?? "").trim().toUpperCase();
  switch (reason) {
    case "REVERSE_BE":
    case "REVERSE_TP":
      return {
        label: "Reverse BE",
        className: "bg-blue-100 text-blue-700",
        title: "Reverse cycle closed at the break-even target (≈ fees only).",
      };
    case "REVERSE_BACKSTOP":
    case "REVERSE_SL":
      return {
        label: "Reverse backstop",
        className: "bg-red-100 text-red-700",
        title: "Reverse leg was closed by the exchange backstop — a real loss.",
      };
    case "REVERSE":
      return {
        label: "Reverse",
        className: "bg-amber-100 text-amber-700",
        title: "Legacy reverse cycle (not classified as break-even or backstop).",
      };
    default:
      return null;
  }
}

export default function Dashboard() {
  const [pairs, setPairs] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedPair, setSelectedPair] = useState<string | null>(() => readPersisted().selectedPair ?? null);
  const [chartData, setChartData] = useState<Candle[]>([]);
  const [chartLoading, setChartLoading] = useState(false);
  const [timeframe, setTimeframe] = useState<string>(() => readPersisted().timeframe ?? "1d");
  const [refreshTick, setRefreshTick] = useState(0);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [lastPrice, setLastPrice] = useState<number | null>(null);
  const [adxStatus, setAdxStatus] = useState<Record<string, { adx: number | null; gate: number; ok: boolean }>>({});
  const [botsStatus, setBotsStatus] = useState<Record<string, { is_running: boolean; position: any; current_price: number | null; last_heartbeat: string }>>({});
  const [gridNLevels, setGridNLevels] = useState<number>(() => readPersisted().gridNLevels ?? 10);
  const [gridGate, setGridGate] = useState<number>(() => readPersisted().gridGate ?? 15);
  const [tpPct, setTpPct] = useState<number | null>(() => readPersisted().tpPct ?? null);
  const [orderSizeUsd, setOrderSizeUsd] = useState<number>(() => readPersisted().orderSizeUsd ?? 100);
  // Phase 3: в server-режиме источник истины — БД, стартуем с пустого списка.
  const [grids, setGrids] = useState<GridInstance[]>(() => (SERVER_ENGINE ? [] : loadPersistedGrids()));
  // Phase 3: ошибка разовой миграции localStorage -> БД (показывается один раз).
  const [serverMigrationError, setServerMigrationError] = useState<string | null>(null);
  const serverMigrationRanRef = useRef(false);
  // Черновик поля Order $ по сетке: правится только здесь, g.orderSizeUsd не трогаем.
  const [orderSizeDrafts, setOrderSizeDrafts] = useState<Record<number, string>>({});
  // Сетки, для которых сейчас идёт resize на бирже.
  const [resizingIds, setResizingIds] = useState<Set<number>>(() => new Set());
  const [resetBusy, setResetBusy] = useState(false);
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false);
  const [resetCountdown, setResetCountdown] = useState(5);
  const [prices, setPrices] = useState<Record<string, number>>({});
  const [gridBoundsCache, setGridBoundsCache] = useState<Record<string, GridBounds>>({});
  const nextGridIdRef = useRef(1);
  const rePlacementDoneRef = useRef(false);
  // In-flight guard для синхронизации защитного TP: ключ `${gridId}:${direction}`.
  const tpInFlightRef = useRef<Set<string>>(new Set());
  // In-flight guard для защитного sync (stop + принятие algo-ордеров): ключ `${pair}:${direction}`.
  const protectiveInFlightRef = useRef<Set<string>>(new Set());
  // Метка последней защитной синхронизации по паре: не чаще раза в ~30 с.
  const protectiveSyncAtRef = useRef<Record<string, number>>({});
  const [activeTab, setActiveTab] = useState<"stats" | "history">(() => readPersisted().activeTab ?? "stats");
  const [tradeMode, setTradeMode] = useState<"manual" | "auto">(() => readPersisted().tradeMode ?? "manual");
  // Phase 3: конфиг серверного grid-движка (только server-режим). Источник истины
  // для Manual/Auto в server-режиме; в browser-режиме не используется.
  const [serverEngineConfig, setServerEngineConfig] = useState<GridEngineConfig | null>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyTick, setHistoryTick] = useState(0);
  const chartInstanceRef = useRef<any>(null);
  const chartSeriesRef = useRef<any>(null);
  const chartMarkersSeriesRef = useRef<any>(null);
  const chartRef = useRef<HTMLDivElement>(null);

  // Переживаем перезагрузку страницы: сохраняем сетки и настройки в localStorage.
  useEffect(() => {
    // Phase 3: в server-режиме состояние сеток живёт в БД, localStorage не пишем.
    if (SERVER_ENGINE) return;
    try {
      localStorage.setItem(
        PERSIST_KEY,
        JSON.stringify({ grids, timeframe, gridGate, gridNLevels, tpPct, selectedPair, activeTab, tradeMode, orderSizeUsd }),
      );
    } catch {
      // ignore quota/security errors
    }
  }, [grids, timeframe, gridGate, gridNLevels, tpPct, selectedPair, activeTab, tradeMode, orderSizeUsd]);

  // Phase 3: загрузка сеток из БД (источник истины в server-режиме).
  const loadServerGrids = useCallback(async () => {
    try {
      const res = await fetchServerGrids();
      const rows = Array.isArray(res?.results) ? res.results : [];
      setGrids(rows.map(mapServerGrid));
    } catch (e) {
      console.error("[GRID] server grids fetch failed", e);
    }
  }, []);

  // Phase 3: разовая миграция localStorage -> БД. Выполняется только когда
  // SERVER_ENGINE=true и существует ключ gridsim.state.v1. При успехе ключ
  // переносится в gridsim.state.v1.migrated (данные сохраняются); при ошибке
  // исходный ключ остаётся нетронутым и показывается ошибка. Повторно не мигрируем.
  useEffect(() => {
    if (!SERVER_ENGINE) return;
    if (serverMigrationRanRef.current) return;
    serverMigrationRanRef.current = true;
    let raw: string | null = null;
    try {
      raw = localStorage.getItem(PERSIST_KEY);
    } catch {
      return;
    }
    if (!raw) return;
    void (async () => {
      try {
        // Валидируем JSON и парсим через тот же нормализатор, что и browser-режим.
        const parsed = JSON.parse(raw) as PersistedState;
        if (!parsed || typeof parsed !== "object") throw new Error("invalid persisted state");
        const payload = loadPersistedGrids().map(gridToServerImport);
        const res = await importServerGrids(payload);
        if (!res.ok) throw new Error(res.error ?? "import failed");
        localStorage.setItem(PERSIST_MIGRATED_KEY, raw);
        localStorage.removeItem(PERSIST_KEY);
        console.log("[GRID] migration: browser state imported to DB", {
          imported: res.imported,
          count: payload.length,
        });
        await loadServerGrids();
      } catch (e: any) {
        console.error("[GRID] migration failed; local state kept", e);
        setServerMigrationError(e?.message || "migration failed");
      }
    })();
  }, [loadServerGrids]);

  // Phase 3: поллинг GET /api/grids (~5 c) — состояние сеток для viewer-режима.
  // В browser-режиме не запускается: поведение остаётся прежним.
  useEffect(() => {
    if (!SERVER_ENGINE) return;
    let cancelled = false;
    const poll = async () => {
      if (cancelled) return;
      try {
        const res = await fetchServerGrids();
        if (cancelled) return;
        const rows = Array.isArray(res?.results) ? res.results : [];
        setGrids(rows.map(mapServerGrid));
      } catch {
        // transient fetch errors keep the last known state
      }
    };
    poll();
    const id = setInterval(poll, 5_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Phase 3: конфиг серверного grid-движка — источник истины для Manual/Auto.
  // В browser-режиме ничего не запрашиваем: поведение остаётся прежним.
  useEffect(() => {
    if (!SERVER_ENGINE) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetchGridEngineConfig();
        if (cancelled || !res.ok) return;
        const cfg = res.config ?? null;
        setServerEngineConfig(cfg);
        if (typeof cfg?.autoEnabled === "boolean") {
          setTradeMode(cfg.autoEnabled ? "auto" : "manual");
        }
      } catch (e) {
        console.error("[GRID] grid-engine config fetch failed", e);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Manual/Auto: в server-режиме переключаем авто-создание серверного движка
  // (optimistic + revert при ошибке), в browser-режиме — как раньше, локальный state.
  const selectTradeMode = useCallback(
    async (mode: "manual" | "auto") => {
      if (!SERVER_ENGINE) {
        setTradeMode(mode);
        return;
      }
      const prev = serverEngineConfig;
      const autoEnabled = mode === "auto";
      setTradeMode(mode);
      setServerEngineConfig((c) => ({ ...(c ?? {}), autoEnabled }));
      try {
        const res = await updateGridEngineConfig({ autoEnabled });
        if (!res.ok) throw new Error(res.error || "grid-engine config update failed");
        if (res.config) setServerEngineConfig(res.config);
      } catch (e) {
        setTradeMode(prev?.autoEnabled ? "auto" : "manual");
        setServerEngineConfig(prev ?? null);
        console.error("[GRID] grid-engine config update failed", e);
      }
    },
    [serverEngineConfig],
  );

  // Авто-очистка сеток после серверного сброса: если resetAt новее уже
  // обработанного маркера, стираем сохранённые сетки в этом браузере.
  useEffect(() => {
    let cancelled = false;
    const checkReset = async () => {
      try {
        const res = await fetchGridResetStatus();
        if (cancelled || !res.ok) return;
        const resetAt = Number(res.resetAt);
        if (!Number.isFinite(resetAt) || resetAt <= 0) return;
        const seen = Number(localStorage.getItem(RESET_SEEN_KEY) || 0);
        if (resetAt > seen) {
          localStorage.removeItem(PERSIST_KEY);
          setGrids([]);
          localStorage.setItem(RESET_SEEN_KEY, String(resetAt));
        }
      } catch {
        // failed fetch changes nothing
      }
    };
    checkReset();
    const id = setInterval(checkReset, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Актуальный список сеток для авто-режима (без частых перезапусков сканера).
  const gridsRef = useRef<GridInstance[]>([]);
  useEffect(() => {
    gridsRef.current = grids;
  }, [grids]);

  // Реальное закрытие позиции по сетке: market-close через API, затем финализация
  // по фактическим fill/cumQuote. Общий путь для TP/SL из тика и ручного stop.
  const finalizeRealClose = useCallback(async (c: GridCloseRequest) => {
    // Phase 3: финализацию/закрытие в server-режиме выполняет серверный движок.
    if (SERVER_ENGINE) return;
    const res = await closeGridPosition({
      symbol: c.pair,
      direction: c.direction,
      quantity: c.quantity,
      sinceMs: c.sinceMs,
    });
    const g = gridsRef.current.find((x) => x.id === c.id);
    const side = c.direction;
    const closedSide: "BUY" | "SELL" | undefined =
      side === "long" ? "BUY" : side === "short" ? "SELL" : undefined;
    // Сбрасываем in-flight флаг закрываемой стороны и её TP-ордер (он уже снят
    // через trackCancel); поля остального состояния не трогаем.
    const clearFlagsFor = (x: GridInstance): Partial<GridInstance> => {
      const cleared: Partial<GridInstance> = {
        longExitInFlight: side === "short" ? x.longExitInFlight : false,
        shortExitInFlight: side === "long" ? x.shortExitInFlight : false,
      };
      if (side) {
        const ids = { ...(x.tpOrderIds ?? {}) };
        const prices = { ...(x.tpOrderPrices ?? {}) };
        delete ids[side];
        delete prices[side];
        cleared.tpOrderIds = ids.long == null && ids.short == null ? undefined : ids;
        cleared.tpOrderPrices = prices.long == null && prices.short == null ? undefined : prices;
      }
      return cleared;
    };

    const err = (res.error ?? "").toLowerCase();
    const nothingToClose =
      !res.ok && (err.includes("nothing toclose") || err.includes("position is 0"));

    if (!res.ok && !nothingToClose) {
      console.error("[GRID] close failed", { pair: c.pair, reason: c.reason, direction: side, error: res.error });
      setGrids((prev) => prev.map((x) => (x.id === c.id ? { ...x, ...clearFlagsFor(x) } : x)));
      return;
    }

    let exitAvgPrice = Number(res.avgPrice ?? 0);
    let exitQty = Number(res.executedQty ?? 0);
    const fundingUsd = Number(res.fundingUsd ?? 0);

    // Защита от гонки: /close иногда возвращает нули, пока филл ещё не виден.
    const closeMeta = res as { orderId?: unknown; cumQuote?: unknown };
    const closeOrderId = Number(closeMeta.orderId);
    if (
      !nothingToClose &&
      (exitAvgPrice <= 0 || Number(closeMeta.cumQuote ?? 0) <= 0) &&
      Number.isFinite(closeOrderId)
    ) {
      const fallback = await fetchGridFills({ symbol: c.pair, orderIds: [closeOrderId] }).catch(() => null);
      const hit = (fallback?.results ?? []).find((r: any) => Number(r?.executedQty) > 0);
      if (hit) {
        exitAvgPrice = Number(hit.avgPrice ?? 0);
        exitQty = Number(hit.executedQty ?? 0);
        const filledQuote = Number(hit.cumQuote ?? 0);
        if (!(exitQty > 0) && filledQuote > 0 && exitAvgPrice > 0) exitQty = filledQuote / exitAvgPrice;
      }
      if (!(exitAvgPrice > 0)) {
        console.error("[GRID] close fill unresolved", { pair: c.pair, orderId: closeMeta.orderId });
        setGrids((prev) => prev.map((x) => (x.id === c.id ? { ...x, ...clearFlagsFor(x) } : x)));
        return;
      }
    }

    const fills = g?.fillEntries ?? [];
    const closedFills = closedSide ? sideFills(fills, closedSide) : fills;
    const otherFills = closedSide ? fills.filter((f) => f.side !== closedSide) : [];
    const closedNotional = notional(closedFills);
    // Одна сторона — PnL только по ней; полное закрытие — по обеим.
    const netUsd = closedSide
      ? sidePnlUsd(closedSide, closedFills, exitAvgPrice, exitQty, fundingUsd)
      : realPnlUsd(fills, exitAvgPrice, exitQty, fundingUsd);
    const pct = closedNotional > 0 ? (netUsd / closedNotional) * 100 : 0;
    const totalPnl = (g?.realizedPnl ?? 0) + pct;
    const totalUsd = (g?.realizedUsd ?? 0) + netUsd;
    const entry = closedFills.length > 0 ? avgEntry(closedFills) : g?.startPrice ?? 0;
    // Другая сторона ещё держит лоты — сетка остаётся активной.
    const keepActive = !!closedSide && otherFills.length > 0;
    const phase: GridPhase = c.reason === "tp" ? "done" : "stopped";

    setGrids((prev) =>
      prev.map((x) => {
        if (x.id !== c.id) return x;
        const nextPnl = x.realizedPnl + pct;
        const nextUsd = x.realizedUsd + netUsd;
        if (keepActive) {
          return {
            ...x,
            phase: "active" as GridPhase,
            realizedPnl: nextPnl,
            realizedUsd: nextUsd,
            lastResult: `${nextPnl.toFixed(2)}%`,
            fillEntries: otherFills,
            openLots: otherFills.map((f) => f.level).sort((a, b) => a - b),
            ...clearFlagsFor(x),
          };
        }
        return {
          ...x,
          phase,
          realizedPnl: nextPnl,
          realizedUsd: nextUsd,
          unrealizedPnl: null,
          lastResult: `${nextPnl.toFixed(2)}%`,
          openLots: [],
          fillEntries: [],
          lastPrice: null,
          testnetOrderIds: [],
          stopOrderIds: [],
          ...clearFlagsFor(x),
          tpOrderIds: undefined,
          tpOrderPrices: undefined,
        };
      }),
    );

    if (keepActive) return;

    sendTelegramNotify({
      pair: c.pair,
      pnl: totalPnl,
      tpPct: c.tpPct,
      startPrice: entry,
      closePrice: Number.isFinite(exitAvgPrice) && exitAvgPrice > 0 ? exitAvgPrice : g?.lastPrice ?? 0,
    }).catch(() => {});
    if (g && hadPosition(g)) {
      saveGridResult({
        uid: `${c.id}-${c.sinceMs}`,
        symbol: c.pair,
        timeframe: g?.timeframe ?? "",
        phase,
        exitReason: c.reason === "stop" ? "manual" : c.reason,
        tpPct: c.tpPct,
        gate: g?.gate ?? 0,
        levels: g?.levels ?? 0,
        lo: g?.lo ?? 0,
        hi: g?.hi ?? 0,
        mid: g?.midPrice ?? 0,
        entry,
        exit: Number.isFinite(exitAvgPrice) && exitAvgPrice > 0 ? exitAvgPrice : null,
        pnl: totalPnl,
        pnlUsd: totalUsd,
        positions: closedFills.length,
        createdAt: new Date(c.sinceMs).toISOString(),
        finishedAt: new Date().toISOString(),
        orderSizeUsd: g?.orderSizeUsd,
      }).catch(() => {});
    }
  }, []);

  // Синхронизация защитного TP на бирже: ставим/меняем/снимаем
  // TAKE_PROFIT_MARKET closePosition=true, чтобы профит срабатывал без клиента.
  const syncGridTp = useCallback(async (r: GridTpRequest) => {
    const res = await upsertGridTp({
      symbol: r.pair,
      direction: r.direction,
      tpPrice: r.tpPrice,
      tpOrderId: r.tpOrderId ?? null,
    });
    if (!res.ok) {
      console.error("[GRID] tp sync failed", {
        pair: r.pair,
        direction: r.direction,
        tpPrice: r.tpPrice,
        error: res.error,
      });
      return;
    }
    // Phase 3: TP выставляем на бирже, а состояние пишем в БД (viewer не держит истину).
    if (SERVER_ENGINE) {
      const g = gridsRef.current.find((x) => x.id === r.id);
      const nextIds = { ...(g?.tpOrderIds ?? {}) };
      const nextPrices = { ...(g?.tpOrderPrices ?? {}) };
      const newId = Number(res.tpOrderId);
      if (r.tpPrice == null || !Number.isFinite(newId) || newId <= 0) {
        delete nextIds[r.direction];
        delete nextPrices[r.direction];
      } else {
        nextIds[r.direction] = newId;
        nextPrices[r.direction] = r.tpPrice;
      }
      const ids = nextIds.long == null && nextIds.short == null ? undefined : nextIds;
      const prices = nextPrices.long == null && nextPrices.short == null ? undefined : nextPrices;
      const uid = g ? gridUid(g) : String(r.id);
      await patchGrid(uid, {
        tpOrderIds: ids ?? {},
        tpOrderPrices: prices ?? {},
      }).catch((e) => console.error("[GRID] tp patch failed", { uid, error: e }));
      setGrids((prev) => prev.map((x) => (x.id === r.id ? { ...x, tpOrderIds: ids, tpOrderPrices: prices } : x)));
      return;
    }
    setGrids((prev) =>
      prev.map((x) => {
        if (x.id !== r.id) return x;
        const nextIds = { ...(x.tpOrderIds ?? {}) };
        const nextPrices = { ...(x.tpOrderPrices ?? {}) };
        const newId = Number(res.tpOrderId);
        if (r.tpPrice == null || !Number.isFinite(newId) || newId <= 0) {
          // Снятие TP (или сервер не вернул id) — очищаем сторону.
          delete nextIds[r.direction];
          delete nextPrices[r.direction];
        } else {
          nextIds[r.direction] = newId;
          // Храним запрошенную цель: она сравнивается с ней же в тике, поэтому
          // округление биржевого tickSize не вызывает цикл amend.
          nextPrices[r.direction] = r.tpPrice;
        }
        const ids = nextIds.long == null && nextIds.short == null ? undefined : nextIds;
        const prices = nextPrices.long == null && nextPrices.short == null ? undefined : nextPrices;
        return { ...x, tpOrderIds: ids, tpOrderPrices: prices };
      }),
    );
  }, []);

  // После восстановления не даём новым сеткам занять уже существующие id.
  useEffect(() => {
    const maxId = grids.reduce((m, g) => (g.id > m ? g.id : m), 0);
    if (nextGridIdRef.current <= maxId) nextGridIdRef.current = maxId + 1;
  }, [grids]);

  // Re-placement on restore: for each restored grid with phase === "active",
  // run the same placement logic as for fresh activated grids.
  useEffect(() => {
    // Phase 3: восстановление/перевыставление — задача browser-движка.
    if (SERVER_ENGINE) return;
    if (rePlacementDoneRef.current) return;
    const activeGrids = grids.filter((g) => g.phase === "active" && g.levelPrices.length > 0);
    if (activeGrids.length === 0) {
      rePlacementDoneRef.current = true;
      return;
    }
    rePlacementDoneRef.current = true;
    void (async () => {
      for (const g of activeGrids) {
        if (g.testnetOrderIds.length > 0) continue;
        // Двусторонняя сетка: выше mid — SELL, ниже mid — BUY, уровень == mid пропускаем.
        const orders = g.levelPrices
          .map((price) => {
            const side: "BUY" | "SELL" | null =
              price > g.midPrice ? "SELL" : price < g.midPrice ? "BUY" : null;
            return side ? { price, side } : null;
          })
          .filter((o): o is { price: number; side: "BUY" | "SELL" } => o != null);
        const res = await createTestnetGridOrders({
          symbol: g.pair,
          orders,
          orderSizeUsd: g.orderSizeUsd,
          leverage: 50,
          lo: g.lo,
          hi: g.hi,
          slBufferPct: g.edgePct * 100,
        });
        if (!res.ok || !Array.isArray(res.results)) {
          console.error("[GRID] Placement failed", {
            pair: g.pair,
            tf: g.timeframe,
            error: res.error,
            failed: res.results?.filter((r: any) => r.error),
          });
          setGrids((prev) =>
            prev.map((x) =>
              x.id === g.id
                ? { ...x, lastPlacementError: res.error ?? "unknown error", failedLevels: res.results?.filter((r: any) => r.error)?.map((r: any) => r.level) ?? [] }
                : x,
            ),
          );
          continue;
        }
        const ids = res.results
          .filter((r: any) => Number.isFinite(r?.orderId))
          .map((r: any) => Number(r.orderId));
        const stopIds = [res.stops?.long?.algoId, res.stops?.short?.algoId].filter(
          (n): n is number => Number.isFinite(n as number),
        );
        const failed = res.results
          .filter((r: any) => r.error)
          .map((r: any) => r.level);
        const skipped = res.results.filter((r: any) => r?.skipped).map((r: any) => r.level);
        setGrids((prev) =>
          prev.map((x) =>
            x.id === g.id
              ? {
                  ...x,
                  testnetOrderIds: ids,
                  stopOrderIds: stopIds,
                  lastPlacementError: failed.length > 0 ? res.error ?? "partial failure" : undefined,
                  failedLevels: failed.length > 0 ? failed : undefined,
                  skippedLevels: skipped.length > 0 ? skipped : undefined,
                }
              : x,
          ),
        );
        if (failed.length > 0) {
          console.error("[GRID] Placement failed", {
            pair: g.pair,
            tf: g.timeframe,
            error: res.error,
            failed: res.results?.filter((r: any) => r.error),
          });
        }
      }
    })();
  }, []);

   useEffect(() => {
    if (!selectedPair) return;
    const symbol = selectedPair;
    const id = setInterval(async () => {
      try {
        const price = await fetchLastPrice(symbol);
        if (Number.isFinite(price as number)) {
          setLastPrice(price as number);
        }
      } catch {
        // ignore transient fetch errors
      }
    }, 10_000);
    return () => clearInterval(id);
  }, [selectedPair, timeframe]);

  useEffect(() => {
    // Phase 3: активация waiting->active, TP/SL-триггеры, аннигиляция/ролл и
    // закрытия — всё это в server-режиме делает серверный движок.
    if (SERVER_ENGINE) return;
    const notifications: {
      pair: string;
      pnl: number;
      tpPct: number;
      startPrice: number;
      closePrice: number;
    }[] = [];
    const saves: Parameters<typeof saveGridResult>[0][] = [];
    const activated: { id: number; pair: string; direction: string; timeframe: string; startPrice: number; gate: number; adx: number | null; tpPct: number; gridLevels: number }[] = [];
    const cancellations: { pair: string; orderIds: number[] }[] = [];
    const stopCancellations: { pair: string; orderIds: number[] }[] = [];
    const closes: GridCloseRequest[] = [];
    const tpRequests: GridTpRequest[] = [];
    let changed = false;

    // Отменяем и лимитки сетки, и защитные STOP/TP-ордера (отдельным эндпоинтом).
    const trackCancel = (gg: GridInstance) => {
      if (gg.testnetOrderIds.length > 0) {
        cancellations.push({ pair: gg.pair, orderIds: gg.testnetOrderIds });
      }
      const protectiveIds = [...(gg.stopOrderIds ?? []), ...tpOrderIdList(gg)];
      if (protectiveIds.length > 0) {
        stopCancellations.push({ pair: gg.pair, orderIds: protectiveIds });
      }
    };

    const next = grids.map((g) => {
      if (g.phase !== "waiting" && g.phase !== "active") return g;
      const price = prices[g.pair];
      if (price == null) return g;

      // Фаза ожидания: сетку пересчитываем (середина может сдвигаться), сделки
      // не открываем, пока цена не коснётся середины. Если сетка исчезла
      // (ADX выше гейта), а середина так и не достигнута — задачу отменяем.
      if (g.phase === "waiting") {
        const key = `${g.pair}|${g.timeframe}`;
        const hasAdx = getTimeframeAdx(adxStatus, g.pair, g.timeframe) != null;
        const gateOk = timeframePassesGridGate(adxStatus, g.pair, g.timeframe, g.gate);

        if (hasAdx && !gateOk) {
          changed = true;
          trackCancel(g);
          return { ...g, phase: "stopped" as GridPhase, unrealizedPnl: null, testnetOrderIds: [], stopOrderIds: [], tpOrderIds: undefined, tpOrderPrices: undefined };
        }

        // Без свежих границ окна обновлять нечего — продолжаем ждать.
        const bounds = gridBoundsCache[key] ?? null;
        if (!bounds) return g;

        const mid = bounds.mid;
        const touched = g.startSide === "above" ? price <= mid : price >= mid;
        const boundsChanged = g.midPrice !== mid || g.lo !== bounds.lo || g.hi !== bounds.hi;

        if (!touched) {
          if (!boundsChanged) return g;
          changed = true;
          const nLv = nLevelsOf(g);
          return {
            ...g,
            lo: bounds.lo,
            hi: bounds.hi,
            midPrice: mid,
            startPrice: mid,
            levelPrices: gridLevelPrices(bounds.lo, bounds.hi, nLv),
          };
        }

        // Касание середины — фиксируем сетку этого момента и активируемся.
        changed = true;
        const nLvActive = nLevelsOf(g);
        activated.push({
          id: g.id,
          pair: g.pair,
          direction: "LONG",
          timeframe: g.timeframe,
          startPrice: mid,
          gate: g.gate,
          adx: getTimeframeAdx(adxStatus, g.pair, g.timeframe),
          tpPct: g.tpPct,
          gridLevels: nLvActive,
        });
        return {
          ...g,
          lo: bounds.lo,
          hi: bounds.hi,
          midPrice: mid,
          startPrice: mid,
          levelPrices: gridLevelPrices(bounds.lo, bounds.hi, nLvActive),
          phase: "active" as GridPhase,
          unrealizedPnl: null,
            openLots: [],
            lastPrice: price,
            testnetOrderIds: [],
          };
      }

      // Активная сетка двусторонняя: выше mid — SELL (шорт), ниже mid — BUY
      // (лонг), уровень == mid не торгуется. Новые лоты набираем только когда
      // ADX ниже гейта.
      const prevPrice = g.lastPrice ?? price;
      const gateOk = timeframePassesGridGate(adxStatus, g.pair, g.timeframe, g.gate);
      const fills = g.fillEntries ?? [];
      const realMode = g.testnetOrderIds.length > 0;
      const lots = realMode
        ? fills.map((f) => f.level).sort((a, b) => a - b)
        : [...g.openLots];

      // Симуляция (без выставленных ордеров): BUY на пробое уровня вниз, SELL на пробое вверх.
      if (!realMode && gateOk) {
        for (const lv of g.levelPrices) {
          const lvSide: "BUY" | "SELL" | null =
            lv > g.midPrice ? "SELL" : lv < g.midPrice ? "BUY" : null;
          if (lvSide == null) continue;
          if (lots.some((e) => Math.abs(e - lv) < 1e-9)) continue;
          if (lvSide === "BUY" && prevPrice > lv && price <= lv) lots.push(lv);
          if (lvSide === "SELL" && prevPrice < lv && price >= lv) lots.push(lv);
        }
      }

      const longLevels = realMode
        ? sideFills(fills, "BUY").map((f) => f.level)
        : lots.filter((l) => l < g.midPrice);
      const shortLevels = realMode
        ? sideFills(fills, "SELL").map((f) => f.level)
        : lots.filter((l) => l > g.midPrice);
      const avgLong = realMode
        ? avgEntry(sideFills(fills, "BUY"))
        : avgEntryByNotional(longLevels) ?? 0;
      const avgShort = realMode
        ? avgEntry(sideFills(fills, "SELL"))
        : avgEntryByNotional(shortLevels) ?? 0;
      const hasLong = longLevels.length > 0;
      const hasShort = shortLevels.length > 0;

      // Динамический TP: пересчитывается каждый тик и жёстко ограничен mid, чтобы
      // не рисковать противоположной стороной (огромный tpPct закроется по mid).
      const longTp = avgLong > 0 ? Math.min(avgLong * (1 + g.tpPct / 100), g.midPrice) : null;
      const shortTp =
        avgShort > 0 ? Math.max(avgShort * (1 - g.tpPct / 100), g.midPrice) : null;

      // Стоп-лосс по параметрам сетки: SL% от средней входа и Edge% от крайнего
      // ордера; применяется более близкий к цене (меньший риск) стоп.
      const { long: longSl, short: shortSl } = computeGridStops(g, avgLong, avgShort);

      let working: GridInstance = realMode ? g : { ...g, openLots: lots };
      let dirty = false;

      if (realMode) {
        // Реальные выходы: не финализируем сами — закрываем сторону по рынку.
        const requestLongExit = (reason: CloseReason) => {
          if (working.longExitInFlight) return;
          trackCancel(g);
          closes.push({
            id: g.id,
            pair: g.pair,
            reason,
            direction: "long",
            quantity: sumQty(sideFills(fills, "BUY")),
            tpPct: g.tpPct,
            sinceMs: g.createdAt,
          });
          working = { ...working, longExitInFlight: true };
          dirty = true;
        };
        const requestShortExit = (reason: CloseReason) => {
          if (working.shortExitInFlight) return;
          trackCancel(g);
          closes.push({
            id: g.id,
            pair: g.pair,
            reason,
            direction: "short",
            quantity: sumQty(sideFills(fills, "SELL")),
            tpPct: g.tpPct,
            sinceMs: g.createdAt,
          });
          working = { ...working, shortExitInFlight: true };
          dirty = true;
        };
        if (hasLong && price <= longSl) requestLongExit("sl");
        if (hasShort && price >= shortSl) requestShortExit("sl");
        if (hasLong && longTp != null && price >= longTp) requestLongExit("tp");
        if (hasShort && shortTp != null && price <= shortTp) requestShortExit("tp");

        // Авто-синк TP только по торговому событию: сторона получила новый набор
        // филлов (изменилась сигнатура) → ставим/двигаем защитный TP на бирже.
        // Правка tpPct сама синк не запускает — для неё кнопка в строке сетки.
        const sigOf = (fs: GridFill[]) => `${fs.length}:${(avgEntry(fs) || 0).toFixed(8)}`;
        const enqueueTp = (
          sideName: "long" | "short",
          sideHasFills: boolean,
          target: number | null,
          exiting: boolean,
        ) => {
          if (exiting) return;
          const existingId = g.tpOrderIds?.[sideName];
          const key = `${g.id}:${sideName}`;
          if (tpInFlightRef.current.has(key)) return;
          const sig = sigOf(sideFills(fills, sideName === "long" ? "BUY" : "SELL"));
          if (sideHasFills && target != null) {
            // Нет живого TP или филлы изменились с прошлого авто-синка.
            if (existingId == null || sig !== g.tpAutoSig?.[sideName]) {
              tpRequests.push({
                id: g.id,
                pair: g.pair,
                direction: sideName,
                tpPrice: target,
                tpOrderId: existingId ?? null,
              });
              const nextSig = { ...(working.tpAutoSig ?? {}) };
              nextSig[sideName] = sig;
              working = { ...working, tpAutoSig: nextSig };
              dirty = true;
            }
          } else if (!sideHasFills && existingId != null) {
            // Сторона потеряла все филлы — снимаем её TP.
            tpRequests.push({
              id: g.id,
              pair: g.pair,
              direction: sideName,
              tpPrice: null,
              tpOrderId: existingId,
            });
            const nextSig = { ...(working.tpAutoSig ?? {}) };
            delete nextSig[sideName];
            working = {
              ...working,
              tpAutoSig: nextSig.long == null && nextSig.short == null ? undefined : nextSig,
            };
            dirty = true;
          }
        };
        enqueueTp("long", hasLong, longTp, !!working.longExitInFlight);
        enqueueTp("short", hasShort, shortTp, !!working.shortExitInFlight);
      } else {
        // Симуляция: закрываем сторону по её TP/SL, убирая только её лоты.
        const closeSimSide = (which: "long" | "short", reason: CloseReason) => {
          if (working.phase !== "active") return;
          const sideLevels = which === "long" ? longLevels : shortLevels;
          if (sideLevels.length === 0) return;
          const sideAvg = which === "long" ? avgLong : avgShort;
          const pnlPct = reason === "tp" ? g.tpPct : openPnlPct(sideLevels, price) ?? 0;
          const sideUsd = (sideLevels.length * g.orderSizeUsd * pnlPct) / 100;
          const total = working.realizedPnl + pnlPct;
          const totalUsd = working.realizedUsd + sideUsd;
          const remaining = working.openLots.filter(
            (l) => !sideLevels.some((e) => Math.abs(e - l) < 1e-9),
          );
          dirty = true;
          if (remaining.length > 0) {
            working = {
              ...working,
              realizedPnl: total,
              realizedUsd: totalUsd,
              openLots: remaining,
              lastResult: `${total.toFixed(2)}%`,
            };
            return;
          }
          notifications.push({
            pair: g.pair,
            pnl: total,
            tpPct: g.tpPct,
            startPrice: sideAvg > 0 ? sideAvg : g.startPrice,
            closePrice: price,
          });
          saves.push({
            uid: `${g.id}-${g.createdAt}`,
            symbol: g.pair,
            timeframe: g.timeframe,
            phase: reason === "tp" ? "done" : "stopped",
            exitReason: reason,
            tpPct: g.tpPct,
            gate: g.gate,
            levels: g.levels,
            lo: g.lo,
            hi: g.hi,
            mid: g.midPrice,
            entry: sideAvg > 0 ? sideAvg : g.startPrice,
            exit: price,
            pnl: total,
            pnlUsd: totalUsd,
            positions: sideLevels.length,
            createdAt: new Date(g.createdAt).toISOString(),
            finishedAt: new Date().toISOString(),
            orderSizeUsd: g.orderSizeUsd,
          });
          trackCancel(g);
          working = {
            ...working,
            phase: reason === "tp" ? "done" : "stopped",
            realizedPnl: total,
            realizedUsd: totalUsd,
            unrealizedPnl: null,
            lastResult: `${total.toFixed(2)}%`,
            openLots: [],
            lastPrice: null,
            testnetOrderIds: [],
            stopOrderIds: [],
            tpOrderIds: undefined,
            tpOrderPrices: undefined,
            fillEntries: [],
          };
        };
        if (hasLong && price <= longSl) closeSimSide("long", "sl");
        if (hasShort && price >= shortSl) closeSimSide("short", "sl");
        if (hasLong && longTp != null && price >= longTp) closeSimSide("long", "tp");
        if (hasShort && shortTp != null && price <= shortTp) closeSimSide("short", "tp");
      }

      if (dirty) {
        changed = true;
        return working;
      }

      // Нереализованный PnL: номинал-взвешенное среднее по сторонам (short прибылен при падении).
      const longNotional = realMode
        ? notional(sideFills(fills, "BUY"))
        : longLevels.length * g.orderSizeUsd;
      const shortNotional = realMode
        ? notional(sideFills(fills, "SELL"))
        : shortLevels.length * g.orderSizeUsd;
      const longPnlPct = avgLong > 0 ? (price / avgLong - 1) * 100 : 0;
      const shortPnlPct = avgShort > 0 && price > 0 ? (avgShort / price - 1) * 100 : 0;
      const totalNotional = longNotional + shortNotional;
      const unreal =
        totalNotional > 0
          ? (longPnlPct * longNotional + shortPnlPct * shortNotional) / totalNotional
          : null;
      const nextLots = realMode ? g.openLots : lots;
      if (
        g.lastPrice === price &&
        g.unrealizedPnl === unreal &&
        nextLots.length === g.openLots.length
      ) {
        return g;
      }
      changed = true;
      return { ...g, openLots: nextLots, lastPrice: price, unrealizedPnl: unreal };
    });

    if (!changed && tpRequests.length === 0) return;
    if (changed) {
      setGrids(next);
      notifications.forEach((n) => sendTelegramNotify(n).catch(() => {}));
      activated.forEach((a) => sendTelegramStart(a).catch(() => {}));
    }
    void (async () => {
      for (const a of activated) {
        const g = next.find((x) => x.id === a.id);
        if (!g) continue;
        // Двусторонняя сетка: выше mid — SELL, ниже mid — BUY, уровень == mid пропускаем.
        const orders = g.levelPrices
          .map((price) => {
            const side: "BUY" | "SELL" | null =
              price > g.midPrice ? "SELL" : price < g.midPrice ? "BUY" : null;
            return side ? { price, side } : null;
          })
          .filter((o): o is { price: number; side: "BUY" | "SELL" } => o != null);
        const res = await createTestnetGridOrders({
          symbol: g.pair,
          orders,
          orderSizeUsd: g.orderSizeUsd,
          leverage: 50,
          lo: g.lo,
          hi: g.hi,
          slBufferPct: g.edgePct * 100,
        });
        if (!res.ok || !Array.isArray(res.results)) {
          console.error("[GRID] Placement failed", {
            pair: g.pair,
            tf: g.timeframe,
            error: res.error,
            failed: res.results?.filter((r: any) => r.error),
          });
          setGrids((prev) =>
            prev.map((x) =>
              x.id === g.id
                ? {
                    ...x,
                    lastPlacementError: res.error ?? "unknown error",
                    failedLevels: res.results?.filter((r: any) => r.error)?.map((r: any) => r.level) ?? [],
                  }
                : x,
            ),
          );
          continue;
        }
        const ids = res.results
          .filter((r: any) => Number.isFinite(r?.orderId))
          .map((r: any) => Number(r.orderId));
        const stopIds = [res.stops?.long?.algoId, res.stops?.short?.algoId].filter(
          (n): n is number => Number.isFinite(n as number),
        );
        const failed = res.results
          .filter((r: any) => r.error)
          .map((r: any) => r.level);
        const skipped = res.results.filter((r: any) => r?.skipped).map((r: any) => r.level);
        setGrids((prev) =>
          prev.map((x) =>
            x.id === g.id
              ? {
                  ...x,
                  testnetOrderIds: ids,
                  stopOrderIds: stopIds,
                  lastPlacementError: failed.length > 0 ? res.error ?? "partial failure" : undefined,
                  failedLevels: failed.length > 0 ? failed : undefined,
                  skippedLevels: skipped.length > 0 ? skipped : undefined,
                }
              : x,
          ),
        );
        const stillActive = gridsRef.current.some((x) => x.id === g.id && x.phase === "active");
        if (!stillActive) {
          cancelTestnetGridOrders({ symbol: g.pair, orderIds: ids }).catch(() => {});
          const protectiveIds = [...stopIds, ...tpOrderIdList(g)];
          if (protectiveIds.length > 0) {
            cancelGridStops({ symbol: g.pair, orderIds: protectiveIds }).catch(() => {});
          }
        }
        if (failed.length > 0) {
          console.error("[GRID] Placement failed", {
            pair: g.pair,
            tf: g.timeframe,
            error: res.error,
            failed: res.results?.filter((r: any) => r.error),
          });
        }
      }
    })();
    cancellations.forEach((c) => {
      if (c.orderIds.length > 0) {
        cancelTestnetGridOrders({ symbol: c.pair, orderIds: c.orderIds }).catch(() => {});
      } else {
        // id не успели сохраниться: cancelAll снёс бы TP/SL скальпер-бота, поэтому
        // ничего не отменяем, а только логируем.
        console.warn("[GRID] cancel skipped: no tracked orderIds", { pair: c.pair });
      }
    });
    stopCancellations.forEach((c) => {
      cancelGridStops({ symbol: c.pair, orderIds: c.orderIds }).catch(() => {});
    });
    saves.forEach((s) => saveGridResult(s).catch(() => {}));
    closes.forEach((c) => {
      void finalizeRealClose(c);
    });
    tpRequests.forEach((r) => {
      const key = `${r.id}:${r.direction}`;
      if (tpInFlightRef.current.has(key)) return;
      tpInFlightRef.current.add(key);
      void syncGridTp(r).finally(() => {
        tpInFlightRef.current.delete(key);
      });
    });
  }, [prices, grids, gridBoundsCache, adxStatus, finalizeRealClose, syncGridTp]);

  // Пары, по которым есть незавершённые сетки — по ним опрашиваем цену.
  const trackedPairsKey = Array.from(
    new Set(
      grids
        .filter((g) => g.phase === "waiting" || g.phase === "active")
        .map((g) => g.pair),
    ),
  )
    .sort()
    .join(",");

  useEffect(() => {
    if (!trackedPairsKey) return;
    const pairs = trackedPairsKey.split(",");
    let cancelled = false;
    const poll = async () => {
      const updates: Record<string, number> = {};
      await Promise.all(
        pairs.map(async (p) => {
          const price = await fetchLastPrice(p);
          if (price != null) updates[p] = price;
        }),
      );
      if (!cancelled && Object.keys(updates).length > 0) {
        setPrices((prev) => ({ ...prev, ...updates }));
      }
    };
    poll();
    const id = setInterval(poll, 10_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [trackedPairsKey]);

  // Окна (пара|ТФ) с незавершёнными сетками — держим границы свежими,
  // чтобы после TP можно было сразу пересчитать новую сетку в любом окне.
  const trackedWindowsKey = Array.from(
    new Set(
      grids
        .filter((g) => g.phase === "waiting" || g.phase === "active")
        .map((g) => `${g.pair}|${g.timeframe}`),
    ),
  )
    .sort()
    .join(",");

  useEffect(() => {
    if (!trackedWindowsKey) return;
    const windows = trackedWindowsKey.split(",").map((k) => {
      const [pair, tf] = k.split("|");
      return { pair, tf };
    });
    let cancelled = false;
    const load = async () => {
      const updates: Record<string, GridBounds> = {};
      await Promise.all(
        windows.map(async ({ pair, tf }) => {
          try {
            const res = await fetchHistory(pair, tf);
            const rows = (res?.data || []).map((k: any) => ({
              low: parseFloat(k.l),
              high: parseFloat(k.h),
            }));
            const b = computeGridBounds(rows, tf);
            if (b) updates[`${pair}|${tf}`] = b;
          } catch {
            // keep previous cached bounds on transient errors
          }
        }),
      );
      if (!cancelled && Object.keys(updates).length > 0) {
        setGridBoundsCache((prev) => ({ ...prev, ...updates }));
      }
    };
    load();
    const id = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [trackedWindowsKey]);

  // Реальные исполнения: для активных сеток с выставленными ордерами подтягиваем
  // фактические филлы Binance и синхронизируем fillEntries/openLots. При ошибке
  // оставляем предыдущее состояние (без обнуления).
  useEffect(() => {
    // Phase 3: филлы Binance в server-режиме опрашивает серверный движок.
    if (SERVER_ENGINE) return;
    let cancelled = false;
    const pollFills = async () => {
      const targets = gridsRef.current.filter(
        (g) => g.phase === "active" && g.testnetOrderIds.length > 0,
      );
      for (const g of targets) {
        const res = await fetchGridFills({ symbol: g.pair, orderIds: g.testnetOrderIds, sinceMs: g.createdAt }).catch(() => null);
        if (cancelled) return;
        if (!res || !res.ok) continue;
        const results = Array.isArray(res.results) ? res.results : [];
        const fillEntries: GridFill[] = results
          .filter((r: any) => Number(r?.executedQty) > 0)
          .map((r: any) => {
            const px = Number(r?.price ?? 0);
            const rawSide = typeof r?.side === "string" ? r.side.toUpperCase() : "";
            // Сторона из результата, иначе — по уровню относительно mid.
            const side: "BUY" | "SELL" =
              rawSide === "BUY" || rawSide === "SELL"
                ? rawSide
                : px > g.midPrice
                  ? "SELL"
                  : "BUY";
            return {
              level: px,
              side,
              qty: Number(r?.executedQty ?? 0),
              entry: Number(r?.avgPrice ?? r?.price ?? 0),
              cumQuote: Number(r?.cumQuote ?? 0),
              time: Number.isFinite(Number(r?.time)) ? Number(r?.time) : undefined,
            };
          })
          .filter(
            (f: GridFill) =>
              Number.isFinite(f.level) &&
              Number.isFinite(f.qty) &&
              Number.isFinite(f.entry) &&
              Number.isFinite(f.cumQuote),
          );
        // orderId -> price/side из того же ответа /fills: resize шлёт бирже
        // именно эти resting-ордера, поэтому цена и сторона нужны на каждый id.
        const entryOrders = results
          .filter((r: any) => Number.isFinite(Number(r?.orderId)))
          .map((r: any) => {
            const px = Number(r?.price ?? 0);
            const rawSide = typeof r?.side === "string" ? r.side.toUpperCase() : "";
            const side: "BUY" | "SELL" =
              rawSide === "BUY" || rawSide === "SELL"
                ? rawSide
                : px > g.midPrice
                  ? "SELL"
                  : "BUY";
            return { orderId: Number(r.orderId), price: px, side };
          });
        const openLots = fillEntries.map((f) => f.level).sort((a, b) => a - b);
        const sumExecuted = fillEntries.reduce((s, f) => s + f.qty, 0);
        const positionAmt = res.positionAmt == null ? null : Number(res.positionAmt);
        // Авторитетная позиция с биржи: число — сохраняем, null — позиция
        // недоступна, иначе (ответ без поля) оставляем прежнее значение.
        const posUpdate: number | null | undefined =
          res.positionAmt === null
            ? null
            : Number.isFinite(Number(res.positionAmt))
              ? Number(res.positionAmt)
              : undefined;
        if (positionAmt != null && Number.isFinite(positionAmt) && Math.abs(sumExecuted - Math.abs(positionAmt)) > 0) {
          console.warn("[GRID] position drift", { pair: g.pair, sumExecuted, positionAmt });
        }

        // Реконсиляция: позиция обнулилась, а сетка ещё активна и имеет филлы —
        // значит защитный ордер сработал на бирже (ПК/браузер/api-server мог быть
        // выключен). Определяем причину по тому, какой из защитных ордеров исполнен:
        // сначала TP, затем STOP, иначе прежний фолбэк (последняя цена, reason sl).
        if (positionAmt === 0 && (g.fillEntries?.length ?? 0) > 0) {
          const fills = g.fillEntries ?? [];
          const stopIds = g.stopOrderIds ?? [];
          const tpIds = tpOrderIdList(g);
          // Reason подтверждаем только по реально сработавшему защитному ордеру:
          // иначе нейтральный "closed", а не выдуманный "sl".
          let reason: "tp" | "sl" | "closed" = "closed";
          let exitAvgPrice = Number(g.lastPrice ?? 0);
          let exitQty = sumQty(fills);
          // orderId реально сработавшего защитного algo-ордера: по нему берём
          // фактические закрывающие филлы (фолбэк — actualPrice/actualQty).
          let hitOrderId: number | null = null;
          // Защита теперь algo-ордера: сработавший определяем по algoStatus,
          // а не по филлам обычного ордера.
          const algoIds = Array.from(new Set([...tpIds, ...stopIds]));
          if (algoIds.length > 0) {
            const statusRes = await fetchAlgoStatus({ symbol: g.pair, algoIds }).catch(() => null);
            if (cancelled) return;
            const results = statusRes?.results ?? [];
            const triggered = (id: number) =>
              results.find((r: any) => Number(r?.algoId) === id && isTriggeredAlgo(r?.algoStatus));
            const tpHit = tpIds.map(triggered).find((r: any) => r != null);
            if (tpHit) {
              reason = "tp";
              const hitOrderIdRaw = Number(tpHit.actualOrderId ?? 0);
              if (Number.isFinite(hitOrderIdRaw) && hitOrderIdRaw > 0) hitOrderId = hitOrderIdRaw;
              const hitPx = Number(tpHit.actualPrice ?? 0);
              const hitQty = Number(tpHit.actualQty ?? 0);
              if (Number.isFinite(hitPx) && hitPx > 0) exitAvgPrice = hitPx;
              if (Number.isFinite(hitQty) && hitQty > 0) exitQty = hitQty;
            } else {
              const slHit = stopIds.map(triggered).find((r: any) => r != null);
              if (slHit) {
                reason = "sl";
                const hitOrderIdRaw = Number(slHit.actualOrderId ?? 0);
                if (Number.isFinite(hitOrderIdRaw) && hitOrderIdRaw > 0) hitOrderId = hitOrderIdRaw;
                const hitPx = Number(slHit.actualPrice ?? 0);
                const hitQty = Number(slHit.actualQty ?? 0);
                if (Number.isFinite(hitPx) && hitPx > 0) exitAvgPrice = hitPx;
                if (Number.isFinite(hitQty) && hitQty > 0) exitQty = hitQty;
              }
            }
          }
          if (reason !== "closed" && hitOrderId != null) {
            // Подтверждённая защита: настоящие цена/объём закрытия из userTrades.
            const tr = await fetchUserTrades({ symbol: g.pair, orderId: hitOrderId }).catch(() => null);
            if (cancelled) return;
            const tradeQty = (tr?.trades ?? []).reduce((s, t) => s + Number(t?.qty ?? 0), 0);
            const tradeQuote = (tr?.trades ?? []).reduce((s, t) => s + Number(t?.quoteQty ?? 0), 0);
            const totQty = Number.isFinite(Number(tr?.totalQty)) ? Number(tr?.totalQty) : tradeQty;
            const totQuote = Number.isFinite(Number(tr?.totalQuoteQty)) ? Number(tr?.totalQuoteQty) : tradeQuote;
            if (totQty > 0 && Number.isFinite(totQuote)) {
              exitQty = totQty;
              exitAvgPrice = totQuote / totQty;
            }
          } else if (reason === "closed") {
            // Защита не подтверждена: закрывающая сторона — противоположная
            // стороне, державшей лоты; берём только её сделки после последнего
            // входа, иначе fallback на lastPrice.
            const buyFills = sideFills(fills, "BUY");
            const sellFills = sideFills(fills, "SELL");
            let heldSide: "BUY" | "SELL" | null = null;
            if (buyFills.length > 0 && sellFills.length === 0) heldSide = "BUY";
            else if (sellFills.length > 0 && buyFills.length === 0) heldSide = "SELL";
            else if (buyFills.length > 0 && sellFills.length > 0) {
              heldSide = realNotionalUsd(buyFills) >= realNotionalUsd(sellFills) ? "BUY" : "SELL";
            }
            if (heldSide != null) {
              const closingSide: "BUY" | "SELL" = heldSide === "BUY" ? "SELL" : "BUY";
              const entryFills = heldSide === "BUY" ? buyFills : sellFills;
              let lastEntryTime = 0;
              for (const f of entryFills) {
                const t = Number(f.time ?? 0);
                if (Number.isFinite(t) && t > lastEntryTime) lastEntryTime = t;
              }
              const minTime = lastEntryTime > 0 ? lastEntryTime : g.createdAt;
              const tr = await fetchUserTrades({ symbol: g.pair, sinceMs: g.createdAt }).catch(() => null);
              if (cancelled) return;
              const closing = (tr?.trades ?? []).filter(
                (t) =>
                  String(t?.side ?? "").toUpperCase() === closingSide &&
                  Number.isFinite(Number(t?.time)) &&
                  Number(t?.time) >= minTime,
              );
              let qty = 0;
              let quote = 0;
              for (const t of closing) {
                qty += Number(t?.qty ?? 0);
                quote += Number(t?.quoteQty ?? 0);
              }
              if (qty > 0 && Number.isFinite(quote)) {
                exitQty = qty;
                exitAvgPrice = quote / qty;
              }
            }
          }
          // PnL по сторонам: BUY (long) зарабатывает на росте, SELL (short) — на падении.
          // При двух сторонах суммируем; процент — от суммарного номинала входа.
          let netUsd = 0;
          let entryNotional = 0;
          for (const side of ["BUY", "SELL"] as const) {
            const sideFillSet = sideFills(fills, side);
            if (sideFillSet.length === 0) continue;
            netUsd += sidePnlUsd(side, sideFillSet, exitAvgPrice, sumQty(sideFillSet), 0);
            entryNotional += realNotionalUsd(sideFillSet);
          }
          const pct = entryNotional > 0 ? (netUsd / entryNotional) * 100 : 0;
          const totalPnl = g.realizedPnl + pct;
          const totalUsd = g.realizedUsd + netUsd;
          const entry = realEntryAvg(fills) || g.startPrice;
          const phase: GridPhase = reason === "tp" ? "done" : "stopped";
          if (g.testnetOrderIds.length > 0) {
            cancelTestnetGridOrders({ symbol: g.pair, orderIds: g.testnetOrderIds }).catch(() => {});
          }
          const protectiveIds = [...stopIds, ...tpIds];
          if (protectiveIds.length > 0) {
            // Снимаем ордер-сиблинг (исполненный всё равно уже FILLED).
            cancelGridStops({ symbol: g.pair, orderIds: protectiveIds }).catch(() => {});
          }
          if (hadPosition(g)) {
            saveGridResult({
              uid: `${g.id}-${g.createdAt}`,
              symbol: g.pair,
              timeframe: g.timeframe,
              phase,
              exitReason: reason,
              tpPct: g.tpPct,
              gate: g.gate,
              levels: g.levels,
              lo: g.lo,
              hi: g.hi,
              mid: g.midPrice,
              entry,
              exit: Number.isFinite(exitAvgPrice) && exitAvgPrice > 0 ? exitAvgPrice : g.lastPrice,
              pnl: totalPnl,
              pnlUsd: totalUsd,
              positions: fills.length,
              createdAt: new Date(g.createdAt).toISOString(),
              finishedAt: new Date().toISOString(),
              orderSizeUsd: g.orderSizeUsd,
            }).catch(() => {});
          }
          console.warn("[GRID] exchange protective order detected, grid finalized", {
            pair: g.pair,
            reason,
            exitAvgPrice,
            exitQty,
          });
          setGrids((prev) =>
            prev.map((x) =>
              x.id === g.id
                ? {
                    ...x,
                    phase,
                    realizedPnl: totalPnl,
                    realizedUsd: totalUsd,
                    unrealizedPnl: null,
                    lastResult: `${totalPnl.toFixed(2)}%`,
                    openLots: [],
                    fillEntries: [],
                    entryOrders: undefined,
                    testnetOrderIds: [],
                    stopOrderIds: [],
                    tpOrderIds: undefined,
                    tpOrderPrices: undefined,
                  }
                : x,
            ),
          );
          continue;
        }

        setGrids((prev) =>
          prev.map((x) =>
            x.id === g.id
              ? {
                  ...x,
                  fillEntries,
                  entryOrders,
                  openLots,
                  ...(posUpdate !== undefined ? { positionAmt: posUpdate } : {}),
                }
              : x,
          ),
        );
      }
    };
    pollFills();
    const id = setInterval(pollFills, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Защитная синхронизация algo-ордеров: для активных реальных сеток не чаще
  // раза в ~30 с на пару принимаем уже выставленные stop/tp по clientAlgoId,
  // добиваем отсутствующий STOP и TP. Защита должна жить на бирже без клиента.
  useEffect(() => {
    // Phase 3: защитную STOP/TP-синхронизацию в server-режиме ведёт серверный движок.
    if (SERVER_ENGINE) return;
    let cancelled = false;
    const sync = async () => {
      const targets = gridsRef.current.filter(
        (g) => g.phase === "active" && g.testnetOrderIds.length > 0,
      );
      if (targets.length === 0) return;
      const pairs = Array.from(new Set(targets.map((g) => g.pair)));
      const now = Date.now();
      for (const pair of pairs) {
        if (cancelled) return;
        const last = protectiveSyncAtRef.current[pair] ?? 0;
        if (now - last < 30_000) continue;
        protectiveSyncAtRef.current[pair] = now;

        const openRes = await fetchOpenAlgo(pair).catch(() => null);
        if (cancelled || !openRes || !openRes.ok) continue;
        // Разбираем открытые algo-ордера по префиксу (stop/tp) и суффиксу (сторона).
        const stopBy: { long?: number; short?: number } = {};
        const stopPriceBy: { long?: number; short?: number } = {};
        const tpBy: { long?: number; short?: number } = {};
        const tpPriceBy: { long?: number; short?: number } = {};
        for (const o of openRes.orders) {
          const cid = String(o?.clientAlgoId ?? "");
          const id = Number(o?.algoId);
          if (!Number.isFinite(id) || id <= 0) continue;
          const side = cid.endsWith("_long") ? "long" : cid.endsWith("_short") ? "short" : null;
          if (!side) continue;
          if (cid.startsWith("gridsl_")) {
            stopBy[side] = id;
            const p = Number(o?.triggerPrice);
            if (Number.isFinite(p) && p > 0) stopPriceBy[side] = p;
          } else if (cid.startsWith("gridtp_")) {
            tpBy[side] = id;
            const p = Number(o?.triggerPrice);
            if (Number.isFinite(p) && p > 0) tpPriceBy[side] = p;
          }
        }

        for (const g of targets.filter((x) => x.pair === pair)) {
          if (cancelled) return;

          // Принимаем выставленную не нами защиту в состояние сетки.
          if (stopBy.long != null || stopBy.short != null || tpBy.long != null || tpBy.short != null) {
            setGrids((prev) =>
              prev.map((x) => {
                if (x.id !== g.id) return x;
                const stopIds = new Set(x.stopOrderIds ?? []);
                if (stopBy.long != null) stopIds.add(stopBy.long);
                if (stopBy.short != null) stopIds.add(stopBy.short);
                const nextTp = { ...(x.tpOrderIds ?? {}) };
                const nextPrices = { ...(x.tpOrderPrices ?? {}) };
                if (tpBy.long != null) {
                  nextTp.long = tpBy.long;
                  if (tpPriceBy.long != null) nextPrices.long = tpPriceBy.long;
                }
                if (tpBy.short != null) {
                  nextTp.short = tpBy.short;
                  if (tpPriceBy.short != null) nextPrices.short = tpPriceBy.short;
                }
                return {
                  ...x,
                  stopOrderIds: stopIds.size > 0 ? Array.from(stopIds) : x.stopOrderIds,
                  tpOrderIds: nextTp.long == null && nextTp.short == null ? undefined : nextTp,
                  tpOrderPrices: nextPrices.long == null && nextPrices.short == null ? undefined : nextPrices,
                };
              }),
            );
          }

          const fills = g.fillEntries ?? [];
          const longHasFills = sideFills(fills, "BUY").length > 0;
          const shortHasFills = sideFills(fills, "SELL").length > 0;
          // Стоп ставим только по фактической позиции с биржи: без неё
          // (pos == null) сторона неизвестна и запрос пропускаем целиком.
          const pos =
            typeof g.positionAmt === "number" && Number.isFinite(g.positionAmt)
              ? g.positionAmt
              : null;
          const hasLongPos = pos != null && pos > 0;
          const hasShortPos = pos != null && pos < 0;

          const avgLong = avgEntry(sideFills(fills, "BUY"));
          const avgShort = avgEntry(sideFills(fills, "SELL"));
          const desiredStops = computeGridStops(g, avgLong, avgShort);

          // STOP: ставим, если стопа нет, и заменяем, если выставленный триггер
          // расходится с нужным по параметрам сетки (SL% / Edge%) больше чем на 1e-4.
          const placeStop = async (sideName: "long" | "short", triggerPrice: number) => {
            const key = `${pair}:${sideName}`;
            if (protectiveInFlightRef.current.has(key)) return;
            protectiveInFlightRef.current.add(key);
            try {
              // Замена: сервер снимает переданный algoId и ставит новый стоп.
              const liveId = stopBy[sideName];
              const res = await placeGridStop({
                symbol: pair,
                direction: sideName,
                triggerPrice,
                algoId: liveId,
              });
              if (cancelled) return;
              const newId = Number(res.algoId);
              if (res.ok && Number.isFinite(newId) && newId > 0) {
                stopBy[sideName] = newId;
                stopPriceBy[sideName] = triggerPrice;
                setGrids((prev) =>
                  prev.map((x) => {
                    if (x.id !== g.id) return x;
                    // Заменённый id убираем, новый кладём вместо него.
                    const ids = new Set(x.stopOrderIds ?? []);
                    if (liveId != null) ids.delete(liveId);
                    ids.add(newId);
                    return { ...x, stopOrderIds: Array.from(ids) };
                  }),
                );
              } else if (res.error) {
                console.error("[GRID] stop placement failed", { pair, direction: sideName, error: res.error });
              }
            } finally {
              protectiveInFlightRef.current.delete(key);
            }
          };
          const stopNeedsReplace = (sideName: "long" | "short", want: number) => {
            if (stopBy[sideName] == null) return true;
            const live = stopPriceBy[sideName];
            // Неизвестный триггер не трогаем, чтобы не дёргать стоп каждый цикл.
            if (!Number.isFinite(live as number) || (live as number) <= 0) return false;
            return Math.abs((live as number) - want) / Math.max(Math.abs(want), 1e-9) > 1e-4;
          };
          if (hasLongPos && stopNeedsReplace("long", desiredStops.long)) {
            await placeStop("long", desiredStops.long);
          }
          if (hasShortPos && stopNeedsReplace("short", desiredStops.short)) {
            await placeStop("short", desiredStops.short);
          }

          // TP: сторона с филлами и без algo-TP — ставим текущую цель (avg по
          // филлам с ограничением по mid), как в сигнальном закрытии.
          const enqueueTp = (sideName: "long" | "short", hasFills: boolean, avg: number, target: number | null) => {
            if (!hasFills || avg <= 0 || target == null) return;
            if (tpBy[sideName] != null) return;
            const key = `${g.id}:${sideName}`;
            const pKey = `${pair}:tp:${sideName}`;
            if (tpInFlightRef.current.has(key) || protectiveInFlightRef.current.has(pKey)) return;
            tpInFlightRef.current.add(key);
            protectiveInFlightRef.current.add(pKey);
            void syncGridTp({
              id: g.id,
              pair: g.pair,
              direction: sideName,
              tpPrice: target,
              tpOrderId: g.tpOrderIds?.[sideName] ?? null,
            })
              .catch(() => {})
              .finally(() => {
                tpInFlightRef.current.delete(key);
                protectiveInFlightRef.current.delete(pKey);
              });
          };
          const longTp = avgLong > 0 ? Math.min(avgLong * (1 + g.tpPct / 100), g.midPrice) : null;
          const shortTp = avgShort > 0 ? Math.max(avgShort * (1 - g.tpPct / 100), g.midPrice) : null;
          enqueueTp("long", longHasFills, avgLong, longTp);
          enqueueTp("short", shortHasFills, avgShort, shortTp);
        }
      }
    };
    sync();
    const id = setInterval(sync, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [syncGridTp]);

  // AUTO-режим: как только по паре появляется рассчитанная сетка (ADX < гейта 15
  // на любом ТФ), сразу запускаем торговлю с TP 1%. Одна сетка на монету.
  useEffect(() => {
    // Phase 3: авто-создание сеток в server-режиме выполняет серверный движок
    // (GRID_AUTO_ENABLED); viewer новых сеток не создаёт.
    if (SERVER_ENGINE) return;
    if (tradeMode !== "auto") return;
    let cancelled = false;
    const AUTO_GATE = 15;
    const AUTO_TP = 10;
    const tfs = ["5m", "15m", "30m", "1h", "4h", "12h", "1d"] as const;

    const run = async () => {
      const busy = new Set(
        gridsRef.current
          .filter((g) => g.phase === "waiting" || g.phase === "active")
          .map((g) => g.pair),
      );
      // Символы, занятые скальпер-ботом, не трогаем: у него свои TP/SL.
      try {
        const bots = await fetchBotsStatus();
        for (const [pair, bot] of Object.entries(bots || {})) {
          let hasSize = false;
          if (bot?.position) {
            let parsed: any = bot.position;
            if (typeof parsed === "string") {
              try {
                parsed = JSON.parse(parsed);
              } catch {
                parsed = true;
              }
            }
            if (parsed === true) {
              hasSize = true;
            } else if (typeof parsed === "number") {
              hasSize = parsed !== 0;
            } else if (parsed && typeof parsed === "object") {
              const rawSize =
                (parsed as any).size ??
                (parsed as any).quantity ??
                (parsed as any).amount ??
                (parsed as any).qty;
              const size = typeof rawSize === "string" ? parseFloat(rawSize) : rawSize;
              hasSize =
                typeof size === "number" && !Number.isNaN(size)
                  ? size !== 0
                  : Object.values(parsed).some((v) => typeof v === "number" && v !== 0);
            }
          }
          if (bot?.is_running || hasSize) busy.add(pair);
        }
      } catch {
        // не удалось получить статусы ботов — работаем только по grid-busy
      }
      const candidates: { pair: string; tf: string }[] = [];
      for (const [pair, entry] of Object.entries(adxStatus)) {
        if (busy.has(pair)) continue;
        const tf = tfs.find((t) => {
          const a = (entry as any)?.[`tf${t}`]?.adx;
          return a != null && a < AUTO_GATE;
        });
        if (tf) candidates.push({ pair, tf });
      }
      if (candidates.length === 0) return;

      for (const { pair, tf } of candidates) {
        if (cancelled) return;
        try {
          const price = await fetchLastPrice(pair);
          if (price == null) continue;
          const res = await fetchHistory(pair, tf);
          const rows = (res?.data || []).map((k: any) => ({
            low: parseFloat(k.l),
            high: parseFloat(k.h),
          }));
          const b = computeGridBounds(rows, tf);
          if (!b) continue;
          if (cancelled) return;
          const startSide: "above" | "below" = price > b.mid ? "above" : "below";
          setGrids((prev) => {
            if (prev.some((g) => g.pair === pair && (g.phase === "waiting" || g.phase === "active"))) {
              return prev;
            }
            return [
              ...prev,
              {
                id: nextGridIdRef.current++,
                pair,
                timeframe: tf,
                tpPct: AUTO_TP,
                slPct: 2,
                edgePct: 2,
                gate: AUTO_GATE,
                levels: gridNLevels,
                lo: b.lo,
                hi: b.hi,
                midPrice: b.mid,
                startPrice: b.mid,
                startSide,
                phase: "waiting",
                unrealizedPnl: null,
                realizedPnl: 0,
                realizedUsd: 0,
                lastResult: null,
                createdAt: Date.now(),
                orderSizeUsd,
                levelPrices: gridLevelPrices(b.lo, b.hi, gridNLevels),
                openLots: [],
                lastPrice: null,
                testnetOrderIds: [],
              },
            ];
          });
          setPrices((prev) => ({ ...prev, [pair]: price }));
          setGridBoundsCache((prev) => ({ ...prev, [`${pair}|${tf}`]: b }));
        } catch {
          // ignore transient errors
        }
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [tradeMode, adxStatus, gridNLevels, orderSizeUsd]);

  useEffect(() => {
    if (activeTab !== "history") return;
    let cancelled = false;
    setHistoryLoading(true);
    fetchGridHistory(200)
      .then((rows) => {
        if (!cancelled) setHistory(rows);
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeTab, historyTick]);

  const loadPairs = useCallback(async () => {
    try {
      const data = await fetchPairs();
      const list = Array.isArray(data) ? data : [];
      setPairs([...new Set(list)]);
    } catch {
      // ignore errors, keep empty list
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadPairs();
    const id = setInterval(loadPairs, 15_000);
    return () => clearInterval(id);
  }, [loadPairs]);

  useEffect(() => {
    if (!selectedPair) return;
    let cancelled = false;
    const loadBots = async () => {
      try {
        const data = await fetchBotsStatus();
        if (!cancelled && Object.keys(data).length > 0) {
          setBotsStatus(data);
        }
      } catch {
        // ignore transient errors
      }
    };
    loadBots();
    const id = setInterval(loadBots, 15_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [selectedPair]);

  useEffect(() => {
    if (!selectedPair) return;
    let cancelled = false;
    const loadAdx = async () => {
      try {
        const data = await fetchAdx();
        const normalized: Record<string, { adx: number | null; gate: number; ok: boolean }> = {};
        Object.entries(data || {}).forEach(([symbol, tfMap]) => {
          const entry = tfMap as Record<string, { adx: number | null; gate: number; ok: boolean }>;
          const tfs = ["1h", "4h", "12h", "1d"].filter((tf) => entry[tf]);
          if (tfs.length === 0 && !entry["5m"]) return;
          const adx = tfs.map((tf) => entry[tf].adx).find((v) => v != null) ?? null;
          const gate = entry[tfs[0]]?.gate ?? 15;
          const ok = tfs.some((tf) => entry[tf].ok);
          normalized[symbol] = { adx, gate, ok };
          (normalized[symbol] as any).tf5m = entry["5m"] || null;
          (normalized[symbol] as any).tf15m = entry["15m"] || null;
          (normalized[symbol] as any).tf30m = entry["30m"] || null;
          (normalized[symbol] as any).tf1h = entry["1h"] || null;
          (normalized[symbol] as any).tf4h = entry["4h"] || null;
          (normalized[symbol] as any).tf12h = entry["12h"] || null;
          (normalized[symbol] as any).tf1d = entry["1d"] || null;
        });
        if (!cancelled && Object.keys(normalized).length > 0) {
          setAdxStatus(normalized);
        }
      } catch {
        // ignore transient errors
      }
    };
    loadAdx();
    const id = setInterval(loadAdx, 60_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [selectedPair]);

  useEffect(() => {
    if (!selectedPair) return;
    const id = setInterval(() => {
      setRefreshTick((t) => t + 1);
    }, 10_000);
    return () => clearInterval(id);
  }, [selectedPair]);

  useEffect(() => {
    if (!selectedPair) return;
    let cancelled = false;
    setChartLoading(true);
    fetchHistory(selectedPair, timeframe)
      .then((res) => {
        if (cancelled) return;
        const rows: Candle[] = (res.data || [])
          .map((k) => {
            const ts = Number(k.t);
            const d = new Date(ts);
            if (timeframe === "1d") {
              const dateStr = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
              return {
                time: dateStr,
                open: parseFloat(k.o),
                high: parseFloat(k.h),
                low: parseFloat(k.l),
                close: parseFloat(k.c),
              };
            }
            const seconds = Math.floor(ts / 1000);
            return {
              time: Number.isFinite(seconds) ? seconds : "",
              open: parseFloat(k.o),
              high: parseFloat(k.h),
              low: parseFloat(k.l),
              close: parseFloat(k.c),
            };
          })
          .filter((row) => {
            if (typeof row.time === "string") return row.time.length > 0;
            return Number.isFinite(row.time);
          });
        setChartData(rows);
        if (rows.length > 0) {
          setLastPrice(rows[rows.length - 1].close);
        }
      })
      .catch(() => {
        if (!cancelled) setChartData([]);
      })
      .finally(() => {
        if (!cancelled) setChartLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedPair, timeframe, refreshTick]);

  const calcLevels = useCallback((rows: Candle[]) => {
    if (rows.length < 5) return [];
    const tail = rows.slice(-120);
    const highs: { price: number; index: number; time: Candle["time"] }[] = [];
    const lows: { price: number; index: number; time: Candle["time"] }[] = [];
    for (let i = 1; i < tail.length - 1; i++) {
      const prev = tail[i - 1];
      const curr = tail[i];
      const next = tail[i + 1];
      if (curr.high > prev.high && curr.high > next.high) {
        highs.push({ price: curr.high, index: i, time: curr.time });
      }
      if (curr.low < prev.low && curr.low < next.low) {
        lows.push({ price: curr.low, index: i, time: curr.time });
      }
    }
    highs.sort((a, b) => b.price - a.price);
    lows.sort((a, b) => a.price - b.price);
    const topHighs = highs.slice(0, 1);
    const topLows = lows.slice(0, 1);
    const levels: { price: number; type: "resistance" | "support"; time: Candle["time"] }[] = [
      ...topHighs.map((x) => ({ price: x.price, type: "resistance" as const, time: x.time })),
      ...topLows.map((x) => ({ price: x.price, type: "support" as const, time: x.time })),
    ];
    levels.sort((a, b) => b.price - a.price);
    return levels;
  }, []);

  const calcGridBounds = useCallback(
    (rows: Candle[]) => computeGridBounds(rows, timeframe),
    [timeframe],
  );

  const calcGridLevels = useCallback(
    (rows: Candle[], nLevels = 10) => computeGridLevels(rows, timeframe, nLevels),
    [timeframe],
  );

  useEffect(() => {
    if (!chartRef.current) return;
    let chart: any;
    try {
      const createChart = (lightweightCharts as any).createChart || (lightweightCharts as any).default?.createChart;
      if (!createChart) return;
      chart = createChart(chartRef.current, {
        width: chartRef.current.clientWidth || 800,
        height: 400,
        layout: { background: { color: "#ffffff" }, textColor: "#000" },
        rightPriceScale: { scaleMargins: { top: 0.05, bottom: 0.05 } },
        timeScale: {
          timeVisible: timeframe !== "1d",
          secondsVisible: false,
          useLocalTime: true,
          tickMarkFormatter: (time: any) => {
            const d = typeof time === "number" ? new Date(time * 1000) : null;
            const pad = (n: number) => String(n).padStart(2, "0");
            const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
            if (d) {
              if (timeframe !== "1d") {
                return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
              }
              return `${pad(d.getDate())} ${months[d.getMonth()]}`;
            }
            if (typeof time === "string") {
              if (timeframe === "1d") {
                const parts = time.split("-");
                if (parts.length >= 2) {
                  const monthIndex = parseInt(parts[1], 10) - 1;
                  return `${parts[2]} ${months[monthIndex]}`;
                }
                return time;
              }
              return time;
            }
            return String(time);
          },
        },
      });
      const series = chart.addSeries((lightweightCharts as any).CandlestickSeries, {
        upColor: "#22c55e",
        downColor: "#ef4444",
        borderVisible: false,
        wickUpColor: "#22c55e",
        wickDownColor: "#ef4444",
      });
      const markersSeries = chart.addSeries((lightweightCharts as any).LineSeries, {
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        lineWidth: 0,
        pointMarkersVisible: true,
        pointMarkerSize: 6,
        priceScaleId: "left",
      });
      chartInstanceRef.current = chart;
      chartSeriesRef.current = series;
      chartMarkersSeriesRef.current = markersSeries;
    } catch (e) {
      console.error("Chart init error", e);
    }
    return () => {
      chartInstanceRef.current = null;
      chartSeriesRef.current = null;
      chartMarkersSeriesRef.current = null;
      try { chart?.remove?.(); } catch {}
    };
  }, [selectedPair, timeframe]);

  useEffect(() => {
    if (!chartSeriesRef.current || chartData.length === 0) return;
    try {
      const prev = chartSeriesRef.current.dataByIndex?.() ?? chartSeriesRef.current?.data?.() ?? [];
      const prevStr = JSON.stringify(prev);
      const nextStr = JSON.stringify(chartData);
      if (prevStr !== nextStr) {
        console.log("Chart data changed", chartData.length);
        chartSeriesRef.current.setData(chartData);
        chartInstanceRef.current?.timeScale()?.fitContent();
      } else {
        console.log("Chart data unchanged");
      }
    } catch (e) {
      console.error("Chart data error", e);
    }
  }, [chartData, calcLevels]);

  useEffect(() => {
    if (!chartSeriesRef.current || chartData.length === 0) return;
    const draw = () => {
      try {
        chartSeriesRef.current.priceLines?.().forEach((line: any) => {
          try { chartSeriesRef.current.removePriceLine(line); } catch {}
        });
      } catch {}
      const levels = calcLevels(chartData);
      if (levels.length > 0) {
        levels.forEach((level) => {
          try {
            chartSeriesRef.current.createPriceLine({
              price: level.price,
              color: level.type === "resistance" ? "#ef4444" : "#22c55e",
              lineWidth: 1,
              lineStyle: 2,
              axisLabelVisible: true,
              title: level.type === "resistance" ? "R" : "S",
            });
          } catch {}
        });
        const markers = levels.map((level) => ({
          time: level.time,
          value: level.price,
          marker: {
            color: level.type === "resistance" ? "#ef4444" : "#22c55e",
            shape: "circle",
            size: 6,
          },
        }));
        try {
          chartMarkersSeriesRef.current?.setData(markers);
        } catch {}
      }
      const gridLevels = calcGridLevels(chartData, gridNLevels);
       // Гейт по текущему таймфрейму: новая сетка видна только если ADX этого же
       // таймфрейма ниже гейта. Контракт зафиксирован в ./lib/gridGate.ts —
       // не заменять на .some()/.find().
       const passesGate = timeframePassesGridGate(adxStatus, selectedPair, timeframe, gridGate);
      const chartGrid = [...grids]
        .reverse()
        .find((g) => g.pair === selectedPair && (g.phase === "waiting" || g.phase === "active"));
      // Удержание: если по сетке открыта хотя бы одна позиция, уровни не
      // скрываются гейтом — сетка живёт до срабатывания TP.
      const hasOpenPosition =
        chartGrid?.phase === "active" && chartGrid.openLots.length > 0;
      const gridPricesToDraw = hasOpenPosition
        ? chartGrid!.levelPrices
        : passesGate
          ? gridLevels.map((level) => level.price)
          : [];
      gridPricesToDraw.forEach((price) => {
        try {
          chartSeriesRef.current.createPriceLine({
            price,
            color: "#f59e0b",
            lineWidth: 1,
            lineStyle: 1,
            axisLabelVisible: true,
            title: "",
          });
        } catch {}
      });
      if (chartGrid && chartSeriesRef.current) {
        // TP/AVG считаются по сторонам и повторяют торговую логику тика:
        // long — TP вверх (ограничен mid), short — TP вниз (ограничен mid).
        const fills = chartGrid.fillEntries ?? [];
        const realMode = chartGrid.testnetOrderIds.length > 0;
        const longFills = sideFills(fills, "BUY");
        const shortFills = sideFills(fills, "SELL");
        const longLots = realMode
          ? longFills.map((f) => f.level)
          : chartGrid.openLots.filter((l) => l < chartGrid.midPrice);
        const shortLots = realMode
          ? shortFills.map((f) => f.level)
          : chartGrid.openLots.filter((l) => l > chartGrid.midPrice);
        const avgLong = realMode ? avgEntry(longFills) : avgEntryByNotional(longLots) ?? 0;
        const avgShort = realMode ? avgEntry(shortFills) : avgEntryByNotional(shortLots) ?? 0;
        const longTp =
          avgLong > 0
            ? Math.min(avgLong * (1 + chartGrid.tpPct / 100), chartGrid.midPrice)
            : null;
        const shortTp =
          avgShort > 0
            ? Math.max(avgShort * (1 - chartGrid.tpPct / 100), chartGrid.midPrice)
            : null;
        const bothSides = avgLong > 0 && avgShort > 0;
        if (chartGrid.phase === "active") {
          const drawSide = (side: "long" | "short", avg: number, target: number | null) => {
            if (!(avg > 0) || target == null) return;
            try {
              chartSeriesRef.current.createPriceLine({
                price: target,
                color: "#3b82f6",
                lineWidth: 2,
                lineStyle: 0,
                axisLabelVisible: true,
                title: `TP ${side} ${chartGrid.tpPct}%`,
              });
            } catch {}
            try {
              chartSeriesRef.current.createPriceLine({
                price: avg,
                color: "#f97316",
                lineWidth: 1,
                lineStyle: 2,
                axisLabelVisible: true,
                title: bothSides ? `AVG ${side}` : "AVG",
              });
            } catch {}
          };
          drawSide("long", avgLong, longTp);
          drawSide("short", avgShort, shortTp);
        }
        try {
          chartSeriesRef.current.createPriceLine({
            price: chartGrid.midPrice,
            color: "#8b5cf6",
            lineWidth: 2,
            lineStyle: 2,
            axisLabelVisible: true,
            title: "MID",
          });
        } catch {}
      }
    };
    const id = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(id);
  }, [chartData, calcLevels, calcGridLevels, gridGate, gridNLevels, selectedPair, adxStatus, grids, timeframe]);

  // Сетка считается "рассчитанной" для активного окна, когда для текущего
  // таймфрейма проходит ADX-гейт и есть уровни. Кнопка старта доступна только тогда.
  const currentGridLevels = selectedPair ? calcGridLevels(chartData, gridNLevels) : [];
  const currentGridBounds = selectedPair ? calcGridBounds(chartData) : null;
  const gridReady =
    !!selectedPair &&
    timeframePassesGridGate(adxStatus, selectedPair, timeframe, gridGate) &&
    currentGridLevels.length > 0 &&
    currentGridBounds != null;

  const canStartTrading = gridReady && !!tpPct && lastPrice != null;
  const startBlockedReason = !gridReady
    ? "No computed grid for the current timeframe"
    : !tpPct
      ? "Select TP %"
      : lastPrice == null
        ? "No current price"
        : undefined;

  // Phase 3: ручной stop в server-режиме — снимаем tracked-ордера, закрываем
  // позиции по рынку через /grid-orders/close и пишем итог в БД.
  const stopGridServer = useCallback(
    async (g: GridInstance) => {
      const uid = gridUid(g);
      if (g.testnetOrderIds.length > 0) {
        cancelTestnetGridOrders({ symbol: g.pair, orderIds: g.testnetOrderIds }).catch(() => {});
      }
      const protectiveIds = [...(g.stopOrderIds ?? []), ...tpOrderIdList(g)];
      if (protectiveIds.length > 0) {
        cancelGridStops({ symbol: g.pair, orderIds: protectiveIds }).catch(() => {});
      }
      const fills = g.fillEntries ?? [];
      let realizedPnl = g.realizedPnl;
      let realizedUsd = g.realizedUsd;
      const closeSide = async (direction: "long" | "short", sideFillsArr: GridFill[]) => {
        if (sideFillsArr.length === 0) return;
        const res = await closeGridPosition({
          symbol: g.pair,
          direction,
          quantity: sumQty(sideFillsArr),
          sinceMs: g.createdAt,
        }).catch(() => null);
        const exitAvgPrice = Number(res?.avgPrice ?? 0);
        const exitQty = Number(res?.executedQty ?? 0);
        const fundingUsd = Number(res?.fundingUsd ?? 0);
        if (!res?.ok || !(exitAvgPrice > 0)) {
          console.error("[GRID] server stop close failed", {
            pair: g.pair,
            direction,
            error: res?.error,
          });
          return;
        }
        const side: "BUY" | "SELL" = direction === "long" ? "BUY" : "SELL";
        const entryNotional = notional(sideFillsArr);
        const net = sidePnlUsd(side, sideFillsArr, exitAvgPrice, exitQty, fundingUsd);
        realizedPnl += entryNotional > 0 ? (net / entryNotional) * 100 : 0;
        realizedUsd += net;
      };
      await closeSide("long", sideFills(fills, "BUY"));
      await closeSide("short", sideFills(fills, "SELL"));
      const patch = {
        phase: "stopped" as GridPhase,
        realizedPnl,
        realizedUsd,
        lastResult: `${realizedPnl.toFixed(2)}%`,
        openLots: [] as number[],
        fillEntries: [] as GridFill[],
        testnetOrderIds: [] as number[],
        stopOrderIds: [] as number[],
        tpOrderIds: {},
        tpOrderPrices: {},
      };
      await patchGrid(uid, patch).catch(() => {});
      setGrids((prev) =>
        prev.map((x) =>
          x.id === g.id
            ? {
                ...x,
                ...patch,
                unrealizedPnl: null,
                tpOrderIds: undefined,
                tpOrderPrices: undefined,
              }
            : x,
        ),
      );
    },
    [],
  );

  const stopGrid = (id: number) => {
    const g = grids.find((x) => x.id === id);
    if (!g || (g.phase !== "waiting" && g.phase !== "active")) return;
    // Phase 3: в server-режиме stop целиком идёт через API + запись в БД.
    if (SERVER_ENGINE) {
      void stopGridServer(g);
      return;
    }
    if (g.testnetOrderIds.length > 0) {
      // Реальный stop: отменяем resting-ордера и закрываем каждую сторону по рынку.
      if (g.longExitInFlight || g.shortExitInFlight) return;
      cancelTestnetGridOrders({ symbol: g.pair, orderIds: g.testnetOrderIds }).catch(() => {});
      const protectiveIds = [...(g.stopOrderIds ?? []), ...tpOrderIdList(g)];
      if (protectiveIds.length > 0) {
        cancelGridStops({ symbol: g.pair, orderIds: protectiveIds }).catch(() => {});
      }
      const longFills = sideFills(g.fillEntries, "BUY");
      const shortFills = sideFills(g.fillEntries, "SELL");
      setGrids((prev) =>
        prev.map((x) =>
          x.id === id
            ? {
                ...x,
                longExitInFlight: longFills.length > 0,
                shortExitInFlight: shortFills.length > 0,
              }
            : x,
        ),
      );
      void (async () => {
        if (longFills.length > 0) {
          await finalizeRealClose({ id: g.id, pair: g.pair, reason: "stop", direction: "long", quantity: sumQty(longFills), tpPct: g.tpPct, sinceMs: g.createdAt });
        }
        if (shortFills.length > 0) {
          await finalizeRealClose({ id: g.id, pair: g.pair, reason: "stop", direction: "short", quantity: sumQty(shortFills), tpPct: g.tpPct, sinceMs: g.createdAt });
        }
        if (longFills.length === 0 && shortFills.length === 0) {
          await finalizeRealClose({ id: g.id, pair: g.pair, reason: "stop", tpPct: g.tpPct, sinceMs: g.createdAt });
        }
      })();
      return;
    }
    const total = g.realizedPnl + (g.unrealizedPnl ?? 0);
    const openNotional = g.openLots.length * g.orderSizeUsd;
    const usdTotal = g.realizedUsd + ((g.unrealizedPnl ?? 0) * openNotional) / 100;
    const active = g.phase === "active";
    setGrids((prev) =>
      prev.map((x) =>
        x.id !== id
          ? x
              : {
                  ...x,
                  phase: "stopped" as GridPhase,
                  realizedPnl: active ? total : x.realizedPnl,
                  realizedUsd: active ? usdTotal : x.realizedUsd,
                  unrealizedPnl: null,
                  lastResult: active ? `${total.toFixed(2)}%` : x.lastResult,
                  testnetOrderIds: [],
                  stopOrderIds: [],
                  tpOrderIds: undefined,
                  tpOrderPrices: undefined,
                },
      ),
    );
    if (hadPosition(g)) {
      saveGridResult({
        uid: `${g.id}-${g.createdAt}`,
        symbol: g.pair,
        timeframe: g.timeframe,
        phase: "stopped",
        exitReason: "manual",
        tpPct: g.tpPct,
        gate: g.gate,
        levels: g.levels,
        lo: g.lo,
        hi: g.hi,
        mid: g.midPrice,
        entry: avgEntryByNotional(g.openLots) ?? g.startPrice,
        exit: active && prices[g.pair] != null ? prices[g.pair] : null,
        pnl: active ? total : 0,
        pnlUsd: active ? usdTotal : 0,
        positions: active ? g.openLots.length : 0,
        createdAt: new Date(g.createdAt).toISOString(),
        finishedAt: new Date().toISOString(),
        orderSizeUsd: g.orderSizeUsd,
      }).catch(() => {});
    }
    if (g.testnetOrderIds.length > 0) {
      cancelTestnetGridOrders({ symbol: g.pair, orderIds: g.testnetOrderIds }).catch(() => {});
    }
    const protectiveIds = [...(g.stopOrderIds ?? []), ...tpOrderIdList(g)];
    if (protectiveIds.length > 0) {
      cancelGridStops({ symbol: g.pair, orderIds: protectiveIds }).catch(() => {});
    }
  };

  // Изменение TP% у сетки: сразу пересчитывает цель TP у активной
  // (при открытых лотах линия TP и триггер сдвигаются без перезапуска).
  // Для waiting/done/stopped просто обновляет сохранённое значение.
  const updateGridTp = (id: number, nextTpPct: number) => {
    if (!Number.isFinite(nextTpPct) || nextTpPct <= 0) return;
    const clamped = Math.min(nextTpPct, 1000);
    setGrids((prev) =>
      prev.map((g) => (g.id === id ? { ...g, tpPct: clamped } : g)),
    );
    // Phase 3: серверный движок применит tpPct на следующем tick.
    if (SERVER_ENGINE) {
      const g = gridsRef.current.find((x) => x.id === id);
      if (g) void patchGrid(gridUid(g), { tpPct: clamped }).catch(() => {});
    }
  };

  // Изменение SL% / Edge% сетки: только правит сохранённое значение и
  // ограничивает его диапазоном 0.1..50. Защитный стоп на бирже подтягивает
  // protective-sync в течение ~30 с — отдельная кнопка Apply не нужна.
  const updateGridSl = (id: number, which: "sl" | "edge", value: number) => {
    if (!Number.isFinite(value)) return;
    const clamped = Math.min(Math.max(value, 0.1), 50);
    setGrids((prev) =>
      prev.map((g) =>
        g.id === id ? { ...g, [which === "sl" ? "slPct" : "edgePct"]: clamped } : g,
      ),
    );
    // Phase 3: серверный движок применит SL%/Edge% на следующем tick.
    if (SERVER_ENGINE) {
      const g = gridsRef.current.find((x) => x.id === id);
      if (g) {
        const patch = which === "sl" ? { slPct: clamped } : { edgePct: clamped };
        void patchGrid(gridUid(g), patch).catch(() => {});
      }
    }
  };

  // Ручное выставление TP на биржу: пересчитывает цель по текущему tpPct и
  // синхронизирует защитный ордер по каждой стороне с филлами. Нужно потому,
  // что правка TP% намеренно не запускает авто-синк в тике.
  const commitGridTp = (id: number) => {
    const g = grids.find((x) => x.id === id);
    if (!g) return;
    const fills = g.fillEntries ?? [];
    const longFills = sideFills(fills, "BUY");
    const shortFills = sideFills(fills, "SELL");
    const sigFor = (fs: GridFill[]) => `${fs.length}:${(avgEntry(fs) || 0).toFixed(8)}`;
    const jobs: Array<{ side: "long" | "short"; tpPrice: number }> = [];
    const longAvg = avgEntry(longFills);
    if (longFills.length > 0 && longAvg > 0) {
      jobs.push({ side: "long", tpPrice: Math.min(longAvg * (1 + g.tpPct / 100), g.midPrice) });
    }
    const shortAvg = avgEntry(shortFills);
    if (shortFills.length > 0 && shortAvg > 0) {
      jobs.push({ side: "short", tpPrice: Math.max(shortAvg * (1 - g.tpPct / 100), g.midPrice) });
    }
    if (jobs.length === 0) return;
    setGrids((prev) =>
      prev.map((x) => {
        if (x.id !== id) return x;
        const nextSig = { ...(x.tpAutoSig ?? {}) };
        if (longFills.length > 0) nextSig.long = sigFor(longFills);
        if (shortFills.length > 0) nextSig.short = sigFor(shortFills);
        return { ...x, tpAutoSig: nextSig, tpCommitBusy: true };
      }),
    );
    const pending = jobs.map((j) =>
      syncGridTp({
        id: g.id,
        pair: g.pair,
        direction: j.side,
        tpPrice: j.tpPrice,
        tpOrderId: g.tpOrderIds?.[j.side] ?? null,
      }).catch((err) => {
        console.error("[GRID] tp commit failed", {
          pair: g.pair,
          direction: j.side,
          tpPrice: j.tpPrice,
          error: err,
        });
      }),
    );
    void Promise.allSettled(pending).then(() => {
      setGrids((prev) => prev.map((x) => (x.id === id ? { ...x, tpCommitBusy: false } : x)));
    });
  };

  // Изменение размера ордера сетки. waiting (ордера ещё не выставлены) —
  // только локально; active с resting-ордерами и без филлов — resize на бирже.
  const applyGridOrderSize = (id: number, rawDraft: string) => {
    const g = gridsRef.current.find((x) => x.id === id);
    if (!g) return;
    const next = Number(rawDraft);
    const locked =
      (g.phase !== "waiting" && g.phase !== "active") || (g.fillEntries?.length ?? 0) > 0;
    if (locked || !Number.isFinite(next) || next <= 0 || next === g.orderSizeUsd) return;
    if (resizingIds.has(id)) return;

    // Phase 3: в server-режиме размер пишем в БД, а активные resting-ордера
    // пересчитываем через /grid-orders/resize (цены/стороны — из /grid-orders/fills).
    if (SERVER_ENGINE) {
      setResizingIds((prev) => new Set(prev).add(id));
      void (async () => {
        try {
          const patch: { orderSizeUsd: number; testnetOrderIds?: number[] } = { orderSizeUsd: next };
          if (g.phase === "active" && g.testnetOrderIds.length > 0) {
            const fillsRes = await fetchGridFills({
              symbol: g.pair,
              orderIds: g.testnetOrderIds,
              sinceMs: g.createdAt,
            }).catch(() => null);
            const results = Array.isArray(fillsRes?.results) ? (fillsRes as any).results : [];
            const orders = results
              .filter((r: any) => Number.isFinite(Number(r?.orderId)))
              .map((r: any) => {
                const px = Number(r?.price ?? 0);
                const rawSide = typeof r?.side === "string" ? r.side.toUpperCase() : "";
                const side: "BUY" | "SELL" =
                  rawSide === "BUY" || rawSide === "SELL"
                    ? rawSide
                    : px > g.midPrice
                      ? "SELL"
                      : "BUY";
                return { orderId: Number(r.orderId), price: px, side };
              });
            if (orders.length > 0) {
              const res = await resizeGridOrders({ symbol: g.pair, orderSizeUsd: next, orders });
              if (!res.ok) {
                console.error("[GRID] resize failed", {
                  pair: g.pair,
                  orderSizeUsd: next,
                  error: res.error,
                  errors: res.errors,
                });
                return;
              }
              const byOldId = new Map<number, any>();
              for (const r of res.results ?? []) byOldId.set(Number(r?.orderId), r);
              patch.testnetOrderIds = g.testnetOrderIds.map((oldId) => {
                const r = byOldId.get(oldId);
                const newId = Number(r?.newOrderId);
                return r && Number.isFinite(newId) && newId > 0 ? newId : oldId;
              });
              const confirmed = Number(res.orderSizeUsd);
              patch.orderSizeUsd = Number.isFinite(confirmed) && confirmed > 0 ? confirmed : next;
            }
          }
          await patchGrid(gridUid(g), patch).catch(() => {});
          setGrids((prev) => prev.map((x) => (x.id === id ? { ...x, ...patch } : x)));
          setOrderSizeDrafts((prev) => ({ ...prev, [id]: String(patch.orderSizeUsd) }));
        } catch (e: any) {
          console.error("[GRID] resize failed", {
            pair: g.pair,
            orderSizeUsd: next,
            error: e?.message || e,
          });
        } finally {
          setResizingIds((prev) => {
            const set = new Set(prev);
            set.delete(id);
            return set;
          });
        }
      })();
      return;
    }

    const entries = g.entryOrders ?? [];
    if (g.phase === "waiting" || entries.length === 0) {
      setGrids((prev) => prev.map((x) => (x.id === id ? { ...x, orderSizeUsd: next } : x)));
      setOrderSizeDrafts((prev) => ({ ...prev, [id]: String(next) }));
      return;
    }

    setResizingIds((prev) => new Set(prev).add(id));
    void (async () => {
      try {
        const res = await resizeGridOrders({
          symbol: g.pair,
          orderSizeUsd: next,
          orders: entries.map((o) => ({ orderId: o.orderId, price: o.price, side: o.side })),
        });
        if (!res.ok) {
          console.error("[GRID] resize failed", {
            pair: g.pair,
            orderSizeUsd: next,
            error: res.error,
            errors: res.errors,
          });
          return;
        }
        const results = Array.isArray(res.results) ? res.results : [];
        const byOldId = new Map<number, any>();
        for (const r of results) byOldId.set(Number(r?.orderId), r);
        const confirmed = Number(res.orderSizeUsd);
        const size = Number.isFinite(confirmed) && confirmed > 0 ? confirmed : next;
        setGrids((prev) =>
          prev.map((x) => {
            if (x.id !== id) return x;
            const testnetOrderIds = x.testnetOrderIds.map((oldId) => {
              const r = byOldId.get(oldId);
              const newId = Number(r?.newOrderId);
              return r && Number.isFinite(newId) && newId > 0 ? newId : oldId;
            });
            const entryOrders = (x.entryOrders ?? []).map((o) => {
              const r = byOldId.get(o.orderId);
              if (!r) return o;
              const newId = Number(r?.newOrderId);
              const price = Number(r?.price);
              return {
                orderId: Number.isFinite(newId) && newId > 0 ? newId : o.orderId,
                price: Number.isFinite(price) ? price : o.price,
                side: o.side,
              };
            });
            return { ...x, orderSizeUsd: size, testnetOrderIds, entryOrders };
          }),
        );
        setOrderSizeDrafts((prev) => ({ ...prev, [id]: String(size) }));
      } catch (e: any) {
        console.error("[GRID] resize failed", {
          pair: g.pair,
          orderSizeUsd: next,
          error: e?.message || e,
        });
      } finally {
        setResizingIds((prev) => {
          const set = new Set(prev);
          set.delete(id);
          return set;
        });
      }
    })();
  };

  const clearFinished = () => {
    // Best-effort: снимаем оставшиеся защитные ордера завершённых сеток.
    for (const g of gridsRef.current) {
      if (g.phase === "waiting" || g.phase === "active") continue;
      const protectiveIds = [...(g.stopOrderIds ?? []), ...tpOrderIdList(g)];
      if (protectiveIds.length > 0) {
        cancelGridStops({ symbol: g.pair, orderIds: protectiveIds }).catch(() => {});
      }
      if (g.testnetOrderIds.length > 0) {
        cancelTestnetGridOrders({ symbol: g.pair, orderIds: g.testnetOrderIds }).catch(() => {});
      }
    }
    // В server-режиме список сеток поллится из GET /api/grids, поэтому без
    // удаления в БД завершённые строки вернутся через ~5 с. Удаляем их на
    // сервере фоном, не блокируя локальный фильтр (оптимистичный UI).
    if (SERVER_ENGINE) {
      void (async () => {
        for (const phase of ["done", "stopped"] as const) {
          try {
            const res = await deleteAllGrids(phase);
            if (!res.ok) {
              console.error("[GRID] clear finished: server delete failed", {
                phase,
                error: res.error,
              });
            }
          } catch (e: any) {
            console.error("[GRID] clear finished: server delete failed", {
              phase,
              error: e?.message || e,
            });
          }
        }
      })();
    }
    setGrids((prev) => prev.filter((g) => g.phase === "waiting" || g.phase === "active"));
  };

  // Cancelable confirmation for the reset: the modal arms for 5 s before the
  // destructive action can be confirmed. Esc or backdrop click aborts.
  useEffect(() => {
    if (!resetConfirmOpen) return;
    setResetCountdown(5);
    const id = setInterval(() => {
      setResetCountdown((c) => (c <= 1 ? 0 : c - 1));
    }, 1000);
    return () => clearInterval(id);
  }, [resetConfirmOpen]);

  useEffect(() => {
    if (!resetConfirmOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !resetBusy) setResetConfirmOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [resetConfirmOpen, resetBusy]);

  const resetEverything = async () => {
    setResetBusy(true);
    try {
      const stop = await stopAllBotsAndReset();
      const reset = await resetGridAccount();
      // Phase 3: в server-режиме дополнительно чистим таблицу grids в БД.
      let serverGridsDeleted: number | undefined;
      if (SERVER_ENGINE) {
        const del = await deleteAllGrids();
        if (!del.ok) {
          console.error("[RESET] server grids delete failed", del.error);
        } else {
          serverGridsDeleted = del.deleted;
        }
      }
      localStorage.removeItem(PERSIST_KEY);
      localStorage.setItem(RESET_SEEN_KEY, String(reset?.resetAt ?? Date.now()));
      // Защитные STOP/TP-ордера снимаем явно (серверный /reset чистит аккаунт целиком).
      for (const g of grids) {
        const protectiveIds = [...(g.stopOrderIds ?? []), ...tpOrderIdList(g)];
        if (protectiveIds.length > 0) {
          cancelGridStops({ symbol: g.pair, orderIds: protectiveIds }).catch(() => {});
        }
      }
      setGrids([]);
      const botsStopped = stop?.bots_cleared ?? "—";
      const canceledOrders = reset?.canceledOrders ?? "—";
      const closedPositions = Array.isArray(reset?.closedPositions) ? reset.closedPositions.length : "—";
      const gridHistoryUpdated = reset?.gridHistoryUpdated ?? "—";
      const resetErrors = Array.isArray(reset?.errors)
        ? reset.errors.map((e: any) => e?.error ?? String(e))
        : [];
      const errors = [stop?.error, reset?.error, ...resetErrors].filter(Boolean);
      const serverPart =
        serverGridsDeleted != null ? `; server grids deleted: ${serverGridsDeleted}` : "";
      const summary = `Bots stopped: ${botsStopped}; orders canceled: ${canceledOrders}; positions closed: ${closedPositions}; grid_history stopped: ${gridHistoryUpdated}${serverPart}${errors.length ? `; errors: ${errors.join("; ")}` : ""}`;
      console.log("[RESET] reset everything", { stop, reset, summary });
      alert(summary);
    } finally {
      setResetBusy(false);
    }
  };

  // Долларовый PnL: размер одного ордера × процент.
  const fmtUsd = (pnlPct: number, size: number = orderSizeUsd) =>
    `${pnlPct >= 0 ? "+" : "-"}$${Math.abs((size * pnlPct) / 100).toFixed(2)}`;

  // В server-режиме активный режим берём из конфига движка, а не из локального state.
  const effectiveTradeMode: "manual" | "auto" = SERVER_ENGINE
    ? serverEngineConfig?.autoEnabled
      ? "auto"
      : "manual"
    : tradeMode;

  if (loading) {
    return <div className="p-6">Loading pairs...</div>;
  }

  return (
    <div className="p-6">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold text-black">Trading Pairs</h1>
        <div className="ml-4 flex items-center gap-2 text-xs">
          <span className="font-semibold text-black">Mode:</span>
          <Button
            size="sm"
            variant={effectiveTradeMode === "manual" ? "default" : "outline"}
            onClick={() => void selectTradeMode("manual")}
            className={
              effectiveTradeMode === "manual"
                ? "bg-black text-white hover:bg-black/90"
                : "bg-white text-black border-gray-300 hover:bg-gray-100"
            }
          >
            manual
          </Button>
          <Button
            size="sm"
            variant={effectiveTradeMode === "auto" ? "default" : "outline"}
            onClick={() => void selectTradeMode("auto")}
            className={
              effectiveTradeMode === "auto"
                ? "bg-black text-white hover:bg-black/90"
                : "bg-white text-black border-gray-300 hover:bg-gray-100"
            }
          >
            auto
          </Button>
          {SERVER_ENGINE && (
            <span
              className="ml-2 text-[10px] text-zinc-500"
              title={
                serverEngineConfig?.restartRequired
                  ? "Engine enable/interval changes need a restart"
                  : undefined
              }
            >
              auto applies on the next engine tick (~10s)
              {serverEngineConfig ? (
                <>
                  {" "}
                  · interval{" "}
                  {typeof serverEngineConfig.intervalMs === "number"
                    ? `${Math.round(serverEngineConfig.intervalMs / 1000)}s`
                    : "—"}{" "}
                  · autoMax {serverEngineConfig.autoMax ?? "—"} · order $
                  {serverEngineConfig.autoOrderUsd ?? "—"}
                </>
              ) : null}
              {serverEngineConfig?.restartRequired ? " · restartRequired" : ""}
            </span>
          )}
        </div>
      </div>
      {serverMigrationError && (
        <div className="mb-4 rounded border border-red-300 bg-red-50 p-3 text-xs text-red-700">
          Local state migration to the server failed: {serverMigrationError}. The browser
          state was kept, so the migration will not run twice.
        </div>
      )}
      {pairs.length === 0 ? (
        <p className="text-zinc-500">No pairs found</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {pairs.map((pair) => {
            const isActive = selectedPair === pair;
            return (
              <Button
                key={pair}
                variant="outline"
                size="sm"
                onClick={() => setSelectedPair(isActive ? null : pair)}
                className={
                  "border " +
                  (isActive
                    ? "bg-blue-600 text-white border-blue-600 hover:bg-blue-700"
                    : "bg-white text-black border-gray-300 hover:bg-gray-100")
                }
              >
                {pair}
              </Button>
            );
          })}
        </div>
      )}

      {selectedPair && (
        <div className="mt-6">
          <div className="flex items-center gap-3 mb-2">
            <h2 className="text-xl font-semibold text-black">{selectedPair}</h2>
            {lastPrice !== null && (
              <span className="text-lg font-mono text-green-600">
                ${lastPrice.toFixed(4)}
              </span>
            )}
            <div className="flex gap-1">
              {(["5m", "15m", "30m", "1h", "4h", "12h", "1d"] as const).map((iv) => (
                <Button
                  key={iv}
                  size="sm"
                  variant={timeframe === iv ? "default" : "outline"}
                  onClick={() => setTimeframe(iv)}
                  className={
                    "min-w-[40px] " +
                    (timeframe === iv
                      ? "bg-black text-white hover:bg-black/90"
                      : "bg-white text-black border-gray-300 hover:bg-gray-100")
                  }
                >
                  {iv}
                </Button>
              ))}
          </div>
          {tpPct != null && (
            <div className="mb-2 text-xs text-black">
              estimated profit: {(() => {
                const lows = chartData.map((r) => r.low);
                const highs = chartData.map((r) => r.high);
                const lo = Math.min(...lows);
                const hi = Math.max(...highs);
                const range = hi - lo;
                const estimated = range * (tpPct / 100);
                return Number.isFinite(estimated) ? `~${estimated.toFixed(4)} USDT` : "N/A";
              })()}
            </div>
          )}
          </div>
          <div className="mb-2 flex flex-wrap gap-2 text-xs">
            {(["1h", "4h", "12h", "1d"] as const).map((tf) => {
              const color =
                tf === "1h"
                  ? "bg-sky-100 text-sky-700"
                  : tf === "4h"
                    ? "bg-emerald-100 text-emerald-700"
                    : tf === "12h"
                      ? "bg-amber-100 text-amber-700"
                      : "bg-purple-100 text-purple-700";
              const passing = pairs.filter((p) => timeframePassesGridGate(adxStatus, p, tf, gridGate));
              return (
                <>
                  <span className="font-semibold text-black">ADX &lt; {gridGate} ({tf}):</span>
                  {passing.length === 0 && (
                    <span className="text-zinc-500">no data</span>
                  )}
                  {passing.map((p) => (
                    <span key={`${tf}-${p}`} className={`rounded px-2 py-0.5 ${color}`}>
                      {p}
                    </span>
                  ))}
                </>
              );
            })}
            <span className="ml-2 font-semibold text-black">Count:</span>
            <Button
              size="sm"
              variant={gridNLevels === 10 ? "default" : "outline"}
              onClick={() => setGridNLevels(10)}
              className={
                "min-w-[40px] " +
                (gridNLevels === 10
                  ? "bg-black text-white hover:bg-black/90"
                  : "bg-white text-black border-gray-300 hover:bg-gray-100")
              }
            >
              L10
            </Button>
            <Button
              size="sm"
              variant={gridNLevels === 20 ? "default" : "outline"}
              onClick={() => setGridNLevels(20)}
              className={
                "min-w-[40px] " +
                (gridNLevels === 20
                  ? "bg-black text-white hover:bg-black/90"
                  : "bg-white text-black border-gray-300 hover:bg-gray-100")
              }
            >
              L20
            </Button>
            <span className="ml-2 font-semibold text-black">Gate:</span>
            {([15, 20, 25] as const).map((g) => (
              <Button
                key={g}
                size="sm"
                variant={gridGate === g ? "default" : "outline"}
                onClick={() => setGridGate(g)}
                className={
                  "min-w-[40px] " +
                  (gridGate === g
                    ? "bg-black text-white hover:bg-black/90"
                    : "bg-white text-black border-gray-300 hover:bg-gray-100")
                }
              >
                {g}
              </Button>
            ))}
            <span className="ml-2 font-semibold text-black">TP %:</span>
            {([1, 2, 5, 10] as const).map((pct) => (
              <Button
                key={pct}
                size="sm"
                variant={tpPct === pct ? "default" : "outline"}
                onClick={() => setTpPct(tpPct === pct ? null : pct)}
                className={
                  "min-w-[40px] " +
                  (tpPct === pct
                    ? "bg-black text-white hover:bg-black/90"
                    : "bg-white text-black border-gray-300 hover:bg-gray-100")
                }
              >
                {pct}%
              </Button>
            ))}
            <span className="ml-2 font-semibold text-black">Order $:</span>
            <input
              type="number"
              min="0"
              step="1"
              value={orderSizeUsd}
              onChange={(e) => setOrderSizeUsd(Math.max(0, Number(e.target.value) || 0))}
              className="w-20 rounded border border-gray-300 px-2 py-1 text-black"
              title="Size of one grid order (in dollars)"
            />
            <Button
              size="sm"
              variant="outline"
              disabled={tradeMode === "auto" || !canStartTrading}
              title={tradeMode === "auto" ? "In auto mode grids start automatically" : startBlockedReason}
              onClick={() => {
                if (!lastPrice || !tpPct || !gridReady || !currentGridBounds) return;
                const pair = selectedPair || "";
                const midPrice = currentGridBounds.mid;
                // Ждём касания середины: если цена выше — ждём снижения, если ниже — роста.
                const startSide: "above" | "below" = lastPrice > midPrice ? "above" : "below";
                setPrices((prev) => ({ ...prev, [pair]: lastPrice }));
                setGridBoundsCache((prev) => ({
                  ...prev,
                  [`${pair}|${timeframe}`]: currentGridBounds,
                }));
                setGrids((prev) => [
                  ...prev,
                  {
                    id: nextGridIdRef.current++,
                    pair,
                    timeframe,
                    tpPct,
                    slPct: 2,
                    edgePct: 2,
                    gate: gridGate,
                    levels: currentGridLevels.length,
                    lo: currentGridBounds.lo,
                    hi: currentGridBounds.hi,
                    midPrice,
                    startPrice: midPrice,
                    startSide,
                    phase: "waiting",
                    unrealizedPnl: null,
                    realizedPnl: 0,
                    realizedUsd: 0,
                    lastResult: null,
                    createdAt: Date.now(),
                    orderSizeUsd,
                    levelPrices: currentGridLevels.map((l) => l.price),
                    openLots: [],
                    lastPrice: null,
                    testnetOrderIds: [],
                  },
                  ]);
              }}
              className={
                "ml-2 " +
                (canStartTrading
                  ? "bg-white text-black border-gray-300 hover:bg-gray-100"
                  : "bg-white text-black border-gray-300 opacity-50 cursor-not-allowed")
              }
            >
              start trading
            </Button>
          </div>
          <div ref={chartRef} className="h-[400px] w-full border" />
          {selectedPair && (
            <div className="mt-3 rounded border bg-white p-3 text-xs text-black">
              <div className="flex items-center gap-3 mb-2">
                <button
                  type="button"
                  className={activeTab === "stats" ? "font-semibold underline" : "text-zinc-500 hover:text-black"}
                  onClick={() => setActiveTab("stats")}
                >
                  Statistics
                </button>
                <button
                  type="button"
                  className={activeTab === "history" ? "font-semibold underline" : "text-zinc-500 hover:text-black"}
                  onClick={() => setActiveTab("history")}
                >
                  History
                </button>
                <div className="ml-auto flex items-center gap-2">
                  {activeTab === "stats" && (
                    <>
                      <span>grids: <span className="font-mono">{grids.length}</span></span>
                      {grids.some((g) => g.phase === "done" || g.phase === "stopped") && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={clearFinished}
                          className="bg-white text-black border-gray-300 hover:bg-gray-100"
                        >
                          clear finished
                        </Button>
                      )}
                    </>
                  )}
                  {activeTab === "history" && (
                    <>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setHistoryTick((t) => t + 1)}
                        className="bg-white text-black border-gray-300 hover:bg-gray-100"
                      >
                        refresh
                      </Button>
                      {history.length > 0 && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={async () => {
                            if (!window.confirm("Clear all grid history?")) return;
                            await clearGridHistory();
                            setHistory([]);
                            setHistoryTick((t) => t + 1);
                          }}
                          className="bg-white text-black border-gray-300 hover:bg-gray-100"
                        >
                          clear history
                        </Button>
                      )}
                    </>
                  )}
                </div>
              </div>
              {activeTab === "stats" && (
                <>
              <div className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-3">
                <div>pair: <span className="font-mono">{selectedPair}</span></div>
                <div>grid TF: <span className="font-mono">{timeframe}</span></div>
                <div>gate: <span className="font-mono">{gridGate}</span></div>
                <div>levels: <span className="font-mono">{gridNLevels}</span></div>
                <div>TP %: <span className="font-mono">{tpPct ?? "—"}</span></div>
                <div>order $: <span className="font-mono">{orderSizeUsd}</span></div>
                <div>
                  ADX ({timeframe}):{" "}
                  <span className="font-mono">{getTimeframeAdx(adxStatus, selectedPair, timeframe) ?? "—"}</span>
                </div>
                <div>grid lo: <span className="font-mono">{currentGridBounds ? currentGridBounds.lo.toFixed(4) : "—"}</span></div>
                <div>grid hi: <span className="font-mono">{currentGridBounds ? currentGridBounds.hi.toFixed(4) : "—"}</span></div>
                <div>grid mid: <span className="font-mono">{currentGridBounds ? currentGridBounds.mid.toFixed(4) : "—"}</span></div>
                <div>
                  bot:{" "}
                  <span className={botsStatus[selectedPair]?.is_running ? "text-green-600" : "text-zinc-500"}>
                    {botsStatus[selectedPair]?.is_running ? "running" : "stopped"}
                  </span>
                </div>
                <div>
                   position:{" "}
                  <span className="font-mono">
                    {(() => {
                      const openPositions = grids
                        .filter((g) => g.pair === selectedPair && g.phase === "active")
                        .reduce((sum, g) => sum + g.openLots.length, 0);
                      return openPositions > 0 ? `${openPositions} open` : "none";
                    })()}
                  </span>
                </div>
                <div>
                  last price:{" "}
                  <span className="font-mono">
                    {(() => {
                      const live = lastPrice ?? botsStatus[selectedPair]?.current_price ?? null;
                      return live != null ? live.toFixed(4) : "—";
                    })()}
                  </span>
                </div>
                <div className="col-span-2 sm:col-span-3">
                  last heartbeat:{" "}
                  <span className="font-mono">
                    {botsStatus[selectedPair]?.last_heartbeat
                      ? new Date(botsStatus[selectedPair].last_heartbeat).toLocaleString()
                      : "—"}
                  </span>
                </div>
              </div>
              <div className="mt-2 mb-1 flex items-center gap-2">
                <span className="font-semibold">Grids</span>
                {/* Phase 3: активный режим движка — server (viewer) или browser. */}
                <span
                  className={`inline-flex items-center rounded px-2 py-0.5 text-[10px] font-semibold ${
                    SERVER_ENGINE ? "bg-indigo-100 text-indigo-700" : "bg-zinc-200 text-zinc-700"
                  }`}
                  title={
                    SERVER_ENGINE
                      ? "Server engine: grids run in the api-server; this tab is a viewer"
                      : "Browser engine: grids run in this browser tab"
                  }
                >
                  engine: {SERVER_ENGINE ? "server" : "browser"}
                </span>
              </div>
              {grids.length === 0 ? (
                <div className="text-zinc-500">No running grids</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="min-w-full text-xs">
                    <thead>
                      <tr className="text-left text-zinc-500">
                        <th className="pr-3">#</th>
                        <th className="pr-3">pair</th>
                        <th className="pr-3">TF</th>
                        <th className="pr-3">L</th>
                        <th className="pr-3">TP%</th>
                        <th className="pr-3">TP price</th>
                        <th className="pr-3">SL%</th>
                        <th className="pr-3">Edge%</th>
                        <th className="pr-3">Order $</th>
                        <th className="pr-3">mid</th>
                        <th className="pr-3">phase</th>
                        <th className="pr-3">pos</th>
                        <th className="pr-3">unreal.</th>
                        <th className="pr-3">real.</th>
                        <th className="pr-3">last</th>
                        <th className="pr-3">placed</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {grids.map((g) => (
                        <tr key={g.id} className="border-t">
                          <td className="pr-3">{g.id}</td>
                          <td className="pr-3 font-mono">{g.pair}</td>
                          <td className="pr-3">{g.timeframe}</td>
                          <td className="pr-3">{g.levels}</td>
                          <td className="pr-3">
                            {g.phase === "waiting" || g.phase === "active" ? (
                              <span className="inline-flex items-center gap-1">
                                <input
                                  type="number"
                                  min="0.1"
                                  step="0.1"
                                  value={g.tpPct}
                                  onChange={(e) => updateGridTp(g.id, Number(e.target.value))}
                                  className="w-16 rounded border border-gray-300 px-1 py-0.5 text-black"
                                  title={'Change the grid TP % (applies to Binance only via "Push TP to Binance")'}
                                />
                                <span>%</span>
                                {g.phase === "active" &&
                                  g.testnetOrderIds.length > 0 &&
                                  (() => {
                                    const fills = g.fillEntries ?? [];
                                    const longFills = sideFills(fills, "BUY");
                                    const shortFills = sideFills(fills, "SELL");
                                    const hasFills = longFills.length > 0 || shortFills.length > 0;
                                    const longAvg = avgEntry(longFills);
                                    const shortAvg = avgEntry(shortFills);
                                    const expLong =
                                      longFills.length > 0 && longAvg > 0
                                        ? Math.min(longAvg * (1 + g.tpPct / 100), g.midPrice)
                                        : null;
                                    const expShort =
                                      shortFills.length > 0 && shortAvg > 0
                                        ? Math.max(shortAvg * (1 - g.tpPct / 100), g.midPrice)
                                        : null;
                                    // Цель считается применённой, если tpOrderPrices
                                    // содержит её с относительной точностью 1e-4.
                                    const bad: Array<{ side: "long" | "short"; exp: number }> = [];
                                    const check = (side: "long" | "short", exp: number | null) => {
                                      if (exp == null) return;
                                      const prev = g.tpOrderPrices?.[side];
                                      if (
                                        typeof prev !== "number" ||
                                        !Number.isFinite(prev) ||
                                        Math.abs(prev - exp) / Math.max(Math.abs(exp), 1e-9) > 1e-4
                                      ) {
                                        bad.push({ side, exp });
                                      }
                                    };
                                    check("long", expLong);
                                    check("short", expShort);
                                    return (
                                      <>
                                        <Button
                                          size="sm"
                                          variant="outline"
                                          disabled={!!g.tpCommitBusy || !hasFills}
                                          onClick={() => commitGridTp(g.id)}
                                          className="whitespace-nowrap bg-white text-black border-gray-300 hover:bg-gray-100"
                                        >
                                          {g.tpCommitBusy ? "Sending…" : "Push TP to Binance"}
                                        </Button>
                                        <span
                                          className={`inline-flex items-center px-1 py-0.5 rounded cursor-help ${
                                            bad.length === 0 ? "text-zinc-500" : "bg-red-100 text-red-700"
                                          }`}
                                          title={
                                            bad.length === 0
                                              ? "Exchange TP orders match the computed target"
                                              : bad.map((b) => `${b.side}: expected ${b.exp}`).join("\n")
                                          }
                                        >
                                          {bad.length === 0 ? "TP on exchange" : "TP not applied"}
                                        </span>
                                      </>
                                    );
                                  })()}
                              </span>
                            ) : (
                              <span className="font-mono">{g.tpPct}%</span>
                            )}
                          </td>
                          <td className="pr-3 font-mono">
                            {(() => {
                              const fills = g.fillEntries ?? [];
                              const realMode = g.testnetOrderIds.length > 0;
                              const longFills = sideFills(fills, "BUY");
                              const shortFills = sideFills(fills, "SELL");
                              const avgLong = realMode
                                ? avgEntry(longFills)
                                : avgEntryByNotional(g.openLots.filter((l) => l < g.midPrice)) ?? 0;
                              const avgShort = realMode
                                ? avgEntry(shortFills)
                                : avgEntryByNotional(g.openLots.filter((l) => l > g.midPrice)) ?? 0;
                              const longTp =
                                avgLong > 0 ? Math.min(avgLong * (1 + g.tpPct / 100), g.midPrice) : null;
                              const shortTp =
                                avgShort > 0 ? Math.max(avgShort * (1 - g.tpPct / 100), g.midPrice) : null;
                              const fmtPrice = (p: number) =>
                                p >= 1000 ? p.toFixed(1) : p >= 1 ? p.toFixed(4) : p.toFixed(6);
                              const onExchange = (v?: number) =>
                                typeof v === "number" && Number.isFinite(v) ? fmtPrice(v) : "—";
                              const title = `Expected: long ${
                                longTp != null ? fmtPrice(longTp) : "—"
                              }, short ${shortTp != null ? fmtPrice(shortTp) : "—"} | On exchange: long ${onExchange(
                                g.tpOrderPrices?.long,
                              )}, short ${onExchange(g.tpOrderPrices?.short)}`;
                              if (longTp != null && shortTp != null) {
                                return (
                                  <span title={title}>
                                    L {fmtPrice(longTp)} / S {fmtPrice(shortTp)}
                                  </span>
                                );
                              }
                              if (longTp != null) {
                                return <span title={title}>{fmtPrice(longTp)}</span>;
                              }
                              if (shortTp != null) {
                                return <span title={title}>{fmtPrice(shortTp)}</span>;
                              }
                              return (
                                <span className="text-zinc-500" title={title}>
                                  —
                                </span>
                              );
                            })()}
                          </td>
                          <td className="pr-3">
                            {g.phase === "waiting" || g.phase === "active" ? (
                              (() => {
                                const stops = computeGridStops(
                                  g,
                                  avgEntry(sideFills(g.fillEntries, "BUY")),
                                  avgEntry(sideFills(g.fillEntries, "SELL")),
                                );
                                return (
                                  <span className="inline-flex items-center gap-1">
                                    <input
                                      type="number"
                                      min="0.1"
                                      max="50"
                                      step="0.1"
                                      value={g.slPct}
                                      onChange={(e) => updateGridSl(g.id, "sl", Number(e.target.value))}
                                      className="w-16 rounded border border-gray-300 px-1 py-0.5 text-black"
                                      title={`Stop-loss % from the average entry\nlong \u2248 ${stops.long.toFixed(4)}, short \u2248 ${stops.short.toFixed(4)}`}
                                    />
                                    <span>%</span>
                                  </span>
                                );
                              })()
                            ) : (
                              <span className="font-mono">{g.slPct}%</span>
                            )}
                          </td>
                          <td className="pr-3">
                            {g.phase === "waiting" || g.phase === "active" ? (
                              <span className="inline-flex items-center gap-1">
                                <input
                                  type="number"
                                  min="0.1"
                                  max="50"
                                  step="0.1"
                                  value={g.edgePct}
                                  onChange={(e) => updateGridSl(g.id, "edge", Number(e.target.value))}
                                  className="w-16 rounded border border-gray-300 px-1 py-0.5 text-black"
                                  title="Stop offset % from the outermost grid order (backstop)"
                                />
                                <span>%</span>
                              </span>
                            ) : (
                              <span className="font-mono">{g.edgePct}%</span>
                            )}
                          </td>
                          <td className="pr-3">
                            {(() => {
                              const locked = !(
                                (g.phase === "waiting" || g.phase === "active") &&
                                (g.fillEntries?.length ?? 0) === 0
                              );
                              if (locked) {
                                return (
                                  <span
                                    className="font-mono"
                                    title="Lot size is locked after the first fill"
                                  >
                                    {g.orderSizeUsd}
                                  </span>
                                );
                              }
                              const draft = orderSizeDrafts[g.id] ?? String(g.orderSizeUsd);
                              const parsed = Number(draft);
                              const busy = resizingIds.has(g.id);
                              const invalid =
                                !Number.isFinite(parsed) || parsed <= 0 || parsed === g.orderSizeUsd;
                              return (
                                <span className="inline-flex items-center gap-1">
                                  <input
                                    type="number"
                                    min="0.1"
                                    step="1"
                                    value={draft}
                                    onChange={(e) =>
                                      setOrderSizeDrafts((prev) => ({ ...prev, [g.id]: e.target.value }))
                                    }
                                    className="w-16 rounded border border-gray-300 px-1 py-0.5 text-black"
                                    title="Size of one grid order (in dollars)"
                                  />
                                  <Button
                                    size="sm"
                                    variant="outline"
                                    disabled={invalid || busy}
                                    onClick={() => applyGridOrderSize(g.id, draft)}
                                    className="whitespace-nowrap bg-white text-black border-gray-300 hover:bg-gray-100"
                                  >
                                    {busy ? "Applying…" : "Apply"}
                                  </Button>
                                  <span className="text-zinc-500">can be changed until the first fill</span>
                                </span>
                              );
                            })()}
                          </td>
                          <td className="pr-3 font-mono">{g.midPrice.toFixed(4)}</td>
                          <td className="pr-3">
                            <span
                              className={
                                g.phase === "active"
                                  ? "text-green-600"
                                  : g.phase === "waiting"
                                    ? "text-amber-600"
                                    : g.phase === "done"
                                      ? "text-blue-600"
                                      : "text-zinc-500"
                              }
                            >
                              {g.phase}
                            </span>
                          </td>
                          <td className="pr-3">
                            {g.phase === "active"
                              ? (() => {
                                  const fills = g.fillEntries ?? [];
                                  const longs =
                                    fills.length > 0
                                      ? fills.filter((f) => f.side === "BUY").length
                                      : g.openLots.filter((p) => p < g.midPrice).length;
                                  const shorts =
                                    fills.length > 0
                                      ? fills.filter((f) => f.side === "SELL").length
                                      : g.openLots.filter((p) => p > g.midPrice).length;
                                  return `L: ${longs} / S: ${shorts}`;
                                })()
                              : "0"}
                          </td>
                          <td className={`pr-3 font-mono ${(g.unrealizedPnl ?? 0) >= 0 ? "text-green-600" : "text-red-600"}`}>
                            {(() => {
                              if (g.phase !== "active") return "—";
                              const fills = g.fillEntries ?? [];
                              const live = prices[g.pair] ?? g.lastPrice;
                              if (fills.length > 0 && live != null) {
                                const longFills = sideFills(fills, "BUY");
                                const shortFills = sideFills(fills, "SELL");
                                const ln = notional(longFills);
                                const sn = notional(shortFills);
                                const totalN = ln + sn;
                                if (totalN > 0) {
                                  const la = avgEntry(longFills);
                                  const sa = avgEntry(shortFills);
                                  const lp = la > 0 ? (live / la - 1) * 100 : 0;
                                  const sp = sa > 0 && live > 0 ? (sa / live - 1) * 100 : 0;
                                  const pctReal = (lp * ln + sp * sn) / totalN;
                                  return `${pctReal.toFixed(2)}% (${fmtUsd(pctReal, totalN)})`;
                                }
                              }
                              if (g.unrealizedPnl != null) {
                                return `${g.unrealizedPnl.toFixed(2)}% (${fmtUsd(g.unrealizedPnl, g.orderSizeUsd * g.openLots.length)})`;
                              }
                              return "—";
                            })()}
                          </td>
                          <td className={`pr-3 font-mono ${g.realizedPnl >= 0 ? "text-green-600" : "text-red-600"}`}>
                            {g.realizedPnl.toFixed(2)}% ({g.realizedUsd >= 0 ? "+" : "-"}${Math.abs(g.realizedUsd).toFixed(2)})
                          </td>
                          <td className="pr-3 font-mono">{g.lastResult ?? "—"}</td>
                          <td className="pr-3">
                            {g.phase === "active" && g.testnetOrderIds.length > 0 && (
                              <span
                                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs cursor-help ${
                                  (g.failedLevels?.length ?? 0) > 0 ? "bg-red-100 text-red-700" : "bg-green-100 text-green-700"
                                }`}
                                title={
                                  (g.failedLevels?.length ?? 0) > 0
                                    ? `Placed: ${g.testnetOrderIds.length} / Skipped: ${g.skippedLevels?.length ?? 0} / Failed: ${g.failedLevels?.length ?? 0}\nErrors: ${g.lastPlacementError ?? "unknown"}\nSkipped levels: ${g.skippedLevels?.join(", ") ?? "—"}\nFailed levels: ${g.failedLevels?.join(", ") ?? "—"}`
                                    : `Placed: ${g.testnetOrderIds.length} / Skipped: ${g.skippedLevels?.length ?? 0} / Failed: 0\nSkipped levels: ${g.skippedLevels?.join(", ") ?? "—"}`
                                }
                              >
                                Placed: {g.testnetOrderIds.length} / Skipped: {g.skippedLevels?.length ?? 0} / Failed: {g.failedLevels?.length ?? 0}
                              </span>
                            )}
                            {g.phase === "active" && g.testnetOrderIds.length === 0 && g.lastPlacementError && (
                              <span
                                className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs cursor-help bg-red-100 text-red-700"
                                title={`Error: ${g.lastPlacementError}\nFailed levels: ${g.failedLevels?.join(", ") ?? "—"}`}
                              >
                                Placement failed
                              </span>
                            )}
                            {g.phase === "active" && g.testnetOrderIds.length === 0 && !g.lastPlacementError && (
                              <span className="text-zinc-500">pending</span>
                            )}
                            {g.phase !== "active" && <span className="text-zinc-500">—</span>}
                          </td>
                          <td className="pr-3">
                            {(g.phase === "waiting" || g.phase === "active") && (
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => stopGrid(g.id)}
                                className="bg-white text-black border-gray-300 hover:bg-gray-100"
                              >
                                stop
                              </Button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
                </>
              )}
              {activeTab === "history" && (
                historyLoading ? (
                  <div className="text-zinc-500">loading…</div>
                ) : history.length === 0 ? (
                  <div className="text-zinc-500">history is empty</div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="min-w-full text-xs">
                      <thead>
                        <tr className="text-left text-zinc-500">
                          <th className="pr-3">#</th>
                          <th className="pr-3">pair</th>
                          <th className="pr-3">TF</th>
                          <th className="pr-3">L</th>
                          <th className="pr-3">TP%</th>
                          <th className="pr-3">mid</th>
                          <th className="pr-3">entry</th>
                          <th className="pr-3">exit</th>
                          <th className="pr-3">PnL</th>
                          <th className="pr-3">phase</th>
                          <th className="pr-3">reason</th>
                          <th className="pr-3">finished</th>
                        </tr>
                      </thead>
                      <tbody>
                        {history.map((h) => (
                          <tr key={h.id} className="border-t">
                            <td className="pr-3">{h.id}</td>
                            <td className="pr-3 font-mono">{h.symbol}</td>
                            <td className="pr-3">{h.timeframe}</td>
                            <td className="pr-3">{h.levels}</td>
                            <td className="pr-3">{h.tp_pct}%</td>
                            <td className="pr-3 font-mono">{Number(h.mid).toFixed(4)}</td>
                            <td className="pr-3 font-mono">{Number(h.entry).toFixed(4)}</td>
                            <td className="pr-3 font-mono">{h.exit != null ? Number(h.exit).toFixed(4) : "—"}</td>
                            <td className={`pr-3 font-mono ${(h.pnl ?? 0) >= 0 ? "text-green-600" : "text-red-600"}`}>
                              {h.pnl != null
                                ? `${h.pnl >= 0 ? "+" : ""}${Number(h.pnl).toFixed(2)}% (${
                                    h.pnl_usd != null
                                      ? `${Number(h.pnl_usd) >= 0 ? "+" : "-"}$${Math.abs(Number(h.pnl_usd)).toFixed(2)}`
                                      : fmtUsd(Number(h.pnl), Number(h.order_size_usd ?? orderSizeUsd))
                                  })`
                                : "—"}
                            </td>
                            <td className="pr-3">{h.phase}</td>
                            <td className="pr-3">
                              {(() => {
                                const meta = exitReasonMeta(h.exit_reason);
                                if (!meta) return h.exit_reason;
                                return (
                                  <span
                                    className={`inline-flex items-center px-2 py-0.5 rounded text-xs cursor-help ${meta.className}`}
                                    title={meta.title}
                                  >
                                    {meta.label}
                                  </span>
                                );
                              })()}
                            </td>
                            <td className="pr-3">{h.finished_at ? new Date(h.finished_at).toLocaleString() : "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )
              )}
            </div>
          )}
        </div>
      )}

      <div className="mt-10 rounded-lg border-2 border-red-300 bg-red-50 p-4">
        <h2 className="text-lg font-bold text-red-700">Danger zone</h2>
        <p className="mt-1 text-xs text-red-700">
          Stops all bots, cancels all open orders, closes all positions, and clears all grids.
        </p>
        <Button
          size="sm"
          variant="destructive"
          onClick={() => setResetConfirmOpen(true)}
          disabled={resetBusy}
          className="mt-3 bg-red-600 text-white hover:bg-red-700"
        >
          {resetBusy ? "Resetting…" : "Reset everything"}
        </Button>
      </div>

      {resetConfirmOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget && !resetBusy) setResetConfirmOpen(false);
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="reset-confirm-title"
            className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl"
          >
            <h3 id="reset-confirm-title" className="text-lg font-bold text-black">
              Reset everything?
            </h3>
            <p className="mt-2 text-sm text-zinc-700">This will:</p>
            <ul className="mt-1 list-disc pl-5 text-sm text-zinc-700">
              <li>Stop all bots</li>
              <li>Cancel all open orders</li>
              <li>Close all positions</li>
              <li>Clear all grids</li>
            </ul>
            <p className="mt-3 text-xs font-semibold text-red-600">This action cannot be undone.</p>
            <div className="mt-5 flex justify-end gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => setResetConfirmOpen(false)}
                disabled={resetBusy}
                className="bg-white text-black border-gray-300 hover:bg-gray-100"
              >
                Cancel
              </Button>
              <Button
                size="sm"
                variant="destructive"
                disabled={resetCountdown > 0 || resetBusy}
                onClick={() => {
                  setResetConfirmOpen(false);
                  void resetEverything();
                }}
                className="bg-red-600 text-white hover:bg-red-700"
              >
                {resetBusy ? "Resetting…" : resetCountdown > 0 ? `Reset in ${resetCountdown}…` : "Reset everything"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
