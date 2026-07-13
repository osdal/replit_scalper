import { sqliteTable, text, real, integer } from "drizzle-orm/sqlite-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

export const botsTable = sqliteTable("bots", {
  symbol:             text("symbol").primaryKey(),
  mode:               text("mode").notNull(),
  timeframe:          text("timeframe").notNull(),
  leverage:           integer("leverage").notNull(),
  risk_pct:           real("risk_pct").notNull(),
  sl_pct:             real("sl_pct").notNull(),
  tp1_pct:            real("tp1_pct").notNull(),
  tp1_close_pct:      real("tp1_close_pct").notNull(),
  tp2_pct:            real("tp2_pct").notNull(),
  ema_fast:           integer("ema_fast").notNull(),
  ema_slow:           integer("ema_slow").notNull(),
  volume_ma_period:   integer("volume_ma_period").notNull(),
  volume_multiplier:  real("volume_multiplier").notNull(),
  htf_enabled:        integer("htf_enabled", { mode: "boolean" }).notNull().default(false),
  htf_timeframe:      text("htf_timeframe"),
  htf_ema_fast:       integer("htf_ema_fast"),
  htf_ema_slow:       integer("htf_ema_slow"),
  auto_mode:          integer("auto_mode", { mode: "boolean" }).notNull().default(true),
  paper_balance:      real("paper_balance").notNull().default(1000),
  log_file:           text("log_file").notNull(),
  // Runtime status
  is_running:         integer("is_running", { mode: "boolean" }).notNull().default(false),
  last_heartbeat:     text("last_heartbeat"),
  current_price:      real("current_price"),
  position:           text("position"),   // JSON string
  updated_at:         text("updated_at").notNull().$defaultFn(() => new Date().toISOString()),
});

export const insertBotSchema = createInsertSchema(botsTable);
export type InsertBot = z.infer<typeof insertBotSchema>;
export type Bot = typeof botsTable.$inferSelect;
