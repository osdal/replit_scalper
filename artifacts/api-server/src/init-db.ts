import { sql, eq } from "drizzle-orm";
import fs from "fs";
import path from "path";
import yaml from "js-yaml";
import { config } from "dotenv";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BOT_ENV = (process.env.BOT_ENV || "testnet").trim().toLowerCase() || "testnet";
config({ path: path.resolve(__dirname, `../../../.env.${BOT_ENV}`) });
config({ path: path.resolve(__dirname, "../../../.env"), override: false });

// @workspace/db подключается динамическим импортом СТРОГО ПОСЛЕ загрузки
// env-файлов: сам модуль читает только корневой .env (без override) и,
// получив DATABASE_PATH до вызова config() выше, открыл бы НЕ ТУ БД —
// для live это означало бы миграцию и правки карточек в testnet-базе.
const { db } = await import("@workspace/db");
const { botsTable } = await import("@workspace/db/schema");
const { GRID_HISTORY_CREATE_SQL, GRID_HISTORY_MIGRATIONS, GRID_CREATE_SQL, GRID_MIGRATIONS } =
  await import("@workspace/db");

// Use absolute paths from .env for reliability
const projectRoot = path.resolve(__dirname, "../../../");
const BOT_DIR = process.env.BOT_DIR 
  ? (process.env.BOT_DIR.match(/^[A-Za-z]:/) 
      ? process.env.BOT_DIR
      : path.join(projectRoot, process.env.BOT_DIR))
  : path.join(projectRoot, "bot");
const BOT_CONFIG_DIR = process.env.BOT_CONFIG_DIR
  ? (path.isAbsolute(process.env.BOT_CONFIG_DIR)
      ? process.env.BOT_CONFIG_DIR
      : path.join(projectRoot, process.env.BOT_CONFIG_DIR))
  : path.join(BOT_DIR, "configs", BOT_ENV);

// Создаём таблицы
await db.run(sql`CREATE TABLE IF NOT EXISTS bots (
  symbol TEXT PRIMARY KEY, mode TEXT NOT NULL, timeframe TEXT NOT NULL,
  leverage INTEGER NOT NULL, risk_pct REAL NOT NULL, sl_pct REAL NOT NULL,
  tp1_pct REAL NOT NULL, tp1_close_pct REAL NOT NULL, tp2_pct REAL NOT NULL,
  ema_fast INTEGER NOT NULL, ema_slow INTEGER NOT NULL,
  volume_ma_period INTEGER NOT NULL, volume_multiplier REAL NOT NULL,
  htf_enabled INTEGER NOT NULL DEFAULT 0, htf_timeframe TEXT,
  htf_ema_fast INTEGER, htf_ema_slow INTEGER,
  htf2_enabled INTEGER NOT NULL DEFAULT 0, htf2_timeframe TEXT,
  htf2_ema_fast INTEGER, htf2_ema_slow INTEGER,
  auto_mode INTEGER NOT NULL DEFAULT 1, paper_balance REAL NOT NULL DEFAULT 1000,
  log_file TEXT NOT NULL, is_running INTEGER NOT NULL DEFAULT 0,
  last_heartbeat TEXT, current_price REAL, position TEXT,
  llm_status TEXT, updated_at TEXT NOT NULL,
  trade_mode TEXT NOT NULL DEFAULT 'manual',
  position_size_usd REAL NOT NULL DEFAULT 0,
  position_size_pct REAL NOT NULL DEFAULT 1,
  reverse_chain_max INTEGER NOT NULL DEFAULT 10,
  max_position_notional_usd REAL NOT NULL DEFAULT 0,
  max_position_pct_equity REAL NOT NULL DEFAULT 0,
  armed INTEGER NOT NULL DEFAULT 1,
  stop_reason TEXT,
  stop_requested INTEGER NOT NULL DEFAULT 0,
  relay_only INTEGER NOT NULL DEFAULT 0
)`);

// Миграция: добавляем колонку llm_status, если её ещё нет (старые БД)
await db.run(sql`ALTER TABLE bots ADD COLUMN llm_status TEXT`).catch(() => { /* уже есть */ });

await db.run(sql`CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
  direction TEXT NOT NULL, entry_price REAL NOT NULL, exit_price REAL,
  qty REAL NOT NULL, sl_price REAL NOT NULL DEFAULT 0,
  tp1_price REAL NOT NULL DEFAULT 0, tp2_price REAL NOT NULL DEFAULT 0,
  pnl REAL, exit_reason TEXT, entry_time TEXT NOT NULL, exit_time TEXT,
  is_open INTEGER NOT NULL DEFAULT 1, ema_fast REAL, ema_slow REAL,
  volume REAL, volume_ma REAL, mode TEXT NOT NULL DEFAULT 'live',
  status TEXT NOT NULL DEFAULT 'open', reject_reason TEXT,
  rsi REAL, macd REAL, macd_signal REAL, macd_hist REAL,
  bb_upper REAL, bb_middle REAL, bb_lower REAL, atr REAL,
  preset TEXT, commission REAL,
  leg_pnl REAL, cycle_pnl REAL, chain_depth INTEGER, cycle_close_reason TEXT
)`);

