/**
 * Глобальное управление рисками по всему портфелю.
 * Все боты работают как отдельные процессы — здесь единая точка, где
 * согласуются два правила:
 *   1) максимум N одновременно открытых позиций;
 *   2) пауза после K подряд убытков (общий счётчик) — пропуск M сигналов.
 *
 * Состояние подряд убытков хранится в БД (trading_control, одна строка id=1),
 * т.к. боты распределены и in-memory не согласован.
 */
import { Router } from "express";
import { db, tradesTable, tradingControlTable, recoveryChainsTable, botsTable } from "@workspace/db";
import { eq, sql, gte } from "drizzle-orm";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import yaml from "js-yaml";
import { stopAllBots } from "./bots";
import { binanceRequest } from "../grid-orders-lib";
import { SlotReservations, sanitizeMaxPositions } from "../lib/slotReservations";

const router = Router();

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const CONFIG_PATH = process.env.RECOVERY_CONFIG_PATH ||
  path.resolve(__dirname, "../../../../bot/recovery_config.yaml");

function sanitizeIgnoreAfterHours(value: unknown, fallback: number): number {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return fallback;
  return n;
}

/**
 * ENV-оверрайды поверх yaml. Нужны, чтобы включить «протухание» позиций
 * (MAX_POSITIONS_IGNORE_AFTER_HOURS) только на одном стеке: yaml общий для
 * api-testnet и api-live, а менять поведение live нельзя.
 */
function applyEnvOverrides<T extends { max_positions: number; max_positions_ignore_after_hours: number }>(cfg: T): T {
  const env_max = process.env.MAX_POSITIONS;
  if (env_max !== undefined && env_max !== null && env_max !== "") {
    // sanitize: Number("abc")=NaN, а `x >= NaN` всегда false → лимит молча отключился бы.
    cfg.max_positions = sanitizeMaxPositions(env_max, cfg.max_positions);
  }
  const env_ignore = process.env.MAX_POSITIONS_IGNORE_AFTER_HOURS;
  if (env_ignore !== undefined && env_ignore !== null && env_ignore !== "") {
    cfg.max_positions_ignore_after_hours = sanitizeIgnoreAfterHours(
      env_ignore, cfg.max_positions_ignore_after_hours,
    );
  }
  return cfg;
}

function readConfig(): { max_positions: number; loss_streak_trigger: number; loss_pause_signals: number; max_free_debt_usd: number; daily_loss_limit_usd: number; pause_timeout_minutes: number; max_positions_ignore_after_hours: number } {
  const def = { max_positions: 2, loss_streak_trigger: 2, loss_pause_signals: 3, max_free_debt_usd: 15.0, daily_loss_limit_usd: 8.0, pause_timeout_minutes: 120, max_positions_ignore_after_hours: 2 };
  try {
    const raw = yaml.load(fs.readFileSync(CONFIG_PATH, "utf8")) as any;
    const cfg = {
      max_positions: Number(raw.max_positions) || def.max_positions,
      loss_streak_trigger: Number(raw.loss_streak_trigger) || def.loss_streak_trigger,
      loss_pause_signals: Number(raw.loss_pause_signals) || def.loss_pause_signals,
      max_free_debt_usd: Number(raw.max_free_debt_usd) || def.max_free_debt_usd,
      daily_loss_limit_usd: Number(raw.daily_loss_limit_usd) || def.daily_loss_limit_usd,
      pause_timeout_minutes: raw.pause_timeout_minutes !== undefined
        ? Number(raw.pause_timeout_minutes)
        : def.pause_timeout_minutes,
      max_positions_ignore_after_hours: raw.max_positions_ignore_after_hours !== undefined
        ? sanitizeIgnoreAfterHours(raw.max_positions_ignore_after_hours, def.max_positions_ignore_after_hours)
        : def.max_positions_ignore_after_hours,
    };
    return applyEnvOverrides(cfg);
  } catch {
    return applyEnvOverrides({ ...def });
  }
}

