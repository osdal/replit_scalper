import { sqliteTable, text, real, integer, index } from "drizzle-orm/sqlite-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

/**
 * Цепочка компенсации убытка.
 * status:
 *   free   — долг свободен, ждёт что его захватит следующий сигнал
 *   locked — долг захвачен конкретным ботом/сделкой, позиция открыта
 *   closed — цепочка закрыта (компенсатор закрылся в плюс)
 */
export const recoveryChainsTable = sqliteTable("recovery_chains", {
  id:            integer("id").primaryKey({ autoIncrement: true }),
  debt_amount:   real("debt_amount").notNull(),      // текущий долг в USDT
  status:        text("status").notNull().default("free"), // free | locked | closed
  locked_by:     text("locked_by"),                  // symbol бота который захватил долг
  locked_trade_id: integer("locked_trade_id"),        // id сделки-компенсатора в trades
  created_at:    text("created_at").notNull(),
  updated_at:    text("updated_at").notNull(),
  closed_at:     text("closed_at"),
}, (table) => ({
  statusIdx: index("idx_recovery_status").on(table.status),
}));

export const insertRecoveryChainSchema = createInsertSchema(recoveryChainsTable).omit({ id: true });
export type InsertRecoveryChain = z.infer<typeof insertRecoveryChainSchema>;
export type RecoveryChain = typeof recoveryChainsTable.$inferSelect;
