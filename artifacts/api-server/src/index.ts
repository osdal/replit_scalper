import app from "./app";
import { logger } from "./lib/logger";
import { db, botsTable } from "@workspace/db";
import { eq } from "drizzle-orm";
import { exec } from "child_process";
import { promisify } from "util";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { reloadConfigsFromYaml } from "./routes/bots";

const execAsync = promisify(exec);
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

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

loadRootEnv();

async function resetStaleRunningBots(): Promise<void> {
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
      }
    }
  } catch (e) {
    logger.warn({ err: e }, "Could not reset stale bot statuses");
  }
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

// При старте сбрасываем статус ботов у которых нет реального процесса
resetStaleRunningBots().then(() => reloadConfigsFromYaml()).then(() => {
  app.listen(port, (err) => {
    if (err) {
      logger.error({ err }, "Error listening on port");
      process.exit(1);
    }
    logger.info({ port }, "Server listening");
  });
});
