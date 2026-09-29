import { sqliteTable, text, real, integer } from "drizzle-orm/sqlite-core";

// Очередь релей-сигналов testnet → live. Testnet-бот публикует вход+стоп/тейки,
// live-бот потребляет (consumed_at) и открывает позицию своим объёмом.
export const signalsTable = sqliteTable("signals", {
  id:          integer("id").primaryKey({ autoIncrement: true }),
  symbol:      text("symbol").notNull(),
  kind:        text("kind").notNull().default("entry"),
  direction:   text("direction").notNull(),
  entry_price: real("entry_price").notNull(),
  sl_price:    real("sl_price"),
  tp1_price:   real("tp1_price"),
  tp2_price:   real("tp2_price"),
  preset:      text("preset"),
  source:      text("source"),
  payload:     text("payload"),
  created_at:  text("created_at").notNull(),
  consumed_at: text("consumed_at"),
  // pending | consumed | skipped (+ причина в note) — для счётчиков в дашборде.
  status:      text("status").notNull().default("pending"),
  note:        text("note"),
});
