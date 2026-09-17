import { Router } from "express";
import {
  db,
  gridsTable,
  GRID_CREATE_SQL,
  GRID_MIGRATIONS,
  type GridRow,
} from "@workspace/db";
import { desc, eq, sql, type SQL } from "drizzle-orm";
import { notifyTokenGuard } from "../middlewares/notifyAuth";
import { logger } from "../lib/logger";

const router = Router();

// Таблица создаётся лениво, чтобы не требовать ручного init-db. DDL — из схемы
// (@workspace/db), чтобы список колонок не расходился с Drizzle и init-db.
const ensureTable = (async () => {
  await db.run(sql.raw(GRID_CREATE_SQL));
  for (const migration of GRID_MIGRATIONS) {
    await db.run(sql.raw(migration)).catch(() => {});
  }
})().catch((e) => {
  logger.error({ err: e }, "grids table ensure failed");
});

const PHASES = ["waiting", "active", "done", "stopped"];
const DIRECTIONS = ["long", "short", "both"];

type FieldKind =
  | "num"
  | "int"
  | "str"
  | "phase"
  | "direction"
  | "jsonArr"
  | "jsonObj"
  | "time"
  | "engine";

interface FieldDef {
  col: string;
  inputs: string[];
  kind: FieldKind;
  fallback?: string | number | null;
}

// Порядок совпадает с порядком колонок в GRID_CREATE_SQL (uid и updated_at — отдельно).
const FIELDS: FieldDef[] = [
  { col: "symbol",             inputs: ["symbol", "pair"],          kind: "str" },
  { col: "timeframe",          inputs: ["timeframe"],               kind: "str" },
  { col: "phase",              inputs: ["phase"],                   kind: "phase",     fallback: "waiting" },
  { col: "direction",          inputs: ["direction"],               kind: "direction", fallback: "both" },
  { col: "lo",                 inputs: ["lo"],                      kind: "num" },
  { col: "hi",                 inputs: ["hi"],                      kind: "num" },
  { col: "midPrice",           inputs: ["midPrice"],                kind: "num" },
  { col: "gate",               inputs: ["gate"],                    kind: "num" },
  { col: "levels",             inputs: ["levels"],                  kind: "int" },
  { col: "levelPrices",        inputs: ["levelPrices"],             kind: "jsonArr" },
  { col: "tpPct",              inputs: ["tpPct"],                   kind: "num" },
  { col: "slPct",              inputs: ["slPct"],                   kind: "num" },
  { col: "edgePct",            inputs: ["edgePct"],                 kind: "num" },
  { col: "orderSizeUsd",       inputs: ["orderSizeUsd"],            kind: "num" },
  { col: "leverage",           inputs: ["leverage"],                kind: "int" },
  { col: "testnetOrderIds",    inputs: ["testnetOrderIds"],         kind: "jsonArr" },
  { col: "stopOrderIds",       inputs: ["stopOrderIds"],            kind: "jsonArr" },
  { col: "tpOrderIds",         inputs: ["tpOrderIds"],              kind: "jsonObj" },
  { col: "tpOrderPrices",      inputs: ["tpOrderPrices"],           kind: "jsonObj" },
  { col: "fillEntries",        inputs: ["fillEntries"],             kind: "jsonArr" },
  { col: "openLots",           inputs: ["openLots"],                kind: "jsonArr" },
  { col: "positionAmt",        inputs: ["positionAmt"],             kind: "num" },
  { col: "realizedPnl",        inputs: ["realizedPnl"],             kind: "num" },
  { col: "realizedUsd",        inputs: ["realizedUsd"],             kind: "num" },
  { col: "lastPrice",          inputs: ["lastPrice"],               kind: "num" },
  { col: "lastPlacementError", inputs: ["lastPlacementError"],      kind: "str" },
  { col: "engine",             inputs: ["engine"],                  kind: "engine",    fallback: "browser" },
  { col: "created_at",         inputs: ["createdAt", "created_at"], kind: "time" },
  { col: "activated_at",       inputs: ["activatedAt", "activated_at"], kind: "time" },
  { col: "finished_at",        inputs: ["finishedAt", "finished_at"],   kind: "time" },
];

const WRITE_COLUMNS = ["uid", ...FIELDS.map((f) => f.col), "updated_at"];

function pick(b: any, keys: string[]): unknown {
  if (!b || typeof b !== "object") return undefined;
  for (const k of keys) {
    if (Object.prototype.hasOwnProperty.call(b, k)) return (b as any)[k];
  }
  return undefined;
}

function hasAny(b: any, keys: string[]): boolean {
  if (!b || typeof b !== "object") return false;
  return keys.some((k) => Object.prototype.hasOwnProperty.call(b, k));
}

