import { Router } from "express";
import { spawn } from "child_process";
import path from "path";
import fs from "fs";

const router = Router();
const BOT_DIR = process.env.BOT_DIR || path.resolve("../../bot");

// Хранит активные задачи оптимизации
const runningJobs: Map<string, {
  process: ReturnType<typeof spawn>;
  output: string[];
  status: "running" | "done" | "error";
  startedAt: string;
  results: object | null;
}> = new Map();

// POST /optimizer/run — запустить оптимизацию
router.post("/run", (req, res) => {
  const { symbol, timeframe, start, end, trials = 100, config } = req.body;

  if (!symbol || !start || !end) {
    return res.status(400).json({ error: "symbol, start, end are required" });
  }

  const jobId = `${symbol}_${Date.now()}`;
  const configFile = config || `config_${symbol.replace("USDT", "").toLowerCase()}.yaml`;

  const args = [
    "optimizer.py",
    "--config", configFile,
    "--symbol", symbol,
    "--trials", String(trials),
    "--start", start,
    "--end", end,
  ];
  if (timeframe) args.push("--timeframe", timeframe);

  const proc = spawn("python", args, { cwd: BOT_DIR });

  const job = {
    process: proc,
    output: [] as string[],
    status: "running" as const,
    startedAt: new Date().toISOString(),
    results: null as object | null,
  };
  runningJobs.set(jobId, job);

  proc.stdout.on("data", (data: Buffer) => {
    const lines = data.toString().split("\n").filter(Boolean);
    job.output.push(...lines);
    // Ограничиваем буфер
    if (job.output.length > 500) job.output = job.output.slice(-500);
  });

  proc.stderr.on("data", (data: Buffer) => {
    job.output.push(`[ERR] ${data.toString().trim()}`);
  });

  proc.on("close", (code) => {
    job.status = code === 0 ? "done" : "error";
    // Парсим результаты из вывода
    job.results = parseResults(job.output);
  });

  res.json({ jobId, message: `Optimization started for ${symbol}` });
});

// GET /optimizer/jobs — список активных задач
router.get("/jobs", (_req, res) => {
  const jobs = Array.from(runningJobs.entries()).map(([id, job]) => ({
    jobId: id,
    status: job.status,
    startedAt: job.startedAt,
    linesCount: job.output.length,
  }));
  res.json(jobs);
});

// GET /optimizer/jobs/:jobId — статус и вывод задачи
router.get("/jobs/:jobId", (req, res) => {
  const job = runningJobs.get(req.params.jobId);
  if (!job) return res.status(404).json({ error: "Job not found" });

  // Последние 50 строк вывода для прогресса
  const recentOutput = job.output.slice(-50);
  // Парсим прогресс из строки типа: [████░░] 45/100  best=2.3456
  const progressLine = [...job.output].reverse().find(l => l.includes("/") && l.includes("best="));
  let progress = 0;
  let best = 0;
  let current = 0;
  let total = 0;
  if (progressLine) {
    const m = progressLine.match(/(\d+)\/(\d+)\s+best=([\d.]+)/);
    if (m) {
      current = parseInt(m[1]);
      total = parseInt(m[2]);
      best = parseFloat(m[3]);
      progress = Math.round(current / total * 100);
    }
  }

  res.json({
    jobId: req.params.jobId,
    status: job.status,
    startedAt: job.startedAt,
    progress,
    current,
    total,
    best,
    output: recentOutput,
    results: job.results,
  });
});

// DELETE /optimizer/jobs/:jobId — остановить задачу
router.delete("/jobs/:jobId", (req, res) => {
  const job = runningJobs.get(req.params.jobId);
  if (!job) return res.status(404).json({ error: "Job not found" });
  job.process.kill("SIGTERM");
  job.status = "error";
  runningJobs.delete(req.params.jobId);
  res.json({ success: true });
});

// GET /optimizer/results/:symbol — последние CSV результаты
router.get("/results/:symbol", (req, res) => {
  const logsDir = path.join(BOT_DIR, "logs");
  try {
    const files = fs.readdirSync(logsDir)
      .filter(f => f.startsWith("optimization_") && f.endsWith(".csv"))
      .sort().reverse();
    if (!files.length) return res.json([]);

    const content = fs.readFileSync(path.join(logsDir, files[0]), "utf8");
    const lines = content.split("\n").filter(Boolean);
    if (lines.length < 2) return res.json([]);

    const headers = lines[0].split(",");
    const rows = lines.slice(1, 11).map(line => {
      const vals = line.split(",");
      return Object.fromEntries(headers.map((h, i) => [h.trim(), vals[i]?.trim()]));
    });
    res.json(rows);
  } catch (e) {
    res.json([]);
  }
});

function parseResults(output: string[]): object | null {
  // Ищем строки с результатами в выводе
  const results: { rank: number; score: number; params: Record<string, number> }[] = [];
  let inTable = false;

  for (const line of output) {
    if (line.includes("TOP") && line.includes("RESULTS")) { inTable = true; continue; }
    if (inTable && line.match(/^\s+\d+\s+[\d.]+/)) {
      const parts = line.trim().split(/\s+/);
      if (parts.length >= 9) {
        results.push({
          rank: parseInt(parts[0]),
          score: parseFloat(parts[1]),
          params: {
            ema_fast: parseInt(parts[2]),
            ema_slow: parseInt(parts[3]),
            sl_pct: parseFloat(parts[4]),
            tp1_pct: parseFloat(parts[5]),
            tp2_pct: parseFloat(parts[6]),
            volume_multiplier: parseFloat(parts[7]),
            tp1_close_pct: parseInt(parts[8]),
          }
        });
      }
    }
    if (inTable && line.includes("===") && results.length > 0) break;
  }

  return results.length ? results : null;
}

export default router;
