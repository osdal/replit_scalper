import { sql, eq, and, ne } from "drizzle-orm";
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
const {
  botsTable,
  usersTable,
  userInternalAccountsTable,
  internalTransactionsTable,
  clientsTable,
  clientAccountsTable,
  tradesTable,
  ledgerTable,
} = await import("@workspace/db/schema");
const { GRID_HISTORY_CREATE_SQL, GRID_HISTORY_MIGRATIONS, GRID_CREATE_SQL, GRID_MIGRATIONS } =
  await import("@workspace/db");
const { sanitizeMaxPositions } = await import("./lib/slotReservations");

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

// Слоты на бота: приоритет у ключа max_positions в yaml, иначе — MAX_POSITIONS
// из docker-compose. Раньше фолбэком был жёсткий 1: init-db запускается
// ПРИ КАЖДОМ старте контейнера и затирал bots.max_positions обратно в 1, хотя
// MAX_POSITIONS=3. Бот получал 1 слот из /check и писал «[PAUSE] slots full
// (2/1)», хотя /trading/status показывал 3.
const ENV_MAX_POSITIONS = sanitizeMaxPositions(process.env.MAX_POSITIONS, 1);

// Создаём таблицы
await db.run(sql`CREATE TABLE IF NOT EXISTS bots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_key TEXT NOT NULL UNIQUE,
  client_id INTEGER NOT NULL,
  symbol TEXT NOT NULL, mode TEXT NOT NULL, timeframe TEXT NOT NULL,
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
  expired_after_minutes INTEGER NOT NULL DEFAULT 120,
  max_positions INTEGER NOT NULL DEFAULT 1,
  max_positions_with_expired INTEGER NOT NULL DEFAULT 3,
  slot_release_minutes INTEGER NOT NULL DEFAULT 360,
  position_loss_pct REAL NOT NULL DEFAULT 5,
  total_loss_pct REAL NOT NULL DEFAULT 10,
  armed INTEGER NOT NULL DEFAULT 1,
  stop_reason TEXT,
  stop_requested INTEGER NOT NULL DEFAULT 0,
  relay_only INTEGER NOT NULL DEFAULT 0
)`);

// Миграция: добавляем колонку llm_status, если её ещё нет (старые БД)
await db.run(sql`ALTER TABLE bots ADD COLUMN llm_status TEXT`).catch(() => { /* уже есть */ });
// Миграция: новые колонки для лимитов позиций
await db.run(sql`ALTER TABLE bots ADD COLUMN expired_after_minutes INTEGER NOT NULL DEFAULT 120`).catch(() => { /* уже есть */ });
await db.run(sql`ALTER TABLE bots ADD COLUMN max_positions INTEGER NOT NULL DEFAULT 1`).catch(() => { /* уже есть */ });
await db.run(sql`ALTER TABLE bots ADD COLUMN max_positions_with_expired INTEGER NOT NULL DEFAULT 3`).catch(() => { /* уже есть */ });
// Миграция: освобождение слота (отдельно от протухания) + проценты убытка.
await db.run(sql`ALTER TABLE bots ADD COLUMN slot_release_minutes INTEGER NOT NULL DEFAULT 360`).catch(() => { /* уже есть */ });
await db.run(sql`ALTER TABLE bots ADD COLUMN position_loss_pct REAL NOT NULL DEFAULT 5`).catch(() => { /* уже есть */ });
await db.run(sql`ALTER TABLE bots ADD COLUMN total_loss_pct REAL NOT NULL DEFAULT 10`).catch(() => { /* уже есть */ });

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

// История ног reverse-циклов: каждая нога отдельной строкой, включая ещё
// открытую. Позволяет показать клиенту/админу все ноги позиции по клику.
await db.run(sql`
  CREATE TABLE IF NOT EXISTS trade_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    client_id INTEGER,
    bot_id INTEGER,
    symbol TEXT NOT NULL,
    leg_index INTEGER NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    qty REAL NOT NULL,
    sl_price REAL,
    tp1_price REAL,
    pnl REAL,
    exit_reason TEXT,
    entry_time TEXT NOT NULL,
    exit_time TEXT,
    is_open INTEGER NOT NULL DEFAULT 0,
    commission REAL,
    created_at TEXT NOT NULL
  )
`);
await db.run(sql`CREATE INDEX IF NOT EXISTS idx_trade_legs_trade ON trade_legs(trade_id)`);
await db.run(sql`CREATE INDEX IF NOT EXISTS idx_trade_legs_client ON trade_legs(client_id)`);

