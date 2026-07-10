import { Router } from "express";
import { db, botsTable } from "@workspace/db";
import { eq } from "drizzle-orm";
import { spawn, exec, type ChildProcess } from "child_process";
import { promisify } from "util";
import path from "path";
import fs from "fs";
import yaml from "js-yaml";
import { fileURLToPath } from "url";

const execAsync = promisify(exec);
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Resolve BOT_DIR - try multiple possible locations
let BOT_DIR: string;
if (process.env.BOT_DIR) {
  const envBotDir = process.env.BOT_DIR;
  if (envBotDir.match(/^[A-Za-z]:/) || path.isAbsolute(envBotDir)) {
    BOT_DIR = envBotDir;
  } else {
    // Try project root + envBotDir, then cwd + envBotDir
    const projectRoot = path.resolve(__dirname, "../../..");
    const tryPath1 = path.join(projectRoot, envBotDir);
    const tryPath2 = path.join(process.cwd(), envBotDir);
    const tryPath3 = envBotDir; // as-is
    BOT_DIR = [tryPath1, tryPath2, tryPath3].find(p => fs.existsSync(p)) || tryPath1;
  }
} else {
  // Try multiple possible locations for bot directory
  const projectRoot = path.resolve(__dirname, "../../..");
  const possiblePaths = [
    path.join(projectRoot, "bot"),
    path.join(process.cwd(), "bot"),
    path.join(projectRoot, "artifacts", "api-server", "..", "bot"),
  ];
  BOT_DIR = possiblePaths.find(p => fs.existsSync(p)) || path.join(projectRoot, "bot");
}

const router = Router();
const botProcesses: Map<string, ChildProcess> = new Map();

/**
 * Обновляет config_<symbol>.yaml через отдельный Python-процесс.
 * Node.js не может импортировать .py файлы как модули — update_yaml_config()
 * живёт в bot/config.py и вызывается здесь через CLI-обёртку
 * bot/update_config_cli.py, которой параметры передаются как JSON через stdin
 * (тот же паттерн, что уже используется для backtest_runner.py).
 */
function updateYamlConfig(symbol: string, params: Record<string, unknown>): Promise<void> {
  return new Promise((resolve, reject) => {
    const proc = spawn("python", ["update_config_cli.py", symbol, BOT_DIR], { cwd: BOT_DIR });

    let output = "";
    let error = "";
    proc.stdout.on("data", (d) => (output += d));
    proc.stderr.on("data", (d) => (error += d));

    proc.on("close", () => {
      try {
        const parsed = JSON.parse(output.trim());
        if (parsed.error) {
          reject(new Error(parsed.error));
        } else {
          resolve();
        }
      } catch {
        reject(new Error(error || "update_config_cli.py produced no parseable output"));
      }
    });

    proc.on("error", (err) => reject(err));

    proc.stdin.write(JSON.stringify(params));
    proc.stdin.end();
  });
}

