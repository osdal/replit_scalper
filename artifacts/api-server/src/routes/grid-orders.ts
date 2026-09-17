import { Router } from "express";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { notifyTokenGuard } from "../middlewares/notifyAuth";
import { logger } from "../lib/logger";
import { db, botsTable, tradingControlTable, gridHistoryTable } from "@workspace/db";
import { eq, inArray } from "drizzle-orm";
import {
  MAX_ORDER_SIZE_USD,
  MAX_LEVERAGE,
  MAX_LEVELS,
  MAX_RESIZE_ORDERS,
  SYMBOL_RE,
  GridOrderError,
  getBinanceEnv as getEnv,
  binanceGet,
  binancePost,
  binancePut,
  binanceDelete,
  OPEN_ALGO_ORDERS_PATH,
  ALGO_OPEN_ORDERS_PATH,
  roundStep,
  floorStep,
  numOr0,
  positionQuantity,
  createGridOrders,
  cancelOrderIds,
  cancelAllSymbolOrders,
  cancelAlgoOrderIds,
  placeGridStop,
  syncGridTp,
  fetchGridFills,
  fetchOpenAlgoOrders,
  fetchAlgoStatus,
  closeGridPosition,
  type LevelSide,
} from "../grid-orders-lib";

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

    const out = await createGridOrders({
      symbol: sym,
      plan,
      orderSizeUsd,
      leverage: requestedLeverage,
      lo: Number(body.lo),
      hi: Number(body.hi),
      slBufferPct: Number(body.slBufferPct ?? 2),
      placeStops: body.placeStops !== false,
    });

    return res.json({
      symbol: sym,
      testnet,
      orderSizeUsd: out.orderSizeUsd,
      results: out.results,
      skipped: out.skipped,
      stops: out.stops,
    });
  } catch (e: any) {
    if (e instanceof GridOrderError) {
      return res.status(e.status).json({ error: e.message });
    }
    logger.error({ err: e }, "[grid-orders] create failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

/**
 * POST /api/grid-orders/resize
 * body: { symbol, orderSizeUsd, orders: Array<{ orderId: number, price: number, side: "BUY"|"SELL" }> }
 * Меняет размер лота уже выставленных LIMIT-ордеров: сначала пробует amend quantity
 * на месте (PUT /fapi/v1/order), при ошибке — cancel + новая LIMIT-заявка. Разрешено
 * только до первого fill: при ненулевой позиции возвращается 409, при невозможности
 * проверить позицию — 502 (fail-closed).
 */
router.post(
  "/resize",
  notifyTokenGuard,
  requireTestnet,
  requireGridOrdersEnabled,
  async (req, res) => {
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

      const orderSizeUsd = Number(body.orderSizeUsd);
      if (!Number.isFinite(orderSizeUsd) || orderSizeUsd <= 0 || orderSizeUsd > MAX_ORDER_SIZE_USD) {
        return res.status(400).json({ error: `orderSizeUsd must be in (0, ${MAX_ORDER_SIZE_USD}]` });
      }

      const rawOrders = body.orders;
      if (!Array.isArray(rawOrders) || rawOrders.length === 0 || rawOrders.length > MAX_RESIZE_ORDERS) {
        return res
          .status(400)
          .json({ error: `orders must be a non-empty array (max ${MAX_RESIZE_ORDERS})` });
      }
      type ResizeSide = "BUY" | "SELL";
      const orders: Array<{ orderId: number; price: number; side: ResizeSide }> = [];
      for (const entry of rawOrders) {
        const orderId = Number((entry as any)?.orderId);
        const price = Number((entry as any)?.price);
        const side = String((entry as any)?.side ?? "").trim().toUpperCase();
        if (!Number.isInteger(orderId) || orderId <= 0) {
          return res.status(400).json({ error: "each order orderId must be a positive integer" });
        }
        if (!Number.isFinite(price) || price <= 0) {
          return res.status(400).json({ error: "each order price must be a finite number > 0" });
        }
        if (side !== "BUY" && side !== "SELL") {
          return res.status(400).json({ error: "each order side must be exactly BUY or SELL" });
        }
        orders.push({ orderId, price, side: side as ResizeSide });
      }

      // 1. Менять лот можно только до первого fill: позиция обязана быть нулевой.
      try {
        const raw = await binanceGet("/fapi/v2/positionRisk", { symbol: sym });
        const list: any[] = Array.isArray(raw) ? raw : raw ? [raw] : [];
        const entry = list.find((p: any) => String(p?.symbol ?? "").toUpperCase() === sym);
        const positionAmt = numOr0(entry?.positionAmt);
        if (Math.abs(positionAmt) > 0) {
          return res.status(409).json({
            error: "position is open; lot size can only be changed before the first fill",
          });
        }
      } catch (e) {
        logger.warn(
          { symbol: sym, err: (e as Error).message },
          "[grid-orders] resize positionRisk failed, failing closed"
        );
        return res.status(502).json({ error: "cannot verify position" });
      }

      // 2. Шаги/минимум по символу.
      const ei = await binanceGet("/fapi/v1/exchangeInfo", { symbol: sym });
      const info = (ei as any).symbols?.find((s: any) => s.symbol === sym);
      if (!info) {
        return res.status(404).json({ error: "symbol not found" });
      }
      const lot = (info.filters || []).find((f: any) => f.filterType === "LOT_SIZE");
      const priceFilter = (info.filters || []).find((f: any) => f.filterType === "PRICE_FILTER");
      const stepSize = lot ? parseFloat(lot.stepSize) : 1;
      const tickSize = priceFilter ? parseFloat(priceFilter.tickSize) : 0.0001;
      const minNotional = Number(
        (info.filters || []).find((f: any) => f.filterType === "MIN_NOTIONAL")?.notional || 5
      );

      let effectiveOrderSizeUsd = orderSizeUsd;
      if (orderSizeUsd < minNotional) {
        logger.warn(
          { symbol: sym, requested: orderSizeUsd, minNotional },
          "[grid-orders] resize orderSizeUsd raised to MIN_NOTIONAL"
        );
        effectiveOrderSizeUsd = Math.max(orderSizeUsd, minNotional);
      }

      const results: Array<{
        orderId: number;
        newOrderId: number;
        price: number;
        side: ResizeSide;
        qty: number;
        amended: boolean;
      }> = [];
      const errors: Array<{ orderId: number; error: string }> = [];

      logger.info(
        { symbol: sym, orderCount: orders.length, effectiveOrderSizeUsd },
        "[grid-orders] resize attempt"
      );

      for (const order of orders) {
        const { orderId, price, side } = order;

        // 3. Количество под новый размер лота с учётом stepSize/MIN_NOTIONAL.
        let qty = Math.max(stepSize, roundStep(effectiveOrderSizeUsd / price, stepSize));
        if (qty * price < minNotional) {
          qty = roundStep(Math.ceil(minNotional / price / stepSize) * stepSize, stepSize);
        }
        if (!Number.isFinite(qty) || qty <= 0) {
          errors.push({ orderId, error: "computed quantity is not finite or <= 0" });
          continue;
        }

        const priceRounded = roundStep(price, tickSize);

        // 4. Пробуем amend quantity на месте.
        try {
          const r = await binancePut("/fapi/v1/order", { orderId, symbol: sym, quantity: qty });
          const newOrderId = Number(r?.orderId);
          results.push({
            orderId,
            newOrderId: Number.isFinite(newOrderId) ? newOrderId : orderId,
            price: priceRounded,
            side,
            qty,
            amended: true,
          });
          continue;
        } catch (amendErr) {
          logger.warn(
            { symbol: sym, orderId, err: (amendErr as Error).message },
            "[grid-orders] resize amend failed, replacing order"
          );
        }

        // 5. Amend не удался: cancel + новая LIMIT-заявка (параметры как в /create).
        try {
          await binanceDelete("/fapi/v1/order", { symbol: sym, orderId });
          const base = sym.slice(0, 10);
          let cid = `grid_${base}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
          cid = cid.slice(0, 36);
          const r = await binancePost("/fapi/v1/order", {
            symbol: sym,
            side,
            type: "LIMIT",
            timeInForce: "GTC",
            price: priceRounded,
            quantity: qty,
            newClientOrderId: cid,
          });
          const newOrderId = Number(r?.orderId);
          results.push({
            orderId,
            newOrderId: Number.isFinite(newOrderId) ? newOrderId : orderId,
            price: priceRounded,
            side,
            qty,
            amended: false,
          });
        } catch (e) {
          errors.push({ orderId, error: (e as Error).message });
        }
      }

      const amended = results.filter((r) => r.amended).length;
      const replaced = results.filter((r) => !r.amended).length;
      logger.info(
        { symbol: sym, effectiveOrderSizeUsd, amended, replaced, errorCount: errors.length },
        "[grid-orders] resize complete"
      );

      return res.json({
        symbol: sym,
        orderSizeUsd: effectiveOrderSizeUsd,
        results,
        errors,
      });
    } catch (e: any) {
      logger.error({ err: e }, "[grid-orders] resize failed");
      return res.status(500).json({ error: e?.message || "failed" });
    }
  }
);

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

    const out = await placeGridStop({
      symbol: sym,
      direction,
      triggerPrice: Number(body.triggerPrice),
      lo: Number(body.lo),
      hi: Number(body.hi),
      slBufferPct: Number(body.slBufferPct ?? 2),
      algoId: Number(body.algoId),
    });

    return res.json({
      symbol: sym,
      direction,
      algoId: out.algoId,
      orderId: out.algoId,
      triggerPrice: out.triggerPrice,
      replaced: out.replaced,
    });
  } catch (e: any) {
    if (e instanceof GridOrderError) {
      return res.status(e.status).json({ error: e.message });
    }
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
      const r = await cancelAllSymbolOrders(sym);
      logger.info(
        { symbol: sym, cancelAll: true, cancelled: r?.cancelled || 0 },
        "[grid-orders] cancel complete"
      );
      return res.json({ symbol: sym, testnet, cancelled: r?.cancelled || 0, result: r });
    }

    if (!Array.isArray(orderIds) || orderIds.length === 0) {
      return res.status(400).json({ error: "orderIds or cancelAll required" });
    }

    const results = await cancelOrderIds(sym, orderIds);

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

    const { canceled, errors } = await cancelAlgoOrderIds(sym, orderIds);

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

    const out = await syncGridTp({
      symbol: sym,
      direction,
      tpPrice: Number(body.tpPrice),
      tpOrderId: Number(body.tpOrderId),
    });

    if (out.tpPrice == null) {
      return res.json(
        out.canceled
          ? { symbol: sym, direction, tpOrderId: null, canceled: true }
          : { symbol: sym, direction, tpOrderId: null },
      );
    }

    return res.json({
      symbol: sym,
      direction,
      tpOrderId: out.tpOrderId,
      orderId: out.tpOrderId,
      tpPrice: out.tpPrice,
      amended: false,
      replaced: true,
    });
  } catch (e: any) {
    if (e instanceof GridOrderError) {
      return res.status(e.status).json({ error: e.message });
    }
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

    const orders = await fetchOpenAlgoOrders(sym);

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

    const results = await fetchAlgoStatus(sym, algoIds);

    return res.json({ symbol: sym, results });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] algo-status failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

const MAX_FILL_ORDER_IDS = 100;

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

    const fills = await fetchGridFills(sym, orderIds, hasSinceMs ? sinceMs : null);

    return res.json({
      symbol: sym,
      positionAmt: fills.positionAmt,
      entryPrice: fills.entryPrice,
      results: fills.results,
    });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] fills failed");
    return res.status(500).json({ error: e?.message || "failed" });
  }
});

const MAX_TRADES = 500;
const DEFAULT_TRADES_LIMIT = 1000;

/**
 * POST /api/grid-orders/trades
 * body: { symbol, orderId?, sinceMs?, limit? }
 * Отдаёт нормализованные сделки (GET /fapi/v1/userTrades) и агрегаты по ним:
 * totalQty, totalQuoteQty, avgPrice. Фильтр: orderId -> startTime -> без фильтра.
 * limit по умолчанию 1000 и ограничен диапазоном [1, 1000]; в ответе не более
 * MAX_TRADES последних сделок (агрегаты считаются по возвращённым сделкам).
 */
router.post("/trades", notifyTokenGuard, requireTestnet, requireGridOrdersEnabled, async (req, res) => {
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

    const hasOrderId = body.orderId !== undefined && body.orderId !== null && body.orderId !== "";
    let orderId: number | null = null;
    if (hasOrderId) {
      const id = Number(body.orderId);
      if (!Number.isInteger(id) || id <= 0) {
        return res.status(400).json({ error: "orderId must be a positive integer" });
      }
      orderId = id;
    }

    const hasSinceMs = body.sinceMs !== undefined && body.sinceMs !== null && body.sinceMs !== "";
    let sinceMs: number | null = null;
    if (hasSinceMs) {
      const ms = Number(body.sinceMs);
      if (!Number.isFinite(ms) || ms <= 0) {
        return res.status(400).json({ error: "sinceMs must be a finite positive number" });
      }
      sinceMs = ms;
    }

    const rawLimit = Number(body.limit ?? DEFAULT_TRADES_LIMIT);
    const limit = Number.isFinite(rawLimit)
      ? Math.min(DEFAULT_TRADES_LIMIT, Math.max(1, Math.trunc(rawLimit)))
      : DEFAULT_TRADES_LIMIT;

    let filterKind: "orderId" | "sinceMs" | "none" = "none";
    let params: Record<string, string | number>;
    if (orderId !== null) {
      filterKind = "orderId";
      params = { symbol: sym, orderId, limit };
    } else if (sinceMs !== null) {
      filterKind = "sinceMs";
      params = { symbol: sym, startTime: sinceMs, limit };
    } else {
      params = { symbol: sym, limit };
    }

    let raw: any;
    try {
      raw = await binanceGet("/fapi/v1/userTrades", params);
    } catch (e) {
      logger.error(
        { symbol: sym, filter: filterKind, err: (e as Error).message },
        "[grid-orders] trades fetch failed"
      );
      return res.status(502).json({ error: (e as Error).message });
    }

    const list: any[] = Array.isArray(raw) ? raw : raw ? [raw] : [];
    const normalized = list.map((t: any) => ({
      orderId: numOr0(t?.orderId),
      side: String(t?.side ?? ""),
      price: numOr0(t?.price),
      qty: numOr0(t?.qty),
      quoteQty: numOr0(t?.quoteQty),
      commission: numOr0(t?.commission),
      commissionAsset: String(t?.commissionAsset ?? ""),
      realizedPnl: numOr0(t?.realizedPnl),
      time: numOr0(t?.time),
    }));
    const trades = normalized.slice(Math.max(0, normalized.length - MAX_TRADES));

    const totalQty = trades.reduce((sum, t) => sum + t.qty, 0);
    const totalQuoteQty = trades.reduce((sum, t) => sum + t.quoteQty, 0);
    const avgPrice = totalQty > 0 ? totalQuoteQty / totalQty : 0;

    logger.info(
      { symbol: sym, filter: filterKind, count: trades.length },
      "[grid-orders] trades fetched"
    );

    return res.json({
      symbol: sym,
      count: trades.length,
      totalQty,
      totalQuoteQty,
      avgPrice,
      trades,
    });
  } catch (e: any) {
    logger.error({ err: e }, "[grid-orders] trades failed");
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

    const out = await closeGridPosition({
      symbol: sym,
      direction,
      quantity: Number(body.quantity),
      sinceMs: Number(body.sinceMs),
    });

    return res.json(out);
  } catch (e: any) {
    if (e instanceof GridOrderError) {
      return res.status(e.status).json({ error: e.message });
    }
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
