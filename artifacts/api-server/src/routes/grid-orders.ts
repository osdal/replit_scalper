import { Router } from "express";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { notifyTokenGuard } from "../middlewares/notifyAuth";
import { logger } from "../lib/logger";
import { db, botsTable, tradingControlTable, gridHistoryTable } from "@workspace/db";
import { eq, inArray } from "drizzle-orm";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const RESET_MARKER = path.resolve(__dirname, "../../../data/grid_reset.json");

const router = Router();

/** Читает время последнего серверного сброса; 0 при любой ошибке. */
function readResetMarker(): number {
  try {
    const raw = fs.readFileSync(RESET_MARKER, "utf8");
    const at = Number(JSON.parse(raw)?.resetAt);
    return Number.isFinite(at) ? at : 0;
  } catch {
    return 0;
  }
}

/** Записывает время серверного сброса; ошибки проглатываются. */
function writeResetMarker(at: number): void {
  try {
    fs.mkdirSync(path.dirname(RESET_MARKER), { recursive: true });
    fs.writeFileSync(RESET_MARKER, JSON.stringify({ resetAt: at }));
  } catch {
    // ignore fs errors
  }
}

const MAX_ORDER_SIZE_USD = 10_000;
const MAX_LEVERAGE = 150;
const MAX_LEVELS = 100;
const SYMBOL_RE = /^[A-Z0-9]{2,20}USDT$/;