// Найти PID процесса бота по имени конфига (Windows + Linux)
async function findBotPid(symbol: string): Promise<number | null> {
  const configFile = `config_${symbol.replace("USDT", "").toLowerCase()}.yaml`;
  try {
    if (process.platform === "win32") {
      // Windows 11+ doesn't have wmic, use Get-CimInstance
      const { stdout } = await execAsync(
        `powershell -Command "Get-CimInstance -ClassName Win32_Process -Filter \\\"Name='python.exe'\\\" | Select-Object ProcessId,CommandLine | ConvertTo-Json"`
      );
      const processes = JSON.parse(stdout);
      for (const proc of processes) {
        if (proc.CommandLine && proc.CommandLine.includes(configFile)) {
          const pid = parseInt(proc.ProcessId);
          if (!isNaN(pid) && pid > 0) return pid;
        }
      }
    } else {
      // Linux/Mac fallback
      const { stdout } = await execAsync(`pgrep -f "${configFile}"`);
      const pid = parseInt(stdout.trim());
      if (!isNaN(pid)) return pid;
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

    // Синхронизируем изменения в config_<symbol>.yaml на диске — это то,
    // что реально читает Python-бот при запуске, БД для него не источник
    // правды. Если запись в YAML провалится — сообщаем об этом явно,
    // вместо того чтобы вернуть успех при несинхронизированном состоянии.
    try {
      await updateYamlConfig(symbol, configUpdates);
    } catch (yamlErr) {
      return res.status(500).json({
        error: `DB updated, but failed to write config_*.yaml: ${yamlErr}`,
        dbUpdated: updated,
      });
    }

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

router.post("/refresh", async (_req, res) => {
  try {
    await reloadConfigsFromYaml();
    for (const symbol of Array.from(botProcesses.keys())) {
      const proc = botProcesses.get(symbol);
      if (proc?.killed) botProcesses.delete(symbol);
    }
    const configs = fs.readdirSync(BOT_DIR).filter((f: string) => /^config_\w+\.yaml$/.test(f));
    for (const file of configs) {
      const symbol = (file.replace("config_", "").replace(".yaml", "").toUpperCase() + "USDT");
      const pid = await findBotPid(symbol);
      await db.update(botsTable).set({
        is_running: !!pid,
        last_heartbeat: pid ? new Date().toISOString() : null,
      }).where(eq(botsTable.symbol, symbol));
    }
    const bots = await db.select().from(botsTable);
    res.json({ refreshed: bots.length, bots: bots.map(b => ({ symbol: b.symbol, is_running: b.is_running })) });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.post("/stop-all", async (_req, res) => {
  try {
    await stopAllBots();
    const bots = await db.select().from(botsTable);
    res.json({ success: true, message: "All bots stopped", bots: bots.map(b => ({ symbol: b.symbol, is_running: b.is_running })) });
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
    const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
    const proc = spawn(pythonCmd, ["main.py", configFile], {
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

router.post("/stop-all", async (_req, res) => {
  try {
    const stoppedBots: string[] = [];
    for (const [symbol, proc] of botProcesses) {
      if (!proc.killed) {
        proc.kill();
        stoppedBots.push(symbol);
      }
    }
    botProcesses.clear();
    
    // Clear lock files
    const lockFiles = fs.readdirSync(BOT_DIR).filter(f => f.startsWith("bot.lock."));
    for (const lockFile of lockFiles) {
      fs.unlinkSync(path.join(BOT_DIR, lockFile));
    }
    
    // Also kill any Python processes running bots
    execAsync("taskkill /IM python.exe /F 2>nul || true", { stdio: "ignore" });
    
    res.json({ success: true, message: `Stopped ${stoppedBots.length} bots`, bots: stoppedBots });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.post("/:symbol/stop", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();

    // Сначала пробуем через Map (запущен через дашборд)
    const proc = botProcesses.get(symbol);
    if (proc) {
      if (!proc.killed) {
        // Windows: use kill() without signal (TerminateProcess)
        // Linux/Mac: SIGTERM then SIGKILL
        if (process.platform === "win32") {
          proc.kill();
        } else {
          proc.kill("SIGTERM");
        }
        await new Promise(resolve => setTimeout(resolve, 3000));
        if (!proc.killed) {
          if (process.platform === "win32") {
            proc.kill();
          } else {
            proc.kill("SIGKILL");
          }
        }
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
      if (fs.existsSync(stateFile)) {
        fs.unlinkSync(stateFile);
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

export async function reloadConfigsFromYaml(): Promise<void> {
  // Resolve BOT_DIR dynamically - check multiple possible locations
  let resolvedBotDir = BOT_DIR;
  const possiblePaths = [
    BOT_DIR,
    path.join(__dirname, "../../../bot"),
    path.join(process.cwd(), "bot"),
    path.join(path.dirname(__dirname), "bot"),
    path.join(path.dirname(path.dirname(__dirname)), "bot"),
  ];
  for (const p of possiblePaths) {
    try {
      if (fs.existsSync(p) && fs.readdirSync(p).some((f: string) => /^config_\w+\.yaml$/.test(f))) {
        resolvedBotDir = p;
        break;
      }
    } catch {}
  }
  
  const configs = fs.readdirSync(resolvedBotDir).filter((f: string) => /^config_\w+\.yaml$/.test(f) && f !== "config.yaml");
  for (const file of configs) {
    const raw = yaml.load(fs.readFileSync(path.join(resolvedBotDir, file), "utf8")) as Record<string, unknown>;
    const symbol = (raw.symbol as string).toUpperCase();
    const [existing] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
    const values = {
      mode:             (raw.mode as string) || "paper",
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
      auto_mode:        (raw.auto_mode as boolean) ?? true,
      paper_balance:    (raw.paper_balance as number) || 1000,
      log_file:         raw.log_file as string,
      is_running:       false,
      position:         null,
      updated_at:       new Date().toISOString(),
    };
    if (existing) {
      await db.update(botsTable).set(values).where(eq(botsTable.symbol, symbol));
    } else {
      await db.insert(botsTable).values({ symbol, ...values });
    }
  }
  await db.update(botsTable).set({ is_running: false, position: null });
}

async function stopAllBots(): Promise<void> {
  const symbols = Array.from(botProcesses.keys());
  // SIGTERM first (Windows: use kill() without signal)
  for (const symbol of symbols) {
    const proc = botProcesses.get(symbol);
    if (proc && !proc.killed) {
      if (process.platform === "win32") {
        proc.kill();
      } else {
        proc.kill("SIGTERM");
      }
    }
    botProcesses.delete(symbol);
  }
  // Wait for SIGTERM to take effect
  await new Promise(resolve => setTimeout(resolve, 3000));
  // Force kill any survivors and clean up state files
  for (const symbol of symbols) {
    const proc = botProcesses.get(symbol);
    if (proc && !proc.killed) {
      if (process.platform === "win32") {
        proc.kill();
      } else {
        proc.kill("SIGKILL");
      }
    }
    const stateFile = path.join(BOT_DIR, `state_${symbol.toLowerCase()}.json`);
    try { if (fs.existsSync(stateFile)) fs.unlinkSync(stateFile); } catch {}
  }
  const configFiles = fs.readdirSync(BOT_DIR).filter((f: string) => /^config_\w+\.yaml$/.test(f));
  for (const file of configFiles) {
    const symbol = file.replace("config_", "").replace(".yaml", "").toUpperCase() + "USDT";
    const pid = await findBotPid(symbol);
    if (pid) {
      try { if (process.platform === "win32") { await execAsync(`taskkill /PID ${pid} /F`); } else { process.kill(pid, "SIGKILL"); } } catch {}
    }
  }
  await db.update(botsTable).set({ is_running: false, position: null, updated_at: new Date().toISOString() });
}

export default router;
