import { sqliteTable, text, real, integer } from "drizzle-orm/sqlite-core";

// Единственный источник DDL: и lazy-создание в route/engine, и init-db берут SQL отсюда,
// чтобы список колонок не расходился между копиями.
export const GRID_CREATE_SQL = `CREATE TABLE IF NOT EXISTS grids (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL UNIQUE,
  symbol TEXT,
  timeframe TEXT,
  phase TEXT,
  direction TEXT,
  lo REAL,
  hi REAL,
  midPrice REAL,
  gate REAL,
  levels INTEGER,
  levelPrices TEXT,
  tpPct REAL,
  slPct REAL,
  edgePct REAL,
  orderSizeUsd REAL,
  leverage INTEGER,
  testnetOrderIds TEXT,
  stopOrderIds TEXT,
  tpOrderIds TEXT,
  tpOrderPrices TEXT,
  fillEntries TEXT,
  openLots TEXT,
  positionAmt REAL,
  realizedPnl REAL,
  realizedUsd REAL,
  lastPrice REAL,
  lastPlacementError TEXT,
  engine TEXT DEFAULT 'browser',
  created_at TEXT,
  activated_at TEXT,
  finished_at TEXT,
  updated_at TEXT
)`;

// Идемпотентные миграции для уже созданных БД (ошибка "duplicate column" игнорируется).
// Покрывают любую частично созданную таблицу grids (кроме id/uid) — полный набор колонок
// из GRID_CREATE_SQL, чтобы старые/промежуточные БД догонялись до актуальной схемы.
export const GRID_MIGRATIONS: string[] = [
  "ALTER TABLE grids ADD COLUMN symbol TEXT",
  "ALTER TABLE grids ADD COLUMN timeframe TEXT",
  "ALTER TABLE grids ADD COLUMN phase TEXT",
  "ALTER TABLE grids ADD COLUMN direction TEXT",
  "ALTER TABLE grids ADD COLUMN lo REAL",
  "ALTER TABLE grids ADD COLUMN hi REAL",
  "ALTER TABLE grids ADD COLUMN midPrice REAL",
  "ALTER TABLE grids ADD COLUMN gate REAL",
  "ALTER TABLE grids ADD COLUMN levels INTEGER",
  "ALTER TABLE grids ADD COLUMN levelPrices TEXT",
  "ALTER TABLE grids ADD COLUMN tpPct REAL",
  "ALTER TABLE grids ADD COLUMN slPct REAL",
  "ALTER TABLE grids ADD COLUMN edgePct REAL",
  "ALTER TABLE grids ADD COLUMN orderSizeUsd REAL",
  "ALTER TABLE grids ADD COLUMN leverage INTEGER",
  "ALTER TABLE grids ADD COLUMN testnetOrderIds TEXT",
  "ALTER TABLE grids ADD COLUMN stopOrderIds TEXT",
  "ALTER TABLE grids ADD COLUMN tpOrderIds TEXT",
  "ALTER TABLE grids ADD COLUMN tpOrderPrices TEXT",
  "ALTER TABLE grids ADD COLUMN fillEntries TEXT",
  "ALTER TABLE grids ADD COLUMN openLots TEXT",
  "ALTER TABLE grids ADD COLUMN positionAmt REAL",
  "ALTER TABLE grids ADD COLUMN realizedPnl REAL",
  "ALTER TABLE grids ADD COLUMN realizedUsd REAL",
  "ALTER TABLE grids ADD COLUMN lastPrice REAL",
  "ALTER TABLE grids ADD COLUMN lastPlacementError TEXT",
  "ALTER TABLE grids ADD COLUMN engine TEXT DEFAULT 'browser'",
  "ALTER TABLE grids ADD COLUMN activated_at TEXT",
  "ALTER TABLE grids ADD COLUMN finished_at TEXT",
];

export const gridsTable = sqliteTable("grids", {
  id:                 integer("id").primaryKey({ autoIncrement: true }),
  uid:                text("uid").notNull().unique(),
  symbol:             text("symbol"),
  timeframe:          text("timeframe"),
  phase:              text("phase"),          // waiting | active | done | stopped
  direction:          text("direction"),      // long | short | both
  lo:                 real("lo"),
  hi:                 real("hi"),
  midPrice:           real("midPrice"),
  gate:               real("gate"),
  levels:             integer("levels"),
  levelPrices:        text("levelPrices"),    // JSON array
  tpPct:              real("tpPct"),
  slPct:              real("slPct"),
  edgePct:            real("edgePct"),
  orderSizeUsd:       real("orderSizeUsd"),
  leverage:           integer("leverage"),
  testnetOrderIds:    text("testnetOrderIds"), // JSON array
  stopOrderIds:       text("stopOrderIds"),    // JSON array
  tpOrderIds:         text("tpOrderIds"),      // JSON object { long?, short? }
  tpOrderPrices:      text("tpOrderPrices"),   // JSON object { long?, short? }
  fillEntries:        text("fillEntries"),     // JSON array
  openLots:           text("openLots"),        // JSON array
  positionAmt:        real("positionAmt"),
  realizedPnl:        real("realizedPnl"),
  realizedUsd:        real("realizedUsd"),
  lastPrice:          real("lastPrice"),
  lastPlacementError: text("lastPlacementError"),
  engine:             text("engine").default("browser"),
  created_at:         text("created_at"),
  activated_at:       text("activated_at"),
  finished_at:        text("finished_at"),
  updated_at:         text("updated_at"),
});

export type GridRow = typeof gridsTable.$inferSelect;
