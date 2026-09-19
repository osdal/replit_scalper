import { Router } from "express";
import { notifyTokenGuard } from "../middlewares/notifyAuth";
import {
  GRID_ENGINE_AUTO_ENV_KEYS,
  GRID_ENGINE_RESTART_REQUIRED,
  applyGridEngineConfigToEnv,
  getGridEngineConfig,
  readGridEngineConfigFile,
  writeGridEngineConfigFile,
  type GridEngineAutoConfigKey,
} from "../grid-engine";

const router = Router();

// Числовые границы валидации POST /config (int-поля допускают дробные значения:
// движок всё равно читает их через envInt, т.е. усекает).
const NUMERIC_BOUNDS: Record<
  Exclude<GridEngineAutoConfigKey, "autoEnabled">,
  { min: number; max: number; minExclusive: boolean }
> = {
  autoTpPct:      { min: 0.1, max: 50,    minExclusive: false },
  autoSlPct:      { min: 0.1, max: 50,    minExclusive: false },
  autoEdgePct:    { min: 0.1, max: 50,    minExclusive: false },
  autoGate:       { min: 1,   max: 100,   minExclusive: false },
  autoMax:        { min: 0, max: 20,    minExclusive: false },
  autoTotalMax:   { min: 0, max: 100,   minExclusive: false },
  autoOrderUsd:   { min: 0, max: 10000, minExclusive: true },
  autoLeverage:   { min: 1, max: 125,   minExclusive: false },
};

const AUTO_FIELDS = Object.keys(GRID_ENGINE_AUTO_ENV_KEYS) as GridEngineAutoConfigKey[];

function boundsText(b: { min: number; max: number; minExclusive: boolean }): string {
  return b.minExclusive ? `(${b.min}, ${b.max}]` : `[${b.min}, ${b.max}]`;
}

/**
 * GET /api/grid-engine/config
 * Эффективный runtime-конфиг движка (значения из process.env с дефолтами).
 * restartRequired перечисляет поля, читаемые только при старте движка.
 */
router.get("/config", notifyTokenGuard, (_req, res) => {
  return res.json(getGridEngineConfig());
});

/**
 * POST /api/grid-engine/config
 * body (partial): { autoEnabled?, autoTpPct?, autoSlPct?, autoEdgePct?, autoGate?,
 * autoMax?, autoTotalMax?, autoOrderUsd?, autoLeverage? }
 * Валидирует поля, зеркалит их в process.env (движок применит на следующем tick)
 * и персистит в data/grid-engine.json. Изменения engineEnabled/intervalMs/
 * staleActiveMinutes не принимаются — они всегда перечислены в restartRequired.
 */
router.post("/config", notifyTokenGuard, (req, res) => {
  const body = req.body;
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return res.status(400).json({ ok: false, error: "body must be a JSON object" });
  }

  const unknown = Object.keys(body).filter((k) => !AUTO_FIELDS.includes(k as GridEngineAutoConfigKey));
  if (unknown.length > 0) {
    return res
      .status(400)
      .json({ ok: false, error: `unknown field(s): ${unknown.join(", ")}` });
  }

  const updates: Record<string, unknown> = {};
  for (const field of AUTO_FIELDS) {
    if (!Object.prototype.hasOwnProperty.call(body, field)) continue;
    const value = (body as Record<string, unknown>)[field];

    if (field === "autoEnabled") {
      if (typeof value !== "boolean") {
        return res.status(400).json({ ok: false, error: "autoEnabled must be a boolean" });
      }
    } else {
      const bounds = NUMERIC_BOUNDS[field];
      if (typeof value !== "number" || !Number.isFinite(value)) {
        return res.status(400).json({ ok: false, error: `${field} must be a finite number` });
      }
      const aboveMin = bounds.minExclusive ? value > bounds.min : value >= bounds.min;
      if (!aboveMin || value > bounds.max) {
        return res
          .status(400)
          .json({ ok: false, error: `${field} must be in ${boundsText(bounds)}` });
      }
    }

    updates[GRID_ENGINE_AUTO_ENV_KEYS[field]] = value;
  }

  const applied = applyGridEngineConfigToEnv(updates);
  const existing = readGridEngineConfigFile() ?? {};
  if (!writeGridEngineConfigFile({ ...existing, ...updates })) {
    return res.status(500).json({ ok: false, error: "failed to persist grid-engine config" });
  }

  return res.json({
    ok: true,
    config: getGridEngineConfig(),
    applied,
    restartRequired: [...GRID_ENGINE_RESTART_REQUIRED],
  });
});

export default router;
