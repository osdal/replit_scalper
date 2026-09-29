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
  htf2_enabled:       integer("htf2_enabled", { mode: "boolean" }).notNull().default(false),
  htf2_timeframe:     text("htf2_timeframe"),
  htf2_ema_fast:      integer("htf2_ema_fast"),
  htf2_ema_slow:      integer("htf2_ema_slow"),
  auto_mode:          integer("auto_mode", { mode: "boolean" }).notNull().default(true),
  paper_balance:      real("paper_balance").notNull().default(1000),
  log_file:           text("log_file").notNull(),
  // Режим движка: manual = обычная логика стратегий; auto = последовательности (заглушка).
  trade_mode:         text("trade_mode").notNull().default("manual"),
  // Нотионал позиции в USD, задаётся из UI (0 = не переопределять сайзинг).
  position_size_usd:  real("position_size_usd").notNull().default(0),
  // % СВОБОДНОГО депозита (availableBalance) на маржу, live-only (BOT_ENV=live).
  // margin = free_balance * pct/100; позиция = margin * leverage. По умолчанию 1%.
  // Приоритет: ниже position_size_usd, выше LIVE_DEFAULT_MARGIN_USD/margin_pct. 0 = выкл.
  position_size_pct:  real("position_size_pct").notNull().default(1),
  // Макс. шагов reverse-цепочки (0 = без лимита).
  reverse_chain_max:  integer("reverse_chain_max").notNull().default(10),
  // Потолки нотионала позиции: абсолютный USD и % от equity (0 = выкл).
  max_position_notional_usd: real("max_position_notional_usd").notNull().default(0),
  max_position_pct_equity:   real("max_position_pct_equity").notNull().default(0),
  // Live arm-gate: торговля разрешена только при armed=1. В testnet по умолчанию 1.
  armed:              integer("armed", { mode: "boolean" }).notNull().default(true),
  // Причина последней самоостановки бота (напр. "watchdog_no_candles"); null если нет.
  stop_reason:        text("stop_reason"),
  // Мягкая остановка: бот доводит текущую позицию и выходит сам (Kill — отдельный эндпоинт).
  stop_requested:     integer("stop_requested", { mode: "boolean" }).notNull().default(false),
  // Relay-only: бот не открывает собственные входы, только позиции из релея testnet.
  relay_only:         integer("relay_only", { mode: "boolean" }).notNull().default(false),
  // Runtime status
  is_running:         integer("is_running", { mode: "boolean" }).notNull().default(false),
  last_heartbeat:     text("last_heartbeat"),
  current_price:      real("current_price"),
  position:           text("position"),   // JSON string
  llm_status:         text("llm_status"), // JSON string: состояние LLM-фильтра (провайдеры, ошибки)
  updated_at:         text("updated_at").notNull().$defaultFn(() => new Date().toISOString()),
});

export const insertBotSchema = createInsertSchema(botsTable);
export type InsertBot = z.infer<typeof insertBotSchema>;
export type Bot = typeof botsTable.$inferSelect;
