#!/bin/bash
echo "=== test write in container ==="
docker exec replit_scalper-api-testnet node -e '
const fs = require("fs");
const path = require("path");
const BOT_DIR = "/app/bot";
const BOT_ENV = "testnet";
const GLOBAL_LIMITS_FILE = path.resolve(BOT_DIR, "..", "data", "global_limits_" + BOT_ENV + ".json");
console.log("FILE PATH:", GLOBAL_LIMITS_FILE);
try {
  fs.mkdirSync(path.dirname(GLOBAL_LIMITS_FILE), { recursive: true });
  fs.writeFileSync(GLOBAL_LIMITS_FILE, JSON.stringify({max_positions: 9, max_positions_with_expired: 25}, null, 2));
  console.log("WRITE OK");
} catch (e) {
  console.log("ERROR:", e.message);
}
console.log("EXISTS:", fs.existsSync(GLOBAL_LIMITS_FILE));
'