import { Router } from "express";
import { db, botsTable } from "@workspace/db";
import { eq } from "drizzle-orm";
import { spawn, exec, type ChildProcess } from "child_process";
import { promisify } from "util";
import path from "path";
import { update_yaml_config } from "../../../bot/config.py";

const execAsync = promisify(exec);
const router = Router();
const botProcesses: Map<string, ChildProcess> = new Map();
const BOT_DIR = process.env.BOT_DIR || path.resolve("../../bot");

// Найти PID процесса бота по имени конфига (Windows + Linux)
async function findBotPid(symbol: string): Promise<number | null> {
  const configFile = `config_${symbol.replace("USDT", "").toLowerCase()}.yaml`;
  try {
    // Windows
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
  } catch {
    try {
      // Linux/Mac fallback
      const { stdout } = await execAsync(`pgrep -f "${configFile}"`);
      const pid = parseInt(stdout.trim());
      if (!isNaN(pid)) return pid;
    } catch {}
  }
  return null;
}

// Убить процесс по PID
async function killPid(pid: number): Promise<void> {
  try {
    if (process.platform === "win32") {
      await execAsync(`taskkill /PID ${pid} /F`);
    } else {
      process.kill(pid, "SIGKILL");
    }
  } catch (e) {
    // процесс уже завершён
  }
}

router.get("/", async (_req, res) => {
  try {
    const bots = await db.select().from(botsTable);
    res.json(bots.map(b => ({ ...b, position: b.position ? JSON.parse(b.position as string) : null })));
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.get("/:symbol", async (req, res) => {
  try {
    const [bot] = await db.select().from(botsTable)
      .where(eq(botsTable.symbol, req.params.symbol.toUpperCase()));
    if (!bot) return res.status(404).json({ error: "Bot not found" });
    res.json({ ...bot, position: bot.position ? JSON.parse(bot.position as string) : null });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.put("/:symbol/config", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const configUpdates = { ...req.body };
    delete configUpdates.updated_at;
    delete configUpdates.is_running;
    delete configUpdates.last_heartbeat;
    delete configUpdates.current_price;
    delete configUpdates.position;
    const [updated] = await db.update(botsTable)
      .set({ ...req.body, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol)).returning();
    if (!updated) return res.status(404).json({ error: "Bot not found" });
    update_yaml_config(symbol, configUpdates, BOT_DIR);
    res.json(updated);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.patch("/:symbol", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const body = { ...req.body };
    if (body.position && typeof body.position === "object") {
      body.position = JSON.stringify(body.position);
    }
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

    // Проверяем не запущен ли уже (через Map или через поиск PID)
    if (botProcesses.has(symbol)) {
      return res.json({ success: false, message: "Bot already running (dashboard)" });
    }
    const existingPid = await findBotPid(symbol);
    if (existingPid) {
      return res.json({ success: false, message: `Bot already running (PID ${existingPid}). Stop it first.` });
    }

    const configFile = `config_${symbol.replace("USDT", "").toLowerCase()}.yaml`;
    const proc = spawn("python", ["main.py", configFile], {
      cwd: BOT_DIR,
      detached: false,
      stdio: ["ignore", "pipe", "pipe"], // pipe stdout/stderr чтобы видеть логи
    });
    botProcesses.set(symbol, proc);

    // Перенаправляем вывод бота в консоль API сервера
    const botTag = `[BOT ${symbol}]`;
    proc.stdout?.on("data", (d) => {
      const lines = d.toString().trim().split("\n");
      for (const line of lines) {
        if (line.trim()) console.log(`${botTag} ${line}`);
      }
    });
    proc.stderr?.on("data", (d) => {
      const lines = d.toString().trim().split("\n");
      for (const line of lines) {
        if (line.trim()) console.error(`${botTag} ${line}`);
      }
    });

    await db.update(botsTable)
      .set({ is_running: true, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol));

    proc.on("exit", async () => {
      botProcesses.delete(symbol);
      await db.update(botsTable)
        .set({ is_running: false, updated_at: new Date().toISOString() })
        .where(eq(botsTable.symbol, symbol));
    });

    res.json({ success: true, message: `Bot ${symbol} started` });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.post("/:symbol/stop", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();

    // Сначала пробуем через Map (запущен через дашборд)
    const proc = botProcesses.get(symbol);
    if (proc) {
      // Используем SIGKILL для форсированного завершения, если SIGTERM не сработает
      if (!proc.killed) {
        proc.kill("SIGKILL");
      }
      botProcesses.delete(symbol);
    }

    // Потом ищем процесс запущенный вручную
    const pid = await findBotPid(symbol);
    if (pid) {
      await killPid(pid);
    }

    if (!proc && !pid) {
      return res.json({ success: false, message: "Bot not running" });
    }

    // Удаляем файл состояния позиции
    const stateFile = path.join(BOT_DIR, `state_${symbol.toLowerCase()}.json`);
    try {
      if (require("fs").existsSync(stateFile)) {
        require("fs").unlinkSync(stateFile);
        console.log(`[STOP] Deleted state file: ${stateFile}`);
      }
    } catch (e) {
      console.warn(`[STOP] Could not delete state file: ${e}`);
    }

    await db.update(botsTable)
      .set({ is_running: false, position: null, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol));

    res.json({ success: true, message: `Bot ${symbol} stopped` });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

export default router;
