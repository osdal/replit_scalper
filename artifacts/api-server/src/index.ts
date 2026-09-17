// Загружаем переменные из корневого .env если они не заданы
function loadRootEnv() {
  const rootEnvPath = path.resolve(__dirname, "../../../.env");
  try {
    if (fs.existsSync(rootEnvPath)) {
      const content = fs.readFileSync(rootEnvPath, "utf8");
      for (const line of content.split("\n")) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith("#")) continue;
        const [key, ...rest] = trimmed.split("=");
        if (!key || rest.length === 0) continue;
        const value = rest.join("=").trim();
        if (!process.env[key]) {
          process.env[key] = value;
        }
      }
      logger.info("Loaded env from root .env");
    }
  } catch (e) {
    logger.warn({ err: e }, "Could not load root .env");
  }
}

import "./env";
import app from "./app";
import { logger } from "./lib/logger";
import { db, botsTable } from "@workspace/db";
import { eq } from "drizzle-orm";
import { exec } from "child_process";
import { promisify } from "util";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { reloadConfigsFromYaml, autoRestartBots } from "./routes/bots";
import { recoverStaleChains } from "./routes/recovery";
import { startGridEngine } from "./grid-engine";

const execAsync = promisify(exec);
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

loadRootEnv();

/**
 * Помечает is_running=0 у ботов, чей процесс не найден, и возвращает список
 * таких символов — их затем пытается поднять autoRestartBots(). Возврат списка
 * (а не просто сброс) нужен, чтобы отличить "бот был запущен и его надо
 * вернуть" от "бот и так стоял".
 */
async function resetStaleRunningBots(): Promise<string[]> {
  const staleSymbols: string[] = [];
  try {
    const bots = await db.select().from(botsTable);
    for (const bot of bots) {
      if (!bot.is_running) continue;
      const configFile = `config_${bot.symbol.replace("USDT", "").toLowerCase()}.yaml`;
      let isAlive = false;
      try {
        if (process.platform === "win32") {
          const { stdout } = await execAsync(
            `powershell -Command "Get-CimInstance -ClassName Win32_Process -Filter \\"Name='python.exe'\\" | Select-Object ProcessId,CommandLine | ConvertTo-Json"`
          );
          try {
            const processes = JSON.parse(stdout);
            const procList = Array.isArray(processes) ? processes : [processes];
            isAlive = procList.some((p: any) => p.CommandLine?.includes(configFile));
          } catch {
            isAlive = false;
          }
        } else {
          const { stdout } = await execAsync(
            `ps aux | grep "python.*main.py.*${configFile}" | grep -v grep`
          );
          isAlive = stdout.includes(configFile);
        }
      } catch {}
      if (!isAlive) {
        await db.update(botsTable)
          .set({ is_running: false, updated_at: new Date().toISOString() })
          .where(eq(botsTable.symbol, bot.symbol));
        logger.info({ symbol: bot.symbol }, "Reset stale bot status to stopped");
        staleSymbols.push(bot.symbol);
      }
    }
  } catch (e) {
    logger.warn({ err: e }, "Could not reset stale bot statuses");
  }
  return staleSymbols;
}

const rawPort = process.env["PORT"];

if (!rawPort) {
  throw new Error(
    "PORT environment variable is required but was not provided.",
  );
}

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

// При старте: сброс "мёртвых" is_running -> подтягиваем конфиги из YAML ->
// автоматически поднимаем ботов, которые были запущены (AUTO_RESTART_BOTS) ->
// только затем слушаем порт.
let stopGridEngine: (() => void) | null = null;
resetStaleRunningBots()
  .then((stale) => reloadConfigsFromYaml().then(() => stale))
  .then((stale) => autoRestartBots(stale))
  .then(() => {
    app.listen(port, (err) => {
      if (err) {
        logger.error({ err }, "Error listening on port");
        process.exit(1);
      }
      logger.info({ port }, "Server listening");
      stopGridEngine = startGridEngine();
    });
  });

// Останавливаем таймер grid-движка при завершении процесса.
function shutdown(signal: string): void {
  logger.info({ signal }, "Shutting down");
  if (stopGridEngine) stopGridEngine();
  process.exit(0);
}
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));

// Восстанавливаем "зависшие" locked recovery-цепочки: на старте и периодически.
// Если бот-владелец цепочки мёртв (упал между claim и открытием позиции) —
// цепочка возвращается в free, иначе она навсегда остаётся locked и долг
// выпадает из ротации.
recoverStaleChains()
  .then((n) => { if (n > 0) logger.info({ released: n }, "Released stale locked recovery chains at startup"); })
  .catch((e) => logger.warn({ err: e }, "Could not recover stale recovery chains at startup"));

const CHAIN_CLEANUP_INTERVAL_MS = 30 * 60 * 1000; // каждые 30 минут
setInterval(async () => {
  try {
    const released = await recoverStaleChains();
    if (released > 0) logger.info({ released }, "Released stale locked recovery chains (periodic)");
  } catch (e) {
    logger.warn({ err: e }, "Periodic stale recovery chain cleanup failed");
  }
}, CHAIN_CLEANUP_INTERVAL_MS);