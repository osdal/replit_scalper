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
import { db, tradesTable, tradingControlTable, recoveryChainsTable } from "@workspace/db";
import { eq, sql, gte } from "drizzle-orm";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import yaml from "js-yaml";

const router = Router();

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const CONFIG_PATH = process.env.RECOVERY_CONFIG_PATH ||
  path.resolve(__dirname, "../../../../bot/recovery_config.yaml");

function readConfig(): { max_positions: number; loss_streak_trigger: number; loss_pause_signals: number; max_free_debt_usd: number; daily_loss_limit_usd: number; pause_timeout_minutes: number } {
  const def = { max_positions: 2, loss_streak_trigger: 2, loss_pause_signals: 3, max_free_debt_usd: 15.0, daily_loss_limit_usd: 8.0, pause_timeout_minutes: 120 };
  try {
    const raw = yaml.load(fs.readFileSync(CONFIG_PATH, "utf8")) as any;
    return {
      max_positions: Number(raw.max_positions) || def.max_positions,
      loss_streak_trigger: Number(raw.loss_streak_trigger) || def.loss_streak_trigger,
      loss_pause_signals: Number(raw.loss_pause_signals) || def.loss_pause_signals,
      max_free_debt_usd: Number(raw.max_free_debt_usd) || def.max_free_debt_usd,
      daily_loss_limit_usd: Number(raw.daily_loss_limit_usd) || def.daily_loss_limit_usd,
      pause_timeout_minutes: raw.pause_timeout_minutes !== undefined
        ? Number(raw.pause_timeout_minutes)
        : def.pause_timeout_minutes,
    };
  } catch {
    return def;
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

async function countOpenPositions(): Promise<number> {
  const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
  const [res] = await db.select({ n: sql<number>`count(*)` })
    .from(tradesTable)
    .where(sql`is_open = 1 AND entry_time >= ${twoHoursAgo}`);
  return Number(res?.n || 0);
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
    const cfg = readConfig();
    const control = await getControlRow();
    const positions = await countOpenPositions();
    const daily_loss = await getDailyLoss();

    // Автосброс устаревшей паузы/счётчика, если давно не было сделок.
    const reset = await autoResetStale(control, cfg.pause_timeout_minutes);
    if (reset) {
      control.loss_streak = 0;
      control.paused_remaining = 0;
    }

    // Дневной лимит убытков — жёсткая остановка на день.
    if (cfg.daily_loss_limit_usd > 0 && daily_loss >= cfg.daily_loss_limit_usd) {
      return res.json({
        allowed: false,
        reason: "daily_loss_limit",
        positions_open: positions,
        loss_streak: control.loss_streak,
        paused_remaining: control.paused_remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    // Пауза активна?
    if (control.paused_remaining > 0) {
      // Бот спросил разрешение — это и есть «сигнал». Пропускаем его и уменьшаем счётчик.
      const remaining = control.paused_remaining - 1;
      // Пауза закончилась (последний пропущенный сигнал) — сбрасываем серию убытков,
      // чтобы после «охлаждения» бот вышел в рынок с чистым листом.
      const resetStreak = remaining === 0;
      await db.update(tradingControlTable)
        .set({
          paused_remaining: remaining,
          loss_streak: resetStreak ? 0 : control.loss_streak,
          updated_at: new Date().toISOString(),
        })
        .where(eq(tradingControlTable.id, 1));
      return res.json({
        allowed: false,
        reason: "pause",
        positions_open: positions,
        loss_streak: resetStreak ? 0 : control.loss_streak,
        paused_remaining: remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    if (positions >= cfg.max_positions) {
      return res.json({
        allowed: false,
        reason: "max_positions",
        positions_open: positions,
        loss_streak: control.loss_streak,
        paused_remaining: control.paused_remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    return res.json({
      allowed: true,
      reason: null,
      positions_open: positions,
      loss_streak: control.loss_streak,
      paused_remaining: control.paused_remaining,
      daily_loss: Number(daily_loss.toFixed(2)),
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/**
 * POST /api/trading/result
 * Бот сообщает результат закрытой сделки. Обновляет общий счётчик подряд убытков.
 * body: { pnl: number }
 * Если pnl >= 0 — сброс счётчика. Если pnl < 0 — инкремент; при достижении
 * loss_streak_trigger включается пауза (paused_remaining = loss_pause_signals).
 */
router.post("/result", async (req, res) => {
  try {
    const { pnl } = req.body;
    if (pnl === undefined) return res.status(400).json({ error: "pnl is required" });

    const cfg = readConfig();
    const control = await getControlRow();
    const now = new Date().toISOString();

    let loss_streak = control.loss_streak;
    let paused_remaining = control.paused_remaining;

    if (pnl >= 0) {
      loss_streak = 0; // победа сбрасывает серию
      paused_remaining = 0; // и сразу снимает паузу, если она была активна
    } else {
      loss_streak += 1;
      if (loss_streak >= cfg.loss_streak_trigger) {
        paused_remaining = cfg.loss_pause_signals; // включаем паузу на M сигналов
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
    const positions = await countOpenPositions();
    const daily_loss = await getDailyLoss();
    const free_debt = await getFreeDebt();
    res.json({
      positions_open: positions,
      max_positions: cfg.max_positions,
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
