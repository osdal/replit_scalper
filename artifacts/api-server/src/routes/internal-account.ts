import { Router, type Request, type Response } from "express";
import { db } from "@workspace/db";
import { clientsTable, clientAccountsTable } from "@workspace/db/schema";
import { eq } from "drizzle-orm";
import { getClientStatement, setClientBalance, topUpClient } from "../lib/clientAccount";

const router = Router();

// Дашборд показывает счёт владельца (system@local) без отдельного логина.
// Биллинг ключуется по clients.id, а не по legacy users.id.
async function ownerClientId(): Promise<number> {
  const [row] = await db
    .select({ id: clientsTable.id })
    .from(clientsTable)
    .where(eq(clientsTable.email, "system@local"))
    .limit(1);
  if (!row) throw new Error("Клиент system@local не найден (init-db не применялся?)");
  return Number(row.id);
}

/**
 * Ручное изменение баланса — только с админ-токеном из .env.
 * Без заданного INTERNAL_ACCOUNT_ADMIN_TOKEN пополнение отключено:
 * порт 5001 слушается на всех интерфейсах, и открытый money-endpoint
 * был бы дырой в безопасности.
 */
function requireAdmin(req: Request, res: Response): boolean {
  const expected = String(process.env.INTERNAL_ACCOUNT_ADMIN_TOKEN || "").trim();
  if (!expected) {
    res.status(503).json({
      error: "Пополнение отключено: задайте INTERNAL_ACCOUNT_ADMIN_TOKEN в .env и перезапустите API",
    });
    return false;
  }
  if (String(req.header("x-admin-token") || "") !== expected) {
    res.status(403).json({ error: "Неверный x-admin-token" });
    return false;
  }
  return true;
}

// ---------- баланс + недавние операции ----------
router.get("/me", async (_req, res) => {
  try {
    return res.json(await getClientStatement(await ownerClientId(), 100));
  } catch (e: any) {
    return res.status(500).json({ error: e.message || String(e) });
  }
});

// ---------- полная выписка ----------
router.get("/statement", async (req, res) => {
  try {
    const limit = Math.min(Number(req.query.limit) || 200, 1000);
    return res.json(await getClientStatement(await ownerClientId(), limit));
  } catch (e: any) {
    return res.status(500).json({ error: e.message || String(e) });
  }
});

// ---------- список клиентов (для выбора счёта в дашборде) ----------
router.get("/clients", async (_req, res) => {
  try {
    const rows = await db
      .select({
        id: clientsTable.id,
        email: clientsTable.email,
        status: clientsTable.status,
        role: clientsTable.role,
      })
      .from(clientsTable)
      .orderBy(clientsTable.id);
    const balances = await db
      .select()
      .from(clientAccountsTable);
    const byClient = new Map(balances.map((b: any) => [Number(b.client_id), Number(b.balance_usd)]));
    return res.json({
      clients: rows.map((r) => ({
        id: Number(r.id),
        email: r.email,
        status: r.status,
        role: r.role,
        balance_usd: byClient.get(Number(r.id)) ?? 0,
      })),
    });
  } catch (e: any) {
    return res.status(500).json({ error: e.message || String(e) });
  }
});

// ---------- карточка клиента: счёт + выписка ----------
router.get("/clients/:id", async (req, res) => {
  try {
    const clientId = Number(req.params.id);
    if (!Number.isFinite(clientId) || clientId <= 0) {
      return res.status(400).json({ error: "Некорректный clientId" });
    }
    const [client] = await db
      .select({ id: clientsTable.id, email: clientsTable.email, status: clientsTable.status, role: clientsTable.role })
      .from(clientsTable)
      .where(eq(clientsTable.id, clientId))
      .limit(1);
    if (!client) return res.status(404).json({ error: "Клиент не найден" });
    const stmt = await getClientStatement(clientId, 200);
    return res.json({ client, ...stmt });
  } catch (e: any) {
    return res.status(500).json({ error: e.message || String(e) });
  }
});

// ---------- ручное пополнение (админ) ----------
router.post("/topup", async (req, res) => {
  if (!requireAdmin(req, res)) return;
  try {
    const { amountUsd, clientId, reason, idempotencyKey } = req.body as {
      amountUsd?: number;
      clientId?: number;
      reason?: string;
      idempotencyKey?: string;
    };
    if (amountUsd === undefined) {
      return res.status(400).json({ error: "Нужен amountUsd (сумма пополнения > 0)" });
    }
    const targetClientId = clientId !== undefined ? Number(clientId) : await ownerClientId();
    if (!Number.isFinite(targetClientId) || targetClientId <= 0) {
      return res.status(400).json({ error: "Некорректный clientId" });
    }
    const result = await topUpClient({
      clientId: targetClientId,
      amountUsd: Number(amountUsd),
      reason,
      actor: "admin",
      idempotencyKey,
    });
    return res.json({ ok: true, ...result });
  } catch (e: any) {
    return res.status(400).json({ error: e.message || String(e) });
  }
});

// ---------- установить баланс клиента (корректировка на разницу) ----------
router.post("/clients/:id/balance", async (req, res) => {
  if (!requireAdmin(req, res)) return;
  try {
    const clientId = Number(req.params.id);
    const { balanceUsd, reason } = req.body as { balanceUsd?: number; reason?: string };
    if (!Number.isFinite(clientId) || clientId <= 0) {
      return res.status(400).json({ error: "Некорректный clientId" });
    }
    if (balanceUsd === undefined || !Number.isFinite(Number(balanceUsd))) {
      return res.status(400).json({ error: "Нужен balanceUsd" });
    }
    const target = Math.round(Number(balanceUsd) * 10000) / 10000;
    const result = await setClientBalance({
      clientId,
      balanceUsd: target,
      reason: reason || "Ручная корректировка баланса",
      actor: "admin",
    });
    return res.json({ ok: true, ...result });
  } catch (e: any) {
    return res.status(400).json({ error: e.message || String(e) });
  }
});

// ---------- старый роут начисления прибыли больше не поддерживается ----------
// Модель A (прибыль зачислялась на счёт за вычетом 10%) отменена:
// прибыль остаётся на Binance, с Internal Account списывается только комиссия.
router.post("/credit", (_req, res) => {
  return res.status(410).json({
    error: "Модель изменена: прибыль на внутренний счёт не зачисляется",
    hint: "Для пополнения используйте POST /api/internal-account/topup с x-admin-token",
  });
});

export default router;