// Мигрируем существующие БД: добавляем status/reject_reason и индикаторы/пресет, если их нет.
const { rows: tradeCols } = await db.run(sql`PRAGMA table_info(trades)`);
const tradeColNames: string[] = (tradeCols as any[]).map((r: any) => String(r.name));
const tradeExtraCols: Array<[string, string]> = [
  ["status", "TEXT NOT NULL DEFAULT 'open'"],
  ["reject_reason", "TEXT"],
  ["rsi", "REAL"],
  ["macd", "REAL"],
  ["macd_signal", "REAL"],
  ["macd_hist", "REAL"],
  ["bb_upper", "REAL"],
  ["bb_middle", "REAL"],
  ["bb_lower", "REAL"],
  ["atr", "REAL"],
  ["preset", "TEXT"],
  ["commission", "REAL"],
  ["quote_volume", "REAL"],
  ["leg_pnl", "REAL"],
  ["cycle_pnl", "REAL"],
  ["chain_depth", "INTEGER"],
  ["cycle_close_reason", "TEXT"],
];
for (const [col, ddl] of tradeExtraCols) {
  if (!tradeColNames.includes(col)) {
    await db.run(sql`ALTER TABLE trades ADD COLUMN ${sql.raw(col)} ${sql.raw(ddl)}`);
    console.log(`  Added column trades.${col}`);
  }
}
await db.run(sql`UPDATE trades SET status='closed' WHERE is_open=0 AND status != 'rejected'`);

// Мигрируем bots: добавляем HTF2 колонки если их нет.
const { rows: botCols } = await db.run(sql`PRAGMA table_info(bots)`);
const botColNames: string[] = (botCols as any[]).map((r: any) => String(r.name));
const botExtraCols: Array<[string, string]> = [
  ["htf2_enabled", "INTEGER NOT NULL DEFAULT 0"],
  ["htf2_timeframe", "TEXT"],
  ["htf2_ema_fast", "INTEGER"],
  ["htf2_ema_slow", "INTEGER"],
  ["trade_mode", "TEXT NOT NULL DEFAULT 'manual'"],
  ["position_size_usd", "REAL NOT NULL DEFAULT 0"],
  ["position_size_pct", "REAL NOT NULL DEFAULT 1"],
  ["armed", "INTEGER NOT NULL DEFAULT 1"],
  ["stop_reason", "TEXT"],
  ["stop_requested", "INTEGER NOT NULL DEFAULT 0"],
  ["relay_only", "INTEGER NOT NULL DEFAULT 0"],
  ["reverse_chain_max", "INTEGER NOT NULL DEFAULT 10"],
  ["max_position_notional_usd", "REAL NOT NULL DEFAULT 0"],
  ["max_position_pct_equity", "REAL NOT NULL DEFAULT 0"],
];
for (const [col, ddl] of botExtraCols) {
  if (!botColNames.includes(col)) {
    await db.run(sql`ALTER TABLE bots ADD COLUMN ${sql.raw(col)} ${sql.raw(ddl)}`);
    console.log(`  Added column bots.${col}`);
  }
}

await db.run(sql`CREATE TABLE IF NOT EXISTS recovery_chains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  debt_amount REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'free',
  locked_by TEXT,
  locked_trade_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT
)`);

await db.run(sql`CREATE TABLE IF NOT EXISTS trading_control (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  loss_streak INTEGER NOT NULL DEFAULT 0,
  paused_remaining INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
)`);

// Мигрируем trading_control: добавляем active_open (атомарный счётчик открытых слотов), если его нет.
const { rows: ctlCols } = await db.run(sql`PRAGMA table_info(trading_control)`);
const ctlColNames: string[] = (ctlCols as any[]).map((r: any) => String(r.name));
if (!ctlColNames.includes("active_open")) {
  await db.run(sql`ALTER TABLE trading_control ADD COLUMN active_open INTEGER NOT NULL DEFAULT 0`);
  console.log("  Added column trading_control.active_open");
}

// Очередь релей-сигналов testnet → live.
await db.run(sql`CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'entry',
  direction TEXT NOT NULL,
  entry_price REAL NOT NULL,
  sl_price REAL,
  tp1_price REAL,
  tp2_price REAL,
  preset TEXT,
  source TEXT,
  payload TEXT,
  created_at TEXT NOT NULL,
  consumed_at TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  note TEXT
)`);
// Мигрируем signals: status/note, если таблица уже была.
const { rows: sigCols } = await db.run(sql`PRAGMA table_info(signals)`);
const sigColNames: string[] = (sigCols as any[]).map((r: any) => String(r.name));
if (!sigColNames.includes("status")) {
  await db.run(sql`ALTER TABLE signals ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'`);
  console.log("  Added column signals.status");
}
if (!sigColNames.includes("note")) {
  await db.run(sql`ALTER TABLE signals ADD COLUMN note TEXT`);
  console.log("  Added column signals.note");
}

await db.run(sql.raw(GRID_HISTORY_CREATE_SQL));

