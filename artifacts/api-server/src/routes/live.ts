import { Router } from "express";
import { spawn } from "child_process";
import fs from "fs";
import path from "path";
import { db, botsTable } from "@workspace/db";
import { sql } from "drizzle-orm";
import { BOT_DIR, BOT_ENV } from "../botPaths";
import { binanceRequest, syncBinanceTime } from "../grid-orders-lib";
import { getWalletBalanceUsd } from "../lib/account-balance";
import {
  dailyRiskSnapshot,
  dayKey,
  resetBaselineTo,
  resolvePositionLossPct,
} from "../lib/dailyRisk";
import { startBotProcess, stopAllBots } from "./bots";

const router = Router();

/**
 * Состояние kill-switch переживает рестарт контейнера: иначе после перезапуска
 * API флаг `dailyLossTripped` сбрасывался бы и боты могли снова открывать входы
 * в тот же день, уже потеряв дневной лимит.
 */
const TRIP_FILE = path.resolve(
  BOT_DIR, "..", "data", `live_daily_loss_state_${BOT_ENV}.json`,
);

type TripState = { day: string; tripped: boolean; symbols: string[] };

function loadTripState(): TripState {
  try {
    const raw = JSON.parse(fs.readFileSync(TRIP_FILE, "utf8")) as Partial<TripState>;
    return {
      day: String(raw?.day ?? ""),
      tripped: Boolean(raw?.tripped),
      symbols: Array.isArray(raw?.symbols) ? raw.symbols.map((s) => String(s).toUpperCase()) : [],
    };
  } catch {
    return { day: "", tripped: false, symbols: [] };
  }
}

function saveTripState(state: TripState): void {
  try {
    fs.mkdirSync(path.dirname(TRIP_FILE), { recursive: true });
    fs.writeFileSync(TRIP_FILE, JSON.stringify(state, null, 2));
  } catch (e) {
    console.error("[live] trip state save failed:", String(e));
  }
}

let tripState = loadTripState();
// Флаг активен только в «свой» день: на смене дня лимит снова доступен.
let dailyLossTripped = tripState.tripped && tripState.day === dayKey();

/**
 * Дневной стоп (kill-switch).
 *
 * Единая формула с гейтом входов (`lib/dailyRisk.ts`): net за день по Киеву
 * (реализованный + плавающий) против базы на начало дня × total_loss_pct.
 * При срабатывании: останавливаем ботов, закрываем позиции, запоминаем список
 * ботов (чтобы поднять их на смене дня) и состояние на диск.
 */
export async function checkDailyLossLimit(): Promise<void> {
  if (BOT_ENV !== "live") return;
  await checkDayRollover();
  if (dailyLossTripped) return;

  let snap: Awaited<ReturnType<typeof dailyRiskSnapshot>>;
  try {
    snap = await dailyRiskSnapshot({
      configuredUsd: Number(process.env.LIVE_DAILY_LOSS_LIMIT_USD || "0"),
    });
  } catch (e) {
    console.error("[live] daily loss snapshot failed:", String(e));
    return;
  }
  if (!Number.isFinite(snap.limit_usd) || snap.limit_usd <= 0) return;
  if (!snap.tripped) return;

  dailyLossTripped = true;

  // Кого поднимать завтра: снимок запущенных ДО остановки.
  let symbols: string[] = [];
  try {
    const rows = await db.select({ symbol: botsTable.symbol }).from(botsTable)
      .where(sql`is_running = 1`);
    symbols = rows.map((r) => String(r.symbol).toUpperCase()).filter(Boolean);
  } catch (e) {
    console.error("[live] running bots snapshot failed:", String(e));
  }

  tripState = { day: dayKey(), tripped: true, symbols };
  saveTripState(tripState);

  console.error(
    `[live] DAILY LOSS LIMIT reached: net=${snap.total.toFixed(2)} ` +
    `(realized=${snap.realized.toFixed(2)} unrealized=${snap.unrealized.toFixed(2)}) ` +
    `<= -${snap.limit_usd.toFixed(2)} (=${snap.pct}% of ${snap.baseline.toFixed(2)} ` +
    `at day start ${snap.day}, mode=${snap.mode}) — stopping all bots and flattening positions`,
  );

  const summary = await flattenAll();
  console.error(
    `[live] daily loss flatten ${summary.ok ? "OK" : "FAILED"}: ${summary.output.slice(0, 400)}`,
  );
}

