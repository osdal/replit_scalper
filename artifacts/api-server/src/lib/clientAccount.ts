import { db } from "@workspace/db";
import {
  botsTable,
  tradesTable,
  clientsTable,
  clientAccountsTable,
  ledgerTable,
} from "@workspace/db/schema";
import { and, desc, eq, ne, sql } from "drizzle-orm";

/**
 * Биллинг клиента (модель B, предоплата).
 *
 * Правила:
 *  - Торговая прибыль НЕ зачисляется на внутренний счёт: она остаётся на
 *    биржевом счёте клиента.
 *  - При закрытии ПРИБЫЛЬНОЙ сделки с внутреннего счёта списывается 10 %
 *    (`clients.fee_pct`) от реализованного `pnl` этой сделки.
 *  - Убыточная или нулевая сделка баланс не меняет.
 *  - Счёт пополняется только вручную (подарок/пополнение) — сейчас владельцем,
 *    в будущем самим клиентом. Поэтому баланс может уйти в минус: это долг,
 *    а блокировка новых входов при `balance_usd <= 0` — этап 5.
 *
 * `client_accounts.cum_pnl_usd`/`hwm_pnl_usd` остаются справочными: они
 * показывают накопленный PnL клиента, но на размер комиссии НЕ влияют.
 *
 * Идемпотентность: комиссия по сделке пишется в `ledger` с уникальным
 * `idempotency_key = fee:trade:<id>`. Повторный PATCH той же закрытой сделки
 * (refinalize, ретрай) не спишет комиссию второй раз.
 */

const round4 = (n: number): number => Math.round((n + Number.EPSILON) * 10000) / 10000;

export interface TradeFeeResult {
  tradeId: number;
  clientId: number;
  pnlUsd: number;
  feePct: number;
  feeUsd: number;
  balanceBeforeUsd: number;
  balanceAfterUsd: number;
  cumPnlUsd: number;
  charged: boolean;
  alreadyCharged: boolean;
}

interface AccountRow {
  client_id: number;
  balance_usd: number;
  cum_pnl_usd: number;
  hwm_pnl_usd: number;
  updated_at: string;
}

/**
 * Списывает 10 % прибыли закрытой сделки с предоплаченного баланса клиента.
 *
 * Вызывается из `PATCH /api/trades/:id` на переходе `open → closed` для ЛЮБОЙ
 * сделки (не только прибыльной): убыток не списывает деньги, но обновляет
 * справочный `cum_pnl_usd`. `rejected` и незакрытые сделки игнорируются.
 */
