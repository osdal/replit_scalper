import { Router } from "express";
import { db, botsTable } from "@workspace/db";
import { stopAllBotProcesses, reloadConfigsFromYaml } from "./bots";

const router = Router();

// POST /api/refresh — «Stop All & Reload Configs».
// Раньше здесь была своя копия логики остановки с findBotPid на PowerShell
// (Get-CimInstance). В Linux/Docker она всегда возвращала null: процессы ботов
// НЕ убивались, но БД переводилась в is_running=false, а следующий heartbeat
// снова ставил true — в дашборде выглядело как «остановились и мгновенно
// запустились снова». Плюс молча удалялись state-файлы (на live это оставляло
// реальные позиции без трекинга).
// Теперь используем общие кроссплатформенные хелперы из ./bots:
//   - stopAllBotProcesses() реально убивает процессы и чистит desiredRunning
//     (иначе restartDesiredBots поднял бы их обратно за 30 секунд);
//   - reloadConfigsFromYaml() обновляет конфиги, не трогая is_running/position;
//   - state-файлы НЕ удаляются: последующий Start восстановит из них позицию,
//     SL/TP и TP-цепочку (как это делает /bots/:symbol/kill).
router.post("/", async (_req, res) => {
  try {
    const stopped = await stopAllBotProcesses();
    await reloadConfigsFromYaml();
    // Честно отражаем состояние в БД. position здесь НЕ обнуляем: если бот
    // был убит с открытой позицией, она всё ещё есть на бирже, и скрывать её
    // в UI опаснее, чем показать.
    await db.update(botsTable).set({
      is_running: false,
      updated_at: new Date().toISOString(),
    });
    res.json({
      success: true,
      message: `Остановлено ботов: ${stopped.length}. Конфиги перезагружены из YAML. Готово к запуску с новыми параметрами.`,
      bots: stopped,
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});


router.post("/cancel-orders/:symbol", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const api_key = process.env.BINANCE_API_KEY;
    const api_secret = process.env.BINANCE_API_SECRET;
    if (!api_key || !api_secret) {
      return res.status(500).json({ error: "Binance API keys not configured" });
    }
    const { AsyncClient } = await import("binance");
    const c = await AsyncClient.create({ api_key, api_secret });
    await c.futures_cancel_all_open_orders({ symbol });
    const algoOrders = await c.futures_get_open_algo_orders({ symbol });
    for (const order of algoOrders) {
      await c.futures_cancel_algo_order({ symbol, algoId: order.algoId });
    }
    await c.close_connection();
    res.json({ success: true, message: `All orders cancelled for ${symbol}` });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export default router;