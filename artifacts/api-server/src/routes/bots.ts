import { Router } from "express";
import { db, botsTable } from "@workspace/db";
import { eq } from "drizzle-orm";
import { spawn, type ChildProcess } from "child_process";
import path from "path";
import { fileURLToPath } from "url";

const router = Router();
const botProcesses: Map<string, ChildProcess> = new Map();
const BOT_DIR = process.env.BOT_DIR || path.resolve("../../bot");

// GET /bots
router.get("/", (_req, res) => {
  try {
    const bots = db.select().from(botsTable).all();
    // Парсим position из JSON строки
    const result = bots.map(b => ({
      ...b,
      position: b.position ? JSON.parse(b.position as string) : null,
    }));
    res.json(result);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// GET /bots/:symbol
router.get("/:symbol", (req, res) => {
  try {
    const bot = db.select().from(botsTable)
      .where(eq(botsTable.symbol, req.params.symbol.toUpperCase()))
      .get();
    if (!bot) return res.status(404).json({ error: "Bot not found" });
    res.json({ ...bot, position: bot.position ? JSON.parse(bot.position as string) : null });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// PUT /bots/:symbol/config
router.put("/:symbol/config", (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const updated = db.update(botsTable)
      .set({ ...req.body, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol))
      .returning()
      .get();
    if (!updated) return res.status(404).json({ error: "Bot not found" });
    res.json(updated);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// PATCH /bots/:symbol — runtime update from bot
router.patch("/:symbol", (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const body = { ...req.body };
    // Сериализуем position в JSON если передан объект
    if (body.position && typeof body.position === "object") {
      body.position = JSON.stringify(body.position);
    }
    const updated = db.update(botsTable)
      .set({ ...body, last_heartbeat: new Date().toISOString(), updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol))
      .returning()
      .get();
    if (!updated) return res.status(404).json({ error: "Bot not found" });
    res.json(updated);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// POST /bots/:symbol/start
router.post("/:symbol/start", (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const bot = db.select().from(botsTable).where(eq(botsTable.symbol, symbol)).get();
    if (!bot) return res.status(404).json({ error: "Bot not found" });
    if (botProcesses.has(symbol)) return res.json({ success: false, message: "Bot already running" });

    const configFile = `config_${symbol.replace("USDT", "").toLowerCase()}.yaml`;
    const proc = spawn("python", ["main.py", configFile], { cwd: BOT_DIR, detached: false });
    botProcesses.set(symbol, proc);

    db.update(botsTable).set({ is_running: true, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol)).run();

    proc.on("exit", () => {
      botProcesses.delete(symbol);
      db.update(botsTable).set({ is_running: false, updated_at: new Date().toISOString() })
        .where(eq(botsTable.symbol, symbol)).run();
    });

    res.json({ success: true, message: `Bot ${symbol} started` });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// POST /bots/:symbol/stop
router.post("/:symbol/stop", (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const proc = botProcesses.get(symbol);
    if (!proc) return res.json({ success: false, message: "Bot not running" });

    proc.kill("SIGTERM");
    botProcesses.delete(symbol);
    db.update(botsTable).set({ is_running: false, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol)).run();

    res.json({ success: true, message: `Bot ${symbol} stopped` });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export default router;
