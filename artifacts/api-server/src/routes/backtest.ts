import { Router } from "express";
import { spawn } from "child_process";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PROJECT_ROOT = path.resolve(__dirname, "..", "..", "..", "..");
const BOT_DIR = process.env.BOT_DIR
  ? (path.isAbsolute(process.env.BOT_DIR) ? process.env.BOT_DIR : path.join(PROJECT_ROOT, process.env.BOT_DIR))
  : path.join(PROJECT_ROOT, "bot");

const router = Router();

// POST /backtest/:symbol
// Передаёт параметры бэктеста в Python-процесс через stdin как JSON —
// никакой код не собирается строкой, поэтому нет риска инъекции через
// поля symbol/config (которые приходят из тела запроса от клиента).
router.post("/:symbol", async (req, res) => {
  const symbol = req.params.symbol.toUpperCase();
  const { start, end, config } = req.body;

  if (!start || !end) {
    return res.status(400).json({ error: "start and end are required" });
  }

  const proc = spawn("python", ["backtest_runner.py"], { cwd: BOT_DIR });

  let output = "";
  let error = "";
  proc.stdout.on("data", (d) => (output += d));
  proc.stderr.on("data", (d) => (error += d));

  proc.on("close", (code) => {
    if (code !== 0 && !output.trim()) {
      console.error("[BACKTEST] Python error:", error);
      return res.status(500).json({ error: error || "Backtest failed" });
    }
    try {
      const parsed = JSON.parse(output.trim());
      if (parsed.error) {
        return res.status(400).json(parsed);
      }
      res.json(parsed);
    } catch {
      res.status(500).json({ error: "Failed to parse backtest output", raw: output, stderr: error });
    }
  });

  proc.on("error", (err) => {
    res.status(500).json({ error: `Failed to start backtest process: ${err.message}` });
  });

  // Передаём параметры через stdin как JSON — безопасно, без сборки кода строкой
  proc.stdin.write(JSON.stringify({ symbol, start, end, config }));
  proc.stdin.end();
});

export default router;
