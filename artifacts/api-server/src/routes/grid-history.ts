import { Router } from "express";
import { db, gridHistoryTable, GRID_HISTORY_CREATE_SQL, GRID_HISTORY_MIGRATIONS } from "@workspace/db";
import { desc, sql } from "drizzle-orm";
import { notifyTokenGuard } from "../middlewares/notifyAuth";
import { logger } from "../lib/logger";

const router = Router();

// Таблица создаётся лениво, чтобы не требовать ручного init-db. DDL — из схемы
// (@workspace/db), чтобы список колонок не расходился с Drizzle и init-db.
const ensureTable = (async () => {
  await db.run(sql.raw(GRID_HISTORY_CREATE_SQL));
  for (const migration of GRID_HISTORY_MIGRATIONS) {
    await db.run(sql.raw(migration)).catch(() => {});
  }
})().catch((e) => {
  logger.error({ err: e }, "grid_history table ensure failed");
});

function num(v: unknown, fallback = 0): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

router.post("/", notifyTokenGuard, async (req, res) => {
  try {
    await ensureTable;
    const b = req.body || {};
    const uid = String(b.uid || "").trim();
    if (!uid) {
      return res.status(400).json({ ok: false, error: "uid required" });
    }
    const now = new Date().toISOString();
    await db.run(sql`INSERT OR IGNORE INTO grid_history
      (uid, symbol, timeframe, phase, exit_reason, tp_pct, gate, levels, lo, hi, mid, entry, exit, pnl, pnl_usd, positions, created_at, finished_at, order_size_usd)
      VALUES (
        ${uid},
        ${String(b.symbol ?? "")},
        ${String(b.timeframe ?? "")},
        ${String(b.phase ?? "done")},
        ${String(b.exitReason ?? "tp")},
        ${num(b.tpPct)},
        ${num(b.gate)},
        ${num(b.levels)},
        ${num(b.lo)},
        ${num(b.hi)},
        ${num(b.mid)},
        ${num(b.entry)},
        ${b.exit != null ? num(b.exit) : null},
        ${b.pnl != null ? num(b.pnl) : null},
        ${b.pnlUsd != null ? num(b.pnlUsd) : null},
        ${b.positions != null ? Math.round(num(b.positions)) : null},
        ${String(b.createdAt ?? now)},
        ${String(b.finishedAt ?? now)},
        ${b.orderSizeUsd != null ? num(b.orderSizeUsd) : null}
      )`);
    return res.json({ ok: true });
  } catch (e: any) {
    return res.status(500).json({ ok: false, error: e?.message || "failed" });
  }
});

router.get("/", async (req, res) => {
  try {
    await ensureTable;
    const limitRaw = Number(req.query.limit);
    const limit = Number.isFinite(limitRaw) && limitRaw > 0 ? Math.min(limitRaw, 1000) : 200;
    const rows = await db
      .select()
      .from(gridHistoryTable)
      .orderBy(desc(gridHistoryTable.id))
      .limit(limit);
    return res.json({ results: rows, total: rows.length });
  } catch (e: any) {
    return res.status(500).json({ ok: false, error: e?.message || "failed" });
  }
});

router.delete("/", notifyTokenGuard, async (req, res) => {
  try {
    await ensureTable;
    const filter = String(req.query.filter ?? "").trim();
    if (filter === "no-position") {
      const result = await db.run(
        sql`DELETE FROM grid_history WHERE COALESCE(positions, 0) = 0`,
      );
      return res.json({ deleted: result.rowsAffected });
    }
    if (filter === "cancel") {
      const result = await db.run(
        sql`DELETE FROM grid_history WHERE exit_reason = 'cancel'`,
      );
      return res.json({ deleted: result.rowsAffected });
    }
    if (filter) {
      return res.status(400).json({ ok: false, error: "unknown filter" });
    }
    await db.run(sql`DELETE FROM grid_history`);
    return res.json({ ok: true });
  } catch (e: any) {
    return res.status(500).json({ ok: false, error: e?.message || "failed" });
  }
});

export default router;
