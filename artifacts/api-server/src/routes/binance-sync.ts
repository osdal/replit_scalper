/**
 * Синхронизация истории сделок с Binance Futures API.
 * Использует прямые HTTP запросы с HMAC подписью.
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

  const resp = await fetch(url, {
    headers: { "X-MBX-APIKEY": API_KEY },
  });
  if (!resp.ok) throw new Error(`Binance API error: ${resp.status} ${await resp.text()}`);
  return resp.json();
}

// POST /binance-sync — синхронизировать историю с Binance
router.post("/", async (_req, res) => {
  try {
    if (!API_KEY || !API_SECRET) {
      return res.status(400).json({ error: "BINANCE_API_KEY and BINANCE_API_SECRET not configured" });
    }

    // Очищаем старые записи из локальной БД
    await db.run(sql`DELETE FROM trades`);

    let total = 0;

    for (const symbol of SYMBOLS) {
      try {
        // Получаем историю позиций через userTrades
        const trades: any[] = await binanceGet("/fapi/v1/userTrades", {
          symbol,
          limit: 500,
        });

        // Группируем сделки в позиции
        const positions = groupTrades(trades, symbol);

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

// GET /binance-sync/positions — текущие открытые позиции с Binance
router.get("/positions", async (_req, res) => {
  try {
    const positions: any[] = await binanceGet("/fapi/v2/positionRisk");
    const open = positions.filter(p => parseFloat(p.positionAmt) !== 0);
    res.json(open);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// GET /binance-sync/income — история PnL с Binance
router.get("/income/:symbol", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const income: any[] = await binanceGet("/fapi/v1/income", {
      symbol,
      incomeType: "REALIZED_PNL",
      limit: 100,
    });
    res.json(income);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/**
 * Группирует userTrades в позиции.
 * Binance userTrades содержит отдельные исполнения — объединяем их в открытие/закрытие.
 */
function groupTrades(trades: any[], symbol: string) {
  const positions: any[] = [];
  let openQty = 0;
  let openSide = "";
  let entryPrice = 0;
  let entryTime = "";
  let totalQty = 0;
  let realizedPnl = 0;

  for (const t of trades) {
    const qty  = parseFloat(t.qty);
    const price = parseFloat(t.price);
    const side = t.side; // BUY or SELL
    const pnl  = parseFloat(t.realizedPnl || "0");
    const time = new Date(t.time).toISOString();

    realizedPnl += pnl;

    if (openQty === 0) {
      // Открытие новой позиции
      openSide   = side === "BUY" ? "LONG" : "SHORT";
      entryPrice = price;
      entryTime  = time;
      openQty    = qty;
      totalQty   = qty;
    } else if (
      (openSide === "LONG"  && side === "SELL") ||
      (openSide === "SHORT" && side === "BUY")
    ) {
      // Закрытие позиции
      openQty -= qty;
      if (openQty <= 0.000001) {
        positions.push({
          symbol,
          direction: openSide,
          entry_price: entryPrice,
          exit_price:  price,
          qty:         totalQty,
          sl_price:    0,
          tp1_price:   0,
          tp2_price:   0,
          pnl:         realizedPnl,
          exit_reason: realizedPnl >= 0 ? "TP" : "SL",
          entry_time:  entryTime,
          exit_time:   time,
          is_open:     false,
          mode:        "live",
        });
        openQty = 0;
        realizedPnl = 0;
      }
    } else {
      // Добавление к позиции
      entryPrice = (entryPrice * openQty + price * qty) / (openQty + qty);
      openQty   += qty;
      totalQty  += qty;
    }
  }

  // Если есть незакрытая позиция
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
      entry_time:  entryTime,
      exit_time:   null,
      is_open:     true,
      mode:        "live",
    });
  }

  return positions;
}

export default router;