// Мигрируем существующие БД: добавляем status/reject_reason и индикаторы/пресет, если их нет.
const { rows: tradeCols } = await db.run(sql`PRAGMA table_info(trades)`);
const tradeColNames: string[] = (tradeCols as any[]).map((r: any) => String(r.name));
const tradeExtraCols: Array<[string, string]> = [
  ["bot_id", "INTEGER"],
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
  ["user_id", "INTEGER"],
];
for (const [col, ddl] of botExtraCols) {
  if (!botColNames.includes(col)) {
    await db.run(sql`ALTER TABLE bots ADD COLUMN ${sql.raw(col)} ${sql.raw(ddl)}`);
    console.log(`  Added column bots.${col}`);
  }
}

// ── Платформа: клиенты, ключи, счета, журнал, аудит ────────────────────
// balance_usd — предоплата обслуживания (подарок/пополнения − комиссии),
// а не накопленная прибыль. При балансе ≤ 0 новые входы блокируются.
await db.run(sql`
  CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'client',
    status TEXT NOT NULL DEFAULT 'active',
    fee_pct REAL NOT NULL DEFAULT 10,
    gift_usd REAL NOT NULL DEFAULT 0,
    telegram_chat_id TEXT,
    auto_resume INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  )
`);
await db.run(sql`
  CREATE TABLE IF NOT EXISTS client_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'binance',
    label TEXT,
    api_key_mask TEXT NOT NULL,
    api_key_enc TEXT NOT NULL,
    api_secret_enc TEXT NOT NULL,
    iv TEXT NOT NULL,
    key_version INTEGER NOT NULL DEFAULT 1,
    can_trade INTEGER,
    can_withdraw INTEGER,
    ip_whitelisted INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    last_checked_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  )
`);
await db.run(sql`CREATE INDEX IF NOT EXISTS idx_client_api_keys_client ON client_api_keys(client_id)`);
await db.run(sql`
  CREATE TABLE IF NOT EXISTS client_accounts (
    client_id INTEGER PRIMARY KEY,
    balance_usd REAL NOT NULL DEFAULT 0,
    cum_pnl_usd REAL NOT NULL DEFAULT 0,
    hwm_pnl_usd REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
  )
`);
await db.run(sql`
  CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    balance_after_usd REAL NOT NULL,
    cum_pnl_after_usd REAL,
    related_bot_id INTEGER,
    related_trade_id INTEGER,
    reason TEXT,
    actor TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
  )
`);
await db.run(sql`CREATE INDEX IF NOT EXISTS idx_ledger_client ON ledger(client_id, created_at)`);
await db.run(sql`CREATE INDEX IF NOT EXISTS idx_ledger_trade ON ledger(related_trade_id)`);
await db.run(sql`
  CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    payload TEXT,
    ip TEXT,
    created_at TEXT NOT NULL
  )
`);
await db.run(sql`CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)`);

// Клиент №1 — владелец текущих ботов (system@local). Остальные появятся
// через регистрацию; боты к ним привязываются по client_id.
let client1Id: number;
{
  const [row] = await db
    .select()
    .from(clientsTable)
    .where(eq(clientsTable.email, "system@local"))
    .limit(1);
  if (row) {
    client1Id = Number(row.id);
  } else {
    const [inserted] = await db
      .insert(clientsTable)
      .values({ email: "system@local", password_hash: "", role: "owner", status: "active", fee_pct: 10 })
      .returning();
    client1Id = Number(inserted.id);
    console.log(`  Created client system@local (id=${client1Id})`);
  }
  await db
    .insert(clientAccountsTable)
    .values({ client_id: client1Id })
    .catch(() => { /* счёт уже есть */ });
}

