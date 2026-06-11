import { pgTable, serial, text, real, timestamp, boolean } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

// История сделок (парсится из лог файлов)
export const tradesTable = pgTable("trades", {
  id:            serial("id").primaryKey(),
  symbol:        text("symbol").notNull(),
  direction:     text("direction").notNull(),          // LONG | SHORT
  entry_price:   real("entry_price").notNull(),
  exit_price:    real("exit_price"),
  qty:           real("qty").notNull(),
  sl_price:      real("sl_price").notNull(),
  tp1_price:     real("tp1_price").notNull(),
  tp2_price:     real("tp2_price").notNull(),
  pnl:           real("pnl"),
  exit_reason:   text("exit_reason"),                  // SL | TP1 | TP2
  entry_time:    timestamp("entry_time").notNull(),
  exit_time:     timestamp("exit_time"),
  is_open:       boolean("is_open").notNull().default(true),
  // Индикаторы на момент входа
  ema_fast:      real("ema_fast"),
  ema_slow:      real("ema_slow"),
  volume:        real("volume"),
  volume_ma:     real("volume_ma"),
  mode:          text("mode").notNull().default("live"),
});

export const insertTradeSchema = createInsertSchema(tradesTable).omit({ id: true });
export type InsertTrade = z.infer<typeof insertTradeSchema>;
export type Trade = typeof tradesTable.$inferSelect;