function getEnv() {
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

async function binanceRequest(
  method: "GET" | "POST" | "PUT" | "DELETE",
  path: string,
  params: Record<string, string | number>,
  body?: Record<string, unknown>
): Promise<any> {
  const { apiKey, apiSecret, baseUrl } = getEnv();
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

const binanceGet = (path: string, params: Record<string, string | number> = {}) =>
  binanceRequest("GET", path, params);
const binancePost = (path: string, body: Record<string, unknown>) =>
  binanceRequest("POST", path, {}, body);
const binanceDelete = (path: string, params: Record<string, string | number>) =>
  binanceRequest("DELETE", path, params);
// PUT подписывается как GET/DELETE: параметры в query string, без тела.
const binancePut = (path: string, params: Record<string, string | number>) =>
  binanceRequest("PUT", path, params);

const ALGO_ORDER_PATH = "/fapi/v1/algoOrder";
const OPEN_ALGO_ORDERS_PATH = "/fapi/v1/openAlgoOrders";
const ALGO_OPEN_ORDERS_PATH = "/fapi/v1/algoOpenOrders";
const algoPost = (body: Record<string, unknown>) => binancePost(ALGO_ORDER_PATH, body);
const algoGet = (params: Record<string, string | number>) => binanceGet(ALGO_ORDER_PATH, params);
const algoDelete = (params: Record<string, string | number>) => binanceDelete(ALGO_ORDER_PATH, params);

function roundStep(value: number, step: number): number {
  const s = step >= 1 ? 0 : Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
  const r = Math.round(value / step) * step;
  const rounded = Number(r.toFixed(s));
  if (rounded > 0) return rounded;
  if (!Number.isFinite(step) || step <= 0) return rounded;
  return Number(step.toFixed(s));
}

/** Извлекает ненулевое количество из колонки bots.position (JSON-строка или число). */
function positionQuantity(position: unknown): number {
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

/**
 * Изоляция grid-ордеров от скальпер-бота: не даём выставлять сетку по символу,
 * которым уже управляет бот. Возвращает текст ошибки или null, если символ свободен.
 */
async function symbolIsolationError(sym: string): Promise<string | null> {
  // 1. Необязательный allowlist символов для grid-ордеров.
  const allowRaw = process.env.GRID_SYMBOLS;
  if (allowRaw !== undefined && allowRaw.trim() !== "") {
    const allowed = allowRaw
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    if (!allowed.includes(sym)) {
      return `symbol ${sym} is not in GRID_SYMBOLS`;
    }
  }

  // 2. Символ занят запущенным ботом или ботом с открытой позицией.
  try {
    const [bot] = await db.select().from(botsTable).where(eq(botsTable.symbol, sym));
    if (bot && (bot.is_running || positionQuantity(bot.position) !== 0)) {
      return `symbol ${sym} is in use by scalper bot`;
    }
  } catch (e) {
    logger.warn(
      { symbol: sym, err: (e as Error).message },
      "[grid-orders] bot-in-use check failed, allowing symbol"
    );
  }

  // 3. Глобальная пауза риска (trading_control.paused_remaining).
  try {
    const [control] = await db
      .select()
      .from(tradingControlTable)
      .where(eq(tradingControlTable.id, 1));
    if (control && Number(control.paused_remaining) > 0) {
      return "trading is paused for new positions";
    }
  } catch (e) {
    logger.warn(
      { symbol: sym, err: (e as Error).message },
      "[grid-orders] trading-control check failed, allowing symbol"
    );
  }

  return null;
}

/** Все роуты требуют токен и разрешённый Origin (как /notify и /grid-history). */
router.use(notifyTokenGuard);

function requireTestnet(_req: any, res: any, next: any) {
  const { testnet } = getEnv();
  if (testnet !== true) {
    return res.status(503).json({
      error: "grid order routes require BINANCE_TESTNET=true",
    });
  }
  return next();
}

function requireGridOrdersEnabled(_req: any, res: any, next: any) {
  if (process.env.GRID_ORDERS_ENABLED?.trim().toLowerCase() !== "true") {
    return res.status(503).json({
      error: "grid orders are disabled (set GRID_ORDERS_ENABLED=true to enable)",
    });
  }
  return next();
}

/**
 * POST /api/grid-orders/create
 * body (preferred): { symbol, orders: Array<{ price, side: "BUY"|"SELL" }>, orderSizeUsd, leverage }
 * body (derived):    { symbol, levels: number[], midPrice, orderSizeUsd, leverage }
 * body (legacy):     { symbol, side: "BUY"|"SELL", levels: number[], orderSizeUsd, leverage }
 * Выставляет LIMIT-ордера по уровням сетки (notional ≈ orderSizeUsd на уровень).
 * В derived-режиме сторона уровня выводится из midPrice; уровень, равный midPrice,
 * пропускается. Каждый уровень может иметь собственную сторону (двусторонняя сетка).
 */
router.post("/create", requireTestnet, requireGridOrdersEnabled, async (req, res) => {
  try {
    const { apiKey, apiSecret, testnet } = getEnv();
    if (!apiKey || !apiSecret) {
      return res.status(500).json({ error: "BINANCE_API_KEY/SECRET not configured" });
    }
    const body = req.body || {};
    const sym = String(body.symbol ?? "").trim().toUpperCase();
    if (!SYMBOL_RE.test(sym)) {
      return res.status(400).json({ error: "invalid symbol" });
    }

    // Нормализуем вход в плоский план уровней { level, side }.
    // Приоритет: orders[] -> levels[] + midPrice -> legacy side + levels[].
    type LevelSide = "BUY" | "SELL";
    const ordersInput = Array.isArray(body.orders) ? body.orders : null;
    const levelsInput = Array.isArray(body.levels) ? body.levels : [];
    const midPrice = Number(body.midPrice);
    const hasMidPrice = Number.isFinite(midPrice) && midPrice > 0;
    const plan: Array<{ level: number; side: LevelSide | null }> = [];
    let rawSide = "";

    if (ordersInput) {
      if (ordersInput.length === 0 || ordersInput.length > MAX_LEVELS) {
        return res
          .status(400)
          .json({ error: `orders must be a non-empty array (max ${MAX_LEVELS})` });
      }
      for (const entry of ordersInput) {
        const lv = Number((entry as any)?.price);
        const entrySide = String((entry as any)?.side ?? "").trim().toUpperCase();
        if (!Number.isFinite(lv) || lv <= 0) {
          return res
            .status(400)
            .json({ error: "each order price must be a finite number > 0" });
        }
        if (entrySide !== "BUY" && entrySide !== "SELL") {
          return res
            .status(400)
            .json({ error: "each order side must be exactly BUY or SELL" });
        }
        plan.push({ level: lv, side: entrySide as LevelSide });
      }
    } else if (levelsInput.length > 0 && hasMidPrice) {
      if (levelsInput.length > MAX_LEVELS) {
        return res
          .status(400)
          .json({ error: `levels must be a non-empty array (max ${MAX_LEVELS})` });
      }
      const eps = 1e-9 * Math.abs(midPrice);
      for (const lv of levelsInput) {
        const lvNum = Number(lv);
        let side: LevelSide | null = null;
        if (Number.isFinite(lvNum) && lvNum > 0) {
          const diff = lvNum - midPrice;
          if (Math.abs(diff) > eps) side = diff > 0 ? "SELL" : "BUY";
        }
        plan.push({ level: lvNum, side });
      }
    } else {
      rawSide = String(body.side ?? "").trim().toUpperCase();
      if (rawSide !== "BUY" && rawSide !== "SELL") {
        return res.status(400).json({ error: "side must be exactly BUY or SELL" });
      }
      if (levelsInput.length === 0 || levelsInput.length > MAX_LEVELS) {
        return res
          .status(400)
          .json({ error: `levels must be a non-empty array (max ${MAX_LEVELS})` });
      }
      const side: LevelSide = rawSide === "BUY" ? "BUY" : "SELL";
      for (const lv of levelsInput) plan.push({ level: Number(lv), side });
    }

    const orderSizeUsd = Number(body.orderSizeUsd ?? 100);
    if (!Number.isFinite(orderSizeUsd) || orderSizeUsd <= 0 || orderSizeUsd > MAX_ORDER_SIZE_USD) {
      return res.status(400).json({ error: `orderSizeUsd must be in (0, ${MAX_ORDER_SIZE_USD}]` });
    }
    const requestedLeverage = Number(body.leverage ?? 50);
    if (!Number.isInteger(requestedLeverage) || requestedLeverage < 1 || requestedLeverage > MAX_LEVERAGE) {
      return res.status(400).json({ error: `leverage must be an integer in [1, ${MAX_LEVERAGE}]` });
    }

    const isolationError = await symbolIsolationError(sym);
    if (isolationError) {
      return res.status(409).json({ error: isolationError });
    }

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
      return res.status(500).json({ error: `leverage: ${(e as Error).message}` });
    }

    const ei = await binanceGet("/fapi/v1/exchangeInfo", { symbol: sym });
    const info = (ei as any).symbols?.find((s: any) => s.symbol === sym);
    if (!info) {
      return res.status(404).json({ error: "symbol not found" });
    }
    const lot = (info.filters || []).find((f: any) => f.filterType === "LOT_SIZE");
    const price = (info.filters || []).find((f: any) => f.filterType === "PRICE_FILTER");
    const qStep = lot ? parseFloat(lot.stepSize) : 1;
    const tTick = price ? parseFloat(price.tickSize) : 0.0001;
    const minNotional = Number(
      (info.filters || []).find((f: any) => f.filterType === "MIN_NOTIONAL")?.notional || 5
    );

    let effectiveOrderSizeUsd = orderSizeUsd;
    if (orderSizeUsd < minNotional) {
      logger.warn(
        { symbol: sym, requested: orderSizeUsd, minNotional },
        "[grid-orders] orderSizeUsd raised to MIN_NOTIONAL"
      );
      effectiveOrderSizeUsd = Math.max(orderSizeUsd, minNotional);
    }

    const results: Array<{
      level: number;
      side?: LevelSide | null;
      orderId?: number;
      clientOrderId?: string;
      status?: string;
      error?: string;
      skipped?: boolean;
      reason?: string;
    }> = [];

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

    // Защитные STOP-ордера на бирже: ставятся после входа, чтобы позиция была
    // защищена даже при выключенном ПК/браузере/api-server. closePosition=true
    // закрывает всю позицию по маркерной цене (без quantity/reduceOnly).
    const stops: {
      long?: { algoId: number; orderId: number; triggerPrice: number };
      short?: { algoId: number; orderId: number; triggerPrice: number };
      errors: Array<{ side: string; error: string }>;
    } = { errors: [] };

    const loInput = Number(body.lo);
    const hiInput = Number(body.hi);
    const slBufferRaw = Number(body.slBufferPct ?? 2);
    const slBufferPct = Number.isFinite(slBufferRaw) && slBufferRaw >= 0 ? slBufferRaw : 2;
    const placeStops = body.placeStops !== false;
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

    return res.json({
      symbol: sym,
      testnet,
      orderSizeUsd: effectiveOrderSizeUsd,
      results,
      skipped,
      stops,
    });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] create failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

router.post("/stop", requireTestnet, requireGridOrdersEnabled, async (req, res) => {
  try {
    const { apiKey, apiSecret } = getEnv();
    if (!apiKey || !apiSecret) {
      return res.status(500).json({ error: "BINANCE_API_KEY/SECRET not configured" });
    }
    const body = req.body || {};
    const sym = String(body.symbol ?? "").trim().toUpperCase();
    if (!SYMBOL_RE.test(sym)) {
      return res.status(400).json({ error: "invalid symbol" });
    }

    const directionRaw = String(body.direction ?? "").trim().toLowerCase();
    let direction: "long" | "short" | null = null;
    if (directionRaw === "long" || directionRaw === "longs") {
      direction = "long";
    } else if (directionRaw === "short" || directionRaw === "shorts") {
      direction = "short";
    } else {
      return res.status(400).json({ error: "direction must be long or short" });
    }

    const loInput = Number(body.lo);
    const hiInput = Number(body.hi);
    const triggerInput = Number(body.triggerPrice);
    const hasTrigger = Number.isFinite(triggerInput) && triggerInput > 0;
    const hasLo = Number.isFinite(loInput) && loInput > 0;
    const hasHi = Number.isFinite(hiInput) && hiInput > 0;
    if (!hasTrigger && direction === "long" && !hasLo) {
      return res.status(400).json({ error: "triggerPrice or lo required" });
    }
    if (!hasTrigger && direction === "short" && !hasHi) {
      return res.status(400).json({ error: "triggerPrice or hi required" });
    }

    const slBufferRaw = Number(body.slBufferPct ?? 2);
    const slBufferPct = Number.isFinite(slBufferRaw) && slBufferRaw >= 0 ? slBufferRaw : 2;

    const ei = await binanceGet("/fapi/v1/exchangeInfo", { symbol: sym });
    const info = (ei as any).symbols?.find((s: any) => s.symbol === sym);
    if (!info) {
      return res.status(404).json({ error: "symbol not found" });
    }
    const priceFilter = (info.filters || []).find((f: any) => f.filterType === "PRICE_FILTER");
    const tTick = priceFilter ? parseFloat(priceFilter.tickSize) : 0.0001;

    const triggerPriceRaw = hasTrigger
      ? triggerInput
      : direction === "long"
        ? loInput * (1 - slBufferPct / 100)
        : hiInput * (1 + slBufferPct / 100);
    const triggerPrice = roundStep(triggerPriceRaw, tTick);
    if (!(triggerPrice > 0)) {
      return res.status(400).json({ error: "computed stop price is zero" });
    }

    logger.info({ symbol: sym, direction, triggerPrice }, "[grid-orders] stop attempt");

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
      return res.status(500).json({ error: "no algoId in response" });
    }

    logger.info({ symbol: sym, direction, algoId, triggerPrice }, "[grid-orders] stop complete");
    return res.json({ symbol: sym, direction, algoId, orderId: algoId, triggerPrice });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] stop failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

/**
 * POST /api/grid-orders/cancel
 * body: { symbol, orderIds?: number[], cancelAll?: boolean }
 * cancelAll=true — отменяет все открытые ордера по символу.
 */
router.post("/cancel", requireTestnet, requireGridOrdersEnabled, async (req, res) => {
  try {
    const { apiKey, apiSecret, testnet } = getEnv();
    if (!apiKey || !apiSecret) {
      return res.status(500).json({ error: "BINANCE_API_KEY/SECRET not configured" });
    }
    const { symbol, orderIds, cancelAll = false } = req.body || {};
    const sym = String(symbol ?? "").trim().toUpperCase();
    if (!sym) {
      return res.status(400).json({ error: "symbol required" });
    }

    logger.info(
      {
        symbol: sym,
        cancelAll: Boolean(cancelAll),
        orderCount: Array.isArray(orderIds) ? orderIds.length : 0,
      },
      "[grid-orders] cancel attempt"
    );

    if (cancelAll) {
      const r = await binanceDelete("/fapi/v1/allOpenOrders", { symbol: sym });
      logger.info(
        { symbol: sym, cancelAll: true, cancelled: r?.cancelled || 0 },
        "[grid-orders] cancel complete"
      );
      return res.json({ symbol: sym, testnet, cancelled: r?.cancelled || 0, result: r });
    }

    if (!Array.isArray(orderIds) || orderIds.length === 0) {
      return res.status(400).json({ error: "orderIds or cancelAll required" });
    }

    const results: Array<{ orderId: number; ok: boolean; error?: string }> = [];
    for (const id of orderIds) {
      try {
        await binanceDelete("/fapi/v1/order", { symbol: sym, orderId: id });
        results.push({ orderId: id, ok: true });
      } catch (e) {
        results.push({ orderId: id, ok: false, error: (e as Error).message });
      }
    }

    const succeeded = results.filter((r) => r.ok).length;
    const failed = results.length - succeeded;
    logger.info(
      {
        symbol: sym,
        cancelAll: false,
        orderCount: orderIds.length,
        succeeded,
        failed,
        failures: results
          .filter((r) => !r.ok)
          .map((r) => ({ orderId: r.orderId, error: r.error })),
      },
      "[grid-orders] cancel complete"
    );

    return res.json({ symbol: sym, testnet, results });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] cancel failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

/**
 * POST /api/grid-orders/cancel-stops
 * body: { symbol, orderIds: number[] }
 * Отменяет защитные STOP-ордера по их algoId (DELETE /fapi/v1/algoOrder); для
 * legacy-числовых orderId есть фолбэк на DELETE /fapi/v1/order. Чтобы клиент мог
 * снять защиту при аннулировании сетки. Возвращает успешные id в canceled и
 * ошибки по каждому id в errors, не прерывая остальные отмены.
 */
router.post("/cancel-stops", requireTestnet, requireGridOrdersEnabled, async (req, res) => {
  try {
    const { apiKey, apiSecret, testnet } = getEnv();
    if (!apiKey || !apiSecret) {
      return res.status(500).json({ error: "BINANCE_API_KEY/SECRET not configured" });
    }
    const { symbol, orderIds } = req.body || {};
    const sym = String(symbol ?? "").trim().toUpperCase();
    if (!sym) {
      return res.status(400).json({ error: "symbol required" });
    }
    if (!Array.isArray(orderIds) || orderIds.length === 0) {
      return res.status(400).json({ error: "orderIds required" });
    }

    logger.info(
      { symbol: sym, orderCount: orderIds.length },
      "[grid-orders] cancel-stops attempt"
    );

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
            await binanceDelete("/fapi/v1/order", { symbol: sym, orderId: id });
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

    logger.info(
      {
        symbol: sym,
        succeeded: canceled.length,
        failed: errors.length,
        failures: errors,
      },
      "[grid-orders] cancel-stops complete"
    );

    return res.json({ symbol: sym, testnet, canceled, errors });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] cancel-stops failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

/**
 * POST /api/grid-orders/tp
 * body: { symbol, direction: "long" | "short", tpPrice?: number | null, tpOrderId?: number | null }
 * Держит защитный TP-ордер на бирже: algo TAKE_PROFIT_MARKET + closePosition=true
 * по маркерной цене (без quantity/reduceOnly). Amend для algo-ордеров не
 * поддерживается, поэтому при смене цели старый algoId отменяется и выставляется
 * новый ордер (algoId меняется). При отсутствии/невалидности tpPrice — снимается.
 * Благодаря этому профит фиксируется даже при выключенном ПК/браузере/api-server.
 */
router.post("/tp", notifyTokenGuard, requireTestnet, requireGridOrdersEnabled, async (req, res) => {
  try {
    const { apiKey, apiSecret } = getEnv();
    if (!apiKey || !apiSecret) {
      return res.status(500).json({ error: "BINANCE_API_KEY/SECRET not configured" });
    }
    const body = req.body || {};
    const sym = String(body.symbol ?? "").trim().toUpperCase();
    if (!SYMBOL_RE.test(sym)) {
      return res.status(400).json({ error: "invalid symbol" });
    }

    const directionRaw = String(body.direction ?? "").trim().toLowerCase();
    let direction: "long" | "short" | null = null;
    if (directionRaw === "long" || directionRaw === "longs") {
      direction = "long";
    } else if (directionRaw === "short" || directionRaw === "shorts") {
      direction = "short";
    } else {
      return res.status(400).json({ error: "direction must be long or short" });
    }

    const rawTpOrderId = Number(body.tpOrderId);
    const tpOrderId =
      Number.isFinite(rawTpOrderId) && rawTpOrderId > 0 ? rawTpOrderId : null;
    const tpPriceRaw = Number(body.tpPrice);
    const hasTpPrice = Number.isFinite(tpPriceRaw) && tpPriceRaw > 0;

    // Нет валидной цели: TP не нужен — снимаем старый algo-ордер (best effort).
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
        return res.json({ symbol: sym, direction, tpOrderId: null, canceled: true });
      }
      logger.info(
        { symbol: sym, direction, tpPrice: null, algoId: null, amended: false },
        "[grid-orders] tp sync"
      );
      return res.json({ symbol: sym, direction, tpOrderId: null });
    }

    const ei = await binanceGet("/fapi/v1/exchangeInfo", { symbol: sym });
    const info = (ei as any).symbols?.find((s: any) => s.symbol === sym);
    if (!info) {
      return res.status(404).json({ error: "symbol not found" });
    }
    const priceFilter = (info.filters || []).find((f: any) => f.filterType === "PRICE_FILTER");
    const tickSize = priceFilter ? parseFloat(priceFilter.tickSize) : 0.0001;
    const priceRounded = roundStep(tpPriceRaw, tickSize);
    if (!(priceRounded > 0)) {
      return res.status(400).json({ error: "computed tp price is zero" });
    }

    // Защитный TP: закрытие лонга — SELL (mark >= stopPrice), шорта — BUY (mark <= stopPrice).
    const side: "BUY" | "SELL" = direction === "long" ? "SELL" : "BUY";

    // Amend для algo-ордеров отсутствует: cancel + place, algoId меняется.
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
    return res.json({
      symbol: sym,
      direction,
      tpOrderId: outAlgoId,
      orderId: outAlgoId,
      tpPrice: priceRounded,
      amended: false,
      replaced: true,
    });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] tp failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

router.get("/open-algo", requireTestnet, requireGridOrdersEnabled, async (req, res) => {
  try {
    const { apiKey, apiSecret } = getEnv();
    if (!apiKey || !apiSecret) {
      return res.status(500).json({ error: "BINANCE_API_KEY/SECRET not configured" });
    }
    const sym = String(req.query.symbol ?? "").trim().toUpperCase();
    if (!SYMBOL_RE.test(sym)) {
      return res.status(400).json({ error: "invalid symbol" });
    }

    const raw = await binanceGet(OPEN_ALGO_ORDERS_PATH, { symbol: sym });
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

    logger.info({ symbol: sym, count: orders.length }, "[grid-orders] open algo orders fetched");

    return res.json({ symbol: sym, orders });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] open-algo failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

const MAX_ALGO_STATUS_IDS = 50;

router.post("/algo-status", requireTestnet, requireGridOrdersEnabled, async (req, res) => {
  try {
    const { apiKey, apiSecret } = getEnv();
    if (!apiKey || !apiSecret) {
      return res.status(500).json({ error: "BINANCE_API_KEY/SECRET not configured" });
    }
    const body = req.body || {};
    const sym = String(body.symbol ?? "").trim().toUpperCase();
    if (!SYMBOL_RE.test(sym)) {
      return res.status(400).json({ error: "invalid symbol" });
    }
    const rawIds = body.algoIds;
    if (!Array.isArray(rawIds) || rawIds.length === 0 || rawIds.length > MAX_ALGO_STATUS_IDS) {
      return res
        .status(400)
        .json({ error: `algoIds must be a non-empty array (max ${MAX_ALGO_STATUS_IDS})` });
    }
    const algoIds: number[] = [];
    for (const raw of rawIds) {
      const id = Number(raw);
      if (!Number.isInteger(id) || id <= 0) {
        return res.status(400).json({ error: "algoIds must be positive integers" });
      }
      algoIds.push(id);
    }

    const results: Array<
      | {
          algoId: number;
          algoStatus: string;
          triggerPrice: number;
          actualPrice: number;
          actualQty: number;
          actualOrderId: number;
        }
      | { algoId: number; error: string }
    > = [];

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

    logger.info({ symbol: sym, count: algoIds.length }, "[grid-orders] algo status fetched");

    return res.json({ symbol: sym, results });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] algo-status failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

const MAX_FILL_ORDER_IDS = 100;

/** Number() с безопасным дефолтом 0 (отсутствующие/нечисловые значения). */
function numOr0(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

/** Округляет количество вниз до шага stepSize (не превышая реальный размер позиции). */
function floorStep(value: number, step: number): number {
  if (!Number.isFinite(step) || step <= 0) return value;
  const s = step >= 1 ? 0 : Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
  const r = Math.floor(value / step + 1e-9) * step;
  return Number(r.toFixed(s));
}

const FILL_QUERY_ATTEMPTS = 5;
const FILL_QUERY_DELAYS_MS = [300, 500, 800, 1000];

interface FillData {
  executedQty: number;
  avgPrice: number;
  cumQuote: number;
}

/** Извлекает executedQty/avgPrice/cumQuote из ответа Binance-ордера. */
function normalizeFill(raw: any): FillData {
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

/**
 * Разрешает реальные данные исполнения MARKET-ордера, когда ответ содержит нули:
 * повторный опрос GET /fapi/v1/order, затем агрегация GET /fapi/v1/userTrades.
 * Любая ошибка не пробрасывается — возвращаются лучшие известные значения.
 */
async function resolveFillData(sym: string, orderId: unknown, initial: FillData): Promise<FillData> {
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

interface PositionSnapshot {
  positionAmt: number;
  entryPrice: number;
}

/** Читает текущую позицию по символу; null — если запрос не удался или записи нет. */
async function fetchPosition(sym: string): Promise<PositionSnapshot | null> {
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

interface TradeAggregate {
  qty: number;
  cumQuote: number;
  avgPrice: number;
  price: number;
  side: string;
}

/** Агрегирует userTrades по orderId: qty/quoteQty суммируются, avgPrice = cumQuote/qty. */
function aggregateTradesByOrderId(trades: any[]): Map<number, TradeAggregate> {
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

/**
 * POST /api/grid-orders/fills
 * body: { symbol, orderIds: number[], sinceMs? }
 * Операционный роут: отдаёт статусы/исполнение указанных ордеров и текущую позицию.
 * При наличии sinceMs использует один batch openOrders + userTrades вместо запроса на каждый ордер.
 * Работает всегда (не проходит проверку symbolIsolationError).
 */
router.post("/fills", notifyTokenGuard, requireTestnet, requireGridOrdersEnabled, async (req, res) => {
  try {
    const { apiKey, apiSecret } = getEnv();
    if (!apiKey || !apiSecret) {
      return res.status(500).json({ error: "BINANCE_API_KEY/SECRET not configured" });
    }
    const body = req.body || {};
    const sym = String(body.symbol ?? "").trim().toUpperCase();
    if (!SYMBOL_RE.test(sym)) {
      return res.status(400).json({ error: "invalid symbol" });
    }
    const rawOrderIds = body.orderIds;
    if (!Array.isArray(rawOrderIds) || rawOrderIds.length === 0 || rawOrderIds.length > MAX_FILL_ORDER_IDS) {
      return res
        .status(400)
        .json({ error: `orderIds must be a non-empty array (max ${MAX_FILL_ORDER_IDS})` });
    }
    const orderIds: number[] = [];
    for (const raw of rawOrderIds) {
      const id = Number(raw);
      if (!Number.isInteger(id) || id <= 0) {
        return res.status(400).json({ error: "orderIds must be positive integers" });
      }
      orderIds.push(id);
    }
    const sinceMs = Number(body.sinceMs);
    const hasSinceMs = Number.isFinite(sinceMs) && sinceMs > 0;

    const position = await fetchPosition(sym);

    const results: Array<
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
      | { orderId: number; error: string }
    > = [];

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
          startTime: sinceMs,
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

    return res.json({
      symbol: sym,
      positionAmt: position ? position.positionAmt : null,
      entryPrice: position ? position.entryPrice : null,
      results,
    });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] fills failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

/**
 * POST /api/grid-orders/close
 * body: { symbol, quantity?, sinceMs?, direction?: "long"|"short" }
 * Закрывает позицию MARKET-ордером (reduceOnly) и best-effort считает funding.
 * direction=long закрывает лонг (SELL), direction=short закрывает шорт (BUY);
 * без direction поведение прежнее — закрыть всю позицию.
 * Работает всегда (не проходит проверку symbolIsolationError).
 */
router.post("/close", notifyTokenGuard, requireTestnet, requireGridOrdersEnabled, async (req, res) => {
  try {
    const { apiKey, apiSecret } = getEnv();
    if (!apiKey || !apiSecret) {
      return res.status(500).json({ error: "BINANCE_API_KEY/SECRET not configured" });
    }
    const body = req.body || {};
    const sym = String(body.symbol ?? "").trim().toUpperCase();
    if (!SYMBOL_RE.test(sym)) {
      return res.status(400).json({ error: "invalid symbol" });
    }

    const explicitQty = Number(body.quantity);
    const hasExplicitQty = Number.isFinite(explicitQty) && explicitQty > 0;
    const sinceMs = Number(body.sinceMs);
    const hasSinceMs = Number.isFinite(sinceMs) && sinceMs > 0;

    const directionRaw = String(body.direction ?? "").trim().toLowerCase();
    let direction: "long" | "short" | null = null;
    if (directionRaw !== "") {
      if (directionRaw === "long" || directionRaw === "longs") {
        direction = "long";
      } else if (directionRaw === "short" || directionRaw === "shorts") {
        direction = "short";
      } else {
        return res.status(400).json({ error: "direction must be long or short" });
      }
    }

    const position = await fetchPosition(sym);

    let qty: number;
    let side: "BUY" | "SELL";
    if (direction) {
      side = direction === "long" ? "SELL" : "BUY";
      if (hasExplicitQty) {
        qty = explicitQty;
      } else {
        if (!position) {
          return res.status(400).json({ error: "position unavailable" });
        }
        qty = Math.abs(position.positionAmt);
      }
    } else if (hasExplicitQty) {
      qty = explicitQty;
      side = position && position.positionAmt < 0 ? "BUY" : "SELL";
    } else {
      if (!position) {
        return res.status(400).json({ error: "position unavailable" });
      }
      qty = Math.abs(position.positionAmt);
      side = position.positionAmt < 0 ? "BUY" : "SELL";
    }
    if (!(qty > 0)) {
      return res.status(400).json({ error: "nothing toclose: position is 0" });
    }

    const ei = await binanceGet("/fapi/v1/exchangeInfo", { symbol: sym });
    const info = (ei as any).symbols?.find((s: any) => s.symbol === sym);
    if (!info) {
      return res.status(404).json({ error: "symbol not found" });
    }
    const lot = (info.filters || []).find((f: any) => f.filterType === "LOT_SIZE");
    const qStep = lot ? parseFloat(lot.stepSize) : 1;
    const qtyRounded = floorStep(qty, qStep);
    if (!(qtyRounded > 0)) {
      return res.status(400).json({ error: "nothing toclose: quantity is 0" });
    }

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

    const orderId = r?.orderId;
    const fill = await resolveFillData(sym, orderId, normalizeFill(r));
    const avgPrice = fill.avgPrice;
    const executedQty = fill.executedQty;
    const cumQuote = fill.cumQuote;

    logger.info(
      { symbol: sym, side, direction, quantity: qtyRounded, avgPrice, executedQty, cumQuote, fundingUsd },
      "[grid-orders] close complete"
    );

    return res.json({ symbol: sym, side, direction, orderId, avgPrice, executedQty, cumQuote, fundingUsd });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] close failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

/**
 * POST /api/grid-orders/reset
 * Полный сброс тестового аккаунта: отменяет все открытые ордера (включая algo) по
 * всему аккаунту, закрывает все ненулевые позиции MARKET-ордерами (reduceOnly) и помечает активные
 * записи grid_history как stopped. Идемпотентно и безопасно: частичный сбой любого
 * шага/символа собирается в errors и не прерывает остальные операции.
 */
router.get("/reset-status", notifyTokenGuard, (_req, res) => {
  return res.json({ resetAt: readResetMarker() });
});

router.post("/reset", notifyTokenGuard, requireTestnet, requireGridOrdersEnabled, async (_req, res) => {
  const resetAt = Date.now();
  writeResetMarker(resetAt);
  const errors: Array<{ step: string; symbol?: string; error: string }> = [];
  const canceledSymbols: string[] = [];
  const closedPositions: Array<{ symbol: string; side: string; qty: number; orderId?: number }> = [];
  let canceledOrders = 0;
  let canceledAlgoOrders = 0;
  let gridHistoryUpdated = 0;

  try {
    const { apiKey, apiSecret } = getEnv();
    if (!apiKey || !apiSecret) {
      return res.status(500).json({ error: "BINANCE_API_KEY/SECRET not configured" });
    }

    // 1. Отменяем все открытые ордера на всём аккаунте (GET без symbol).
    try {
      const all = await binanceGet("/fapi/v1/openOrders", {});
      const openOrders: any[] = Array.isArray(all) ? all : all ? [all] : [];
      const countBySymbol = new Map<string, number>();
      for (const o of openOrders) {
        const sym = String(o?.symbol ?? "").trim().toUpperCase();
        if (!sym) continue;
        countBySymbol.set(sym, (countBySymbol.get(sym) ?? 0) + 1);
      }
      for (const [sym, count] of countBySymbol) {
        try {
          await binanceDelete("/fapi/v1/allOpenOrders", { symbol: sym });
          canceledOrders += count;
          canceledSymbols.push(sym);
        } catch (e) {
          errors.push({ step: "cancelOrders", symbol: sym, error: (e as Error).message });
        }
      }
    } catch (e) {
      errors.push({ step: "cancelOrders", error: (e as Error).message });
    }

    // 1b. Отменяем все открытые algo-ордера (conditional STOP/TP) по всему аккаунту.
    try {
      const allAlgo = await binanceGet(OPEN_ALGO_ORDERS_PATH, {});
      const openAlgo: any[] = Array.isArray(allAlgo) ? allAlgo : allAlgo ? [allAlgo] : [];
      const algoCountBySymbol = new Map<string, number>();
      for (const o of openAlgo) {
        const sym = String(o?.symbol ?? "").trim().toUpperCase();
        if (!sym) continue;
        algoCountBySymbol.set(sym, (algoCountBySymbol.get(sym) ?? 0) + 1);
      }
      for (const [sym, count] of algoCountBySymbol) {
        try {
          await binanceDelete(ALGO_OPEN_ORDERS_PATH, { symbol: sym });
          canceledAlgoOrders += count;
        } catch (e) {
          errors.push({ step: "cancelAlgoOrders", symbol: sym, error: (e as Error).message });
        }
      }
    } catch (e) {
      errors.push({ step: "cancelAlgoOrders", error: (e as Error).message });
    }

    // 2. Закрываем все ненулевые позиции MARKET-ордерами reduceOnly.
    try {
      const stepBySymbol = new Map<string, number>();
      try {
        const ei = await binanceGet("/fapi/v1/exchangeInfo", {});
        const symbols: any[] = Array.isArray((ei as any)?.symbols) ? (ei as any).symbols : [];
        for (const s of symbols) {
          const sym = String(s?.symbol ?? "").trim().toUpperCase();
          if (!sym) continue;
          const lot = (s?.filters || []).find((f: any) => f.filterType === "LOT_SIZE");
          const step = lot ? parseFloat(lot.stepSize) : NaN;
          if (Number.isFinite(step) && step > 0) stepBySymbol.set(sym, step);
        }
      } catch (e) {
        errors.push({ step: "exchangeInfo", error: (e as Error).message });
      }

      const positionsRaw = await binanceGet("/fapi/v2/positionRisk", {});
      const positions: any[] = Array.isArray(positionsRaw)
        ? positionsRaw
        : positionsRaw
          ? [positionsRaw]
          : [];
      for (const p of positions) {
        const sym = String(p?.symbol ?? "").trim().toUpperCase();
        const amt = numOr0(p?.positionAmt);
        if (!sym || amt === 0) continue;
        const side: "BUY" | "SELL" = amt > 0 ? "SELL" : "BUY";
        const qty = floorStep(Math.abs(amt), stepBySymbol.get(sym) ?? 0);
        if (!(qty > 0)) {
          errors.push({
            step: "closePositions",
            symbol: sym,
            error: `quantity rounds to 0 (positionAmt=${amt})`,
          });
          continue;
        }
        try {
          const r = await binancePost("/fapi/v1/order", {
            symbol: sym,
            side,
            type: "MARKET",
            quantity: qty,
            reduceOnly: "true",
          });
          const orderId = Number(r?.orderId);
          closedPositions.push({
            symbol: sym,
            side,
            qty,
            orderId: Number.isFinite(orderId) ? orderId : undefined,
          });
        } catch (e) {
          errors.push({ step: "closePositions", symbol: sym, error: (e as Error).message });
        }
      }
    } catch (e) {
      errors.push({ step: "closePositions", error: (e as Error).message });
    }

    // 3. Помечаем активные записи grid_history как stopped.
    try {
      const updated = await db
        .update(gridHistoryTable)
        .set({
          phase: "stopped",
          exit_reason: "reset",
          finished_at: new Date().toISOString(),
        })
        .where(inArray(gridHistoryTable.phase, ["active", "waiting"]))
        .returning();
      gridHistoryUpdated = Array.isArray(updated) ? updated.length : 0;
    } catch (e) {
      errors.push({ step: "gridHistory", error: (e as Error).message });
    }

    const summary = {
      resetAt,
      canceledOrders,
      canceledAlgoOrders,
      canceledSymbols,
      closedPositions,
      gridHistoryUpdated,
      errors,
    };
    logger.info(summary, "[grid-orders] account reset complete");
    return res.json(summary);
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] reset failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

export default router;