// Миграции: добавляем недостающие колонки в уже созданных БД.
for (const migration of GRID_HISTORY_MIGRATIONS) {
  await db.run(sql.raw(migration)).catch(() => { /* колонка уже есть */ });
}

await db.run(sql.raw(GRID_CREATE_SQL));

// То же для таблицы grids: DDL + идемпотентные миграции для старых БД.
for (const migration of GRID_MIGRATIONS) {
  await db.run(sql.raw(migration)).catch(() => { /* колонка уже есть */ });
}

console.log("Tables created");

const configs = fs.readdirSync(BOT_CONFIG_DIR).filter(f => /^config_\w+\.yaml$/.test(f) && f !== "config.yaml");

for (const file of configs) {
  const raw = yaml.load(fs.readFileSync(path.join(BOT_CONFIG_DIR, file), "utf8")) as Record<string, unknown>;
  const symbol = (raw.symbol as string).toUpperCase();
  const [existing] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
  if (existing) {
    // Обновляем mode и конфигурацию из yaml
    await db.update(botsTable).set({
      mode:             (raw.mode as string) || "live",
      timeframe:        raw.timeframe as string,
      leverage:         raw.leverage as number,
      risk_pct:         raw.risk_pct as number,
      sl_pct:           raw.sl_pct as number,
      tp1_pct:          raw.tp1_pct as number,
      tp1_close_pct:    raw.tp1_close_pct as number,
      tp2_pct:          raw.tp2_pct as number,
      ema_fast:         raw.ema_fast as number,
      ema_slow:         raw.ema_slow as number,
      volume_ma_period: raw.volume_ma_period as number,
      volume_multiplier: raw.volume_multiplier as number,
      htf_enabled:      (raw.htf_enabled as boolean) || false,
      htf_timeframe:    (raw.htf_timeframe as string) || null,
      htf_ema_fast:     (raw.htf_ema_fast as number) || null,
      htf_ema_slow:     (raw.htf_ema_slow as number) || null,
      htf2_enabled:     (raw.htf2_enabled as boolean) || false,
      htf2_timeframe:   (raw.htf2_timeframe as string) || null,
      htf2_ema_fast:    (raw.htf2_ema_fast as number) || null,
      htf2_ema_slow:    (raw.htf2_ema_slow as number) || null,
      auto_mode:        (raw.auto_mode as boolean) ?? true,
      paper_balance:    (raw.paper_balance as number) || 1000,
      log_file:         raw.log_file as string,
      trade_mode:       (raw.trade_mode as string) || "manual",
      position_size_usd: (raw.position_size_usd as number) || 0,
      position_size_pct: (raw.position_size_pct as number) ?? 1,
      reverse_chain_max: (raw.reverse_chain_max as number) ?? 10,
      max_position_notional_usd: (raw.max_position_notional_usd as number) || 0,
      max_position_pct_equity: (raw.max_position_pct_equity as number) || 0,
      updated_at:       new Date().toISOString(),
    }).where(eq(botsTable.symbol, symbol));
    console.log(`  ${symbol} updated from ${file}`);
    continue;
  }

  await db.insert(botsTable).values({
    symbol, mode: (raw.mode as string) || "live",
    timeframe: raw.timeframe as string, leverage: raw.leverage as number,
    risk_pct: raw.risk_pct as number, sl_pct: raw.sl_pct as number,
    tp1_pct: raw.tp1_pct as number, tp1_close_pct: raw.tp1_close_pct as number,
    tp2_pct: raw.tp2_pct as number, ema_fast: raw.ema_fast as number,
    ema_slow: raw.ema_slow as number, volume_ma_period: raw.volume_ma_period as number,
    volume_multiplier: raw.volume_multiplier as number,
      htf_enabled: (raw.htf_enabled as boolean) || false,
      htf_timeframe: (raw.htf_timeframe as string) || null,
      htf_ema_fast: (raw.htf_ema_fast as number) || null,
      htf_ema_slow: (raw.htf_ema_slow as number) || null,
      htf2_enabled: (raw.htf2_enabled as boolean) || false,
      htf2_timeframe: (raw.htf2_timeframe as string) || null,
      htf2_ema_fast: (raw.htf2_ema_fast as number) || null,
      htf2_ema_slow: (raw.htf2_ema_slow as number) || null,
    auto_mode: (raw.auto_mode as boolean) ?? true,
    paper_balance: (raw.paper_balance as number) || 1000,
    log_file: raw.log_file as string, is_running: false,
    trade_mode: (raw.trade_mode as string) || "manual",
    position_size_usd: (raw.position_size_usd as number) || 0,
    position_size_pct: (raw.position_size_pct as number) ?? 1,
    reverse_chain_max: (raw.reverse_chain_max as number) ?? 10,
    max_position_notional_usd: (raw.max_position_notional_usd as number) || 0,
    max_position_pct_equity: (raw.max_position_pct_equity as number) || 0,
    armed: BOT_ENV !== "live",
    relay_only: BOT_ENV === "live",
    updated_at: new Date().toISOString(),
  });
  console.log(`  Added ${symbol} from ${file}`);
}

console.log("Done");
