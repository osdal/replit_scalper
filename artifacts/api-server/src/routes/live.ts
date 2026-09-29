import { Router } from "express";
import { spawn } from "child_process";
import fs from "fs";
import path from "path";
import { db, tradesTable } from "@workspace/db";
import { and, eq, gte, sql } from "drizzle-orm";
import { BOT_DIR, BOT_ENV } from "../botPaths";
import { binanceRequest, syncBinanceTime } from "../grid-orders-lib";
import { stopAllBots } from "./bots";

const router = Router();
let dailyLossTripped = false;

// Дневной лимит считается не от нуля, а от «базы» (baseline): при сбросе лимита
// или в новый UTC-день база обнуляется/перезаписывается, давая свежий бюджет.
const BASELINE_FILE = path.resolve(BOT_DIR, "..", "data", "live_daily_loss_baseline.json");

function utcDate(): string {
  return new Date().toISOString().slice(0, 10);
}

function loadBaseline(): { date: string; baseline: number } {
  try {
    const raw = JSON.parse(fs.readFileSync(BASELINE_FILE, "utf8"));
    if (raw && raw.date === utcDate()) {
      return { date: raw.date, baseline: Number(raw.baseline) || 0 };
    }
  } catch { /* no file — start from 0 */ }
  return { date: utcDate(), baseline: 0 };
}

function saveBaseline(state: { date: string; baseline: number }): void {
  try {
    fs.mkdirSync(path.dirname(BASELINE_FILE), { recursive: true });
    fs.writeFileSync(BASELINE_FILE, JSON.stringify(state));
  } catch (e) {
    console.error("[live] baseline save failed:", String(e));
  }
}

let lossState = loadBaseline();

function utcDayStartIso(): string {
  const now = new Date();
  const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  return start.toISOString();
}

async function todayRealizedNet(): Promise<number> {
  const start = utcDayStartIso();
  const [row] = await db
    .select({ net: sql<number>`COALESCE(SUM(pnl),0)` })
    .from(tradesTable)
    .where(and(eq(tradesTable.is_open, false), gte(tradesTable.exit_time, start)));
  return Number(row?.net ?? 0);
}

/** Суммарный НЕреализованный PnL открытых позиций с биржи (live). */
async function exchangeUnrealized(): Promise<number> {
  try {
    const raw = await binanceRequest("GET", "/fapi/v2/positionRisk", {});
    const list: any[] = Array.isArray(raw) ? raw : raw ? [raw] : [];
    let sum = 0;
    for (const p of list) {
      const amt = Number(p?.positionAmt ?? 0) || 0;
      if (Math.abs(amt) <= 0) continue;
      sum += Number(p?.unRealizedProfit ?? 0) || 0;
    }
    return sum;
  } catch (e) {
    console.error("[live] unrealized fetch failed:", String(e));
    return 0;
  }
}

/** Итоговый дневной результат = реализованный net + нереализованный по открытым. */
async function todayTotalNet(): Promise<{ realized: number; unrealized: number; total: number }> {
  const realized = await todayRealizedNet();
  const unrealized = BOT_ENV === "live" ? await exchangeUnrealized() : 0;
  return { realized, unrealized, total: realized + unrealized };
}

/**
 * Дневной лимит убытка: если реализованный net за текущие UTC-сутки опустился
 * ниже -(baseline + LIVE_DAILY_LOSS_LIMIT_USD), останавливаем всех ботов.
 * Только для live-окружения. Сброс базы — POST /api/live/daily-loss/reset.
 */
