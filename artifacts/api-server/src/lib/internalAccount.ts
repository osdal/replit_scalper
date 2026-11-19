import { db } from "@workspace/db";
import { userInternalAccountsTable, internalTransactionsTable, usersTable, botsTable, tradesTable } from "@workspace/db/schema";
import { and, eq, sql } from "drizzle-orm";

/** Комиссия платформы с прибыли (10%). Можно вынести в env/БД позже. */
const PLATFORM_COMMISSION_PCT = 10;

/**
 * Начисляет реализованную прибыль по закрытой сделке на внутренний счёт пользователя.
 *
 * Правила:
 *  - Учитываются только закрытые сделки с pnl > 0 (убытки не списываются, комиссия не начисляется).
 *  - High-water mark: прибыль сначала восстанавливает баланс до исторического максимума,
 *    и только сумма, побившая максимум, считается «новой прибылью» — с неё берётся 10%.
 *  - Это защищает от двойного взимания комиссии за одну и ту же прибыль после убытка.
 *
 * Пример:
 *   balance=1000, hwm=1000. Сделка +100 → новая прибыль 100, комиссия 10, зачислено 90.
 *   balance=1090, hwm=1090. Сделка -200 → счёт не меняется (убыток не списывается).
 *   balance=1090, hwm=1090. Сделка +150 → новая прибыль 150, комиссия 15, зачислено 135.
 *
 * Вызывается из API при закрытии сделки (PATCH /trades/:id) — бот не знает о комиссии.
 */
export interface CreditResult {
  credited: boolean;          // была ли зачислена сумма
  alreadyCredited?: boolean;  // по этой сделке прибыль уже начислялась раньше
  pnlUsd: number;             // реализованная прибыль по сделке
  newProfitUsd: number;       // прибыль, побившая HWM (основа для комиссии)
  commissionUsd: number;      // 10% от newProfit
  netCreditUsd: number;       // newProfit - commission (что реально зачислено)
  balanceBeforeUsd: number;
  balanceAfterUsd: number;
  highWaterMarkAfterUsd: number;
}

/** Уже начисляли прибыль по этой сделке? Защита от повторного PATCH. */
async function hasCreditForTrade(tradeId: number): Promise<boolean> {
  const [row] = await db
    .select({ id: internalTransactionsTable.id })
    .from(internalTransactionsTable)
    .where(and(
      eq(internalTransactionsTable.related_trade_id, tradeId),
      eq(internalTransactionsTable.type, "credit"),
    ))
    .limit(1);
  return Boolean(row);
}

function alreadyCreditedResult(pnlUsd: number): CreditResult {
  return {
    credited: false,
    alreadyCredited: true,
    pnlUsd: Number(pnlUsd.toFixed(4)),
    newProfitUsd: 0,
    commissionUsd: 0,
    netCreditUsd: 0,
    balanceBeforeUsd: 0,
    balanceAfterUsd: 0,
    highWaterMarkAfterUsd: 0,
  };
}

function isUniqueViolation(e: unknown): boolean {
  // drizzle оборачивает ошибку в DrizzleQueryError: код лежит в cause/cause.cause,
  // а message содержит только текст запроса. Проверяем и коды, и сообщения.
  const err = e as any;
  const codes = [
    err?.code,
    err?.rawCode,
    err?.cause?.code,
    err?.cause?.rawCode,
    err?.cause?.cause?.code,
    err?.cause?.cause?.rawCode,
  ];
  if (codes.some((c) => typeof c === "string" && /SQLITE_CONSTRAINT_UNIQUE/i.test(c))) {
    return true;
  }
  const text = [err?.message, err?.cause?.message, err?.cause?.cause?.message]
    .filter(Boolean)
    .join(" ");
  return /UNIQUE constraint failed|SQLITE_CONSTRAINT_UNIQUE/i.test(text);
}

/** Находит (или создаёт) внутренний счёт пользователя и возвращает его. */
async function ensureAccount(userId: number) {
  let acct = await db
    .select()
    .from(userInternalAccountsTable)
    .where(eq(userInternalAccountsTable.user_id, userId))
    .limit(1)
    .then((rows: any[]) => rows[0]);

  if (!acct) {
    const [inserted] = await db
      .insert(userInternalAccountsTable)
      .values({ user_id: userId, balance_usd: 0, high_water_mark_usd: 0 })
      .returning();
    acct = inserted;
  }
  return acct as { user_id: number; balance_usd: number; high_water_mark_usd: number };
}

/** Записывает одну транзакцию во внутреннюю историю. */
async function recordTxn({
  userId,
  type,
  amountUsd,
  balanceAfterUsd,
  relatedTradeId,
}: {
  userId: number;
  type: "credit" | "commission" | "debit" | "withdrawal";
  amountUsd: number;
  balanceAfterUsd: number;
  relatedTradeId?: number | null;
}) {
  await db.insert(internalTransactionsTable).values({
    user_id: userId,
    type,
    amount_usd: Number(amountUsd.toFixed(4)),
    balance_after_usd: Number(balanceAfterUsd.toFixed(4)),
    related_trade_id: relatedTradeId ?? null,
  });
}

/** Находит пользователя-владельца бота по символу сделки. */
async function findBotUserId(symbol: string): Promise<number | null> {
  const [bot] = await db
    .select({ user_id: botsTable.user_id })
    .from(botsTable)
    .where(eq(botsTable.symbol, symbol.toUpperCase()))
    .limit(1);
  return bot?.user_id ?? null;
}

