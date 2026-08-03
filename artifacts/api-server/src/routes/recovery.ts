/**
 * Координация цепочек компенсации убытков между ботами.
 * Все боты работают как отдельные процессы — здесь единая точка
 * атомарного захвата свободного долга, чтобы избежать гонки.
 */
import { Router } from "express";
import { db, recoveryChainsTable } from "@workspace/db";
import { eq, and, lt, sql } from "drizzle-orm";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import yaml from "js-yaml";
import { exec } from "child_process";
import { promisify } from "util";

const execAsync = promisify(exec);

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const router = Router();

// ── Config path ───────────────────────────────────────────────────────────
const CONFIG_PATH = process.env.RECOVERY_CONFIG_PATH ||
  path.resolve(__dirname, "../../../../bot/recovery_config.yaml");

function readRecoveryConfig(): {
  recovery_enabled: boolean;
  recovery_bonus_pct: number;
  recovery_max_pct: number;
  max_free_debt_usd: number;
} {
  try {
    const raw = yaml.load(fs.readFileSync(CONFIG_PATH, "utf8")) as any;
    const val = raw.recovery_enabled;
    return {
      recovery_enabled: val === true || val === "true" || val === "True" || val === 1,
      recovery_bonus_pct: Number(raw.recovery_bonus_pct) || 0,
      recovery_max_pct: Number(raw.recovery_max_pct) || 0,
      max_free_debt_usd: Number(raw.max_free_debt_usd) || 15.0,
    };
  } catch {
    return { recovery_enabled: false, recovery_bonus_pct: 0, recovery_max_pct: 0, max_free_debt_usd: 15.0 };
  }
}

router.get("/config", (_req, res) => {
  res.json(readRecoveryConfig());
});