export async function chargeTradeCommission(tradeId: number): Promise<TradeFeeResult | null> {
  if (!Number.isFinite(tradeId)) return null;

  return db.transaction(async (tx) => {
    const [trade] = await tx
      .select({
        id: tradesTable.id,
        bot_id: tradesTable.bot_id,
        pnl: tradesTable.pnl,
        is_open: tradesTable.is_open,
        status: tradesTable.status,
      })
      .from(tradesTable)
      .where(eq(tradesTable.id, tradeId))
      .limit(1);

    if (!trade) return null;
    if (Boolean(Number(trade.is_open))) return null;
    if (String(trade.status) === "rejected") return null;
    if (trade.bot_id == null) return null;

    const [bot] = await tx
      .select({ client_id: botsTable.client_id })
      .from(botsTable)
      .where(eq(botsTable.id, Number(trade.bot_id)))
      .limit(1);
    if (!bot?.client_id) return null;
    const clientId = Number(bot.client_id);

    const [client] = await tx
      .select({ fee_pct: clientsTable.fee_pct })
      .from(clientsTable)
      .where(eq(clientsTable.id, clientId))
      .limit(1);
    const feePct = Number(client?.fee_pct ?? 10);

    let [acct] = (await tx
      .select()
      .from(clientAccountsTable)
      .where(eq(clientAccountsTable.client_id, clientId))
      .limit(1)) as AccountRow[];

    if (!acct) {
      [acct] = (await tx
        .insert(clientAccountsTable)
        .values({
          client_id: clientId,
          balance_usd: 0,
          cum_pnl_usd: 0,
          hwm_pnl_usd: 0,
          updated_at: new Date().toISOString(),
        })
        .returning()) as AccountRow[];
    }

    const balanceBefore = round4(Number(acct.balance_usd || 0));
    const pnlRaw = Number(trade.pnl ?? 0);
    const pnlUsd = Number.isFinite(pnlRaw) ? round4(pnlRaw) : 0;

    // Накопленный PnL клиента — пересчитываем из trades, а не инкрементом:
    // повторный PATCH и пропущенные закрытия не ломают цифру.
    const [cumRow] = await tx
      .select({ cum: sql<number>`COALESCE(SUM(${tradesTable.pnl}), 0)` })
      .from(tradesTable)
      .innerJoin(botsTable, eq(botsTable.id, tradesTable.bot_id))
      .where(and(
        eq(botsTable.client_id, clientId),
        eq(tradesTable.is_open, false),
        ne(tradesTable.status, "rejected"),
      ));
    const cumPnlUsd = round4(Number(cumRow?.cum ?? 0));

    const idempotencyKey = `fee:trade:${tradeId}`;
    const [already] = await tx
      .select({ id: ledgerTable.id, amount_usd: ledgerTable.amount_usd })
      .from(ledgerTable)
      .where(eq(ledgerTable.idempotency_key, idempotencyKey))
      .limit(1);

    if (already) {
      return {
        tradeId,
        clientId,
        pnlUsd,
        feePct,
        feeUsd: 0,
        balanceBeforeUsd: balanceBefore,
        balanceAfterUsd: balanceBefore,
        cumPnlUsd,
        charged: false,
        alreadyCharged: true,
      } satisfies TradeFeeResult;
    }

    // Убыток/ноль: денег не списываем, только обновляем справочный cum_pnl.
    const feeUsd = pnlUsd > 0 ? round4(pnlUsd * feePct / 100) : 0;
    if (feeUsd <= 0) {
      await tx
        .update(clientAccountsTable)
        .set({
          cum_pnl_usd: cumPnlUsd,
          hwm_pnl_usd: Math.max(round4(Number(acct.hwm_pnl_usd || 0)), cumPnlUsd),
          updated_at: new Date().toISOString(),
        })
        .where(eq(clientAccountsTable.client_id, clientId));

      return {
        tradeId,
        clientId,
        pnlUsd,
        feePct,
        feeUsd: 0,
        balanceBeforeUsd: balanceBefore,
        balanceAfterUsd: balanceBefore,
        cumPnlUsd,
        charged: false,
        alreadyCharged: false,
      } satisfies TradeFeeResult;
    }

    const balanceAfter = round4(balanceBefore - feeUsd);

    // Сначала журнал: UNIQUE(idempotency_key) — последняя защита от гонки.
    // Если запись уже есть, транзакция откатится, и вызывающий получит
    // alreadyCharged без второго списания.
    await tx.insert(ledgerTable).values({
      client_id: clientId,
      type: "fee",
      amount_usd: -feeUsd,
      balance_after_usd: balanceAfter,
      cum_pnl_after_usd: cumPnlUsd,
      related_bot_id: Number(trade.bot_id),
      related_trade_id: tradeId,
      reason: `10 % от прибыли сделки $${pnlUsd.toFixed(4)}`,
      actor: "system",
      idempotency_key: idempotencyKey,
    });

    await tx
      .update(clientAccountsTable)
      .set({
        balance_usd: balanceAfter,
        cum_pnl_usd: cumPnlUsd,
        hwm_pnl_usd: Math.max(round4(Number(acct.hwm_pnl_usd || 0)), cumPnlUsd),
        updated_at: new Date().toISOString(),
      })
      .where(eq(clientAccountsTable.client_id, clientId));

    return {
      tradeId,
      clientId,
      pnlUsd,
      feePct,
      feeUsd,
      balanceBeforeUsd: balanceBefore,
      balanceAfterUsd: balanceAfter,
      cumPnlUsd,
      charged: true,
      alreadyCharged: false,
    } satisfies TradeFeeResult;
  });
}

export interface TopUpResult {
  clientId: number;
  amountUsd: number;
  balanceBeforeUsd: number;
  balanceAfterUsd: number;
  alreadyApplied: boolean;
}

