/**
 * Синхронизация истории сделок с Binance Futures API.
 * Берёт реальный PnL из income history (после комиссий и funding).
 */
import { Router } from "express";
import { db, tradesTable, botsTable } from "@workspace/db";
import { sql } from "drizzle-orm";
import crypto from "crypto";
import fs from "fs";

const router = Router();

const API_KEY    = process.env.BINANCE_API_KEY || "";
const API_SECRET = process.env.BINANCE_API_SECRET || "";
const BASE_URL   = "https://fapi.binance.com";

async function getSymbols(): Promise<string[]> {
  try {
    const bots = await db.select({ symbol: botsTable.symbol }).from(botsTable);
    return bots.map(b => b.symbol).sort();
  } catch {
    // Fallback: read from config files
    try {
      const configs = fs.readdirSync("bot").filter((f: string) => /^config_\w+\.yaml$/.test(f));
      return configs.map(f => f.replace("config_", "").replace(".yaml", "").toUpperCase() + "USDT").sort();
    } catch {
      return [];
    }
  }
}

function sign(params: Record<string, string | number>): string {
  const qs = Object.entries(params).map(([k, v]) => `${k}=${v}`).join("&");
  return crypto.createHmac("sha256", API_SECRET).update(qs).digest("hex");
}

async function binanceGet(path: string, params: Record<string, string | number> = {}): Promise<any> {
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
    }

    const symbols = await getSymbols();
    if (symbols.length === 0) {
      return res.status(400).json({ error: "No bot configs found" });
    }

    // Get existing trades to match against
    const existing = await db.select({
      id: tradesTable.id,
      symbol: tradesTable.symbol,
      direction: tradesTable.direction,
      entry_time: tradesTable.entry_time,
    }).from(tradesTable);

    let updated = 0;
    let skipped = 0;

    for (const symbol of symbols) {
      try {
        const userTrades: any[] = await binanceGet("/fapi/v1/userTrades", {
          symbol, limit: 1000,
        });

        if (!userTrades.length) continue;

        const positions = groupPositions(userTrades, symbol);

        for (const pos of positions) {
          // Find matching trade in DB (1-min entry window)
          const match = existing.find(e => {
            if (e.symbol !== pos.symbol || e.direction !== pos.direction) return false;
            try {
              const t1 = new Date(e.entry_time).getTime();
              const t2 = new Date(pos.entry_time).getTime();
              return Math.abs(t1 - t2) < 60000;
            } catch { return false; }
          });

          if (match) {
            // Update existing trade with real Binance data
            await db.update(tradesTable)
              .set({
                pnl: pos.pnl,
                exit_price: pos.is_open ? null : pos.exit_price,
                exit_reason: pos.is_open ? null : pos.exit_reason,
                exit_time: pos.exit_time,
                is_open: pos.is_open,
                status: pos.is_open ? "open" : "closed",
              })
              .where(eq(tradesTable.id, match.id));
            updated++;
          } else if (!pos.is_open) {
            // Only insert closed trades we don't have — historical cleanup
            await db.insert(tradesTable).values(pos);
            updated++;
          }
        }

        console.log(`[SYNC] ${symbol}: ${positions.length} positions (${updated} updated, ${skipped} skipped)`);
      } catch (e: any) {
        console.error(`[SYNC] Error for ${symbol}:`, e.message);
      }
    }

    res.json({ success: true, synced: updated, skipped, symbols: symbols.length });
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
          status:      "closed",
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
      status:      "open",
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