// PUT /recovery/config — изменить настройки recovery режима
router.put("/config", (req, res) => {
  try {
    const { recovery_enabled, recovery_bonus_pct, recovery_max_pct } = req.body;
    const current = readRecoveryConfig();
    const content = yaml.dump({
      recovery_enabled: !!recovery_enabled,
      recovery_bonus_pct: Number(recovery_bonus_pct) || 0,
      // Если фронтенд не передал recovery_max_pct (он сейчас управляется
      // только через ручное редактирование файла, не через UI) — сохраняем
      // текущее значение, чтобы не затереть его молча при простом
      // переключении тоггла recovery_enabled через дашборд.
      recovery_max_pct: recovery_max_pct !== undefined
        ? Number(recovery_max_pct)
        : current.recovery_max_pct,
    });
    fs.writeFileSync(CONFIG_PATH, `# Общий конфиг режима компенсации убытков (recovery mode)\n# Применяется ко всем ботам одновременно через API сервер\n\n${content}`);
    res.json(readRecoveryConfig());
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/**
 * POST /recovery Бот вызывает это перед новой позиции.
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

    // Потолок долга: если суммарный свободный долг >= лимита, не выдаём
    // новые recovery-позиции, чтобы не наращивать риск дальше.
    if (config.max_free_debt_usd > 0) {
      const [agg] = await db.select({ debt: sql<number>`COALESCE(SUM(debt_amount),0)` })
        .from(recoveryChainsTable)
        .where(sql`status IN ('free','locked')`);
      const totalDebt = Number(agg?.debt || 0);
      if (totalDebt >= config.max_free_debt_usd) {
        return res.json({ chainId: null, debtAmount: 0, enabled: true, reason: "debt_limit", freeDebt: totalDebt });
      }
    }

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

    return res.json({
      chainId: chain.id,
      debtAmount: chain.debt_amount,
      bonusPct: config.recovery_bonus_pct,
      enabled: true,
    });
  } catch (e) {
    return res.status(500).json({ error: String(e) });
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
      // Это была компенсирующая сделка — проверяем что бот является владельцем
      const [chain] = await db.select().from(recoveryChainsTable).where(eq(recoveryChainsTable.id, chainId));
      if (!chain) return res.status(404).json({ error: "Chain not found" });

      // Валидация: только бот, который захватил цепочку, может по ней отчитаться
      if (chain.locked_by && chain.locked_by !== symbol) {
        return res.status(403).json({ error: `Chain #${chainId} is locked by ${chain.locked_by}, not ${symbol}` });
      }

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
    return res.status(500).json({ error: String(e) });
  }
});

/**
 * POST /recovery/release
 * Освобождает захваченную цепочку (переводит обратно в free).
 * Вызывается ботом если не удалось открыть позицию-компенсатор.
 * body: { symbol, chainId }
 */
router.post("/release", async (req, res) => {
  try {
    const { symbol, chainId } = req.body;
    if (!chainId) return res.status(400).json({ error: "chainId is required" });

    const [chain] = await db.select().from(recoveryChainsTable).where(eq(recoveryChainsTable.id, chainId));
    if (!chain) return res.status(404).json({ error: "Chain not found" });

    // Только владелец может освободить
    if (chain.locked_by && chain.locked_by !== symbol) {
      return res.status(403).json({ error: `Chain #${chainId} is locked by ${chain.locked_by}` });
    }

    await db.update(recoveryChainsTable)
      .set({ status: "free", locked_by: null, updated_at: new Date().toISOString() })
      .where(eq(recoveryChainsTable.id, chainId));

    return res.json({ success: true, action: "released" });
  } catch (e) {
    return res.status(500).json({ error: String(e) });
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

// DELETE /recovery/chains/:id — удалить одну цепочку
router.delete("/chains/:id", async (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (isNaN(id)) return res.status(400).json({ error: "Invalid ID" });
    const result = await db.delete(recoveryChainsTable)
      .where(eq(recoveryChainsTable.id, id))
      .returning();
    res.json({ deleted: result.length });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// DELETE /recovery/chains — удалить все цепочки recovery (очистка БД)
router.delete("/chains", async (_req, res) => {
  try {
    const result = await db.delete(recoveryChainsTable).returning();
    res.json({ deleted: result.length });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// ── Восстановление "зависших" locked-цепочек ──────────────────────────────
//
// Цепочка переходит в status="locked" при POST /claim. Если бот упал между
// claim и открытием позиции (или во время открытия), цепочка остаётся locked
// навсегда: claim берёт только free цепочки, поэтому такой долг выпадает из
// ротации. Это возникает, например, когда процесс бота падает/убивается.
//
// Освобождаем locked-цепочку если:
//   1) бот, который её захватил (locked_by), больше не запущен (процесс умер), ИЛИ
//   2) цепочка заблокирована дольше, чем RECOVERY_LOCK_TTL (защита от случая,
//      когда процесс жив, но потерял контроль над цепочкой, либо symbol пуст).
const RECOVERY_LOCK_TTL_MS = (() => {
  const v = Number(process.env.RECOVERY_LOCK_TTL_MINUTES);
  return (Number.isFinite(v) && v > 0 ? v : 360) * 60 * 1000; // по умолчанию 6 часов
})();

async function isBotProcessAlive(symbol: string): Promise<boolean> {
  const configFile = `config_${String(symbol).replace("USDT", "").toLowerCase()}.yaml`;
  try {
    let stdout = "";
    if (process.platform === "win32") {
      ({ stdout } = await execAsync(
        `powershell -Command "Get-CimInstance -ClassName Win32_Process -Filter \\"Name='python.exe'\\" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"`
      ));
    } else {
      ({ stdout } = await execAsync(
        `ps aux | grep "python.*main.py.*${configFile}" | grep -v grep`
      ));
    }
    return stdout.includes(configFile);
  } catch {
    return false;
  }
}

async function recoverStaleChains(): Promise<number> {
  const locked = await db.select()
    .from(recoveryChainsTable)
    .where(eq(recoveryChainsTable.status, "locked"));

  const now = Date.now();
  let released = 0;

  for (const chain of locked) {
    let stale = false;

    // Процесс-владелец мёртв — безусловно освобождаем.
    if (chain.locked_by) {
      stale = !(await isBotProcessAlive(chain.locked_by));
    }

    // Срок блокировки истёк — защитный TTL.
    if (!stale && chain.updated_at) {
      const updatedMs = new Date(chain.updated_at).getTime();
      if (Number.isFinite(updatedMs) && (now - updatedMs) > RECOVERY_LOCK_TTL_MS) {
        stale = true;
      }
    }

    // locked_by пуст (маловероятно, но возможно при старых данных) — холоста.
    if (!chain.locked_by) {
      stale = true;
    }

    if (stale) {
      await db.update(recoveryChainsTable)
        .set({ status: "free", locked_by: null, locked_trade_id: null, updated_at: new Date().toISOString() })
        .where(eq(recoveryChainsTable.id, chain.id));
      released++;
    }
  }

  return released;
}

// POST /recovery/recover-stale — вручную запустить освобождение зависших цепочек
router.post("/recover-stale", async (_req, res) => {
  try {
    const released = await recoverStaleChains();
    res.json({ success: true, released });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export { recoverStaleChains };

export default router;
