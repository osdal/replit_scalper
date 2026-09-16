import { sqliteTable, text, real, integer } from "drizzle-orm/sqlite-core";

// Единственный источник DDL: и lazy-создание в route, и init-db берут SQL отсюда,
// чтобы список колонок не расходился между копиями.
export const GRID_HISTORY_CREATE_SQL = `CREATE TABLE IF NOT EXISTS grid_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL UNIQUE,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  phase TEXT NOT NULL,
  exit_reason TEXT NOT NULL,
  tp_pct REAL NOT NULL,
  gate REAL NOT NULL,
  levels INTEGER NOT NULL,
  lo REAL NOT NULL,
  hi REAL NOT NULL,
  mid REAL NOT NULL,
  entry REAL NOT NULL,
  exit REAL,
  pnl REAL,
  pnl_usd REAL,
  positions INTEGER,
  created_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  order_size_usd REAL
)`;

// Идемпотентные миграции для уже созданных БД (ошибка "duplicate column" игнорируется).
export const GRID_HISTORY_MIGRATIONS: string[] = [
  "ALTER TABLE grid_history ADD COLUMN order_size_usd REAL",
  "ALTER TABLE grid_history ADD COLUMN pnl_usd REAL",
  "ALTER TABLE grid_history ADD COLUMN positions INTEGER",
];

export const gridHistoryTable = sqliteTable("grid_history", {
  id:          integer("id").primaryKey({ autoIncrement: true }),
  uid:         text("uid").notNull().unique(),
  symbol:      text("symbol").notNull(),
  timeframe:   text("timeframe").notNull(),
  phase:       text("phase").notNull(),        // done | stopped
  exit_reason: text("exit_reason").notNull(),  // tp | manual | cancel | sl
  tp_pct:      real("tp_pct").notNull(),
  gate:        real("gate").notNull(),
  levels:      integer("levels").notNull(),
  lo:          real("lo").notNull(),
  hi:          real("hi").notNull(),
  mid:         real("mid").notNull(),
  entry:       real("entry").notNull(),
  exit:        real("exit"),
  pnl:         real("pnl"),
  pnl_usd:     real("pnl_usd"),
  positions:   integer("positions"),
  created_at:  text("created_at").notNull(),
  finished_at: text("finished_at").notNull(),
  order_size_usd: real("order_size_usd"),
});

export type GridHistoryRow = typeof gridHistoryTable.$inferSelect;
