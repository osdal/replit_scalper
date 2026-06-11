import { Router } from "express";
import { db, botsTable } from "@workspace/db";
import { eq } from "drizzle-orm";
import { spawn, type ChildProcess } from "child_process";
import path from "path";

const router = Router();

// Храним процессы ботов в памяти
const botProcesses: Map<string, ChildProcess> = new Map();

const BOT_DIR = process.env.BOT_DIR || path.resolve("../../bot");

// GET /bots
router.get("/", async (_req, res) => {
  const bots = await db.select().from(botsTable);
  res.json(bots);
});

// GET /bots/:symbol
router.get("/:symbol", async (req, res) => {
  const [bot] = await db
    .select()
    .from(botsTable)
    .where(eq(botsTable.symbol, req.params.symbol.toUpperCase()));
  if (!bot) return res.status(404).json({ error: "Bot not found" });
  res.json(bot);
});

// PUT /bots/:symbol/config
router.put("/:symbol/config", async (req, res) => {
  const symbol = req.params.symbol.toUpperCase();
  const [updated] = await db
    .update(botsTable)
    .set({ ...req.body, updated_at: new Date() })
    .where(eq(botsTable.symbol, symbol))
    .returning();
  if (!updated) return res.status(404).json({ error: "Bot not found" });
  res.json(updated);
});

// POST /bots/:symbol/start
router.post("/:symbol/start", async (req, res) => {
  const symbol = req.params.symbol.toUpperCase();
  const [bot] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
  if (!bot) return res.status(404).json({ error: "Bot not found" });

  if (botProcesses.has(symbol)) {
    return res.json({ success: false, message: "Bot already running" });
  }

  const configFile = `config_${symbol.replace("USDT", "").toLowerCase()}.yaml`;
  const proc = spawn("python", ["main.py", configFile], {
    cwd: BOT_DIR,
    detached: false,
  });

  botProcesses.set(symbol, proc);

  await db
    .update(botsTable)
    .set({ is_running: true, updated_at: new Date() })
    .where(eq(botsTable.symbol, symbol));

  proc.on("exit", async () => {
    botProcesses.delete(symbol);
    await db
      .update(botsTable)
      .set({ is_running: false, updated_at: new Date() })
      .where(eq(botsTable.symbol, symbol));
  });

  res.json({ success: true, message: `Bot ${symbol} started` });
});

// POST /bots/:symbol/stop
router.post("/:symbol/stop", async (req, res) => {
  const symbol = req.params.symbol.toUpperCase();
  const proc = botProcesses.get(symbol);
  if (!proc) return res.json({ success: false, message: "Bot not running" });

  proc.kill("SIGTERM");
  botProcesses.delete(symbol);

  await db
    .update(botsTable)
    .set({ is_running: false, updated_at: new Date() })
    .where(eq(botsTable.symbol, symbol));

  res.json({ success: true, message: `Bot ${symbol} stopped` });
});

export default router;

// PATCH /bots/:symbol — обновление рантайм состояния от бота
router.patch("/:symbol", async (req, res) => {
  const symbol = req.params.symbol.toUpperCase();
  const [updated] = await db
    .update(botsTable)
    .set({ ...req.body, last_heartbeat: new Date(), updated_at: new Date() })
    .where(eq(botsTable.symbol, symbol))
    .returning();
  if (!updated) return res.status(404).json({ error: "Bot not found" });
  res.json(updated);
});
