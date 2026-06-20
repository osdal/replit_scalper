import { db } from "@workspace/db";
import { botsTable } from "@workspace/db/schema";
import { sql, eq } from "drizzle-orm";
import fs from "fs";
import path from "path";
import yaml from "js-yaml";

const BOT_DIR = process.env.BOT_DIR || path.resolve("../../bot");

// Создаём таблицы
await db.run(sql`CREATE TABLE IF NOT EXISTS bots (
  symbol TEXT PRIMARY KEY, mode TEXT NOT NULL, timeframe TEXT NOT NULL,
  leverage INTEGER NOT NULL, risk_pct REAL NOT NULL, sl_pct REAL NOT NULL,
  tp1_pct REAL NOT NULL, tp1_close_pct REAL NOT NULL, tp2_pct REAL NOT NULL,
  ema_fast INTEGER NOT NULL, ema_slow INTEGER NOT NULL,
  volume_ma_period INTEGER NOT NULL, volume_multiplier REAL NOT NULL,
  htf_enabled INTEGER NOT NULL DEFAULT 0, htf_timeframe TEXT,
  htf_ema_fast INTEGER, htf_ema_slow INTEGER,
  auto_mode INTEGER NOT NULL DEFAULT 1, paper_balance REAL NOT NULL DEFAULT 1000,
  log_file TEXT NOT NULL, is_running INTEGER NOT NULL DEFAULT 0,
  last_heartbeat TEXT, current_price REAL, position TEXT,
  updated_at TEXT NOT NULL
)`);

await db.run(sql`CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
  direction TEXT NOT NULL, entry_price REAL NOT NULL, exit_price REAL,
  qty REAL NOT NULL, sl_price REAL NOT NULL DEFAULT 0,
  tp1_price REAL NOT NULL DEFAULT 0, tp2_price REAL NOT NULL DEFAULT 0,
  pnl REAL, exit_reason TEXT, entry_time TEXT NOT NULL, exit_time TEXT,
  is_open INTEGER NOT NULL DEFAULT 1, ema_fast REAL, ema_slow REAL,
  volume REAL, volume_ma REAL, mode TEXT NOT NULL DEFAULT 'live'
)`);

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

console.log("Tables created");

const configs = fs.readdirSync(BOT_DIR).filter(f => /^config_\w+\.yaml$/.test(f) && f !== "config.yaml");

for (const file of configs) {
  const raw = yaml.load(fs.readFileSync(path.join(BOT_DIR, file), "utf8")) as Record<string, unknown>;
  const symbol = (raw.symbol as string).toUpperCase();
  const [existing] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
  if (existing) {
    // Обновляем mode и конфигурацию из yaml
    await db.update(botsTable).set({
      mode:             (raw.mode as string) || "paper",
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
      auto_mode:        (raw.auto_mode as boolean) ?? true,
      paper_balance:    (raw.paper_balance as number) || 1000,
      log_file:         raw.log_file as string,
      updated_at:       new Date().toISOString(),
    }).where(eq(botsTable.symbol, symbol));
    console.log(`  ${symbol} updated from ${file}`);
    continue;
  }

  await db.insert(botsTable).values({
    symbol, mode: (raw.mode as string) || "paper",
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
    auto_mode: (raw.auto_mode as boolean) ?? true,
    paper_balance: (raw.paper_balance as number) || 1000,
    log_file: raw.log_file as string, is_running: false,
    updated_at: new Date().toISOString(),
  });
  console.log(`  Added ${symbol} from ${file}`);
}

console.log("Done");