// Миграция bots: symbol PRIMARY KEY → id + bot_key + client_id.
// Два клиента не могли торговать один символ: строка была одна. Теперь
// инстанс бота имеет стабильный bot_key (UNIQUE), а пара (client_id, symbol)
// уникальна. Для клиента №1 bot_key = lower(symbol) — старые URL, lock- и
// state-файлы продолжают работать без переименований.
const { rows: botColsNow } = await db.run(sql`PRAGMA table_info(bots)`);
const botColsNowNames: string[] = (botColsNow as any[]).map((r: any) => String(r.name));
if (!botColsNowNames.includes("id")) {
  const cols = botColsNow as Array<{ name: string; type: string; notnull: number; dflt_value: string | null }>;
  const defs = cols.map((c) => {
    // symbol был PK — в новой таблице это обычная NOT NULL колонка,
    // уникальность даёт составной индекс (client_id, symbol).
    const notNull = c.name === "symbol" ? 1 : Number(c.notnull);
    return `${c.name} ${c.type || "TEXT"}${notNull ? " NOT NULL" : ""}${c.dflt_value != null ? ` DEFAULT ${c.dflt_value}` : ""}`;
  });
  const copyCols = cols
    .map((c) => c.name)
    .filter((n) => !["id", "bot_key", "client_id"].includes(n));
  const list = copyCols.join(", ");
  await db.run(sql`DROP TABLE IF EXISTS bots_migrate_tmp`);
  await db.run(sql`BEGIN IMMEDIATE`);
  try {
    await db.run(sql.raw(`CREATE TABLE bots_migrate_tmp (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_key TEXT NOT NULL UNIQUE,
  client_id INTEGER NOT NULL,
  ${defs.join(",\n  ")}
)`));
    await db.run(sql.raw(
      `INSERT INTO bots_migrate_tmp (bot_key, client_id, ${list})
       SELECT lower(symbol), ${client1Id}, ${list} FROM bots`
    ));
    await db.run(sql`DROP TABLE bots`);
    await db.run(sql`ALTER TABLE bots_migrate_tmp RENAME TO bots`);
    await db.run(sql`COMMIT`);
    console.log(`  Migrated bots → instances (id/bot_key/client_id), ${cols.length} columns`);
  } catch (e) {
    await db.run(sql`ROLLBACK`).catch(() => { /* транзакция уже закрыта */ });
    throw e;
  }
}
await db.run(sql`CREATE UNIQUE INDEX IF NOT EXISTS idx_bots_client_symbol ON bots(client_id, symbol)`);
await db.run(sql`CREATE INDEX IF NOT EXISTS idx_bots_client ON bots(client_id)`);

// Атрибуция существующих сделок: bot_id по символу (клиент №1).
await db.run(sql`
  UPDATE trades SET bot_id = (SELECT b.id FROM bots b WHERE b.symbol = trades.symbol)
  WHERE bot_id IS NULL
`);
await db.run(sql`CREATE INDEX IF NOT EXISTS idx_trades_bot_id ON trades(bot_id)`);

// Внутренние счета пользователей (комиссия 10% с прибыли при продаже ботов).
await db.run(sql`
  CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'buyer',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  )
`);
await db.run(sql`
  CREATE TABLE IF NOT EXISTS user_internal_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    balance_usd REAL NOT NULL DEFAULT 0,
    high_water_mark_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  )
`);
await db.run(sql`
  CREATE TABLE IF NOT EXISTS internal_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    balance_after_usd REAL NOT NULL,
    related_trade_id INTEGER,
    created_at TEXT NOT NULL
  )
`);
// Один credit на сделку: защита от повторного начисления при ретраях PATCH
// (refinalize, ручной повтор). Частичный индекс, чтобы не мешать commission
// и другим типам транзакций по той же сделке.
await db.run(sql`
  CREATE UNIQUE INDEX IF NOT EXISTS idx_internal_credit_once
  ON internal_transactions(related_trade_id)
  WHERE type = 'credit'
`);

// Дефолтный пользователь для ботов, у которых пока нет владельца.
const [defaultUser] = await db
  .select()
  .from(usersTable)
  .where(sql`email = 'system@local'`)
  .limit(1);
let defaultUserId: number;
if (!defaultUser) {
  const [inserted] = await db
    .insert(usersTable)
    .values({ email: "system@local", password_hash: "", role: "owner" })
    .returning();
  defaultUserId = Number(inserted.id);
  await db
    .insert(userInternalAccountsTable)
    .values({ user_id: defaultUserId, balance_usd: 0, high_water_mark_usd: 0 });
  console.log(`  Created default user system@local (id=${defaultUserId})`);
} else {
  defaultUserId = Number(defaultUser.id);
  await db
    .insert(userInternalAccountsTable)
    .values({ user_id: defaultUserId, balance_usd: 0, high_water_mark_usd: 0 })
    .catch(() => { /* счёт уже есть */ });
}

