import { sqliteTable, text, real, integer } from "drizzle-orm/sqlite-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

export const tradesTable = sqliteTable("trades", {
  id:           integer("id").primaryKey({ autoIncrement: true }),
  symbol:       text("symbol").notNull(),
  direction:    text("direction").notNull(),
  entry_price:  real("entry_price").notNull(),
  exit_price:   real("exit_price"),
  qty:          real("qty").notNull(),
  sl_price:     real("sl_price").notNull(),
  tp1_price:    real("tp1_price").notNull(),
  tp2_price:    real("tp2_price").notNull(),
  pnl:          real("pnl"),
  exit_reason:  text("exit_reason"),
  entry_time:   text("entry_time").notNull(),
  exit_time:    text("exit_time"),
  is_open:      integer("is_open", { mode: "boolean" }).notNull().default(true),
  ema_fast:     real("ema_fast"),
  ema_slow:     real("ema_slow"),
  volume:       real("volume"),
  volume_ma:    real("volume_ma"),
  mode:         text("mode").notNull().default("live"),
});

export const insertTradeSchema = createInsertSchema(tradesTable).omit({ id: true });
export type InsertTrade = z.infer<typeof insertTradeSchema>;
export type Trade = typeof tradesTable.$inferSelect;