async function getControlRow() {
  const [row] = await db.select().from(tradingControlTable).where(eq(tradingControlTable.id, 1));
  if (row) return row;
  const now = new Date().toISOString();
  const [created] = await db.insert(tradingControlTable)
    .values({ id: 1, loss_streak: 0, paused_remaining: 0, updated_at: now })
    .onConflictDoNothing()
    .returning();
  return created || { id: 1, loss_streak: 0, paused_remaining: 0, updated_at: now };
}

/**
 * Автосброс паузы/счётчика убытков по времени.
 * Если после последнего события (updated_at) прошло >= pause_timeout_minutes без
 * новых сделок, риск-состояние "забывается": пауза и счётчик подряд убытков
 * сбрасываются. Это не даёт паузе или накопленному счётчику "зомбироваться"
 * через долгую тишину на рынке.
 * Возвращает true, если произошёл сброс.
 */
async function autoResetStale(control: any, timeoutMinutes: number): Promise<boolean> {
  if (timeoutMinutes <= 0) return false;
  if (!control.updated_at) return false;
  const updatedMs = new Date(control.updated_at).getTime();
  if (!Number.isFinite(updatedMs)) return false;
  const elapsedMin = (Date.now() - updatedMs) / 60000;
  if (elapsedMin < timeoutMinutes) return false;
  // Сброс: только если есть что сбрасывать.
  if (control.loss_streak > 0 || control.paused_remaining > 0) {
    await db.update(tradingControlTable)
      .set({ loss_streak: 0, paused_remaining: 0, updated_at: new Date().toISOString() })
      .where(eq(tradingControlTable.id, 1));
    return true;
  }
  return false;
}

// ── Слоты глобального лимита позиций ──────────────────────────────────────────
// Слот привязан к символу. Занято = символы с открытой ПРИНЯТОЙ позицией в БД
// (status != 'rejected'; отклонённые виртуальные слот не занимают) ∪ символы с
// живым резервом (бот уже получил разрешение, но строка в БД ещё не появилась)
// [∪ символы с позицией на бирже при MAX_POSITIONS_EXCHANGE_CHECK=1].
// Подробности и обоснование — lib/slotReservations.ts.
const SLOT_TTL_MS = Math.max(5, Number(process.env.SLOT_RESERVATION_TTL_SEC) || 60) * 1000;
const slots = new SlotReservations({ ttlMs: SLOT_TTL_MS });

const EXCHANGE_CHECK = ["1", "true", "yes", "on"].includes(
  String(process.env.MAX_POSITIONS_EXCHANGE_CHECK ?? "").trim().toLowerCase(),
);
const EXCHANGE_CACHE_MS = 2000;
let exchangeCache: { ts: number; symbols: string[] } | null = null;

/** Символы с ненулевой позицией на бирже; null — не удалось получить (тогда опираемся на БД+резервы). */
async function exchangeOpenSymbols(): Promise<string[] | null> {
  if (exchangeCache && Date.now() - exchangeCache.ts < EXCHANGE_CACHE_MS) return exchangeCache.symbols;
  try {
    const raw = await binanceRequest("GET", "/fapi/v2/positionRisk", {});
    const list: any[] = Array.isArray(raw) ? raw : raw ? [raw] : [];
    const symbols = new Set<string>();
    for (const p of list) {
      const amt = Number(p?.positionAmt ?? 0) || 0;
      if (Math.abs(amt) < 1e-12) continue;
      const sym = String(p?.symbol || "").toUpperCase();
      if (sym) symbols.add(sym);
    }
    exchangeCache = { ts: Date.now(), symbols: [...symbols] };
    return exchangeCache.symbols;
  } catch (e) {
    console.warn("[trading] exchange position check failed, falling back to DB+reservations:", String(e));
    return null;
  }
}

