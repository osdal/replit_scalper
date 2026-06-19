/**
 * Синхронизация истории сделок с Binance Futures API.
 * Берёт реальный PnL из income history (после комиссий и funding).
 */
import { Router } from "express";
import { db, tradesTable } from "@workspace/db";
import { sql } from "drizzle-orm";
import crypto from "crypto";

const router = Router();

const API_KEY    = process.env.BINANCE_API_KEY || "";
const API_SECRET = process.env.BINANCE_API_SECRET || "";
const BASE_URL   = "https://fapi.binance.com";

const SYMBOLS = [
  "ETHUSDT", "SOLUSDT", "BTCUSDT", "BNBUSDT",
  "DOGEUSDT", "TRXUSDT", "XRPUSDT"
];

function sign(params: Record<string, string | number>): string {
  const qs = Object.entries(params).map(([k, v]) => `${k}=${v}`).join("&");
  return crypto.createHmac("sha256", API_SECRET).update(qs).digest("hex");
}

async function binanceGet(path: string, params: Record<string, string | number> = {}) {
  const ts = Date.now();
  const p = { ...params, timestamp: ts };
  const signature = sign(p);
  const qs = Object.entries(p).map(([k, v]) => `${k}=${v}`).join("&");
  const url = `${BASE_URL}${path}?${qs}&signature=${signature}`;
  const resp = await fetch(url, { headers: { "X-MBX-APIKEY": API_KEY } });
  if (!resp.ok) throw new Error(`Binance API ${path}: ${resp.status} ${await resp.text()}`);
  return resp.json();
}

// POST /binance-sync
router.post("/", async (_req, res) => {
  try {
    if (!API_KEY || !API_SECRET) {
      return res.status(400).json({ error: "BINANCE_API_KEY and BINANCE_API_SECRET not configured" });
    }

    // Очищаем старые записи
    await db.run(sql`DELETE FROM trades`);

    let total = 0;

    for (const symbol of SYMBOLS) {
      try {
        // 1. Получаем все исполнения (userTrades) — для entry/exit цен
        const userTrades: any[] = await binanceGet("/fapi/v1/userTrades", {
          symbol, limit: 1000,
        });

        if (!userTrades.length) continue;

        // 2. Группируем userTrades в позиции (realizedPnl уже есть в каждой сделке)
        const positions = groupPositions(userTrades, symbol);

        for (const pos of positions) {
          await db.insert(tradesTable).values(pos);
          total++;
        }

        console.log(`[SYNC] ${symbol}: ${positions.length} positions`);
      } catch (e: any) {
        console.error(`[SYNC] Error for ${symbol}:`, e.message);
      }
    }

    res.json({ success: true, synced: total });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// GET /binance-sync/positions
router.get("/positions", async (_req, res) => {
  try {
    const positions: any[] = await binanceGet("/fapi/v2/positionRisk");
    const open = positions.filter(p => Math.abs(parseFloat(p.positionAmt)) > 0);
    res.json(open);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/**
 * Группирует userTrades в позиции.
 * realizedPnl берётся напрямую из каждой сделки — точное совпадение с Binance.
 */
function groupPositions(trades: any[], symbol: string) {
  const positions: any[] = [];

  let openQty    = 0;
  let openSide   = "";
  let entryPrice = 0;
  let entryTime  = 0;
  let totalQty   = 0;
  let exitPrice  = 0;
  let exitTime   = 0;
  let closePnl   = 0;
  let openCommission = 0;

  for (const t of trades) {
    const qty   = parseFloat(t.qty);
    const price = parseFloat(t.price);
    const side  = t.side as string;
    const time  = t.time as number;
    const pnl        = parseFloat(t.realizedPnl || "0");
    const commission = parseFloat(t.commission || "0");
    const commissionUsd = t.commissionAsset === "USDT" ? commission : 0;
    const netPnl = pnl - commissionUsd;

    if (openQty === 0) {
      // Открытие позиции — запоминаем уплаченную комиссию
      openSide   = side === "BUY" ? "LONG" : "SHORT";
      entryPrice = price;
      entryTime  = time;
      openQty    = qty;
      totalQty   = qty;
      closePnl   = 0;
      openCommission = commissionUsd;
    } else if (
      (openSide === "LONG"  && side === "SELL") ||
      (openSide === "SHORT" && side === "BUY")
    ) {
      // Закрытие — суммируем netPnl (после комиссии закрытия)
      exitPrice  = price;
      exitTime   = time;
      openQty   -= qty;
      closePnl  += netPnl;

      if (openQty <= 0.000001) {
        // Вычитаем комиссию открытия один раз на всю позицию
        const finalPnl = closePnl - openCommission;
        positions.push({
          symbol,
          direction:   openSide,
          entry_price: entryPrice,
          exit_price:  exitPrice,
          qty:         totalQty,
          sl_price:    0,
          tp1_price:   0,
          tp2_price:   0,
          pnl:         Math.round(finalPnl * 10000) / 10000,
          exit_reason: finalPnl >= 0 ? "TP" : "SL",
          entry_time:  new Date(entryTime).toISOString(),
          exit_time:   new Date(exitTime).toISOString(),
          is_open:     false,
          mode:        "live",
        });
        openQty  = 0;
        totalQty = 0;
        closePnl = 0;
        openCommission = 0;
      }
    } else {
      // Добавление к позиции (усреднение входа)
      entryPrice = (entryPrice * openQty + price * qty) / (openQty + qty);
      openQty   += qty;
      totalQty  += qty;
    }
  }

  // Незакрытая позиция
  if (openQty > 0.000001) {
    positions.push({
      symbol,
      direction:   openSide,
      entry_price: entryPrice,
      exit_price:  null,
      qty:         openQty,
      sl_price:    0,
      tp1_price:   0,
      tp2_price:   0,
      pnl:         null,
      exit_reason: null,
      entry_time:  new Date(entryTime).toISOString(),
      exit_time:   null,
      is_open:     true,
      mode:        "live",
    });
  }

  return positions;
}

export default router;

// GET /binance-sync/income/:symbol — сырые income записи с Binance
router.get("/income/:symbol", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const startTime = Date.now() - 30 * 24 * 60 * 60 * 1000;
    const income: any[] = await binanceGet("/fapi/v1/income", {
      symbol, incomeType: "REALIZED_PNL", limit: 1000, startTime,
    });
    res.json(income);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// GET /binance-sync/trades/:symbol — сырые userTrades с Binance  
router.get("/trades/:symbol", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const trades: any[] = await binanceGet("/fapi/v1/userTrades", {
      symbol, limit: 100,
    });
    res.json(trades);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});