// ── Переход на модель B (предоплаченный внутренний счёт) ────────────────────
// Модель A зачисляла на счёт 90% прибыли сделки. Новые правила:
//   • прибыль остаётся на биржевом счету клиента и НЕ зачисляется;
//   • при закрытии прибыльной сделки с баланса списывается 10% её pnl;
//   • убыточная сделка баланс не меняет;
//   • пополнение — только ручное (сейчас владельцем, позже клиентом).
// Один раз обнуляем legacy-баланс клиента №1 и фиксируем справочный
// cum_pnl/hwm на текущем накопленном PnL. Маркер в ledger делает миграцию
// идемпотентной: после ручного пополнения повторный старт ничего не обнулит.
{
  const resetKey = `model-b-reset:client:${client1Id}`;
  const [marker] = await db
    .select({ id: ledgerTable.id })
    .from(ledgerTable)
    .where(eq(ledgerTable.idempotency_key, resetKey))
    .limit(1);

  if (!marker) {
    const [{ cum }] = await db
      .select({ cum: sql<number>`COALESCE(SUM(${tradesTable.pnl}), 0)` })
      .from(tradesTable)
      .innerJoin(botsTable, eq(botsTable.id, tradesTable.bot_id))
      .where(and(
        eq(botsTable.client_id, client1Id),
        eq(tradesTable.is_open, false),
        ne(tradesTable.status, "rejected"),
      ));
    const cumPnl = Math.round(Number(cum || 0) * 10000) / 10000;
    const nowIso = new Date().toISOString();

    await db
      .update(clientAccountsTable)
      .set({
        balance_usd: 0,
        cum_pnl_usd: cumPnl,
        hwm_pnl_usd: Math.max(0, cumPnl),
        updated_at: nowIso,
      })
      .where(eq(clientAccountsTable.client_id, client1Id));

    await db
      .insert(ledgerTable)
      .values({
        client_id: client1Id,
        type: "adjustment",
        amount_usd: 0,
        balance_after_usd: 0,
        cum_pnl_after_usd: cumPnl,
        reason: "Переход на модель B: прибыль не зачисляется, legacy-баланс обнулён",
        actor: "init-db",
        idempotency_key: resetKey,
      })
      .catch(() => { /* гонка двух инстансов — маркер уже есть */ });

    // Легаси-счёт модели A тоже обнуляем, чтобы вкладка «Пользователи»
    // не показывала зачисленную прибыль как доступный баланс.
    await db
      .update(userInternalAccountsTable)
      .set({ balance_usd: 0, high_water_mark_usd: 0, updated_at: nowIso })
      .where(eq(userInternalAccountsTable.user_id, defaultUserId));

    console.log(`  Model B: reset legacy balance (client=${client1Id}), cum_pnl=$${cumPnl.toFixed(4)}`);
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
      expired_after_minutes: (raw.expired_after_minutes as number) ?? 120,
      // max_positions / max_positions_with_expired — ГЛОБАЛЬНЫЕ (ENV_MAX_POSITIONS),
      // per-bot поля убраны. Не перезаписываем из YAML.
      // max_positions: (raw.max_positions as number) ?? ENV_MAX_POSITIONS,
      // max_positions_with_expired: (raw.max_positions_with_expired as number) ?? 3,
      // Новые риск-поля перезаписываем только если ключ реально есть в yaml:
      // иначе UI-значения из БД затирались бы дефолтами при каждом рестарте.
      ...(raw.slot_release_minutes !== undefined
        ? { slot_release_minutes: Number(raw.slot_release_minutes) } : {}),
      ...(raw.position_loss_pct !== undefined
        ? { position_loss_pct: Number(raw.position_loss_pct) } : {}),
      ...(raw.total_loss_pct !== undefined
        ? { total_loss_pct: Number(raw.total_loss_pct) } : {}),
      // Владельца, назначенного вручную (продажа бота покупателю), не затираем.
      user_id:          existing.user_id ?? defaultUserId,
      updated_at:       new Date().toISOString(),
    }).where(eq(botsTable.symbol, symbol));
    console.log(`  ${symbol} updated from ${file}`);
    continue;
  }

  await db.insert(botsTable).values({
    bot_key: symbol.toLowerCase(),
    client_id: client1Id,
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
    expired_after_minutes: (raw.expired_after_minutes as number) ?? 120,
      // max_positions / max_positions_with_expired — ГЛОБАЛЬНЫЕ (ENV_MAX_POSITIONS),
      // per-bot поля убраны. БД оставляет 0, читаются из env в trading.ts.
      max_positions: 0,
      max_positions_with_expired: 0,
      armed: BOT_ENV !== "live",
      relay_only: BOT_ENV === "live",
      user_id: defaultUserId,
      updated_at: new Date().toISOString(),
  });
  console.log(`  Added ${symbol} from ${file}`);
}

console.log("Done");
