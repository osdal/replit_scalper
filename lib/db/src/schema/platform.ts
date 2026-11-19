import { sqliteTable, text, real, integer, index } from "drizzle-orm/sqlite-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

/**
 * Мульти-тенантная платформа: клиенты, их ключи, счета и журнал операций.
 *
 * Отличие от users/user_internal_accounts: там была модель «баланс = накопленная
 * прибыль» (комиссия удерживалась из прибыли). Здесь баланс — предоплата
 * обслуживания: 10% реализованной прибыли СПИСЫВАЕТСЯ с баланса, а прибыль
 * остаётся на биржевом счету клиента. При балансе ≤ 0 новые входы блокируются.
 */
export const clientsTable = sqliteTable("clients", {
  id:               integer("id").primaryKey({ autoIncrement: true }),
  email:            text("email").notNull().unique(),
  password_hash:    text("password_hash").notNull().default(""),
  role:             text("role").notNull().default("client"),   // owner | client
  status:           text("status").notNull().default("active"), // active | paused | suspended
  fee_pct:          real("fee_pct").notNull().default(10),
  gift_usd:         real("gift_usd").notNull().default(0),
  telegram_chat_id: text("telegram_chat_id"),
  auto_resume:      integer("auto_resume", { mode: "boolean" }).notNull().default(true),
  notes:            text("notes"),
  created_at:       text("created_at").notNull().$defaultFn(() => new Date().toISOString()),
  updated_at:       text("updated_at").notNull().$defaultFn(() => new Date().toISOString()),
});

export const insertClientSchema = createInsertSchema(clientsTable).omit({ id: true, created_at: true, updated_at: true });
export type InsertClient = z.infer<typeof insertClientSchema>;
export type Client = typeof clientsTable.$inferSelect;

/** Ключи биржи клиента. Секрет и ключ шифруются AES-256-GCM на стороне API. */
export const clientApiKeysTable = sqliteTable("client_api_keys", {
  id:             integer("id").primaryKey({ autoIncrement: true }),
  client_id:      integer("client_id").notNull(),
  exchange:       text("exchange").notNull().default("binance"),
  label:          text("label"),
  api_key_mask:   text("api_key_mask").notNull(),
  api_key_enc:    text("api_key_enc").notNull(),
  api_secret_enc: text("api_secret_enc").notNull(),
  iv:             text("iv").notNull(),
  key_version:    integer("key_version").notNull().default(1),
  can_trade:      integer("can_trade", { mode: "boolean" }),
  can_withdraw:   integer("can_withdraw", { mode: "boolean" }),
  ip_whitelisted: integer("ip_whitelisted", { mode: "boolean" }),
  status:         text("status").notNull().default("active"),   // active | invalid | revoked
  last_checked_at: text("last_checked_at"),
  last_error:     text("last_error"),
  created_at:     text("created_at").notNull().$defaultFn(() => new Date().toISOString()),
  updated_at:     text("updated_at").notNull().$defaultFn(() => new Date().toISOString()),
}, (t) => [index("idx_client_api_keys_client").on(t.client_id)]);

export const insertClientApiKeySchema = createInsertSchema(clientApiKeysTable).omit({ id: true, created_at: true, updated_at: true });
export type InsertClientApiKey = z.infer<typeof insertClientApiKeySchema>;
export type ClientApiKey = typeof clientApiKeysTable.$inferSelect;

/**
 * Баланс клиента и база для комиссии.
 *  - balance_usd    — предоплата (подарок + пополнения − комиссии);
 *  - cum_pnl_usd    — накопленный реализованный P&L по всем ботам клиента;
 *  - hwm_pnl_usd    — максимум cum_pnl (high-water mark).
 * Комиссия = fee_pct% от max(0, cum_pnl − hwm). Убытки не списываются, но и
 * комиссию не дают, пока HWM не восстановлен.
 */
export const clientAccountsTable = sqliteTable("client_accounts", {
  client_id:  integer("client_id").primaryKey(),
  balance_usd: real("balance_usd").notNull().default(0),
  cum_pnl_usd: real("cum_pnl_usd").notNull().default(0),
  hwm_pnl_usd: real("hwm_pnl_usd").notNull().default(0),
  updated_at:  text("updated_at").notNull().$defaultFn(() => new Date().toISOString()),
});

export type ClientAccount = typeof clientAccountsTable.$inferSelect;

/**
 * Журнал операций — единственный источник истины о балансе.
 * Баланс всегда можно пересчитать как сумму amount_usd.
 * idempotency_key защищает от двойного начисления при ретраях.
 */
export const ledgerTable = sqliteTable("ledger", {
  id:                 integer("id").primaryKey({ autoIncrement: true }),
  client_id:          integer("client_id").notNull(),
  type:               text("type").notNull(),   // gift | topup | fee | adjustment | refund
  amount_usd:         real("amount_usd").notNull(),
  balance_after_usd:  real("balance_after_usd").notNull(),
  cum_pnl_after_usd:  real("cum_pnl_after_usd"),
  related_bot_id:     integer("related_bot_id"),
  related_trade_id:   integer("related_trade_id"),
  reason:             text("reason"),
  actor:              text("actor"),
  idempotency_key:    text("idempotency_key").notNull().unique(),
  created_at:         text("created_at").notNull().$defaultFn(() => new Date().toISOString()),
}, (t) => [
  index("idx_ledger_client").on(t.client_id, t.created_at),
  index("idx_ledger_trade").on(t.related_trade_id),
]);

export const insertLedgerSchema = createInsertSchema(ledgerTable).omit({ id: true, created_at: true });
export type InsertLedger = z.infer<typeof insertLedgerSchema>;
export type LedgerEntry = typeof ledgerTable.$inferSelect;

/** Действия админов и системы: кто изменил баланс, ключи, ботов. */
export const auditLogTable = sqliteTable("audit_log", {
  id:          integer("id").primaryKey({ autoIncrement: true }),
  actor:       text("actor").notNull(),          // email админа | system | bot
  action:      text("action").notNull(),
  target_type: text("target_type"),
  target_id:   text("target_id"),
  payload:     text("payload"),
  ip:          text("ip"),
  created_at:  text("created_at").notNull().$defaultFn(() => new Date().toISOString()),
}, (t) => [index("idx_audit_created").on(t.created_at)]);

export type AuditLogEntry = typeof auditLogTable.$inferSelect;