/** Гарантирует существование дефолтного пользователя (для ботов без явного владельца). */
async function ensureDefaultUser(): Promise<number> {
  let [user] = await db.select().from(usersTable).where(eq(usersTable.email, "system@local")).limit(1);
  if (!user) {
    const [inserted] = await db
      .insert(usersTable)
      .values({
        email: "system@local",
        password_hash: "",
        role: "owner",
      })
      .returning();
    user = inserted ?? null;
    if (!user) throw new Error("Не удалось создать пользователя system@local");
  }
  return user.id as number;
}

/**
 * Точка входа: вызывается при закрытии сделки.
 * tradeId — id записи в trades, userId — владелец бота (если известен),
 * symbol — символ сделки (нужен, если userId не передан — найдём через бота).
 */
export async function creditTradeProfit(params: {
  tradeId: number;
  pnlUsd: number;
  symbol?: string;
  userId?: number;
}): Promise<CreditResult | null> {
  const { tradeId, pnlUsd, symbol, userId } = params;

  // Убытки и нулевой результат — ничего не начисляем.
  if (!pnlUsd || pnlUsd <= 0) {
    return null;
  }

  // Идемпотентность: один credit на сделку. Бот может повторно патчить ту же
  // закрытую строку (refinalize после residual flat, ручной PATCH, ретрай), и
  // без этой проверки прибыль начислялась бы несколько раз.
  if (await hasCreditForTrade(tradeId)) {
    return alreadyCreditedResult(pnlUsd);
  }

  let ownerId: number | null = userId ?? null;
  if (!ownerId && symbol) {
    const botOwner = await findBotUserId(symbol);
    ownerId = botOwner ?? null;
  }
  if (!ownerId) {
    ownerId = await ensureDefaultUser();
  }

  const acct = await ensureAccount(ownerId);
  const balanceBefore = Number(acct.balance_usd || 0);
  const hwmBefore = Number(acct.high_water_mark_usd || 0);

  const potentialNewBalance = balanceBefore + pnlUsd;
  // Новая прибыль — только та часть, которая побивает исторический максимум.
  const newProfitUsd = Math.max(0, potentialNewBalance - hwmBefore);

  let commissionUsd = 0;
  let netCreditUsd = 0;
  let balanceAfter = balanceBefore;
  let hwmAfter = hwmBefore;

  if (newProfitUsd > 0) {
    commissionUsd = Math.round((newProfitUsd * (PLATFORM_COMMISSION_PCT / 100)) * 10000) / 10000;
    netCreditUsd = Number((newProfitUsd - commissionUsd).toFixed(4));
    balanceAfter = Number((balanceBefore + netCreditUsd).toFixed(4));
    if (balanceAfter > hwmBefore) {
      hwmAfter = balanceAfter;
    }

    // Сначала пишем credit-транзакцию: частичный уникальный индекс
    // (related_trade_id WHERE type='credit') не даст двум одновременным PATCH
    // начислить прибыль дважды. При нарушении — выходим ДО изменения баланса.
    try {
      await recordTxn({
        userId: ownerId,
        type: "credit",
        amountUsd: newProfitUsd,
        balanceAfterUsd: Number((balanceBefore + newProfitUsd).toFixed(4)),
        relatedTradeId: tradeId,
      });
    } catch (e) {
      if (isUniqueViolation(e)) {
        return alreadyCreditedResult(pnlUsd);
      }
      throw e;
    }

    await db
      .update(userInternalAccountsTable)
      .set({
        balance_usd: balanceAfter,
        high_water_mark_usd: hwmAfter,
        updated_at: new Date().toISOString(),
      })
      .where(eq(userInternalAccountsTable.user_id, ownerId));

    // Аудит: комиссия платформы (credit уже записан выше).
    await recordTxn({
      userId: ownerId,
      type: "commission",
      amountUsd: -commissionUsd,
      balanceAfterUsd: balanceAfter,
      relatedTradeId: tradeId,
    });
  }

  return {
    credited: newProfitUsd > 0,
    pnlUsd: Number(pnlUsd.toFixed(4)),
    newProfitUsd: Number(newProfitUsd.toFixed(4)),
    commissionUsd: Number(commissionUsd.toFixed(4)),
    netCreditUsd: Number(netCreditUsd.toFixed(4)),
    balanceBeforeUsd: balanceBefore,
    balanceAfterUsd: balanceAfter,
    highWaterMarkAfterUsd: hwmAfter,
  };
}

/** Получить баланс и недавние транзакции пользователя. */
export async function getAccountStatement(userId: number) {
  const acct = await ensureAccount(userId);
  const txns = await db
    .select()
    .from(internalTransactionsTable)
    .where(eq(internalTransactionsTable.user_id, userId))
    .orderBy(sql`${internalTransactionsTable.created_at} DESC`)
    .limit(100);
  return {
    account: acct,
    transactions: (txns as any[]).map((t: any) => ({
      ...t,
      amount_usd: Number(t.amount_usd),
      balance_after_usd: Number(t.balance_after_usd),
    })),
  };
}

/** Создать пользователя (регистрация). */
export async function createUser(email: string, passwordHash: string, role = "buyer") {
  const [existing] = await db.select().from(usersTable).where(eq(usersTable.email, email)).limit(1);
  if (existing) {
    throw new Error("Пользователь с таким email уже существует");
  }
  const [user] = await db
    .insert(usersTable)
    .values({ email, password_hash: passwordHash, role })
    .returning();
  await ensureAccount(user.id as number);
  return user;
}

/** Проверить пользователя по email (для логина — пароль сверяется вызывающим кодом). */
export async function findUserByEmail(email: string) {
  const [user] = await db.select().from(usersTable).where(eq(usersTable.email, email)).limit(1);
  return user ?? null;
}
