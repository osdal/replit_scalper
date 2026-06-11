import { Router } from "express";
import { db, botsTable } from "@workspace/db";
import { eq } from "drizzle-orm";
import { spawn, type ChildProcess } from "child_process";
import path from "path";

const router = Router();
const botProcesses: Map<string, ChildProcess> = new Map();
const BOT_DIR = process.env.BOT_DIR || path.resolve("../../bot");

router.get("/", async (_req, res) => {
  try {
    const bots = await db.select().from(botsTable);
    res.json(bots.map(b => ({ ...b, position: b.position ? JSON.parse(b.position as string) : null })));
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.get("/:symbol", async (req, res) => {
  try {
    const [bot] = await db.select().from(botsTable).where(eq(botsTable.symbol, req.params.symbol.toUpperCase()));
    if (!bot) return res.status(404).json({ error: "Bot not found" });
    res.json({ ...bot, position: bot.position ? JSON.parse(bot.position as string) : null });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.put("/:symbol/config", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const [updated] = await db.update(botsTable).set({ ...req.body, updated_at: new Date().toISOString() }).where(eq(botsTable.symbol, symbol)).returning();
    if (!updated) return res.status(404).json({ error: "Bot not found" });
    res.json(updated);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.patch("/:symbol", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const body = { ...req.body };
    if (body.position && typeof body.position === "object") body.position = JSON.stringify(body.position);
    const [updated] = await db.update(botsTable)
      .set({ ...body, last_heartbeat: new Date().toISOString(), updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol)).returning();
    if (!updated) return res.status(404).json({ error: "Bot not found" });
    res.json(updated);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.post("/:symbol/start", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const [bot] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
    if (!bot) return res.status(404).json({ error: "Bot not found" });
    if (botProcesses.has(symbol)) return res.json({ success: false, message: "Bot already running" });
    const configFile = `config_${symbol.replace("USDT", "").toLowerCase()}.yaml`;
    const proc = spawn("python", ["main.py", configFile], { cwd: BOT_DIR });
    botProcesses.set(symbol, proc);
    await db.update(botsTable).set({ is_running: true, updated_at: new Date().toISOString() }).where(eq(botsTable.symbol, symbol));
    proc.on("exit", async () => {
      botProcesses.delete(symbol);
      await db.update(botsTable).set({ is_running: false, updated_at: new Date().toISOString() }).where(eq(botsTable.symbol, symbol));
    });
    res.json({ success: true, message: `Bot ${symbol} started` });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.post("/:symbol/stop", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const proc = botProcesses.get(symbol);
    if (!proc) return res.json({ success: false, message: "Bot not running" });
    proc.kill("SIGTERM");
    botProcesses.delete(symbol);
    await db.update(botsTable).set({ is_running: false, updated_at: new Date().toISOString() }).where(eq(botsTable.symbol, symbol));
    res.json({ success: true, message: `Bot ${symbol} stopped` });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

export default router;
