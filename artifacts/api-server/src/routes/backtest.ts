import { Router } from "express";
import { spawn } from "child_process";
import path from "path";

const router = Router();
const BOT_DIR = process.env.BOT_DIR || path.resolve("../../bot");

// POST /backtest/:symbol
router.post("/:symbol", async (req, res) => {
  const symbol = req.params.symbol.toUpperCase();
  const { start, end, config } = req.body;

  // Конвертируем JSON в Python-совместимый формат (true -> True, false -> False)
  const configJson = JSON.stringify(config).replace(/: true/g, ": True").replace(/: false/g, ": False");

  // Запускаем Python бэктест через subprocess
  const proc = spawn("python", ["-c", `
import asyncio, json, sys
sys.path.insert(0, '${BOT_DIR}')
from config import Config
from backtester import run_backtest
from binance import AsyncClient
import os
from dotenv import load_dotenv
load_dotenv('${BOT_DIR}/.env')

cfg = Config(
  symbol='${symbol}',
  mode='backtest',
  backtest_start='${start}',
  backtest_end='${end}',
  log_file='logs/backtest.log',
  **${configJson}
)

async def main():
  client = await AsyncClient.create(
    api_key=os.getenv('BINANCE_API_KEY'),
    api_secret=os.getenv('BINANCE_API_SECRET'),
  )
  try:
    stats = await run_backtest(cfg, client, __import__('logging').getLogger())
    print(json.dumps({
      'total_trades': stats.total_trades,
      'wins': stats.wins,
      'losses': stats.losses,
      'win_rate': round(stats.win_rate, 1),
      'total_pnl': round(stats.total_pnl, 4),
      'avg_win': round(stats.avg_win, 4),
      'avg_loss': round(stats.avg_loss, 4),
      'max_drawdown': round(stats.max_drawdown, 2),
      'initial_balance': stats.initial_balance,
      'final_balance': round(stats.final_balance, 2),
      'return_pct': round((stats.final_balance - stats.initial_balance) / stats.initial_balance * 100, 2),
    }))
  finally:
    await client.close_connection()

asyncio.run(main())
  `], { cwd: BOT_DIR });

  let output = "";
  let error  = "";
  proc.stdout.on("data", (d) => output += d);
  proc.stderr.on("data", (d) => error  += d);

  proc.on("close", (code) => {
    if (code !== 0) {
      return res.status(500).json({ error: error || "Backtest failed" });
    }
    try {
      res.json(JSON.parse(output.trim()));
    } catch {
      res.status(500).json({ error: "Failed to parse backtest output" });
    }
  });
});

export default router;
