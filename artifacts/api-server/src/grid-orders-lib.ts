import crypto from "crypto";
import { logger } from "./lib/logger";

/**
 * Общая библиотека grid-ордеров: подпись/повторные попытки Binance, округление
 * шагов, создание сетки, отмена, защитные STOP/TP и опрос филлов. Используется
 * и HTTP-роутами (routes/grid-orders.ts), и серверным движком (grid-engine.ts),
 * чтобы не дублировать логику подписи/округления/алго-ордеров.
 */

export const MAX_ORDER_SIZE_USD = 10_000;
export const MAX_LEVERAGE = 150;
export const MAX_LEVELS = 100;
export const MAX_RESIZE_ORDERS = 50;
export const SYMBOL_RE = /^[A-Z0-9]{2,20}USDT$/;

export type LevelSide = "BUY" | "SELL";

/** Ошибка с HTTP-статусом: роуты мапят её напрямую в ответ, движок — в лог. */
export class GridOrderError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "GridOrderError";
    this.status = status;
  }
}

export function getBinanceEnv() {
  const apiKey = process.env.BINANCE_API_KEY || "";
  const apiSecret = process.env.BINANCE_API_SECRET || "";
  const testnetRaw = process.env.BINANCE_TESTNET;
  const testnetConfigured = testnetRaw !== undefined && String(testnetRaw).trim() !== "";
  const testnet = String(testnetRaw).toLowerCase() === "true";
  const baseUrl = testnet
    ? "https://testnet.binancefuture.com"
    : "https://fapi.binance.com";
  return { apiKey, apiSecret, testnetConfigured, testnet, baseUrl };
}

function sign(params: Record<string, string | number>, apiSecret: string): string {
  const qs = Object.entries(params)
    .map(([k, v]) => `${k}=${v}`)
    .join("&");
  return crypto.createHmac("sha256", apiSecret).update(qs).digest("hex");
}

function signedQuery(params: Record<string, string | number>, apiSecret: string): string {
  const p = { ...params, timestamp: Date.now() };
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(p)) qs.set(k, String(v));
  qs.set("signature", sign(p, apiSecret));
  return qs.toString();
}

export async function binanceRequest(
  method: "GET" | "POST" | "PUT" | "DELETE",
  path: string,
  params: Record<string, string | number>,
  body?: Record<string, unknown>
): Promise<any> {
  const { apiKey, apiSecret, baseUrl } = getBinanceEnv();
  const qs = signedQuery(body ? ({ ...params, ...body } as any) : params, apiSecret);
  const url = method === "POST" ? `${baseUrl}${path}` : `${baseUrl}${path}?${qs}`;

  const MAX_ATTEMPTS = 3;
  const MAX_BAN_WAIT_MS = 3000;

  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const res = await fetch(url, {
      method,
      headers:
        method === "POST"
          ? {
              "Content-Type": "application/x-www-form-urlencoded",
              "X-MBX-APIKEY": apiKey,
            }
          : { "X-MBX-APIKEY": apiKey },
      body: method === "POST" ? qs : undefined,
    });
    if (res.ok) {
      return res.json();
    }
    const status = res.status;
    const text = await res.text();

    const isRateLimit =
      status === 418 ||
      status === 429 ||
      text.includes('"code":-1003') ||
      text.includes('"code":-429') ||
      text.includes("-1003") ||
      text.includes("-429");

    if (!isRateLimit || attempt === MAX_ATTEMPTS) {
      throw new Error(`Binance ${path} ${status}: ${text}`);
    }

    const banMatch = text.match(/banned until (\d+)/i);
    let delay: number | null = null;
    if (banMatch) {
      const wait = Number(banMatch[1]) - Date.now();
      if (Number.isFinite(wait) && wait > MAX_BAN_WAIT_MS) {
        throw new Error(
          `Binance ${path} ${status}: rate limit ban is too long (${wait}ms), aborting: ${text}`
        );
      }
      if (Number.isFinite(wait) && wait > 0) {
        delay = wait;
      }
    }
    if (delay === null) {
      delay = attempt === 1 ? 400 : 1200;
    }

    logger.warn({ path, status, attempt }, "[grid-orders] Binance rate limit, retrying");
    await new Promise((resolve) => setTimeout(resolve, delay));
  }

  throw new Error(`Binance ${path} request failed after ${MAX_ATTEMPTS} attempts`);
}

export const binanceGet = (path: string, params: Record<string, string | number> = {}) =>
  binanceRequest("GET", path, params);
export const binancePost = (path: string, body: Record<string, unknown>) =>
  binanceRequest("POST", path, {}, body);
export const binanceDelete = (path: string, params: Record<string, string | number>) =>
  binanceRequest("DELETE", path, params);
// PUT подписывается как GET/DELETE: параметры в query string, без тела.
export const binancePut = (path: string, params: Record<string, string | number>) =>
  binanceRequest("PUT", path, params);

export const ALGO_ORDER_PATH = "/fapi/v1/algoOrder";
export const OPEN_ALGO_ORDERS_PATH = "/fapi/v1/openAlgoOrders";
export const ALGO_OPEN_ORDERS_PATH = "/fapi/v1/algoOpenOrders";
export const algoPost = (body: Record<string, unknown>) => binancePost(ALGO_ORDER_PATH, body);
export const algoGet = (params: Record<string, string | number>) => binanceGet(ALGO_ORDER_PATH, params);
export const algoDelete = (params: Record<string, string | number>) => binanceDelete(ALGO_ORDER_PATH, params);