/** Символы с открытой принятой позицией в БД. ignoreAfterHours=0 — считать все. */
async function dbOpenSymbols(ignoreAfterHours: number): Promise<string[]> {
  const cutoff = ignoreAfterHours > 0
    ? new Date(Date.now() - ignoreAfterHours * 60 * 60 * 1000).toISOString()
    : null;
  const rows = cutoff
    ? await db.selectDistinct({ symbol: tradesTable.symbol })
        .from(tradesTable)
        .where(sql`is_open = 1 AND status != 'rejected' AND entry_time >= ${cutoff}`)
    : await db.selectDistinct({ symbol: tradesTable.symbol })
        .from(tradesTable)
        .where(sql`is_open = 1 AND status != 'rejected'`);
  return rows.map((r) => String(r.symbol || "").toUpperCase()).filter(Boolean);
}

async function loadOccupiedSymbols(ignoreAfterHours: number): Promise<string[]> {
  const fromDb = await dbOpenSymbols(ignoreAfterHours);
  if (!EXCHANGE_CHECK) return fromDb;
  const fromExchange = await exchangeOpenSymbols();
  return fromExchange ? [...new Set([...fromDb, ...fromExchange])] : fromDb;
}

// Точечная проверка «есть ли позиция по символу на бирже». Нужна только когда
// слот уже занят и запрос информационный (reverse-нога). Кэш на символ, чтобы
// ретраи заблокированной цепочки (раз в тик) не жгли weight.
const SYM_POS_TTL_MS = 5000;
const symPosCache = new Map<string, { ts: number; has: boolean }>();

async function exchangeHasPosition(symbol: string): Promise<boolean> {
  const cached = symPosCache.get(symbol);
  if (cached && Date.now() - cached.ts < SYM_POS_TTL_MS) return cached.has;
  try {
    const raw = await binanceRequest("GET", "/fapi/v2/positionRisk", { symbol });
    const list: any[] = Array.isArray(raw) ? raw : raw ? [raw] : [];
    const has = list.some((p) => Math.abs(Number(p?.positionAmt ?? 0) || 0) > 1e-12);
    symPosCache.set(symbol, { ts: Date.now(), has });
    return has;
  } catch (e) {
    // Не смогли спросить биржу — считаем «нет»: лимит остаётся строгим.
    console.warn(`[trading] own-slot exchange check failed for ${symbol}:`, String(e));
    return false;
  }
}

/** Сумма убытков (pnl < 0) по закрытым сделкам за текущий UTC-день. Положительное число. */
async function getDailyLoss(): Promise<number> {
  const startToday = new Date();
  startToday.setUTCHours(0, 0, 0, 0);
  const [res] = await db.select({ loss: sql<number>`COALESCE(SUM(-pnl),0)` })
    .from(tradesTable)
    .where(sql`is_open = 0 AND pnl < 0 AND exit_time >= ${startToday.toISOString()}`);
  return Number(res?.loss || 0);
}

/** Сумма всех неразрешённых долгов recovery (free + locked). */
async function getFreeDebt(): Promise<number> {
  const [res] = await db.select({ debt: sql<number>`COALESCE(SUM(debt_amount),0)` })
    .from(recoveryChainsTable)
    .where(sql`status IN ('free','locked')`);
  return Number(res?.debt || 0);
}

/**
 * POST /api/trading/check
 * Бот спрашивает перед открытием позиции: можно ли.
 * Учитывает: лимит позиций, паузу после серии убытков, потолок долга,
 * дневной лимит убытков.
 * Возвращает: { allowed, reason?, positions_open, loss_streak, paused_remaining, daily_loss }
 */
