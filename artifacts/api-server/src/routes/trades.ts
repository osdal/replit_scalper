import { Router } from "express";
import { db, tradesTable, recoveryChainsTable } from "@workspace/db";
import { eq, desc, sql } from "drizzle-orm";
import { requireCapability } from "../lib/auth";

const router = Router();

// DELETE /trades — удалить все сделки
router.delete("/", requireCapability("admin_actions"), async (_req, res) => {
  try {
    const result = await db.delete(tradesTable).returning();
    res.json({ deleted: result.length });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// DELETE /trades/clear-tp1 — удалить все TP1 записи (для миграции)
router.delete("/clear-tp1", requireCapability("admin_actions"), async (_req, res) => {
  try {
    const result = await db.delete(tradesTable).where(eq(tradesTable.exit_reason, "TP1")).returning();
    res.json({ deleted: result.length });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.get("/", async (req, res) => {
  try {
    const symbol = req.query.symbol as string | undefined;
    const limit  = parseInt(req.query.limit as string) || 50;
    const offset = parseInt(req.query.offset as string) || 0;
    let query = db.select().from(tradesTable).orderBy(desc(tradesTable.entry_time)).limit(limit).offset(offset);
    if (symbol) query = query.where(eq(tradesTable.symbol, symbol.toUpperCase())) as typeof query;
    const trades = await query;
    const [{ total }] = await db.select({ total: sql<number>`count(*)` }).from(tradesTable);
    res.json({ trades, total });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// PATCH /trades/:id — обновить сделку (закрытие, синхронизация PnL)
router.patch("/:id", requireCapability("control_bots"), async (req, res) => {
  try {
    const id = parseInt(req.params.id);
    const updates: Record<string, unknown> = {};
    const allowed = ["exit_price", "pnl", "exit_reason", "qty", "is_open", "exit_time"];
    for (const key of allowed) {
      if (req.body[key] !== undefined) updates[key] = req.body[key];
    }
    if (Object.keys(updates).length === 0) {
      return res.status(400).json({ error: "No valid fields to update" });
    }
    const result = await db.update(tradesTable).set(updates).where(eq(tradesTable.id, id)).returning();
    if (result.length === 0) {
      return res.status(404).json({ error: "Trade not found" });
    }
    res.json(result[0]);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// DELETE /trades/:id — удалить конкретную сделку
router.delete("/:id", requireCapability("admin_actions"), async (req, res) => {
  try {
    const id = parseInt(req.params.id);
    const result = await db.delete(tradesTable).where(eq(tradesTable.id, id)).returning();
    if (result.length === 0) {
      return res.status(404).json({ error: "Trade not found" });
    }
    res.json({ deleted: 1, id });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

router.get("/stats", async (_req, res) => {
  try {
    const stats = await db.run(sql`
      SELECT
        symbol,
        COUNT(*)                                                       AS total,
        COUNT(CASE WHEN pnl > 0 THEN 1 END)                           AS wins,
        COUNT(CASE WHEN pnl <= 0 THEN 1 END)                          AS losses,
        ROUND(COUNT(CASE WHEN pnl > 0 THEN 1 END)*100.0/MAX(COUNT(*),1),1) AS win_rate,
        ROUND(COALESCE(SUM(pnl),0),4)                                 AS total_pnl,
        ROUND(COALESCE(AVG(CASE WHEN pnl>0 THEN pnl END),0),4)        AS avg_win,
        ROUND(COALESCE(AVG(CASE WHEN pnl<=0 THEN pnl END),0),4)       AS avg_loss
      FROM trades WHERE is_open=0 GROUP BY symbol ORDER BY symbol
    `);
    res.json(stats.rows);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.get("/export", async (_req, res) => {
  try {
    const trades = await db
      .select()
      .from(tradesTable)
      .where(eq(tradesTable.is_open, false))
      .orderBy(desc(tradesTable.entry_time));

    const headers = [
      "ID", "Symbol", "Direction", "Entry Price", "Exit Price",
      "Quantity", "PnL", "Exit Reason", "Entry Time", "Exit Time", "Mode"
    ];

    const rows = trades.map(t => [
      t.id, t.symbol, t.direction, t.entry_price,
      t.exit_price || "", t.qty, t.pnl || "",
      t.exit_reason || "", t.entry_time, t.exit_time || "", t.mode
    ]);

    const csv = [
      headers.join(","),
      ...rows.map(row => row.map(cell =>
        typeof cell === 'string' && cell.includes(',') ? `"${cell}"` : cell
      ).join(","))
    ].join("\n");

    const timestamp = new Date().toISOString().split('T')[0];
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="trades-export-${timestamp}.csv"`);
    res.send(csv);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

router.post("/", requireCapability("control_bots"), async (req, res) => {
  try {
    const [trade] = await db.insert(tradesTable).values(req.body).returning();
    res.status(201).json(trade);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// POST /trades/sync-closed — синхронизировать закрытые позиции с биржей
router.post("/sync-closed", requireCapability("control_bots"), async (_req, res) => {
  try {
    const API_KEY = process.env.BINANCE_API_KEY || "";
    const API_SECRET = process.env.BINANCE_API_SECRET || "";
    
    if (!API_KEY || !API_SECRET) {
      return res.status(400).json({ error: "BINANCE_API_KEY and BINANCE_API_SECRET not configured" });
    }

    // Получаем открытые сделки из БД
    const openTrades = await db.select().from(tradesTable).where(eq(tradesTable.is_open, true));
    
    if (openTrades.length === 0) {
      return res.json({ synced: 0, message: "No open trades in DB" });
    }

    // Получаем текущие позиции с биржи
    const crypto = await import("crypto");
    const BASE_URL = "https://fapi.binance.com";
    
    function sign(params: Record<string, string | number>): string {
      const qs = Object.entries(params).map(([k, v]) => `${k}=${v}`).join("&");
      return crypto.createHmac("sha256", API_SECRET).update(qs).digest("hex");
    }

    function binanceGet(path: string, params: Record<string, string | number> = {}): Promise<any> {
      const ts = Date.now();
      const p = { ...params, timestamp: ts };
      const signature = sign(p);
      const qs = Object.entries(p).map(([k, v]) => `${k}=${v}`).join("&");
      const url = `${BASE_URL}${path}?${qs}&signature=${signature}`;
      return fetch(url, { headers: { "X-MBX-APIKEY": API_KEY } }).then(r => r.json());
    }

    const positions = await binanceGet("/fapi/v2/positionRisk");
    
    // Создаём карту открытых позиций на бирже
    const exchangePositions = new Map<string, number>();
    for (const pos of positions) {
      const amt = Math.abs(parseFloat(pos.positionAmt));
      if (amt > 0) {
        exchangePositions.set(pos.symbol, amt);
      }
    }

    // Закрываем сделки которых нет на бирже
    let closed = 0;
    const now = new Date().toISOString();
    
    for (const trade of openTrades) {
      if (!exchangePositions.has(trade.symbol)) {
        // Получаем PnL из userTrades
        let pnl = 0;
        let exitPrice = trade.entry_price;
        try {
          const userTrades = await binanceGet("/fapi/v1/userTrades", {
            symbol: trade.symbol,
            limit: 20,
          });
          // Ищем сделки которые закрыли нашу позицию
          for (const ut of userTrades) {
            const utTime = new Date(ut.time).toISOString();
            // Берём только сделки после открытия позиции
            if (utTime > trade.entry_time && ut.realizedPnl) {
              // ВАЖНО: Binance считает Realized PNL как реализованный pnl
              // МИНУС уплаченную комиссию по каждой сделке (мы это
              // подтвердили эмпирически в binance-sync.ts groupPositions).
              // realizedPnl сам по себе не включает комиссию — без вычитания
              // эта сумма систематически переоценивает прибыль (или
              // недооценивает убыток) на величину комиссии каждой сделки.
              const realizedPnl = parseFloat(ut.realizedPnl) || 0;
              const commission = parseFloat(ut.commission || "0");
              const commissionUsd = ut.commissionAsset === "USDT" ? commission : 0;
              pnl += realizedPnl - commissionUsd;
            }
          }
          // Рассчитываем exit_price из последней сделки
          if (userTrades.length > 0) {
            const lastTrade = userTrades[userTrades.length - 1];
            exitPrice = parseFloat(lastTrade.price) || trade.entry_price;
          }
        } catch (e) {
          // ignore
        }

        // Позиция закрыта на бирже — обновляем БД
        await db.update(tradesTable)
          .set({
            is_open: false,
            exit_time: now,
            exit_reason: "exchange_closed",
            exit_price: exitPrice,
            pnl: pnl,
          })
          .where(eq(tradesTable.id, trade.id));
        closed++;

        // Если убыток — создаём recovery chain
        if (pnl < 0) {
          await db.insert(recoveryChainsTable).values({
            debt_amount: Math.abs(pnl),
            status: "free",
            created_at: now,
            updated_at: now,
          });
        }
      }
    }

    res.json({ synced: closed, total: openTrades.length });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export default router;