export function roundStep(value: number, step: number): number {
  const s = step >= 1 ? 0 : Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
  const r = Math.round(value / step) * step;
  const rounded = Number(r.toFixed(s));
  if (rounded > 0) return rounded;
  if (!Number.isFinite(step) || step <= 0) return rounded;
  return Number(step.toFixed(s));
}

/** Округляет количество вниз до шага stepSize (не превышая реальный размер позиции). */
export function floorStep(value: number, step: number): number {
  if (!Number.isFinite(step) || step <= 0) return value;
  const s = step >= 1 ? 0 : Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
  const r = Math.floor(value / step + 1e-9) * step;
  return Number(r.toFixed(s));
}

/** Number() с безопасным дефолтом 0 (отсутствующие/нечисловые значения). */
export function numOr0(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

/** Извлекает ненулевое количество из колонки bots.position (JSON-строка или число). */
export function positionQuantity(position: unknown): number {
  if (position === null || position === undefined) return 0;
  const raw = String(position).trim();
  if (raw === "" || raw === "null" || raw === "false" || raw === "undefined") return 0;
  let parsed: any;
  try {
    parsed = JSON.parse(raw);
  } catch {
    const num = Number(raw);
    return Number.isFinite(num) ? num : 0;
  }
  if (parsed === null || parsed === undefined || parsed === false) return 0;
  if (typeof parsed === "number") return Number.isFinite(parsed) ? parsed : 0;
  if (typeof parsed === "string") {
    const num = Number(parsed);
    return Number.isFinite(num) ? num : 0;
  }
  if (typeof parsed === "object") {
    for (const key of ["quantity", "qty", "amt"] as const) {
      const num = Number(parsed[key]);
      if (Number.isFinite(num) && num !== 0) return num;
    }
  }
  return 0;
}

export interface OrderPlanItem {
  level: number;
  side: LevelSide | null;
}

export interface CreateOrderResult {
  level: number;
  side?: LevelSide | null;
  orderId?: number;
  clientOrderId?: string;
  status?: string;
  error?: string;
  skipped?: boolean;
  reason?: string;
}

export interface StopRef {
  algoId: number;
  orderId: number;
  triggerPrice: number;
}

export interface CreateStops {
  long?: StopRef;
  short?: StopRef;
  errors: Array<{ side: string; error: string }>;
}

export interface ExchangeFilters {
  qStep: number;
  tTick: number;
  minNotional: number;
}

/** Шаги/минимум по символу; null — если символ не найден в exchangeInfo. */
export async function getExchangeFilters(symbol: string): Promise<ExchangeFilters | null> {
  const ei = await binanceGet("/fapi/v1/exchangeInfo", { symbol });
  const info = (ei as any).symbols?.find((s: any) => s.symbol === symbol);
  if (!info) return null;
  const lot = (info.filters || []).find((f: any) => f.filterType === "LOT_SIZE");
  const price = (info.filters || []).find((f: any) => f.filterType === "PRICE_FILTER");
  const qStep = lot ? parseFloat(lot.stepSize) : 1;
  const tTick = price ? parseFloat(price.tickSize) : 0.0001;
  const minNotional = Number(
    (info.filters || []).find((f: any) => f.filterType === "MIN_NOTIONAL")?.notional || 5
  );
  return { qStep, tTick, minNotional };
}

export interface CreateGridOrdersParams {
  symbol: string;
  plan: OrderPlanItem[];
  orderSizeUsd: number;
  leverage: number;
  lo?: number;
  hi?: number;
  slBufferPct?: number;
  placeStops?: boolean;
}

export interface CreateGridOrdersResult {
  symbol: string;
  orderSizeUsd: number;
  leverage: number;
  results: CreateOrderResult[];
  skipped: number;
  succeeded: number;
  failed: number;
  stops: CreateStops;
}

/**
 * Выставляет LIMIT-ордера по плану уровней (notional ≈ orderSizeUsd на уровень)
 * и защитные STOP_MARKET по границам lo/hi. Бросает GridOrderError при ошибке
 * leverage/symbol; ошибки отдельных уровней собираются в results.
 */
export async function createGridOrders(
  params: CreateGridOrdersParams
): Promise<CreateGridOrdersResult> {
  const { symbol: sym, plan } = params;
  const orderSizeUsd = Number(params.orderSizeUsd);
  const requestedLeverage = Number(params.leverage);

  let maxLeverage = 20;
  try {
    const bracket = await binanceGet("/fapi/v1/leverageBracket", { symbol: sym });
    const brackets = Array.isArray(bracket) ? bracket : [bracket];
    const symBracket = brackets.find((b: any) => b.symbol === sym);
    if (symBracket?.brackets?.length > 0) {
      maxLeverage = symBracket.brackets[0].initialLeverage;
    }
  } catch (e) {
    logger.warn({ err: e, symbol: sym }, "[grid-orders] leverageBracket failed, using fallback 20");
  }
  const leverage = Math.min(requestedLeverage, maxLeverage);
  const sides = Array.from(
    new Set(plan.map((p) => p.side).filter((s): s is LevelSide => s === "BUY" || s === "SELL"))
  );
  logger.info(
    { symbol: sym, sides, levels: plan.length, leverage },
    "[grid-orders] create attempt"
  );

  try {
    await binancePost("/fapi/v1/leverage", { symbol: sym, leverage });
  } catch (e) {
    logger.error({ err: e, symbol: sym }, "[grid-orders] leverage change failed");
    throw new GridOrderError(500, `leverage: ${(e as Error).message}`);
  }

  const filters = await getExchangeFilters(sym);
  if (!filters) {
    throw new GridOrderError(404, "symbol not found");
  }
  const { qStep, tTick, minNotional } = filters;

  let effectiveOrderSizeUsd = orderSizeUsd;
  if (orderSizeUsd < minNotional) {
    logger.warn(
      { symbol: sym, requested: orderSizeUsd, minNotional },
      "[grid-orders] orderSizeUsd raised to MIN_NOTIONAL"
    );
    effectiveOrderSizeUsd = Math.max(orderSizeUsd, minNotional);
  }

  const results: CreateOrderResult[] = [];

  let refPrice = NaN;
  try {
    const ticker = await binanceGet("/fapi/v1/ticker/price", { symbol: sym });
    refPrice = Number(ticker?.price);
  } catch (e) {
    logger.warn(
      { err: e, symbol: sym },
      "[grid-orders] ticker/price failed, no reference price for level filtering"
    );
  }

  for (const item of plan) {
    const lvNum = item.level;
    const side = item.side;
    if (!Number.isFinite(lvNum) || lvNum <= 0) {
      results.push({ level: lvNum, side, error: "invalid level" });
      continue;
    }
    if (side === null) {
      results.push({ level: lvNum, side: null, skipped: true, reason: "mid level" });
      continue;
    }
    const qty = Math.max(qStep, roundStep(effectiveOrderSizeUsd / lvNum, qStep));
    if (qty * lvNum < minNotional) {
      results.push({ level: lvNum, side, error: `notional below MIN_NOTIONAL (${minNotional})` });
      continue;
    }
    const priceRounded = roundStep(lvNum, tTick);
    if (priceRounded <= 0 || qty <= 0) {
      results.push({ level: lvNum, side, error: "computed price or quantity is zero" });
      continue;
    }
    if (side === "BUY" && Number.isFinite(refPrice) && priceRounded >= refPrice) {
      results.push({ level: lvNum, side, skipped: true });
      continue;
    }
    if (side === "SELL" && Number.isFinite(refPrice) && priceRounded <= refPrice) {
      results.push({ level: lvNum, side, skipped: true });
      continue;
    }
    const base = sym.slice(0, 10);
    let cid = `grid_${base}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
    cid = cid.slice(0, 36);
    try {
      const r = await binancePost("/fapi/v1/order", {
        symbol: sym,
        side,
        type: "LIMIT",
        timeInForce: "GTC",
        quantity: qty,
        price: priceRounded,
        newClientOrderId: cid,
      });
      results.push({
        level: lvNum,
        side,
        orderId: r?.orderId,
        clientOrderId: cid,
        status: r?.status,
      });
    } catch (e) {
      results.push({ level: lvNum, side, error: (e as Error).message });
    }
  }

  // Защитные STOP-ордера на бирже: близко к логике /stop, но по границам lo/hi.
  const stops: CreateStops = { errors: [] };

  const loInput = Number(params.lo);
  const hiInput = Number(params.hi);
  const slBufferRaw = Number(params.slBufferPct ?? 2);
  const slBufferPct = Number.isFinite(slBufferRaw) && slBufferRaw >= 0 ? slBufferRaw : 2;
  const placeStops = params.placeStops !== false;
  const hasBounds =
    Number.isFinite(loInput) && loInput > 0 && Number.isFinite(hiInput) && hiInput > 0;

  if (placeStops && hasBounds) {
    const placedSides = new Set<LevelSide>();
    for (const r of results) {
      if (r.error || r.skipped) continue;
      if (Number.isFinite(Number(r.orderId)) && (r.side === "BUY" || r.side === "SELL")) {
        placedSides.add(r.side);
      }
    }

    // key: "long" — защита лонга (SELL stop), "short" — защита шорта (BUY stop).
    const placeStop = async (key: "long" | "short", triggerPriceRaw: number) => {
      try {
        const triggerPrice = roundStep(triggerPriceRaw, tTick);
        if (!(triggerPrice > 0)) throw new Error("computed stop price is zero");
        const r = await algoPost({
          algoType: "CONDITIONAL",
          symbol: sym,
          side: key === "long" ? "SELL" : "BUY",
          type: "STOP_MARKET",
          triggerPrice,
          closePosition: "true",
          workingType: "MARK_PRICE",
          clientAlgoId: `gridsl_${sym.slice(0, 10)}_${key}`,
        });
        const algoId = Number(r?.algoId);
        if (!Number.isFinite(algoId)) throw new Error("no algoId in response");
        stops[key] = { algoId, orderId: algoId, triggerPrice };
      } catch (e) {
        stops.errors.push({ side: key, error: (e as Error).message });
      }
    };

    if (placedSides.has("BUY")) {
      await placeStop("long", loInput * (1 - slBufferPct / 100));
    }
    if (placedSides.has("SELL")) {
      await placeStop("short", hiInput * (1 + slBufferPct / 100));
    }
  }

  const skipped = results.filter((r) => r.skipped).length;
  const succeeded = results.filter((r) => !r.error && !r.skipped).length;
  const failed = results.length - succeeded - skipped;
  logger.info(
    {
      symbol: sym,
      sides,
      levels: plan.length,
      leverage,
      succeeded,
      failed,
      skipped,
      failures: results
        .filter((r) => r.error)
        .map((r) => ({ level: r.level, side: r.side, error: r.error })),
      stopLong: stops.long
        ? { algoId: stops.long.algoId, orderId: stops.long.orderId, triggerPrice: stops.long.triggerPrice }
        : null,
      stopShort: stops.short
        ? { algoId: stops.short.algoId, orderId: stops.short.orderId, triggerPrice: stops.short.triggerPrice }
        : null,
      stopErrors: stops.errors,
    },
    "[grid-orders] create complete"
  );

  return {
    symbol: sym,
    orderSizeUsd: effectiveOrderSizeUsd,
    leverage,
    results,
    skipped,
    succeeded,
    failed,
    stops,
  };
}

/** Отменяет открытые ордера по списку id; ошибки собираются по каждому id. */
export async function cancelOrderIds(
  symbol: string,
  orderIds: number[]
): Promise<Array<{ orderId: number; ok: boolean; error?: string }>> {
  const results: Array<{ orderId: number; ok: boolean; error?: string }> = [];
  for (const id of orderIds) {
    try {
      await binanceDelete("/fapi/v1/order", { symbol, orderId: id });
      results.push({ orderId: id, ok: true });
    } catch (e) {
      results.push({ orderId: id, ok: false, error: (e as Error).message });
    }
  }
  return results;
}

/** Отменяет все открытые ордера по символу, возвращает ответ Binance. */
export async function cancelAllSymbolOrders(symbol: string): Promise<any> {
  return binanceDelete("/fapi/v1/allOpenOrders", { symbol });
}

/**
 * Отменяет защитные STOP-ордера по algoId (DELETE /fapi/v1/algoOrder) с
 * фолбэком на legacy DELETE /fapi/v1/order. Не прерывает остальные отмены.
 */
export async function cancelAlgoOrderIds(
  symbol: string,
  orderIds: number[]
): Promise<{ canceled: number[]; errors: Array<{ orderId: number; error: string }> }> {
  const canceled: number[] = [];
  const errors: Array<{ orderId: number; error: string }> = [];
  for (const raw of orderIds) {
    const id = Number(raw);
    if (!Number.isInteger(id) || id <= 0) {
      errors.push({ orderId: id, error: "orderId must be a positive integer" });
      continue;
    }
    try {
      try {
        await algoDelete({ algoId: id });
      } catch (algoErr) {
        try {
          await binanceDelete("/fapi/v1/order", { symbol, orderId: id });
        } catch (legacyErr) {
          throw new Error(
            `${(algoErr as Error).message}; legacy orderId fallback: ${(legacyErr as Error).message}`
          );
        }
      }
      canceled.push(id);
    } catch (e) {
      errors.push({ orderId: id, error: (e as Error).message });
    }
  }
  return { canceled, errors };
}

export interface PlaceStopParams {
  symbol: string;
  direction: "long" | "short";
  triggerPrice?: number;
  lo?: number;
  hi?: number;
  slBufferPct?: number;
  algoId?: number | null;
}

export interface PlaceStopResult {
  algoId: number;
  triggerPrice: number;
  replaced: boolean;
}

/**
 * Ставит/заменяет защитный STOP_MARKET. Если передан algoId — снимает старый
 * (best effort) и выставляет новый. Бросает GridOrderError с 400/404.
 */
export async function placeGridStop(params: PlaceStopParams): Promise<PlaceStopResult> {
  const sym = params.symbol;
  const direction = params.direction;

  const loInput = Number(params.lo);
  const hiInput = Number(params.hi);
  const triggerInput = Number(params.triggerPrice);
  const hasTrigger = Number.isFinite(triggerInput) && triggerInput > 0;
  const hasLo = Number.isFinite(loInput) && loInput > 0;
  const hasHi = Number.isFinite(hiInput) && hiInput > 0;
  if (!hasTrigger && direction === "long" && !hasLo) {
    throw new GridOrderError(400, "triggerPrice or lo required");
  }
  if (!hasTrigger && direction === "short" && !hasHi) {
    throw new GridOrderError(400, "triggerPrice or hi required");
  }

  const slBufferRaw = Number(params.slBufferPct ?? 2);
  const slBufferPct = Number.isFinite(slBufferRaw) && slBufferRaw >= 0 ? slBufferRaw : 2;

  const rawAlgoId = Number(params.algoId);
  const prevAlgoId = Number.isInteger(rawAlgoId) && rawAlgoId > 0 ? rawAlgoId : null;

  const filters = await getExchangeFilters(sym);
  if (!filters) {
    throw new GridOrderError(404, "symbol not found");
  }
  const { tTick } = filters;

  const triggerPriceRaw = hasTrigger
    ? triggerInput
    : direction === "long"
      ? loInput * (1 - slBufferPct / 100)
      : hiInput * (1 + slBufferPct / 100);
  const triggerPrice = roundStep(triggerPriceRaw, tTick);
  if (!(triggerPrice > 0)) {
    throw new GridOrderError(400, "computed stop price is zero");
  }

  logger.info(
    { symbol: sym, direction, triggerPrice, replaced: prevAlgoId !== null, prevAlgoId },
    "[grid-orders] stop attempt"
  );

  if (prevAlgoId !== null) {
    try {
      await algoDelete({ algoId: prevAlgoId });
    } catch (e) {
      logger.warn(
        { symbol: sym, direction, algoId: prevAlgoId, err: (e as Error).message },
        "[grid-orders] stop old algo order cancel failed, placing new one"
      );
    }
  }

  const r = await algoPost({
    algoType: "CONDITIONAL",
    symbol: sym,
    side: direction === "long" ? "SELL" : "BUY",
    type: "STOP_MARKET",
    triggerPrice,
    closePosition: "true",
    workingType: "MARK_PRICE",
    clientAlgoId: `gridsl_${sym.slice(0, 10)}_${direction}`,
  });
  const algoId = Number(r?.algoId);
  if (!Number.isFinite(algoId)) {
    logger.error({ symbol: sym, direction }, "[grid-orders] stop failed: no algoId in response");
    throw new GridOrderError(500, "no algoId in response");
  }

  const replaced = prevAlgoId !== null;
  logger.info(
    { symbol: sym, direction, algoId, triggerPrice, replaced },
    "[grid-orders] stop complete"
  );
  return { algoId, triggerPrice, replaced };
}

export interface SyncTpParams {
  symbol: string;
  direction: "long" | "short";
  tpPrice?: number;
  tpOrderId?: number | null;
}

export interface SyncTpResult {
  tpOrderId: number | null;
  tpPrice: number | null;
  canceled: boolean;
}

/**
 * Держит защитный TP-ордер (algo TAKE_PROFIT_MARKET + closePosition). При
 * отсутствии/невалидности tpPrice снимает старый; иначе cancel+place (algoId
 * меняется, amend не поддерживается). Бросает GridOrderError с 400/404.
 */
export async function syncGridTp(params: SyncTpParams): Promise<SyncTpResult> {
  const sym = params.symbol;
  const direction = params.direction;

  const rawTpOrderId = Number(params.tpOrderId);
  const tpOrderId = Number.isFinite(rawTpOrderId) && rawTpOrderId > 0 ? rawTpOrderId : null;
  const tpPriceRaw = Number(params.tpPrice);
  const hasTpPrice = Number.isFinite(tpPriceRaw) && tpPriceRaw > 0;

  if (!hasTpPrice) {
    if (tpOrderId !== null) {
      try {
        await algoDelete({ algoId: tpOrderId });
      } catch (e) {
        logger.warn(
          { symbol: sym, direction, algoId: tpOrderId, err: (e as Error).message },
          "[grid-orders] tp cancel failed"
        );
      }
      logger.info(
        { symbol: sym, direction, tpPrice: null, algoId: tpOrderId, amended: false, replaced: true },
        "[grid-orders] tp sync"
      );
      return { tpOrderId: null, tpPrice: null, canceled: true };
    }
    logger.info(
      { symbol: sym, direction, tpPrice: null, algoId: null, amended: false },
      "[grid-orders] tp sync"
    );
    return { tpOrderId: null, tpPrice: null, canceled: false };
  }

  const filters = await getExchangeFilters(sym);
  if (!filters) {
    throw new GridOrderError(404, "symbol not found");
  }
  const priceRounded = roundStep(tpPriceRaw, filters.tTick);
  if (!(priceRounded > 0)) {
    throw new GridOrderError(400, "computed tp price is zero");
  }

  const side: "BUY" | "SELL" = direction === "long" ? "SELL" : "BUY";

  if (tpOrderId !== null) {
    try {
      await algoDelete({ algoId: tpOrderId });
    } catch (e) {
      logger.warn(
        { symbol: sym, direction, algoId: tpOrderId, err: (e as Error).message },
        "[grid-orders] tp old algo order cancel failed, placing new one"
      );
    }
  }

  const r = await algoPost({
    algoType: "CONDITIONAL",
    symbol: sym,
    side,
    type: "TAKE_PROFIT_MARKET",
    triggerPrice: priceRounded,
    closePosition: "true",
    workingType: "MARK_PRICE",
    clientAlgoId: `gridtp_${sym.slice(0, 10)}_${direction}`,
  });
  const newAlgoId = Number(r?.algoId);
  const outAlgoId = Number.isFinite(newAlgoId) ? newAlgoId : null;
  logger.info(
    { symbol: sym, direction, tpPrice: priceRounded, algoId: outAlgoId, amended: false, replaced: true },
    "[grid-orders] tp sync"
  );
  return { tpOrderId: outAlgoId, tpPrice: priceRounded, canceled: false };
}

export interface PositionSnapshot {
  positionAmt: number;
  entryPrice: number;
}

/** Читает текущую позицию по символу; null — если запрос не удался или записи нет. */
export async function fetchPosition(sym: string): Promise<PositionSnapshot | null> {
  try {
    const raw = await binanceGet("/fapi/v2/positionRisk", { symbol: sym });
    const list: any[] = Array.isArray(raw) ? raw : raw ? [raw] : [];
    const entry = list.find((p: any) => String(p?.symbol ?? "").toUpperCase() === sym);
    if (!entry) return null;
    return {
      positionAmt: numOr0(entry.positionAmt),
      entryPrice: numOr0(entry.entryPrice),
    };
  } catch (e) {
    logger.warn(
      { symbol: sym, err: (e as Error).message },
      "[grid-orders] positionRisk failed"
    );
    return null;
  }
}

export interface FillData {
  executedQty: number;
  avgPrice: number;
  cumQuote: number;
}

/** Извлекает executedQty/avgPrice/cumQuote из ответа Binance-ордера. */
export function normalizeFill(raw: any): FillData {
  return {
    executedQty: numOr0(raw?.executedQty),
    avgPrice: numOr0(raw?.avgPrice),
    cumQuote: numOr0(raw?.cumQuote),
  };
}

/** true, когда данных достаточно для расчёта PnL. */
function fillIsUsable(fill: FillData): boolean {
  return fill.executedQty > 0 && (fill.avgPrice > 0 || fill.cumQuote > 0);
}

/** Довосстанавливает avgPrice/cumQuote друг из друга, если одно из полей нулевое. */
function withDerivedFill(fill: FillData): FillData {
  let { executedQty, avgPrice, cumQuote } = fill;
  if (executedQty > 0) {
    if (avgPrice <= 0 && cumQuote > 0) avgPrice = cumQuote / executedQty;
    if (cumQuote <= 0 && avgPrice > 0) cumQuote = avgPrice * executedQty;
  }
  return { executedQty, avgPrice, cumQuote };
}

const FILL_QUERY_ATTEMPTS = 5;
const FILL_QUERY_DELAYS_MS = [300, 500, 800, 1000];

/**
 * Разрешает реальные данные исполнения MARKET-ордера, когда ответ содержит нули:
 * повторный опрос GET /fapi/v1/order, затем агрегация GET /fapi/v1/userTrades.
 * Любая ошибка не пробрасывается — возвращаются лучшие известные значения.
 */
export async function resolveFillData(
  sym: string,
  orderId: unknown,
  initial: FillData
): Promise<FillData> {
  if (fillIsUsable(initial)) return withDerivedFill(initial);

  const idNum = Number(orderId);
  const hasOrderId = Number.isFinite(idNum) && idNum > 0;

  for (let attempt = 0; attempt < FILL_QUERY_ATTEMPTS && hasOrderId; attempt++) {
    const delayMs = FILL_QUERY_DELAYS_MS[attempt] ?? 1000;
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    try {
      const raw = await binanceGet("/fapi/v1/order", { symbol: sym, orderId: idNum });
      const fill = normalizeFill(raw);
      if (fillIsUsable(fill)) return withDerivedFill(fill);
    } catch (e) {
      logger.warn(
        { symbol: sym, orderId: idNum, attempt: attempt + 1, err: (e as Error).message },
        "[grid-orders] order fill query failed"
      );
    }
  }

  logger.warn(
    { symbol: sym, orderId: idNum },
    "[grid-orders] order fill unavailable, falling back to userTrades"
  );

  const fetchTrades = async (params: Record<string, string | number>): Promise<any[]> => {
    const raw = await binanceGet("/fapi/v1/userTrades", params);
    return Array.isArray(raw) ? raw : [];
  };

  let trades: any[] = [];
  try {
    trades = hasOrderId
      ? await fetchTrades({ symbol: sym, orderId: idNum, limit: 1000 })
      : await fetchTrades({ symbol: sym, startTime: Date.now() - 60_000, limit: 1000 });
  } catch (e) {
    logger.warn(
      { symbol: sym, orderId: idNum, err: (e as Error).message },
      "[grid-orders] userTrades by orderId failed"
    );
    if (!hasOrderId) return withDerivedFill(initial);
    try {
      trades = await fetchTrades({ symbol: sym, startTime: Date.now() - 60_000, limit: 1000 });
    } catch (e2) {
      logger.warn(
        { symbol: sym, err: (e2 as Error).message },
        "[grid-orders] userTrades by startTime failed"
      );
      return withDerivedFill(initial);
    }
  }

  const executedQty = trades.reduce((sum, t) => sum + numOr0(t?.qty), 0);
  const cumQuote = trades.reduce((sum, t) => sum + numOr0(t?.quoteQty), 0);
  const avgPrice = executedQty > 0 ? cumQuote / executedQty : 0;
  const resolved = withDerivedFill({ executedQty, avgPrice, cumQuote });
  return resolved.executedQty > 0 ? resolved : withDerivedFill(initial);
}

export interface TradeAggregate {
  qty: number;
  cumQuote: number;
  avgPrice: number;
  price: number;
  side: string;
}

/** Агрегирует userTrades по orderId: qty/quoteQty суммируются, avgPrice = cumQuote/qty. */
export function aggregateTradesByOrderId(trades: any[]): Map<number, TradeAggregate> {
  const byOrderId = new Map<number, TradeAggregate>();
  for (const t of trades) {
    const orderId = Number(t?.orderId);
    if (!Number.isFinite(orderId)) continue;
    const qty = numOr0(t?.qty);
    const quoteQty = numOr0(t?.quoteQty);
    let agg = byOrderId.get(orderId);
    if (!agg) {
      agg = { qty: 0, cumQuote: 0, avgPrice: 0, price: 0, side: "" };
      byOrderId.set(orderId, agg);
    }
    agg.qty += qty;
    agg.cumQuote += quoteQty;
    agg.avgPrice = agg.qty > 0 ? agg.cumQuote / agg.qty : 0;
    agg.price = numOr0(t?.price);
    agg.side = String(t?.side ?? "");
  }
  return byOrderId;
}

export type FillOrderResult =
  | {
      orderId: number;
      clientOrderId?: string;
      status: string;
      side: string;
      price: number;
      executedQty: number;
      avgPrice: number;
      cumQuote: number;
    }
  | { orderId: number; error: string };

export interface GridFillsResult {
  positionAmt: number | null;
  entryPrice: number | null;
  results: FillOrderResult[];
}

/**
 * Статусы/исполнение указанных ордеров + текущая позиция. При sinceMs —
 * батч openOrders + userTrades; иначе — запрос по каждому ордеру.
 */
export async function fetchGridFills(
  symbol: string,
  orderIds: number[],
  sinceMs?: number | null
): Promise<GridFillsResult> {
  const sym = symbol;
  const hasSinceMs = Number.isFinite(Number(sinceMs)) && Number(sinceMs) > 0;

  const position = await fetchPosition(sym);

  const results: FillOrderResult[] = [];

  let batched = false;
  if (hasSinceMs) {
    try {
      const openRaw = await binanceGet("/fapi/v1/openOrders", { symbol: sym });
      const openOrders: any[] = Array.isArray(openRaw) ? openRaw : openRaw ? [openRaw] : [];
      const openByOrderId = new Map<number, any>();
      for (const o of openOrders) {
        const id = Number(o?.orderId);
        if (Number.isFinite(id)) openByOrderId.set(id, o);
      }

      const tradesRaw = await binanceGet("/fapi/v1/userTrades", {
        symbol: sym,
        startTime: Number(sinceMs),
        limit: 1000,
      });
      const trades: any[] = Array.isArray(tradesRaw) ? tradesRaw : [];
      const tradesByOrderId = aggregateTradesByOrderId(trades);

      for (const orderId of orderIds) {
        const open = openByOrderId.get(orderId);
        const agg = tradesByOrderId.get(orderId);
        if (open) {
          results.push({
            orderId,
            clientOrderId: open?.clientOrderId ? String(open.clientOrderId) : undefined,
            status: String(open?.status ?? ""),
            side: String(open?.side ?? ""),
            price: numOr0(open?.price),
            executedQty: agg ? agg.qty : numOr0(open?.executedQty),
            avgPrice: agg ? agg.avgPrice : numOr0(open?.avgPrice),
            cumQuote: agg ? agg.cumQuote : numOr0(open?.cumQuote),
          });
        } else if (agg) {
          results.push({
            orderId,
            status: "FILLED",
            side: agg.side,
            price: agg.price,
            executedQty: agg.qty,
            avgPrice: agg.avgPrice,
            cumQuote: agg.cumQuote,
          });
        } else {
          results.push({
            orderId,
            status: "CANCELED",
            side: "",
            price: 0,
            executedQty: 0,
            avgPrice: 0,
            cumQuote: 0,
          });
        }
      }
      batched = true;
    } catch (e) {
      logger.warn(
        { symbol: sym, err: (e as Error).message },
        "[grid-orders] batched fills failed, falling back to per-order queries"
      );
    }
  }

  if (!batched) {
    for (const orderId of orderIds) {
      try {
        const r = await binanceGet("/fapi/v1/order", { symbol: sym, orderId });
        results.push({
          orderId,
          clientOrderId: r?.clientOrderId ? String(r.clientOrderId) : undefined,
          status: String(r?.status ?? ""),
          side: String(r?.side ?? ""),
          price: numOr0(r?.price),
          executedQty: numOr0(r?.executedQty),
          avgPrice: numOr0(r?.avgPrice),
          cumQuote: numOr0(r?.cumQuote),
        });
      } catch (e) {
        results.push({ orderId, error: (e as Error).message });
      }
    }
  }

  logger.info(
    { symbol: sym, orderCount: orderIds.length },
    "[grid-orders] fills fetched"
  );

  return {
    positionAmt: position ? position.positionAmt : null,
    entryPrice: position ? position.entryPrice : null,
    results,
  };
}

export interface OpenAlgoOrder {
  algoId: number;
  clientAlgoId: string;
  type: string;
  side: string;
  triggerPrice: number;
  closePosition: boolean;
  workingType: string;
  algoStatus: string;
}

/** Нормализованный список открытых algo-ордеров символа. */
export async function fetchOpenAlgoOrders(symbol: string): Promise<OpenAlgoOrder[]> {
  const raw = await binanceGet(OPEN_ALGO_ORDERS_PATH, { symbol });
  const list: any[] = Array.isArray(raw) ? raw : raw ? [raw] : [];
  const orders = list.map((o: any) => ({
    algoId: numOr0(o?.algoId),
    clientAlgoId: String(o?.clientAlgoId ?? ""),
    type: String(o?.type ?? ""),
    side: String(o?.side ?? ""),
    triggerPrice: numOr0(o?.triggerPrice),
    closePosition:
      o?.closePosition === true || String(o?.closePosition ?? "").toLowerCase() === "true",
    workingType: String(o?.workingType ?? ""),
    algoStatus: String(o?.algoStatus ?? ""),
  }));

  logger.info({ symbol, count: orders.length }, "[grid-orders] open algo orders fetched");

  return orders;
}

export type AlgoStatusResult =
  | {
      algoId: number;
      algoStatus: string;
      triggerPrice: number;
      actualPrice: number;
      actualQty: number;
      actualOrderId: number;
    }
  | { algoId: number; error: string };

/** Статус algo-ордеров по их id (для реконсиляции сработавшей защиты). */
export async function fetchAlgoStatus(
  symbol: string,
  algoIds: number[]
): Promise<AlgoStatusResult[]> {
  const results: AlgoStatusResult[] = [];
  for (const algoId of algoIds) {
    try {
      const r = await algoGet({ algoId });
      results.push({
        algoId,
        algoStatus: String(r?.algoStatus ?? ""),
        triggerPrice: numOr0(r?.triggerPrice),
        actualPrice: numOr0(r?.actualPrice),
        actualQty: numOr0(r?.actualQty),
        actualOrderId: numOr0(r?.actualOrderId),
      });
    } catch (e) {
      results.push({ algoId, error: (e as Error).message });
    }
  }

  logger.info({ symbol, count: algoIds.length }, "[grid-orders] algo status fetched");

  return results;
}

export interface CloseGridPositionParams {
  symbol: string;
  direction?: "long" | "short" | null;
  quantity?: number;
  sinceMs?: number;
}

export interface CloseGridPositionResult {
  symbol: string;
  side: "BUY" | "SELL";
  direction: "long" | "short" | null;
  orderId: number | null;
  avgPrice: number;
  executedQty: number;
  cumQuote: number;
  fundingUsd: number;
}

/**
 * Общий close-хелпер (движок + route /close): закрывает позицию MARKET-ордером
 * reduceOnly, best-effort считает funding и разрешает фактическую цену исполнения
 * через resolveFillData (order -> userTrades). direction=long закрывает лонг
 * (SELL), direction=short — шорт (BUY); без direction закрывает всю позицию.
 * Бросает GridOrderError (400/404) на известные отказы, чтобы вызывающий
 * замапил статус/обработал «nothing toclose».
 */
export async function closeGridPosition(
  params: CloseGridPositionParams
): Promise<CloseGridPositionResult> {
  const sym = String(params.symbol ?? "").trim().toUpperCase();
  const direction =
    params.direction === "long" || params.direction === "short" ? params.direction : null;
  const explicitQty = Number(params.quantity);
  const hasExplicitQty = Number.isFinite(explicitQty) && explicitQty > 0;
  const sinceMs = Number(params.sinceMs);
  const hasSinceMs = Number.isFinite(sinceMs) && sinceMs > 0;

  const position = await fetchPosition(sym);

  let qty: number;
  let side: "BUY" | "SELL";
  if (direction) {
    side = direction === "long" ? "SELL" : "BUY";
    if (hasExplicitQty) {
      qty = explicitQty;
    } else {
      if (!position) throw new GridOrderError(400, "position unavailable");
      qty = Math.abs(position.positionAmt);
    }
  } else if (hasExplicitQty) {
    qty = explicitQty;
    side = position && position.positionAmt < 0 ? "BUY" : "SELL";
  } else {
    if (!position) throw new GridOrderError(400, "position unavailable");
    qty = Math.abs(position.positionAmt);
    side = position.positionAmt < 0 ? "BUY" : "SELL";
  }
  if (!(qty > 0)) throw new GridOrderError(400, "nothing toclose: position is 0");

  const ei = await binanceGet("/fapi/v1/exchangeInfo", { symbol: sym });
  const info = (ei as any).symbols?.find((s: any) => s.symbol === sym);
  if (!info) throw new GridOrderError(404, "symbol not found");
  const lot = (info.filters || []).find((f: any) => f.filterType === "LOT_SIZE");
  const qStep = lot ? parseFloat(lot.stepSize) : 1;
  const qtyRounded = floorStep(qty, qStep);
  if (!(qtyRounded > 0)) throw new GridOrderError(400, "nothing toclose: quantity is 0");

  logger.info(
    { symbol: sym, side, direction, quantity: qtyRounded },
    "[grid-orders] close attempt"
  );

  const r = await binancePost("/fapi/v1/order", {
    symbol: sym,
    side,
    type: "MARKET",
    quantity: qtyRounded,
    reduceOnly: "true",
  });

  let fundingUsd = 0;
  if (hasSinceMs) {
    try {
      const income = await binanceGet("/fapi/v1/income", {
        symbol: sym,
        incomeType: "FUNDING_FEE",
        startTime: sinceMs,
        limit: 1000,
      });
      const rows: any[] = Array.isArray(income) ? income : [];
      fundingUsd = rows.reduce((sum: number, x: any) => sum + numOr0(x?.income), 0);
    } catch (e) {
      logger.warn(
        { symbol: sym, err: (e as Error).message },
        "[grid-orders] funding income fetch failed"
      );
      fundingUsd = 0;
    }
  }

  const rawOrderId = Number(r?.orderId);
  const orderId = Number.isFinite(rawOrderId) && rawOrderId > 0 ? rawOrderId : null;
  const fill = await resolveFillData(sym, orderId, normalizeFill(r));
  const avgPrice = fill.avgPrice;
  const executedQty = fill.executedQty;
  const cumQuote = fill.cumQuote;

  logger.info(
    { symbol: sym, side, direction, quantity: qtyRounded, avgPrice, executedQty, cumQuote, fundingUsd },
    "[grid-orders] close complete"
  );

  return { symbol: sym, side, direction, orderId, avgPrice, executedQty, cumQuote, fundingUsd };
}
