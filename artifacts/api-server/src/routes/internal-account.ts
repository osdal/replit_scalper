import { Router } from "express";
import { db } from "@workspace/db";
import { desc, eq } from "drizzle-orm";
import {
  userInternalAccountsTable,
  internalTransactionsTable,
  usersTable,
} from "@workspace/db/schema";
import { creditTradeProfit, getAccountStatement } from "../lib/internalAccount";

const router = Router();

// Дефолтный пользователь-хозяин (создаётся init-db при старте). Дашборд не
// требует отдельной авторизации — внутренний счёт доступен сразу.
const OWNER_USER_ID = 1;

async function ensureOwnerUserId(): Promise<number> {
  const [row] = await db
    .select({ id: usersTable.id })
    .from(usersTable)
    .where(eq(usersTable.email, "system@local"))
    .limit(1);
  return row ? Number(row.id) : OWNER_USER_ID;
}

// ---------- баланс + недавние транзакции ----------
router.get("/me", async (_req, res) => {
  try {
    const userId = await ensureOwnerUserId();
    const stmt = await getAccountStatement(userId);
    return res.json(stmt);
  } catch (e: any) {
    return res.status(500).json({ error: e.message || String(e) });
  }
});

// ---------- полная выписка ----------
router.get("/statement", async (req, res) => {
  try {
    const userId = await ensureOwnerUserId();
    const limit = Math.min(Number(req.query.limit) || 200, 1000);
    const acct = await db
      .select()
      .from(userInternalAccountsTable)
      .where(eq(userInternalAccountsTable.user_id, userId))
      .limit(1);
    if (acct.length === 0) {
      return res.status(404).json({ error: "Счёт не найден" });
    }
    const txns = await db
      .select()
      .from(internalTransactionsTable)
      .where(eq(internalTransactionsTable.user_id, userId))
      .orderBy(desc(internalTransactionsTable.created_at))
      .limit(limit);
    return res.json({
      account: {
        ...acct[0],
        balance_usd: Number(acct[0].balance_usd),
        high_water_mark_usd: Number(acct[0].high_water_mark_usd),
      },
      transactions: (txns as any[]).map((t: any) => ({
        ...t,
        amount_usd: Number(t.amount_usd),
        balance_after_usd: Number(t.balance_after_usd),
      })),
    });
  } catch (e: any) {
    return res.status(500).json({ error: e.message || String(e) });
  }
});

// ---------- внутреннее начисление прибыли по сделке (вызывается из бота/API при закрытии) ----------
router.post("/credit", async (req, res) => {
  try {
    const { tradeId, pnlUsd, symbol, userId } = req.body as {
      tradeId?: number;
      pnlUsd?: number;
      symbol?: string;
      userId?: number;
    };
    if (tradeId === undefined || pnlUsd === undefined) {
      return res.status(400).json({ error: "Нужны tradeId и pnlUsd" });
    }
    const result = await creditTradeProfit({
      tradeId: Number(tradeId),
      pnlUsd: Number(pnlUsd),
      symbol,
      userId: userId ? Number(userId) : undefined,
    });
    if (!result) {
      return res.json({ ok: true, credited: false, reason: "pnl <= 0" });
    }
    return res.json({ ok: true, ...result });
  } catch (e: any) {
    return res.status(500).json({ error: e.message || String(e) });
  }
});

export default router;
