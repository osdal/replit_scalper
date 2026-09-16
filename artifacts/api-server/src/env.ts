import path from "path";
import fs from "fs";
import { fileURLToPath } from "url";
import dotenv from "dotenv";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// .env живёт в корне репозитория: artifacts/api-server/src -> ../../../.env
const rootEnvPath = path.resolve(__dirname, "../../../.env");

if (fs.existsSync(rootEnvPath)) {
  const result = dotenv.config({ path: rootEnvPath, override: false });
  if (result.error) {
    console.warn(`[env] Failed to load root .env: ${result.error.message}`);
  } else {
    const testnet = process.env.BINANCE_TESTNET ?? "(unset)";
    const hasKey = process.env.BINANCE_API_KEY ? "set" : "missing";
    const hasSecret = process.env.BINANCE_API_SECRET ? "set" : "missing";
    console.log(
      `[env] Loaded root .env from ${rootEnvPath} (BINANCE_TESTNET=${testnet}, BINANCE_API_KEY=${hasKey}, BINANCE_API_SECRET=${hasSecret})`
    );
  }
} else {
  console.warn(`[env] Root .env not found at ${rootEnvPath}; relying on process env`);
}
