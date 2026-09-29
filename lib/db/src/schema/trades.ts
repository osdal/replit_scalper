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
  mode:         text("mode").notNull().default("paper"),
  status:       text("status").notNull().default("open"),  // open | closed | rejected
  reject_reason: text("reject_reason"),                    // причина отклонения (если status='rejected')
  rsi:          real("rsi"),
  macd:         real("macd"),
  macd_signal:  real("macd_signal"),
  macd_hist:    real("macd_hist"),
  bb_upper:     real("bb_upper"),
  bb_middle:    real("bb_middle"),
  bb_lower:     real("bb_lower"),
  atr:          real("atr"),
  preset:       text("preset"),
  // Отчётность цикла: результат последней ноги, итог цикла и число ног.
  leg_pnl:      real("leg_pnl"),
  cycle_pnl:    real("cycle_pnl"),
  chain_depth:  integer("chain_depth"),
  // Причина завершения reverse-цикла: chain_tp | chain_backstop | chain_max |
  // chain_failed | chain_market (для не-reverse пусто).
  cycle_close_reason: text("cycle_close_reason"),
  commission:   real("commission"),
  quote_volume: real("quote_volume"),
});

export const insertTradeSchema = createInsertSchema(tradesTable).omit({ id: true });
export type InsertTrade = z.infer<typeof insertTradeSchema>;
export type Trade = typeof tradesTable.$inferSelect;