/** uid строки: принимает uid, а для миграции из браузера — числовой id сетки. */
function uidOf(b: any): string {
  return String(pick(b, ["uid", "id"]) ?? "").trim();
}

function toNum(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function toInt(v: unknown): number | null {
  const n = toNum(v);
  return n === null ? null : Math.round(n);
}

function toStr(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  return String(v);
}

/** Epoch ms или строка -> ISO-строка; мусор -> null. */
function toIso(v: unknown): string | null {
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "number" && Number.isFinite(v)) {
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
  }
  const s = String(v).trim();
  return s === "" ? null : s;
}

/** JSON-сериализация; ошибки не пробрасываются. */
function toJson(v: unknown, arrayOnly: boolean): string | null {
  if (v === null || v === undefined) return null;
  if (arrayOnly && !Array.isArray(v)) return null;
  if (!Array.isArray(v) && typeof v !== "object") return null;
  try {
    return JSON.stringify(v);
  } catch {
    return null;
  }
}

function toPhase(v: unknown): string | null {
  const s = String(v ?? "").trim().toLowerCase();
  return PHASES.includes(s) ? s : null;
}

function toDirection(v: unknown): string | null {
  const s = String(v ?? "").trim().toLowerCase();
  return DIRECTIONS.includes(s) ? s : null;
}

function toEngine(v: unknown): string {
  const s = toStr(v);
  return s && s.trim() ? s : "browser";
}

function coerce(raw: unknown, field: FieldDef): string | number | null {
  switch (field.kind) {
    case "num":
      return toNum(raw) ?? (field.fallback as number | null) ?? null;
    case "int":
      return toInt(raw) ?? (field.fallback as number | null) ?? null;
    case "str":
      return toStr(raw);
    case "phase":
      return toPhase(raw) ?? (field.fallback as string | null) ?? null;
    case "direction":
      return toDirection(raw) ?? (field.fallback as string | null) ?? null;
    case "jsonArr":
      return toJson(raw, true);
    case "jsonObj":
      return toJson(raw, false);
    case "time":
      return toIso(raw);
    case "engine":
      return toEngine(raw);
  }
}

/**
 * Строит полный набор значений строки, сливая вход с уже сохранённой строкой:
 * отсутствующие во входе поля берутся из existing, а для новой строки — из fallback.
 * Неизвестные поля входа игнорируются.
 */
function buildValues(
  b: any,
  existing: GridRow | null,
  now: string,
): Record<string, string | number | null> | null {
  const uid = uidOf(b) || String(existing?.uid ?? "").trim();
  if (!uid) return null;

  const out: Record<string, string | number | null> = { uid };
  for (const field of FIELDS) {
    if (hasAny(b, field.inputs)) {
      out[field.col] = coerce(pick(b, field.inputs), field);
      continue;
    }
    const ex = existing ? (existing as any)[field.col] : undefined;
    out[field.col] = ex === undefined ? (field.fallback ?? null) : ex;
  }
  out["updated_at"] = now;
  return out;
}

async function findExisting(uid: unknown): Promise<GridRow | null> {
  const u = String(uid ?? "").trim();
  if (!u) return null;
  const [row] = await db.select().from(gridsTable).where(eq(gridsTable.uid, u));
  return row ?? null;
}

async function insertRow(values: Record<string, string | number | null>): Promise<void> {
  const cells = WRITE_COLUMNS.map((col) => values[col] ?? null);
  await db.run(sql`
    INSERT OR REPLACE INTO grids (${sql.raw(WRITE_COLUMNS.join(", "))})
    VALUES (${sql.join(cells.map((cell) => sql`${cell}`), sql`, `)})
  `);
}