export async function checkDailyLossLimit(): Promise<void> {
  const limit = Number(process.env.LIVE_DAILY_LOSS_LIMIT_USD || "0");
  if (BOT_ENV !== "live" || !Number.isFinite(limit) || limit <= 0) return;
  if (dailyLossTripped) return;
  try {
    if (lossState.date !== utcDate()) {
      lossState = { date: utcDate(), baseline: 0 };
      saveBaseline(lossState);
    }
    const { realized, unrealized, total } = await todayTotalNet();
    const delta = total - lossState.baseline;
    if (delta <= -Math.abs(limit)) {
      dailyLossTripped = true;
      console.error(
        `[live] DAILY LOSS LIMIT reached: delta=${delta.toFixed(2)} ` +
        `(realized=${realized.toFixed(2)} unrealized=${unrealized.toFixed(2)} total=${total.toFixed(2)} ` +
        `baseline=${lossState.baseline.toFixed(2)}) <= -${Math.abs(limit)} — ` +
        `stopping all bots and flattening positions`,
      );
      const summary = await flattenAll();
      console.error(
        `[live] daily loss flatten ${summary.ok ? "OK" : "FAILED"}: ${summary.output.slice(0, 400)}`,
      );
    }
  } catch (e) {
    console.error("[live] daily loss check failed:", String(e));
  }
}

