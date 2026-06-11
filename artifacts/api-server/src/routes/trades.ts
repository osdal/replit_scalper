import { Router } from "express";
import { db, tradesTable } from "@workspace/db";
import { eq, desc, count, sql } from "drizzle-orm";

const router = Router();

// GET /trades
router.get("/", async (req, res) => {
  const symbol = req.query.symbol as string | undefined;
  const limit  = parseInt(req.query.limit as string) || 50;
  const offset = parseInt(req.query.offset as string) || 0;

  const query = db.select().from(tradesTable).orderBy(desc(tradesTable.entry_time));
  if (symbol) query.where(eq(tradesTable.symbol, symbol.toUpperCase()));

  const [trades, [{ total }]] = await Promise.all([
    query.limit(limit).offset(offset),
    db.select({ total: count() }).from(tradesTable),
  ]);

  res.json({ trades, total: Number(total) });
});

// GET /trades/stats
router.get("/stats", async (_req, res) => {
  const stats = await db.execute(sql`
    SELECT
      symbol,
      COUNT(*)::int                                        AS total,
      COUNT(*) FILTER (WHERE pnl > 0)::int                AS wins,
      COUNT(*) FILTER (WHERE pnl <= 0)::int               AS losses,
      ROUND((COUNT(*) FILTER (WHERE pnl > 0) * 100.0 / NULLIF(COUNT(*), 0))::numeric, 1) AS win_rate,
      ROUND(COALESCE(SUM(pnl), 0)::numeric, 4)            AS total_pnl,
      ROUND(COALESCE(AVG(pnl) FILTER (WHERE pnl > 0), 0)::numeric, 4) AS avg_win,
      ROUND(COALESCE(AVG(pnl) FILTER (WHERE pnl <= 0), 0)::numeric, 4) AS avg_loss
    FROM trades
    WHERE is_open = false
    GROUP BY symbol
    ORDER BY symbol
  `);
  res.json(stats.rows);
});

export default router;

// POST /trades — записать новую сделку (вызывается ботом)
router.post("/", async (req, res) => {
  const [trade] = await db.insert(tradesTable).values(req.body).returning();
  res.status(201).json(trade);
});

// PATCH /trades/:id — обновить сделку (закрытие)
router.patch("/:id", async (req, res) => {
  const id = parseInt(req.params.id);
  const [trade] = await db
    .update(tradesTable)
    .set(req.body)
    .where(eq(tradesTable.id, id))
    .returning();
  if (!trade) return res.status(404).json({ error: "Trade not found" });
  res.json(trade);
});