/** Ручное пополнение внутреннего счёта (подарок/депозит владельца/клиента). */
export async function topUpClient(params: {
  clientId: number;
  amountUsd: number;
  reason?: string;
  actor?: string;
  idempotencyKey?: string;
}): Promise<TopUpResult> {
  const clientId = Number(params.clientId);
  const amountUsd = round4(Number(params.amountUsd));
  if (!Number.isFinite(clientId) || clientId <= 0) {
    throw new Error("clientId обязателен");
  }
  if (!Number.isFinite(amountUsd) || amountUsd <= 0) {
    throw new Error("amountUsd должен быть больше нуля");
  }
  const key = (params.idempotencyKey || "").trim()
    || `topup:${clientId}:${Date.now()}:${Math.random().toString(36).slice(2, 10)}`;

  return db.transaction(async (tx) => {
    const [existing] = await tx
      .select({
        amount_usd: ledgerTable.amount_usd,
        balance_after_usd: ledgerTable.balance_after_usd,
      })
      .from(ledgerTable)
      .where(eq(ledgerTable.idempotency_key, key))
      .limit(1);

    if (existing) {
      return {
        clientId,
        amountUsd: round4(Number(existing.amount_usd)),
        balanceBeforeUsd: round4(Number(existing.balance_after_usd) - Number(existing.amount_usd)),
        balanceAfterUsd: round4(Number(existing.balance_after_usd)),
        alreadyApplied: true,
      } satisfies TopUpResult;
    }

    let [acct] = (await tx
      .select()
      .from(clientAccountsTable)
      .where(eq(clientAccountsTable.client_id, clientId))
      .limit(1)) as AccountRow[];

    if (!acct) {
      [acct] = (await tx
        .insert(clientAccountsTable)
        .values({
          client_id: clientId,
          balance_usd: 0,
          cum_pnl_usd: 0,
          hwm_pnl_usd: 0,
          updated_at: new Date().toISOString(),
        })
        .returning()) as AccountRow[];
    }

    const balanceBefore = round4(Number(acct.balance_usd || 0));
    const balanceAfter = round4(balanceBefore + amountUsd);

    await tx.insert(ledgerTable).values({
      client_id: clientId,
      type: "topup",
      amount_usd: amountUsd,
      balance_after_usd: balanceAfter,
      cum_pnl_after_usd: round4(Number(acct.cum_pnl_usd || 0)),
      reason: params.reason || "Ручное пополнение",
      actor: params.actor || "admin",
      idempotency_key: key,
    });

    await tx
      .update(clientAccountsTable)
      .set({ balance_usd: balanceAfter, updated_at: new Date().toISOString() })
      .where(eq(clientAccountsTable.client_id, clientId));

    return {
      clientId,
      amountUsd,
      balanceBeforeUsd: balanceBefore,
      balanceAfterUsd: balanceAfter,
      alreadyApplied: false,
    } satisfies TopUpResult;
  });
}

/** Баланс и выписка клиента для дашборда (совместимый вид со старым /me). */
export async function getClientStatement(clientId: number, limit = 100) {
  const id = Number(clientId);
  let [acct] = (await db
    .select()
    .from(clientAccountsTable)
    .where(eq(clientAccountsTable.client_id, id))
    .limit(1)) as AccountRow[];

  if (!acct) {
    [acct] = (await db
      .insert(clientAccountsTable)
      .values({
        client_id: id,
        balance_usd: 0,
        cum_pnl_usd: 0,
        hwm_pnl_usd: 0,
        updated_at: new Date().toISOString(),
      })
      .returning()) as AccountRow[];
  }

  const txns = await db
    .select()
    .from(ledgerTable)
    .where(eq(ledgerTable.client_id, id))
    .orderBy(desc(ledgerTable.created_at))
    .limit(Math.min(Math.max(limit, 1), 1000));

  return {
    account: {
      id: acct.client_id,
      user_id: acct.client_id,
      client_id: acct.client_id,
      balance_usd: round4(Number(acct.balance_usd)),
      cum_pnl_usd: round4(Number(acct.cum_pnl_usd)),
      hwm_pnl_usd: round4(Number(acct.hwm_pnl_usd)),
      // Совместимость со старым дашбордом: HWM баланса больше не используется,
      // отдаём HWM накопленного PnL.
      high_water_mark_usd: round4(Number(acct.hwm_pnl_usd)),
      created_at: acct.updated_at,
      updated_at: acct.updated_at,
    },
    transactions: (txns as any[]).map((t) => ({
      ...t,
      amount_usd: round4(Number(t.amount_usd)),
      balance_after_usd: round4(Number(t.balance_after_usd)),
      cum_pnl_after_usd: t.cum_pnl_after_usd == null ? null : round4(Number(t.cum_pnl_after_usd)),
    })),
  };
}