router.get("/daily-loss", async (_req, res) => {
  const limit = Number(process.env.LIVE_DAILY_LOSS_LIMIT_USD || "0");
  try {
    const { realized, unrealized, total } = await todayTotalNet();
    res.json({
      bot_env: BOT_ENV,
      limit,
      net: total,
      realized,
      unrealized,
      baseline: lossState.baseline,
      delta: total - lossState.baseline,
      tripped: dailyLossTripped,
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/** Сброс дневного лимита: свежий бюджет в `limit` USD от текущего net. */
router.post("/daily-loss/reset", async (_req, res) => {
  try {
    const { total } = await todayTotalNet();
    lossState = { date: utcDate(), baseline: total };
    saveBaseline(lossState);
    dailyLossTripped = false;
    const limit = Number(process.env.LIVE_DAILY_LOSS_LIMIT_USD || "0");
    res.json({ success: true, baseline: total, net: total, delta: 0, limit, tripped: false });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/** Остановить всех ботов и закрыть все позиции по рынку (kill-switch/flatten). */
async function flattenAll(): Promise<{ ok: boolean; output: string }> {
  try {
    await stopAllBots();
  } catch { /* всё равно пытаемся закрыть позиции */ }
  return await new Promise<{ ok: boolean; output: string }>((resolve) => {
    let out = "";
    let settled = false;
    const done = (ok: boolean) => {
      if (!settled) { settled = true; resolve({ ok, output: out }); }
    };
    const proc = spawn("python", ["close_all.py"], {
      cwd: BOT_DIR,
      env: process.env,
      windowsHide: true,
    });
    proc.stdout?.on("data", (d) => { out += d.toString(); });
    proc.stderr?.on("data", (d) => { out += d.toString(); });
    proc.on("error", (e) => { out += String(e); done(false); });
    proc.on("close", (code) => done(code === 0));
    setTimeout(() => done(false), 120_000);
  });
}

/** Kill-switch: остановить всех ботов и закрыть все позиции по рынку. */
router.post("/close-all", async (_req, res) => {
  const summary = await flattenAll();
  res.json({ success: summary.ok, output: summary.output });
});

// ── Лимит просадки: закрытие самой убыточной позиции ─────────────────────────
// Референс = LIVE_DEPOSIT_USD (если задан) либо максимум equity (high-water mark,
// хранится в data/live_drawdown_peak.json). Если просадка от референса достигла
// LIVE_MAX_DRAWDOWN_PCT %, закрываем одну самую убыточную открытую позицию.
const PEAK_FILE = path.resolve(BOT_DIR, "..", "data", "live_drawdown_peak.json");
let lastWorstCloseMs = 0;

// Состояние просадки: peak (максимум баланса) + armed (защёлка срабатывания).
// armed=true  → при достижении порога разрешено закрыть худшую позицию ОДИН раз;
// armed=false → пока просадка держится ≥ порога, повторно НЕ закрываем (иначе
//               черн: API закрыл позицию, бот тут же открыл новую, снова просадка…).
type DdState = { peak: number; armed: boolean };
function loadDdState(): DdState {
  try {
    const raw = JSON.parse(fs.readFileSync(PEAK_FILE, "utf8"));
    return { peak: Number(raw?.peak) || 0, armed: raw?.armed !== false };
  } catch {
    return { peak: 0, armed: true };
  }
}
function saveDdState(state: DdState): void {
  try {
    fs.mkdirSync(path.dirname(PEAK_FILE), { recursive: true });
    fs.writeFileSync(PEAK_FILE, JSON.stringify(state));
  } catch (e) {
    console.error("[live] drawdown state save failed:", String(e));
  }
}

async function liveEquity(): Promise<number> {
  let lastErr: unknown;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const raw = await binanceRequest("GET", "/fapi/v2/account", {});
      return Number(raw?.totalMarginBalance ?? raw?.totalWalletBalance ?? 0) || 0;
    } catch (e) {
      lastErr = e;
      await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
    }
  }
  throw lastErr ?? new Error("equity fetch failed");
}

async function closeWorstPosition(): Promise<{ ok: boolean; output: string }> {
  return await new Promise<{ ok: boolean; output: string }>((resolve) => {
    let out = "";
    let settled = false;
    const done = (ok: boolean) => {
      if (!settled) { settled = true; resolve({ ok, output: out }); }
    };
    const proc = spawn("python", ["close_worst.py"], {
      cwd: BOT_DIR,
      env: process.env,
      windowsHide: true,
    });
    proc.stdout?.on("data", (d) => { out += d.toString(); });
    proc.stderr?.on("data", (d) => { out += d.toString(); });
    proc.on("error", (e) => { out += String(e); done(false); });
    proc.on("close", (code) => done(code === 0));
    setTimeout(() => done(false), 60_000);
  });
}

export async function checkMaxDrawdown(): Promise<void> {
  const threshold = Number(process.env.LIVE_MAX_DRAWDOWN_PCT || "10");
  if (BOT_ENV !== "live" || !Number.isFinite(threshold) || threshold <= 0) return;
  try {
    await syncBinanceTime();
    const equity = await liveEquity();
    if (!(equity > 0)) return;
    const fixedDeposit = Number(process.env.LIVE_DEPOSIT_USD || "0") || 0;
    const state = loadDdState();
    let reference: number;
    if (fixedDeposit > 0) {
      reference = fixedDeposit;
    } else {
      reference = Math.max(state.peak, equity);
      if (reference > state.peak) {
        state.peak = reference;
        state.armed = true; // новый максимум — снова разрешаем срабатывание
        saveDdState(state);
      }
    }
    if (!(reference > 0)) return;
    const ddPct = ((reference - equity) / reference) * 100;
    if (ddPct < threshold) {
      // Восстановились выше порога — взводим защёлку для следующего захода.
      if (!state.armed) { state.armed = true; saveDdState(state); }
      return;
    }
    // Защёлка: на этом заходе уже закрывали — повторно НЕ закрываем.
    if (!state.armed) return;
    const now = Date.now();
    if (now - lastWorstCloseMs < 300_000) return; // не чаще раза в 5 мин
    lastWorstCloseMs = now;
    state.armed = false;
    saveDdState(state);
    console.error(
      `[live] MAX DRAWDOWN ${ddPct.toFixed(2)}% >= ${threshold}% ` +
      `(equity=${equity.toFixed(2)} reference=${reference.toFixed(2)}) — closing worst position`,
    );
    const res = await closeWorstPosition();
    console.error(`[live] drawdown close-worst ${res.ok ? "OK" : "FAILED"}: ${res.output.slice(0, 400)}`);
  } catch (e) {
    console.error("[live] drawdown check failed:", String(e));
  }
}

router.get("/drawdown", async (_req, res) => {
  const threshold = Number(process.env.LIVE_MAX_DRAWDOWN_PCT || "10");
  try {
    await syncBinanceTime();
    const equity = await liveEquity();
    const fixedDeposit = Number(process.env.LIVE_DEPOSIT_USD || "0") || 0;
    const state = loadDdState();
    const reference = fixedDeposit > 0 ? fixedDeposit : Math.max(state.peak, equity);
    const ddPct = reference > 0 ? ((reference - equity) / reference) * 100 : 0;
    res.json({
      bot_env: BOT_ENV, equity, reference, peak: state.peak, armed: state.armed,
      fixed_deposit: fixedDeposit, drawdown_pct: ddPct, threshold_pct: threshold,
      enabled: threshold > 0,
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export default router;