router.post("/check", async (req, res) => {
  try {
    const globalCfg = readConfig();
    const control = await getControlRow();
    const symbol = String(req.body?.symbol ?? "").trim().toUpperCase();

    // Per-bot лимиты из БД (fallback — глобальный yaml/env). Так дашборд может
    // задать max_positions / max_positions_with_expired / expired_after_minutes
    // индивидуально, без перезапуска контейнера.
    let botMaxPositions = globalCfg.max_positions;
    let botMaxWithExpired = 0;
    let botIgnoreAfterHours = globalCfg.max_positions_ignore_after_hours;
    if (symbol) {
      const [bot] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
      if (bot) {
        if (bot.max_positions != null && bot.max_positions > 0) botMaxPositions = bot.max_positions;
        if (bot.max_positions_with_expired != null && bot.max_positions_with_expired > 0) {
          botMaxWithExpired = bot.max_positions_with_expired;
        }
        if (bot.expired_after_minutes != null && bot.expired_after_minutes >= 0) {
          // Convert minutes to hours for server logic
          botIgnoreAfterHours = Math.max(0, Math.round(bot.expired_after_minutes / 60));
        }
      }
    }

    // reserve=true — занять слот атомарно (вызывается ботом непосредственно перед
    // ордером). Без флага запрос «информационный» и ничего не занимает.
    const wantReserve = req.body?.reserve === true && symbol !== "";
    const { occupied: positions } = await slots.inspect(
      () => loadOccupiedSymbols(botIgnoreAfterHours),
    );
    const daily_loss = await getDailyLoss();

    // Автосброс устаревшей паузы/счётчика, если давно не было сделок.
    const reset = await autoResetStale(control, globalCfg.pause_timeout_minutes);
    if (reset) {
      control.loss_streak = 0;
      control.paused_remaining = 0;
    }

    // Дневной лимит убытков — жёсткая остановка на день.
    if (globalCfg.daily_loss_limit_usd > 0 && daily_loss >= globalCfg.daily_loss_limit_usd) {
      return res.json({
        allowed: false,
        reason: "daily_loss_limit",
        positions_open: positions,
        loss_streak: control.loss_streak,
        paused_remaining: control.paused_remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    // Блок после серии убытков активен: при достижении loss_streak_trigger пауза
    // блокирует новые ВХОДЫ (реальные позиции), пока не будет прибыльной сделки.
    // Отклонённые (rejected) сделки по договорённости открываются как позиции и
    // могут снять паузу прибыльной симуляцией.
    if (control.paused_remaining > 0) {
      return res.json({
        allowed: false,
        reason: "loss_streak",
        positions_open: positions,
        loss_streak: control.loss_streak,
        paused_remaining: control.paused_remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    // Лимит позиций: проверка и (при reserve) занятие слота — под одной блокировкой,
    // иначе синхронные сигналы многих ботов видят один и тот же счётчик (race).
    // Символ, который уже занимает слот (открытая позиция/резерв), лимитом не
    // блокируется — это добор reverse-ноги в уже открытую цепочку, а не новая позиция.
    // Второй лимит (max_positions_with_expired) считает ВСЕ открытые позиции, без
    // окна протухания: старые цепочки не могут копиться бесконечно.
    let slot = await slots.check(
      symbol,
      botMaxPositions,
      () => loadOccupiedSymbols(botIgnoreAfterHours),
      wantReserve,
      {
        max: botMaxWithExpired,
        load: () => dbOpenSymbols(0),
      },
    );
    // «Протухшая» позиция (старше max_positions_ignore_after_hours) исключена из
    // occupied и потому не опознаётся как own по БД. Из-за этого следующая
    // reverse-нога такого символа упиралась в max_positions, если слот занял
    // свежий символ, хотя на бирже позиция ЕСТЬ — это добор, а не новый вход.
    // Спрашиваем биржу. Для информационных запросов (reserve=false) — всегда.
    // Для резервирования (reserve=true) — тоже, если на бирже есть живая позиция
    // (реверс-нога в существующей цепочке не должна конкурировать за слот).
    if (!slot.allowed && symbol && !EXCHANGE_CHECK
        && await exchangeHasPosition(symbol)) {
      slot = { ...slot, allowed: true, own: true };
    }
    if (!slot.allowed) {
      return res.json({
        allowed: false,
        reason: slot.reason ?? "max_positions",
        positions_open: slot.occupied,
        positions_reserved: slot.reserved,
        max_positions: botMaxPositions,
        max_positions_with_expired: botMaxWithExpired,
        total_open: slot.totalOccupied ?? null,
        loss_streak: control.loss_streak,
        paused_remaining: control.paused_remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    return res.json({
      allowed: true,
      reason: null,
      positions_open: slot.occupied,
      positions_reserved: slot.reserved,
      max_positions: botMaxPositions,
      max_positions_with_expired: botMaxWithExpired,
      total_open: slot.totalOccupied ?? null,
      own_slot: slot.own,
      reservation_id: slot.reservationId,
      loss_streak: control.loss_streak,
      paused_remaining: control.paused_remaining,
      daily_loss: Number(daily_loss.toFixed(2)),
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/**
 * POST /api/trading/release
 * Бот отпускает резерв слота, если ордер так и не был выставлен (open_position
 * вернул None / исключение). Если бот просто упал — резерв гаснет сам по TTL.
 * body: { symbol: string, reservation_id?: string }
 */
router.post("/release", async (req, res) => {
  try {
    const symbol = String(req.body?.symbol ?? "").trim().toUpperCase();
    if (!symbol) return res.status(400).json({ error: "symbol required" });
    const id = req.body?.reservation_id ? String(req.body.reservation_id) : null;
    const released = await slots.release(symbol, id);
    return res.json({ released });
  } catch (e) {
    return res.status(500).json({ error: String(e) });
  }
});

/**
 * POST /api/trading/result
 * Бот сообщает результат закрытой сделки. Обновляет общий счётчик подряд убытков.
 * body: { pnl: number }
 * Если pnl >= 0 — сброс счётчика. Если pnl < 0 — инкремент; при достижении
 * loss_streak_trigger включается пауза (paused_remaining = loss_pause_signals).
 */
/**
 * POST /api/trading/result
 * Бот сообщает результат закрытой сделки. Обновляет общий счётчик подряд убытков.
 * body: { pnl: number, simulated?: boolean }
 * - реальная сделка (simulated=false/нет): pnl>=0 сбрасывает серию/блок;
 *   pnl<0 инкрементирует серию; при достижении loss_streak_trigger включается блок.
 * - симулированная отклонённая сделка (simulated=true): pnl>=0 снимает блок
 *   (прибыльный сигнал в период защиты — значит серия «сломана»); pnl<0 НЕ влияет
 *   на реальный счётчик (сделка не открывалась).
 */
router.post("/result", async (req, res) => {
  try {
    const { pnl, simulated } = req.body;
    if (pnl === undefined) return res.status(400).json({ error: "pnl is required" });

    const cfg = readConfig();
    const control = await getControlRow();
    const now = new Date().toISOString();

    let loss_streak = control.loss_streak;
    let paused_remaining = control.paused_remaining;

    if (simulated === true) {
      // Симулированная (отклонённая) сделка: только победа снимает блок,
      // убыток не трогает реальный счётчик.
      if (pnl >= 0) {
        loss_streak = 0;
        paused_remaining = 0;
      }
    } else {
      // Реальная сделка. Вариант 3: блокируем входы до первой прибыльной сделки.
      if (pnl >= 0) {
        loss_streak = 0;
        paused_remaining = 0;
      } else {
        loss_streak += 1;
        if (loss_streak >= cfg.loss_streak_trigger) {
          paused_remaining = 1;
        }
      }
    }

    await db.update(tradingControlTable)
      .set({ loss_streak, paused_remaining, updated_at: now })
      .where(eq(tradingControlTable.id, 1));

    res.json({ success: true, loss_streak, paused_remaining });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/**
 * POST /api/trading/close-and-reset
 * Закрывает ВСЕ открытые позиции в БД (реальные и rejected), обнуляет
 * счётчик открытых позиций и серию/паузу убытков. Останавливает всех
 * ботов и стирает их локальные state-файлы (чтобы не было рассинхрона
 * между трекером и БД). Предназначено для кнопки «Close All & Reset».
 */
router.post("/close-and-reset", async (_req, res) => {
  try {
    const now = new Date().toISOString();

    // 1. Закрываем все открытые сделки (и real, и rejected).
    const result = await db.update(tradesTable)
      .set({
        is_open: false,
        status: "closed",
        exit_reason: "manual_reset",
        exit_time: now,
        exit_price: 0,
        pnl: 0,
      })
      .where(eq(tradesTable.is_open, true))
      .returning();

    const closedTrades = result?.length || 0;

    // 2. Удаляем ранее закрытые "manual_reset" записи (остатки прошлых сбросов),
    //    чтобы таблица сделок не засорялась этими техническими строками.
    const clearedManualReset = await db.delete(tradesTable)
      .where(eq(tradesTable.exit_reason, "manual_reset")).returning();

    // 2b. Удаляем отклонённые (rejected) записи без итогового PnL — это «мусор»:
    //     такие записи не дожидаются закрытия симуляцией и навсегда остаются
    //     с PnL «—» в дашборде. По запросу пользователя кнопка их вычищает.
    const clearedNoPnlRejected = await db.delete(tradesTable)
      .where(sql`status = 'rejected' AND pnl IS NULL`).returning();

    // 3. Сброс позиций в таблице ботов: карточки в дашборде берут `position`
    //    именно отсюда, поэтому без чистки позиции «висят» в UI, хоть и
    //    закрыты в trades.
    const botsCleared = await db.update(botsTable)
      .set({ position: null, is_running: false, current_price: null })
      .returning();

    // 4. Обнуляем счётчик открытых позиций и серию убытков.
    await db.update(tradingControlTable)
      .set({ loss_streak: 0, paused_remaining: 0, updated_at: now })
      .where(eq(tradingControlTable.id, 1));

    // 5. Останавливаем всех ботов и стираем их state-файлы (пересинхронизация).
    //    Запускаем в фоне, чтобы ответ клиенту пришёл сразу, а не ждать всё
    //    (~десятки вызовов powershell при большом числе ботов).
    void stopAllBots().catch(() => {});

    // 6. Дополнительно подчищаем любые оставшиеся lock/state файлы.
    try {
      const botDir = path.resolve(__dirname, "../../../../bot");
      if (fs.existsSync(botDir)) {
        const files = fs.readdirSync(botDir);
        for (const f of files) {
          if (f.startsWith("state_") || f.startsWith("bot.lock.")) {
            try { fs.unlinkSync(path.join(botDir, f)); } catch { /* ignore */ }
          }
        }
      }
    } catch { /* ignore */ }

    res.json({
      success: true,
      closed_trades: closedTrades,
      cleared_manual_reset: clearedManualReset?.length || 0,
      cleared_no_pnl_rejected: clearedNoPnlRejected?.length || 0,
      bots_cleared: botsCleared?.length || 0,
      message: `Closed ${closedTrades} open positions, cleared ${clearedManualReset?.length || 0} manual_reset and ${clearedNoPnlRejected?.length || 0} no-PnL rejected records, reset ${botsCleared?.length || 0} bot cards, stopped bots`,
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// GET /api/trading/status — состояние контроля для дашборда/диагностики
router.get("/status", async (_req, res) => {
  try {
    const cfg = readConfig();
    const control = await getControlRow();
    // Автосброс устаревшей паузы/счётчика (аналогично /check).
    if (await autoResetStale(control, cfg.pause_timeout_minutes)) {
      control.loss_streak = 0;
      control.paused_remaining = 0;
    }
    const st = await slots.inspect(() => loadOccupiedSymbols(cfg.max_positions_ignore_after_hours));
    const daily_loss = await getDailyLoss();
    const free_debt = await getFreeDebt();
    res.json({
      positions_open: st.open,
      positions_reserved: st.reserved,
      max_positions: cfg.max_positions,
      exchange_check: EXCHANGE_CHECK,
      max_positions_ignore_after_hours: cfg.max_positions_ignore_after_hours,
      loss_streak: control.loss_streak,
      loss_streak_trigger: cfg.loss_streak_trigger,
      paused_remaining: control.paused_remaining,
      loss_pause_signals: cfg.loss_pause_signals,
      max_free_debt_usd: cfg.max_free_debt_usd,
      free_debt: Number(free_debt.toFixed(2)),
      daily_loss_limit_usd: cfg.daily_loss_limit_usd,
      daily_loss: Number(daily_loss.toFixed(2)),
      pause_timeout_minutes: cfg.pause_timeout_minutes,
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export default router;
