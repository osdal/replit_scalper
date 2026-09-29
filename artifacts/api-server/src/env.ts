import path from "path";
import fs from "fs";
import { fileURLToPath } from "url";
import dotenv from "dotenv";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Окружение выбирается через BOT_ENV (testnet по умолчанию) и читает свой env-файл:
// .env.<BOT_ENV>, иначе общий .env. Это позволяет держать testnet- и live-ключи
// полностью раздельно.
const envName = (process.env.BOT_ENV || "testnet").trim().toLowerCase() || "testnet";
const scopedEnvPath = path.resolve(__dirname, `../../../.env.${envName}`);
const rootEnvPath = path.resolve(__dirname, "../../../.env");

let envPath = rootEnvPath;
let scoped = false;
if (fs.existsSync(scopedEnvPath)) {
  envPath = scopedEnvPath;
  scoped = true;
} else if (envName === "live") {
  throw new Error(
    `[env] BOT_ENV=live but ${scopedEnvPath} not found — refusing to start ` +
      `(live requires its own .env.live with BINANCE_TESTNET=false and real keys)`,
  );
}

if (fs.existsSync(envPath)) {
  // Scoped-файл окружения (.env.live) переопределяет унаследованные env-переменные,
  // иначе пустые/старые значения из окружения процесса перекрывают реальные ключи.
  const result = dotenv.config({ path: envPath, override: scoped });
  if (result.error) {
    console.warn(`[env] Failed to load ${envPath}: ${result.error.message}`);
  } else {
    const testnet = process.env.BINANCE_TESTNET ?? "(unset)";
    const hasKey = process.env.BINANCE_API_KEY ? "set" : "missing";
    const hasSecret = process.env.BINANCE_API_SECRET ? "set" : "missing";
    console.log(
      `[env] Loaded ${envPath} (BOT_ENV=${envName}, BINANCE_TESTNET=${testnet}, ` +
        `BINANCE_API_KEY=${hasKey}, BINANCE_API_SECRET=${hasSecret})`,
    );
    if (scoped && envName === "live" && (hasKey === "missing" || hasSecret === "missing")) {
      throw new Error(
        "[env] BOT_ENV=live but BINANCE_API_KEY/BINANCE_API_SECRET are empty in .env.live — refusing to start",
      );
    }
  }
} else {
  console.warn(`[env] Env file not found at ${envPath}; relying on process env`);
}
