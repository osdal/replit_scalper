import { sqliteTable, text, real, integer } from "drizzle-orm/sqlite-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

/** Пользователи платформы (для продажи ботов покупателям). */
export const usersTable = sqliteTable("users", {
  id:            integer("id").primaryKey({ autoIncrement: true }),
  email:         text("email").notNull().unique(),
  password_hash: text("password_hash").notNull(),
  role:          text("role").notNull().default("buyer"), // owner | buyer
  created_at:    text("created_at").notNull().$defaultFn(() => new Date().toISOString()),
  updated_at:    text("updated_at").notNull().$defaultFn(() => new Date().toISOString()),
});

export const insertUserSchema = createInsertSchema(usersTable).omit({ id: true, created_at: true, updated_at: true });
export type InsertUser = z.infer<typeof insertUserSchema>;
export type User = typeof usersTable.$inferSelect;

/** Внутренний счёт пользователя: баланс и high-water-mark (максимальный баланс за всё время). */
export const userInternalAccountsTable = sqliteTable("user_internal_accounts", {
  id:                  integer("id").primaryKey({ autoIncrement: true }),
  user_id:             integer("user_id").notNull().unique(),
  balance_usd:         real("balance_usd").notNull().default(0),
  high_water_mark_usd: real("high_water_mark_usd").notNull().default(0),
  created_at:          text("created_at").notNull().$defaultFn(() => new Date().toISOString()),
  updated_at:          text("updated_at").notNull().$defaultFn(() => new Date().toISOString()),
});

export const insertUserInternalAccountSchema = createInsertSchema(userInternalAccountsTable).omit({ id: true, created_at: true, updated_at: true });
export type InsertUserInternalAccount = z.infer<typeof insertUserInternalAccountSchema>;
export type UserInternalAccount = typeof userInternalAccountsTable.$inferSelect;

/** История транзакций внутреннего счёта (аудит начислений/списываний). */
export const internalTransactionsTable = sqliteTable("internal_transactions", {
  id:                integer("id").primaryKey({ autoIncrement: true }),
  user_id:           integer("user_id").notNull(),
  type:              text("type").notNull(), // credit | commission | debit | withdrawal
  amount_usd:        real("amount_usd").notNull(),      // знак: + зачисление, - списание
  balance_after_usd: real("balance_after_usd").notNull(),
  related_trade_id:  integer("related_trade_id"),        // сделка, по которой начислено
  created_at:        text("created_at").notNull().$defaultFn(() => new Date().toISOString()),
});

export const insertInternalTransactionSchema = createInsertSchema(internalTransactionsTable).omit({ id: true, created_at: true });
export type InsertInternalTransaction = z.infer<typeof insertInternalTransactionSchema>;
export type InternalTransaction = typeof internalTransactionsTable.$inferSelect;
