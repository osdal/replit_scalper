import app from "./app";
import { logger } from "./lib/logger";
import { db, botsTable } from "@workspace/db";
import { exec } from "child_process";
import { promisify } from "util";

const execAsync = promisify(exec);

async function resetStaleRunningBots(): Promise<void> {
  try {
    const bots = await db.select().from(botsTable);
    for (const bot of bots) {
      if (!bot.is_running) continue;
      // Проверяем реально ли запущен процесс
      const configFile = `config_${bot.symbol.replace("USDT", "").toLowerCase()}.yaml`;
      let isAlive = false;
      try {
        const { stdout } = await execAsync(
          `wmic process where "name='python.exe'" get commandline /format:csv`
        );
        isAlive = stdout.includes(configFile);
      } catch {}
      if (!isAlive) {
        await db.update(botsTable)
          .set({ is_running: false, updated_at: new Date().toISOString() })
          .where(require("drizzle-orm").eq(botsTable.symbol, bot.symbol));
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
resetStaleRunningBots().then(() => {
  app.listen(port, (err) => {
    if (err) {
      logger.error({ err }, "Error listening on port");
      process.exit(1);
    }
    logger.info({ port }, "Server listening");
  });
});
