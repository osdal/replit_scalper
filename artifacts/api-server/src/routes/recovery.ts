/**
 * Координация цепочек компенсации убытков между ботами.
 * Все боты работают как отдельные процессы — здесь единая точка
 * атомарного захвата свободного долга, чтобы избежать гонки.
 */
import { Router } from "express";
import { db, recoveryChainsTable } from "@workspace/db";
import { eq, and } from "drizzle-orm";

const router = Router();

// GET /recovery/config — глобальный флаг включён ли recovery режим
// Хранится в простом JSON файле через fs, не в БД — чтобы менять без БД
import fs from "fs";
import path from "path";

const CONFIG_PATH = process.env.RECOVERY_CONFIG_PATH ||
  path.resolve(process.env.BOT_DIR || "../../bot", "recovery_config.yaml");

function readRecoveryConfig(): { recovery_enabled: boolean; recovery_bonus_pct: number } {
  try {
    const yaml = require("js-yaml");
    const raw = yaml.load(fs.readFileSync(CONFIG_PATH, "utf8")) as any;
    return {
      recovery_enabled: !!raw.recovery_enabled,
      recovery_bonus_pct: Number(raw.recovery_bonus_pct) || 0,
    };
  } catch {
    return { recovery_enabled: false, recovery_bonus_pct: 0 };
  }
}

router.get("/config", (_req, res) => {
  res.json(readRecoveryConfig());
});

// PUT /recovery/config — изменить настройки recovery режима
router.put("/config", (req, res) => {
  try {
    const { recovery_enabled, recovery_bonus_pct } = req.body;
    const yaml = require("js-yaml");
    const content = yaml.dump({
      recovery_enabled: !!recovery_enabled,
      recovery_bonus_pct: Number(recovery_bonus_pct) || 0,
    });
    fs.writeFileSync(CONFIG_PATH, `# Общий конфиг режима компенсации убытков (recovery mode)\n# Применяется ко всем ботам одновременно через API сервер\n\n${content}`);
    res.json(readRecoveryConfig());
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/**
 * POST /recovery/claim
 * Бот вызывает это перед открытием новой позиции.
 * Атомарно захватывает самый старый свободный долг (если есть и recovery включён).
 * body: { symbol: string }
 * Возвращает: { chainId, debtAmount } или { chainId: null } если нет свободного долга.
 */
router.post("/claim", async (req, res) => {
  try {
    const config = readRecoveryConfig();
    if (!config.recovery_enabled) {
      return res.json({ chainId: null, debtAmount: 0, enabled: false });
    }

    const { symbol } = req.body;
    if (!symbol) return res.status(400).json({ error: "symbol is required" });

    // Находим самый старый свободный долг
    const [chain] = await db.select()
      .from(recoveryChainsTable)
      .where(eq(recoveryChainsTable.status, "free"))
      .orderBy(recoveryChainsTable.created_at)
      .limit(1);

    if (!chain) {
      return res.json({ chainId: null, debtAmount: 0, enabled: true });
    }

    // Атомарно захватываем — UPDATE WHERE status='free' AND id=chain.id
    // Если другой бот уже захватил — affected rows = 0
    const updated = await db.update(recoveryChainsTable)
      .set({
        status: "locked",
        locked_by: symbol,
        updated_at: new Date().toISOString(),
      })
      .where(and(
        eq(recoveryChainsTable.id, chain.id),
        eq(recoveryChainsTable.status, "free"),
      ))
      .returning();

    if (!updated.length) {
      // Гонка — кто-то успел раньше, говорим боту что свободного долга нет
      return res.json({ chainId: null, debtAmount: 0, enabled: true });
    }

    res.json({
      chainId: chain.id,
      debtAmount: chain.debt_amount,
      bonusPct: config.recovery_bonus_pct,
      enabled: true,
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/**
 * POST /recovery/report
 * Бот сообщает результат закрытия сделки (свободной или компенсирующей).
 * body: { symbol, pnl, chainId? }
 *
 * Если chainId передан (это была компенсирующая сделка):
 *   - pnl >= 0 → цепочка closed
 *   - pnl < 0  → цепочка снова free, debt_amount += abs(pnl)
 * Если chainId не передан (обычная свободная сделка):
 *   - pnl < 0  → создаём новую цепочку free с debt_amount = abs(pnl)
 *   - pnl >= 0 → ничего не делаем
 */
router.post("/report", async (req, res) => {
  try {
    const { symbol, pnl, chainId } = req.body;
    if (pnl === undefined) return res.status(400).json({ error: "pnl is required" });

    const now = new Date().toISOString();

    if (chainId) {
      // Это была компенсирующая сделка
      const [chain] = await db.select().from(recoveryChainsTable).where(eq(recoveryChainsTable.id, chainId));
      if (!chain) return res.status(404).json({ error: "Chain not found" });

      if (pnl >= 0) {
        await db.update(recoveryChainsTable)
          .set({ status: "closed", updated_at: now, closed_at: now })
          .where(eq(recoveryChainsTable.id, chainId));
        return res.json({ success: true, action: "closed" });
      } else {
        const newDebt = chain.debt_amount + Math.abs(pnl);
        await db.update(recoveryChainsTable)
          .set({ status: "free", debt_amount: newDebt, locked_by: null, updated_at: now })
          .where(eq(recoveryChainsTable.id, chainId));
        return res.json({ success: true, action: "re-freed", newDebt });
      }
    } else {
      // Обычная свободная сделка
      if (pnl < 0) {
        const [created] = await db.insert(recoveryChainsTable).values({
          debt_amount: Math.abs(pnl),
          status: "free",
          created_at: now,
          updated_at: now,
        }).returning();
        return res.json({ success: true, action: "new-chain", chainId: created.id });
      }
      return res.json({ success: true, action: "none" });
    }
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// GET /recovery/chains — список всех цепочек для дашборда
router.get("/chains", async (_req, res) => {
  try {
    const chains = await db.select().from(recoveryChainsTable).orderBy(recoveryChainsTable.created_at);
    res.json(chains);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export default router;