/**
 * Смена дня по Киеву: снимает дневной стоп и (если боты были остановлены
 * лимитом) поднимает их автоматически. Вызывается каждую минуту из
 * checkDailyLossLimit и при старте API.
 */
export async function checkDayRollover(): Promise<void> {
  if (BOT_ENV !== "live") return;
  const today = dayKey();
  if (tripState.day === today) return;

  const wasTripped = tripState.tripped;
  const symbols = tripState.symbols;
  tripState = { day: today, tripped: false, symbols: [] };
  saveTripState(tripState);
  dailyLossTripped = false;
  if (!wasTripped) return;

  const autoStart = String(process.env.DAILY_LOSS_AUTO_RESTART ?? "true").toLowerCase() !== "false";
  console.log(
    `[live] NEW DAY ${today}: daily loss limit reset` +
    (autoStart ? `, restarting ${symbols.length} bots` : " (auto-restart disabled)"),
  );
  if (!autoStart) return;

  for (const symbol of symbols) {
    try {
      const result = await startBotProcess(symbol);
      console.log(
        `[live] day rollover start ${symbol}: ${result.ok ? "OK" : "FAILED"}` +
        (result.message ? ` — ${result.message}` : ""),
      );
    } catch (e) {
      console.error(`[live] day rollover start ${symbol} failed:`, String(e));
    }
  }
}

