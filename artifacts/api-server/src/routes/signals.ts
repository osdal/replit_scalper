import { Router } from "express";
import { db, signalsTable } from "@workspace/db";
import { and, eq, isNull, sql } from "drizzle-orm";

const router = Router();

// Публикация сигнала (вызывает testnet-бот на live-API).
router.post("/", async (req, res) => {
  try {
    const b = req.body || {};
    if (!b.symbol || !b.direction || b.entry_price == null) {
      return res.status(400).json({ error: "symbol, direction, entry_price required" });
    }
    const [row] = await db.insert(signalsTable).values({
      symbol: String(b.symbol).toUpperCase(),
      kind: String(b.kind || "entry"),
      direction: String(b.direction).toUpperCase(),
      entry_price: Number(b.entry_price),
      sl_price: b.sl_price != null ? Number(b.sl_price) : null,
      tp1_price: b.tp1_price != null ? Number(b.tp1_price) : null,
      tp2_price: b.tp2_price != null ? Number(b.tp2_price) : null,
      preset: b.preset != null ? String(b.preset) : null,
      source: b.source != null ? String(b.source) : "testnet",
      payload: b.payload != null ? JSON.stringify(b.payload) : null,
      created_at: new Date().toISOString(),
    }).returning();
    res.json(row);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// Непотреблённые сигналы по символу (FIFO).
router.get("/", async (req, res) => {
  try {
    const symbol = String(req.query.symbol || "").toUpperCase();
    const limit = Math.min(parseInt(String(req.query.limit || "20")) || 20, 100);
    const conds = [isNull(signalsTable.consumed_at)];
    if (symbol) conds.push(eq(signalsTable.symbol, symbol));
    const rows = await db.select().from(signalsTable)
      .where(and(...conds))
      .orderBy(signalsTable.id)
      .limit(limit);
    res.json({ signals: rows });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// Счётчики релея + последний сигнал (для индикатора в дашборде).
router.get("/stats", async (req, res) => {
  try {
    const symbol = String(req.query.symbol || "").toUpperCase();
    const scope = symbol ? eq(signalsTable.symbol, symbol) : undefined;
    const countWhere = async (cond: any): Promise<number> => {
      const where = scope && cond ? and(scope, cond) : (scope || cond);
      const [row] = await db.select({ n: sql<number>`COUNT(*)` }).from(signalsTable).where(where);
      return Number(row?.n ?? 0);
    };
    const pending = await countWhere(isNull(signalsTable.consumed_at));
    const consumed = await countWhere(eq(signalsTable.status, "consumed"));
    const skipped = await countWhere(eq(signalsTable.status, "skipped"));
    const last = (await db.select().from(signalsTable).where(scope)
      .orderBy(sql`${signalsTable.id} DESC`).limit(1))[0] ?? null;
    const lastConsumed = (await db.select().from(signalsTable)
      .where(scope ? and(scope, eq(signalsTable.status, "consumed")) : eq(signalsTable.status, "consumed"))
      .orderBy(sql`${signalsTable.id} DESC`).limit(1))[0] ?? null;
    res.json({ symbol: symbol || null, pending, consumed, skipped, last, last_consumed: lastConsumed });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.post("/:id/ack", async (req, res) => {
  try {
    const id = parseInt(req.params.id);
    const body = req.body || {};
    const status = ["consumed", "skipped", "pending"].includes(String(body.status))
      ? String(body.status) : "consumed";
    const note = body.note != null ? String(body.note).slice(0, 300) : null;
    const [row] = await db.update(signalsTable)
      .set({ consumed_at: new Date().toISOString(), status, note })
      .where(eq(signalsTable.id, id)).returning();
    if (!row) return res.status(404).json({ error: "Signal not found" });
    res.json({ success: true, id, status });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

export default router;
