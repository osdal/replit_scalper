import { pgTable, text, real, boolean, timestamp, integer, jsonb } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

// Конфигурация бота (одна запись на символ)
export const botsTable = pgTable("bots", {
  symbol:        text("symbol").primaryKey(),          // ETHUSDT
  mode:          text("mode").notNull(),               // live | paper
  timeframe:     text("timeframe").notNull(),
  leverage:      integer("leverage").notNull(),
  risk_pct:      real("risk_pct").notNull(),
  sl_pct:        real("sl_pct").notNull(),
  tp1_pct:       real("tp1_pct").notNull(),
  tp1_close_pct: real("tp1_close_pct").notNull(),
  tp2_pct:       real("tp2_pct").notNull(),
  ema_fast:      integer("ema_fast").notNull(),
  ema_slow:      integer("ema_slow").notNull(),
  volume_ma_period:   integer("volume_ma_period").notNull(),
  volume_multiplier:  real("volume_multiplier").notNull(),
  htf_enabled:   boolean("htf_enabled").notNull().default(false),
  htf_timeframe: text("htf_timeframe"),
  htf_ema_fast:  integer("htf_ema_fast"),
  htf_ema_slow:  integer("htf_ema_slow"),
  auto_mode:     boolean("auto_mode").notNull().default(true),
  paper_balance: real("paper_balance").notNull().default(1000),
  log_file:      text("log_file").notNull(),
  // Рантайм статус (обновляется ботом)
  is_running:    boolean("is_running").notNull().default(false),
  last_heartbeat: timestamp("last_heartbeat"),
  current_price: real("current_price"),
  // Открытая позиция (из state файла)
  position:      jsonb("position"),
  updated_at:    timestamp("updated_at").notNull().defaultNow(),
});

export const insertBotSchema = createInsertSchema(botsTable);
export type InsertBot = z.infer<typeof insertBotSchema>;
export type Bot = typeof botsTable.$inferSelect;