router.get("/daily-loss", async (_req, res) => {
  try {
    const snap = await dailyRiskSnapshot({
      configuredUsd: Number(process.env.LIVE_DAILY_LOSS_LIMIT_USD || "0"),
    });
    res.json({
      bot_env: BOT_ENV,
      day: snap.day,
      limit: snap.limit_usd,
      limit_pct: snap.pct,
      limit_source: snap.mode,
      wallet_balance_usd: snap.baseline,
      net: snap.total,
      realized: snap.realized,
      unrealized: snap.unrealized,
      baseline: snap.baseline,
      baseline_source: snap.baseline_source,
      remaining_usd: snap.remaining_usd,
      delta: snap.total,
      degraded: snap.degraded,
      tripped: dailyLossTripped || snap.tripped,
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/**
 * Ручной сброс дневного лимита: новая база = текущий equity (wallet + unrealized),
 * т.е. полный бюджет заново. Флаг снимается, состояние на диске очищается.
 */
router.post("/daily-loss/reset", async (_req, res) => {
  try {
    const snap = await dailyRiskSnapshot({
      configuredUsd: Number(process.env.LIVE_DAILY_LOSS_LIMIT_USD || "0"),
    });
    const wallet = await getWalletBalanceUsd({ fresh: true });
    const baseline = await resetBaselineTo(wallet + snap.unrealized, "manual");
    dailyLossTripped = false;
    tripState = { day: dayKey(), tripped: false, symbols: [] };
    saveTripState(tripState);
    res.json({
      success: true,
      day: baseline.day,
      baseline: baseline.balance,
      net: snap.total,
      delta: 0,
      limit: snap.limit_usd,
      limit_pct: snap.pct,
      limit_source: snap.mode,
      tripped: false,
    });
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

// ── Лимит просадки по КАЖДОЙ позиции отдельно ────────────────────────────────
// Референс — ТЕКУЩИЙ депозит = wallet balance (меняется вместе с балансом).
// Для каждой открытой позиции просадка = её открытый убыток от текущего депозита:
//   loss% = (-unrealized_position / wallet) * 100
// Если у КОНКРЕТНОЙ позиции loss% >= LIVE_MAX_DRAWDOWN_PCT — закрываем ИМЕННО её.
// Ничего не суммируется между позициями; база — всегда текущий баланс.
const lastCloseBySymbol = new Map<string, number>();

type LiveAccount = { wallet: number; upnl: number; equity: number };
async function liveAccount(): Promise<LiveAccount> {
  let lastErr: unknown;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const raw = await binanceRequest("GET", "/fapi/v2/account", {});
      const wallet = Number(raw?.totalWalletBalance ?? 0) || 0;
      const upnl = Number(raw?.totalUnrealizedProfit ?? 0) || 0;
      const equity = Number(raw?.totalMarginBalance ?? wallet + upnl) || 0;
      return { wallet, upnl, equity };
    } catch (e) {
      lastErr = e;
      await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
    }
  }
  throw lastErr ?? new Error("account fetch failed");
}

type LivePosition = { symbol: string; amt: number; upnl: number; lossPct: number };

// Список открытых позиций с per-position просадкой (lossPct = -upnl/wallet*100).
async function livePositions(wallet: number): Promise<LivePosition[]> {
  const raw = await binanceRequest("GET", "/fapi/v2/positionRisk", {});
  const list = Array.isArray(raw) ? raw : [];
  const out: LivePosition[] = [];
  for (const p of list as Array<Record<string, unknown>>) {
    const amt = Number(p?.positionAmt ?? 0) || 0;
    if (Math.abs(amt) < 1e-12) continue;
    const upnl = Number(p?.unRealizedProfit ?? 0) || 0;
    const lossPct = wallet > 0 ? (-upnl / wallet) * 100 : 0;
    out.push({ symbol: String(p?.symbol || ""), amt, upnl, lossPct });
  }
  return out;
}

// Закрывает конкретный символ (close_worst.py <SYMBOL>): cancel orders + market reduceOnly.
async function closePositionSymbol(symbol: string): Promise<{ ok: boolean; output: string }> {
  return await new Promise<{ ok: boolean; output: string }>((resolve) => {
    let out = "";
    let settled = false;
    const done = (ok: boolean) => {
      if (!settled) { settled = true; resolve({ ok, output: out }); }
    };
    const proc = spawn("python", ["close_worst.py", symbol], {
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
  if (BOT_ENV !== "live") return;
  try {
    await syncBinanceTime();
    const acct = await liveAccount();
    if (!(acct.wallet > 0)) return;
    const positions = await livePositions(acct.wallet);
    if (positions.length === 0) return;
    const now = Date.now();
    for (const p of positions) {
      if (p.upnl >= 0) continue;                 // только убыточные
      // Порог — per-bot `position_loss_pct` (% депозита), fallback env/5%.
      const threshold = await resolvePositionLossPct(p.symbol);
      if (!Number.isFinite(threshold) || threshold <= 0) continue;
      if (p.lossPct < threshold) continue;       // < порога — не трогаем
      const last = lastCloseBySymbol.get(p.symbol) || 0;
      if (now - last < 60_000) continue;          // не чаще раза в минуту на символ
      lastCloseBySymbol.set(p.symbol, now);
      console.error(
        `[live] POSITION DRAWDOWN ${p.lossPct.toFixed(2)}% >= ${threshold}% ` +
        `(symbol=${p.symbol} upnl=${p.upnl.toFixed(2)} wallet=${acct.wallet.toFixed(2)}) — closing`,
      );
      const res = await closePositionSymbol(p.symbol);
      console.error(`[live] close ${p.symbol} ${res.ok ? "OK" : "FAILED"}: ${res.output.slice(0, 300)}`);
    }
  } catch (e) {
    console.error("[live] drawdown check failed:", String(e));
  }
}

router.get("/drawdown", async (_req, res) => {
  const defaultThreshold = Number(process.env.LIVE_MAX_DRAWDOWN_PCT || "5");
  try {
    await syncBinanceTime();
    const acct = await liveAccount();
    const positions = await livePositions(acct.wallet);
    const worst = positions.reduce(
      (a, b) => (b.lossPct > (a ? a.lossPct : -Infinity) ? b : a),
      null as LivePosition | null,
    );
    // Пороги per-bot: показываем их в ответе, чтобы UI/диагностика видели, по
    // какому проценту реально закроется позиция.
    const detailed = await Promise.all(positions.map(async (p) => ({
      symbol: p.symbol,
      upnl: p.upnl,
      loss_pct: p.lossPct,
      threshold_pct: await resolvePositionLossPct(p.symbol),
    })));
    res.json({
      bot_env: BOT_ENV, wallet: acct.wallet, upnl: acct.upnl, equity: acct.equity,
      reference: acct.wallet,
      threshold_pct: worst ? await resolvePositionLossPct(worst.symbol) : defaultThreshold,
      default_threshold_pct: defaultThreshold,
      enabled: true,
      drawdown_pct: worst ? worst.lossPct : 0,
      worst_symbol: worst ? worst.symbol : null,
      positions: detailed,
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export default router;
