import { Router } from "express";
import fs from "fs";
import path from "path";
import yaml from "js-yaml";
import { db, botsTable } from "@workspace/db";
import { eq } from "drizzle-orm";
import { spawn, exec, type ChildProcess } from "child_process";
import { promisify } from "util";

const execAsync = promisify(exec);
const router = Router();
const BOT_DIR = process.env.BOT_DIR || path.resolve("../../bot");

async function findBotPid(symbol: string): Promise<number | null> {
  const configFile = `config_${symbol.replace("USDT", "").toLowerCase()}.yaml`;
  try {
    const { stdout } = await execAsync(
      `wmic process where "name='python.exe'" get processid,commandline /format:csv`
    );
    for (const line of stdout.split("\n")) {
      if (line.includes(configFile)) {
        const parts = line.trim().split(",");
        const pid = parseInt(parts[parts.length - 1]);
        if (!isNaN(pid) && pid > 0) return pid;
      }
    }
  } catch {}
  return null;
}

async function stopAllBots(): Promise<void> {
  const configFiles = fs.readdirSync(BOT_DIR).filter((f: string) => /^config_\w+\.yaml$/.test(f));
  for (const file of configFiles) {
    const symbol = file.replace("config_", "").replace(".yaml", "").toUpperCase() + "USDT";
    const pid = await findBotPid(symbol);
    if (pid) {
      try {
        if (process.platform === "win32") {
          await execAsync(`taskkill /PID ${pid} /F`);
        }
      } catch (e) {}
    }
    const stateFile = path.join(BOT_DIR, `state_${symbol.toLowerCase()}.json`);
    try { if (fs.existsSync(stateFile)) fs.unlinkSync(stateFile); } catch {}
  }
  await db.update(botsTable).set({ is_running: false, position: null, updated_at: new Date().toISOString() });
}

async function reloadConfigsFromYaml(): Promise<void> {
  const configs = fs.readdirSync(BOT_DIR).filter((f: string) => /^config_\w+\.yaml$/.test(f) && f !== "config.yaml");
  for (const file of configs) {
    const raw = yaml.load(fs.readFileSync(path.join(BOT_DIR, file), "utf8")) as Record<string, unknown>;
    const symbol = (raw.symbol as string).toUpperCase();
    const [existing] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
    const values = {
      mode: (raw.mode as string) || "paper",
      timeframe: raw.timeframe as string,
      leverage: raw.leverage as number,
      risk_pct: raw.risk_pct as number,
      sl_pct: raw.sl_pct as number,
      tp1_pct: raw.tp1_pct as number,
      tp1_close_pct: raw.tp1_close_pct as number,
      tp2_pct: raw.tp2_pct as number,
      ema_fast: raw.ema_fast as number,
      ema_slow: raw.ema_slow as number,
      volume_ma_period: raw.volume_ma_period as number,
      volume_multiplier: raw.volume_multiplier as number,
      htf_enabled: (raw.htf_enabled as boolean) || false,
      htf_timeframe: (raw.htf_timeframe as string) || null,
      htf_ema_fast: (raw.htf_ema_fast as number) || null,
      htf_ema_slow: (raw.htf_ema_slow as number) || null,
      auto_mode: (raw.auto_mode as boolean) ?? true,
      paper_balance: (raw.paper_balance as number) || 1000,
      log_file: raw.log_file as string,
      is_running: false,
      position: null,
      updated_at: new Date().toISOString(),
    };
    if (existing) {
      await db.update(botsTable).set(values).where(eq(botsTable.symbol, symbol));
    } else {
      await db.insert(botsTable).values({ symbol, ...values });
    }
  }
  await db.update(botsTable).set({ is_running: false, position: null });
}

router.post("/", async (_req, res) => {
  try {
    await stopAllBots();
    await reloadConfigsFromYaml();
    res.json({ success: true, message: "All bots stopped, configs reloaded from YAML. Ready to restart with new parameters." });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

router.post("/cancel-orders/:symbol", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const api_key = process.env.BINANCE_API_KEY;
    const api_secret = process.env.BINANCE_API_SECRET;
    if (!api_key || !api_secret) {
      return res.status(500).json({ error: "Binance API keys not configured" });
    }
    const { AsyncClient } = await import("binance");
    const c = await AsyncClient.create({ api_key, api_secret });
    await c.futures_cancel_all_open_orders({ symbol });
    const algoOrders = await c.futures_get_open_algo_orders({ symbol });
    for (const order of algoOrders) {
      await c.futures_cancel_algo_order({ symbol, algoId: order.algoId });
    }
    await c.close_connection();
    res.json({ success: true, message: `All orders cancelled for ${symbol}` });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export default router;