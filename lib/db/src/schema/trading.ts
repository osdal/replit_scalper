import { sqliteTable, integer, text } from "drizzle-orm/sqlite-core";

/**
 * Глобальное состояние управления рисками по всему портфелю.
 * Одна строка (id=1). Счётчик подряд убытков и остаток паузы общие для всех ботов.
 */
export const tradingControlTable = sqliteTable("trading_control", {
  id:             integer("id").primaryKey(),        // всегда 1
  loss_streak:    integer("loss_streak").notNull().default(0),  // подряд убытков подряд
  paused_remaining: integer("paused_remaining").notNull().default(0), // осталось сигналов пропустить
  active_open:    integer("active_open").notNull().default(0), // счётчик зарезервированных/открытых слотов лимита (атомарный)
  updated_at:     text("updated_at").notNull(),
});
