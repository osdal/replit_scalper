import { Router } from "express";
import { db, tradesTable } from "@workspace/db";
import { eq, desc, sql } from "drizzle-orm";

const router = Router();

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

router.post("/", async (req, res) => {
  try {
    const [trade] = await db.insert(tradesTable).values(req.body).returning();
    res.status(201).json(trade);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.patch("/:id", async (req, res) => {
  try {
    const id = parseInt(req.params.id);
    const [trade] = await db.update(tradesTable).set(req.body).where(eq(tradesTable.id, id)).returning();
    if (!trade) return res.status(404).json({ error: "Trade not found" });
    res.json(trade);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

export default router;