function parseJson(raw: string | null): unknown {
  if (raw == null) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function parseArray(raw: string | null): unknown[] {
  const p = parseJson(raw);
  return Array.isArray(p) ? p : [];
}

function parseObject(raw: string | null): Record<string, unknown> | null {
  const p = parseJson(raw);
  return p && typeof p === "object" && !Array.isArray(p)
    ? (p as Record<string, unknown>)
    : null;
}

/** Парсит JSON-колонки в реальные массивы/объекты; malformed JSON -> [] или null. */
function rowToGrid(row: GridRow) {
  return {
    ...row,
    levelPrices: parseArray(row.levelPrices),
    testnetOrderIds: parseArray(row.testnetOrderIds),
    stopOrderIds: parseArray(row.stopOrderIds),
    tpOrderIds: parseObject(row.tpOrderIds),
    tpOrderPrices: parseObject(row.tpOrderPrices),
    fillEntries: parseArray(row.fillEntries),
    openLots: parseArray(row.openLots),
  };
}

router.get("/", async (_req, res) => {
  try {
    await ensureTable;
    const rows = await db
      .select()
      .from(gridsTable)
      .orderBy(desc(gridsTable.id));
    const results = rows.map(rowToGrid);
    return res.json({ results, total: results.length });
  } catch (e: any) {
    return res.status(500).json({ ok: false, error: e?.message || "failed" });
  }
});

// Миграция состояния браузера: bulk upsert по uid.
router.post("/import", notifyTokenGuard, async (req, res) => {
  try {
    await ensureTable;
    const list = Array.isArray(req.body?.grids) ? req.body.grids : null;
    if (!list) {
      return res.status(400).json({ ok: false, error: "grids array required" });
    }
    const now = new Date().toISOString();
    let imported = 0;
    for (const item of list) {
      const existing = await findExisting(uidOf(item));
      const values = buildValues(item, existing, now);
      if (!values) continue;
      await insertRow(values);
      imported++;
    }
    return res.json({ imported });
  } catch (e: any) {
    return res.status(500).json({ ok: false, error: e?.message || "failed" });
  }
});

router.post("/", notifyTokenGuard, async (req, res) => {
  try {
    await ensureTable;
    const b = req.body || {};
    const uid = uidOf(b);
    if (!uid) {
      return res.status(400).json({ ok: false, error: "uid required" });
    }
    const existing = await findExisting(uid);
    const values = buildValues(b, existing, new Date().toISOString());
    if (!values) {
      return res.status(400).json({ ok: false, error: "uid required" });
    }
    await insertRow(values);
    const [row] = await db.select().from(gridsTable).where(eq(gridsTable.uid, uid));
    return res.json({ ok: true, grid: row ? rowToGrid(row) : null });
  } catch (e: any) {
    return res.status(500).json({ ok: false, error: e?.message || "failed" });
  }
});

router.patch("/:uid", notifyTokenGuard, async (req, res) => {
  try {
    await ensureTable;
    const uid = String(req.params.uid ?? "").trim();
    if (!uid) {
      return res.status(400).json({ ok: false, error: "uid required" });
    }
    const b = req.body || {};
    const sets: SQL[] = [];
    for (const field of FIELDS) {
      if (!hasAny(b, field.inputs)) continue;
      const value = coerce(pick(b, field.inputs), field);
      sets.push(sql`${sql.raw(field.col)} = ${value}`);
    }
    if (sets.length === 0) {
      return res.status(400).json({ ok: false, error: "no updatable fields" });
    }
    sets.push(sql`updated_at = ${new Date().toISOString()}`);
    const result = await db.run(
      sql`UPDATE grids SET ${sql.join(sets, sql`, `)} WHERE uid = ${uid}`,
    );
    if ((result.rowsAffected ?? 0) === 0) {
      return res.status(404).json({ ok: false, error: "not found" });
    }
    const [row] = await db.select().from(gridsTable).where(eq(gridsTable.uid, uid));
    return res.json({ ok: true, grid: row ? rowToGrid(row) : null });
  } catch (e: any) {
    return res.status(500).json({ ok: false, error: e?.message || "failed" });
  }
});

router.delete("/:uid", notifyTokenGuard, async (req, res) => {
  try {
    await ensureTable;
    const uid = String(req.params.uid ?? "").trim();
    if (!uid) {
      return res.status(400).json({ ok: false, error: "uid required" });
    }
    const result = await db.run(sql`DELETE FROM grids WHERE uid = ${uid}`);
    if ((result.rowsAffected ?? 0) === 0) {
      return res.status(404).json({ ok: false, error: "not found" });
    }
    return res.json({ ok: true, deleted: result.rowsAffected });
  } catch (e: any) {
    return res.status(500).json({ ok: false, error: e?.message || "failed" });
  }
});

router.delete("/", notifyTokenGuard, async (req, res) => {
  try {
    await ensureTable;
    const phaseRaw = String(req.query.phase ?? "").trim().toLowerCase();
    if (phaseRaw) {
      if (!PHASES.includes(phaseRaw)) {
        return res.status(400).json({ ok: false, error: "unknown phase" });
      }
      const result = await db.run(sql`DELETE FROM grids WHERE phase = ${phaseRaw}`);
      return res.json({ deleted: result.rowsAffected });
    }
    const result = await db.run(sql`DELETE FROM grids`);
    return res.json({ deleted: result.rowsAffected });
  } catch (e: any) {
    return res.status(500).json({ ok: false, error: e?.message || "failed" });
  }
});

export default router;
