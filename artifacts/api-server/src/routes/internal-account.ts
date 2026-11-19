import { Router } from "express";
import { db } from "@workspace/db";
import { desc, eq } from "drizzle-orm";
import {
  userInternalAccountsTable,
  internalTransactionsTable,
} from "@workspace/db/schema";
import { creditTradeProfit, createUser, findUserByEmail, getAccountStatement } from "../lib/internalAccount";
import { scryptSync, randomBytes, timingSafeEqual } from "crypto";

const router = Router();

// ---------- простая in-memory сессия (токен -> {userId, expiresAt}) ----------
const SESSIONS = new Map<string, { userId: number; expiresAt: number }>();
const SESSION_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 дней

function generateToken(): string {
  return randomBytes(24).toString("hex");
}

/**
 * Ручное начисление (/credit) — отладочный роут. По умолчанию ВЫКЛЮЧЕН:
 * работает только если в окружении задан INTERNAL_ACCOUNT_ADMIN_TOKEN и
 * запрос принёс его в заголовке x-admin-token. Иначе любой, кто дотянется
 * до API, мог бы начислить себе произвольный баланс.
 */
const ADMIN_TOKEN = process.env.INTERNAL_ACCOUNT_ADMIN_TOKEN || "";

function requireAdmin(req: any, res: any, next: any) {
  if (!ADMIN_TOKEN) {
    return res.status(403).json({
      error: "Ручное начисление отключено: задайте INTERNAL_ACCOUNT_ADMIN_TOKEN",
    });
  }
  const got = Buffer.from(String(req.headers["x-admin-token"] ?? ""));
  const expected = Buffer.from(ADMIN_TOKEN);
  if (got.length !== expected.length || !timingSafeEqual(got, expected)) {
    return res.status(401).json({ error: "Неверный x-admin-token" });
  }
  next();
}

function requireAuth(req: any, res: any, next: any) {
  const token = req.headers["x-session-token"] as string | undefined;
  if (!token) return res.status(401).json({ error: "Требуется авторизация" });
  const sess = SESSIONS.get(token);
  if (!sess || sess.expiresAt < Date.now()) {
    SESSIONS.delete(token);
    return res.status(401).json({ error: "Сессия истекла или неверна" });
  }
  (req as any).sessionUserId = sess.userId;
  sess.expiresAt = Date.now() + SESSION_TTL_MS;
  next();
}

function hashPassword(password: string): string {
  const salt = randomBytes(16).toString("hex");
  const derived = scryptSync(password, salt, 64);
  return `${salt}:${derived.toString("hex")}`;
}

function verifyPassword(password: string, stored: string): boolean {
  const [salt, hash] = stored.split(":");
  if (!salt || !hash) return false;
  const derived = scryptSync(password, salt, 64);
  return timingSafeEqual(Buffer.from(hash, "hex"), derived);
}

// ---------- регистрация ----------
router.post("/register", async (req, res) => {
  try {
    const { email, password } = req.body as { email?: string; password?: string };
    if (!email || !password) {
      return res.status(400).json({ error: "Нужны email и пароль" });
    }
    const passwordHash = hashPassword(password);
    const user = await createUser(email, passwordHash, "buyer");
    const token = generateToken();
    SESSIONS.set(token, { userId: Number(user.id), expiresAt: Date.now() + SESSION_TTL_MS });
    return res.json({ ok: true, user: { id: user.id, email: user.email }, token });
  } catch (e: any) {
    return res.status(400).json({ error: e.message || String(e) });
  }
});

// ---------- вход ----------
router.post("/login", async (req, res) => {
  try {
    const { email, password } = req.body as { email?: string; password?: string };
    if (!email || !password) {
      return res.status(400).json({ error: "Нужны email и пароль" });
    }
    const user = await findUserByEmail(email);
    if (!user) {
      return res.status(401).json({ error: "Неверный email или пароль" });
    }
    if (!verifyPassword(password, user.password_hash || "")) {
      return res.status(401).json({ error: "Неверный email или пароль" });
    }
    const token = generateToken();
    SESSIONS.set(token, { userId: Number(user.id), expiresAt: Date.now() + SESSION_TTL_MS });
    return res.json({ ok: true, user: { id: user.id, email: user.email }, token });
  } catch (e: any) {
    return res.status(500).json({ error: e.message || String(e) });
  }
});

// ---------- баланс + недавние транзакции ----------
router.get("/me", requireAuth, async (req, res) => {
  try {
    const userId = (req as any).sessionUserId;
    const stmt = await getAccountStatement(userId);
    return res.json(stmt);
  } catch (e: any) {
    return res.status(500).json({ error: e.message || String(e) });
  }
});

// ---------- полная выписка ----------
router.get("/statement", requireAuth, async (req, res) => {
  try {
    const userId = (req as any).sessionUserId;
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

// ---------- внутреннее начисление прибыли по сделке (админ/отладка) ----------
// Штатное начисление идёт автоматически из PATCH /trades/:id, этот роут нужен
// только для ручных операций. Защищён x-admin-token (env INTERNAL_ACCOUNT_ADMIN_TOKEN).
router.post("/credit", requireAdmin, async (req, res) => {
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
