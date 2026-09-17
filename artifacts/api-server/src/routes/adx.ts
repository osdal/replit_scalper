import { Router } from "express";
import { promises as fsp } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import yaml from "js-yaml";
import { db, botsTable } from "@workspace/db";
import { getKlines, computeAdx } from "../adx-lib";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const router = Router();

/**
 * Ограничитель параллелизма: не более `limit` одновременных вызовов `fn`.
 * `staggerMs` разносит старты соседних вызовов во времени (слот резервируется
 * синхронно, поэтому воркеры соблюдают паузу между стартами).
 */
async function mapLimit<T, R>(
  items: T[],
  limit: number,
  fn: (item: T) => Promise<R>,
  staggerMs = 0
): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;
  let lastStart = 0;
  const reserveStartSlot = async () => {
    if (staggerMs <= 0) return;
    const now = Date.now();
    const target = Math.max(now, lastStart + staggerMs);
    lastStart = target;
    if (target > now) await new Promise((r) => setTimeout(r, target - now));
  };
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    for (;;) {
      const idx = next++;
      if (idx >= items.length) return;
      await reserveStartSlot();
      out[idx] = await fn(items[idx]);
    }
  });
  await Promise.all(workers);
  return out;
}

async function getSymbols(): Promise<string[]> {
  // Источник истины — таблица bots; config_*.yaml остаётся только фолбэком.
  try {
    const rows = await db.selectDistinct({ symbol: botsTable.symbol }).from(botsTable);
    const dbSymbols = [...new Set(rows.map((r) => r.symbol))].sort();
    if (dbSymbols.length > 0) return dbSymbols;
  } catch { /* fall back to config files */ }

  const configDir = path.resolve(__dirname, "../../../../bot");
  const symbols: string[] = [];
  let files: string[];
  try {
    files = (await fsp.readdir(configDir)).filter(
      (f) => f.startsWith("config_") && f.endsWith(".yaml")
    );
  } catch {
    return symbols;
  }
  for (const file of files) {
    try {
      const content = await fsp.readFile(path.join(configDir, file), "utf8");
      const parsed = yaml.load(content) as { symbol?: string };
      if (parsed?.symbol) symbols.push(parsed.symbol.toUpperCase());
    } catch {
      /* ignore unreadable file */
    }
  }
  return [...new Set(symbols)].sort();
}

type TfResult = { adx: number | null; gate: number; ok: boolean };
let cache: { at: number; payload: any } | null = null;
let inflight: Promise<any> | null = null;
// TTL увеличен до 5 минут: пересборка стоит 40 пар × 7 таймфреймов klines,
// поэтому более редкий refresh сокращает REST weight на общем Binance IP.
const CACHE_MS = 300_000;
const MAX_CONCURRENT_SYMBOLS = 3;
// Пауза между стартами соседних пар: запросы klines расходятся во времени, а
// не идут пачкой; суммарный refresh растягивается плавно, без burst-пиков.
const SYMBOL_STAGGER_MS = 150;

async function computeForPair(symbol: string): Promise<Record<string, TfResult>> {
  const gate = 15;
  const timeframes: Array<{ tf: "5m" | "15m" | "30m" | "1h" | "4h" | "12h" | "1d"; limit: number }> = [
    { tf: "5m", limit: 100 },
    { tf: "15m", limit: 100 },
    { tf: "30m", limit: 100 },
    { tf: "1h", limit: 100 },
    { tf: "4h", limit: 100 },
    { tf: "12h", limit: 60 },
    { tf: "1d", limit: 30 },
  ];
  const entries = await Promise.all(
    timeframes.map(async ({ tf, limit }) => {
      try {
        const klines = await getKlines(symbol, tf, limit);
        const highs = klines.map((k: any) => parseFloat(k[2]));
        const lows = klines.map((k: any) => parseFloat(k[3]));
        const closes = klines.map((k: any) => parseFloat(k[4]));
        const adx = computeAdx(highs, lows, closes, 14);
        const valid = Number.isFinite(adx) && adx > 0;
        const result: TfResult = {
          adx: valid ? Number(adx.toFixed(2)) : null,
          gate,
          ok: valid ? adx < gate : false,
        };
        return [tf, result] as const;
      } catch {
        return [tf, { adx: null, gate, ok: false } as TfResult] as const;
      }
    })
  );
  return Object.fromEntries(entries);
}

router.get("/", async (_req, res) => {
  try {
    if (cache && Date.now() - cache.at < CACHE_MS) {
      return res.json(cache.payload);
    }
    // Coalescing: одновременные запросы ждут одну общую пересборку, а не
    // запускают N×7 запросов каждый.
    if (!inflight) {
      inflight = (async () => {
        const symbols = await getSymbols();
        const pairs: Record<string, Record<string, TfResult>> = {};
        await mapLimit(symbols, MAX_CONCURRENT_SYMBOLS, async (symbol) => {
          pairs[symbol] = await computeForPair(symbol);
        }, SYMBOL_STAGGER_MS);
        const payload = { gate: 15, pairs };
        // Не кэшируем полностью деградировавший ответ: иначе все-null ADX
        // держит гейт дашборда отключённым весь TTL.
        const hasLiveAdx = Object.values(pairs).some((tfs) =>
          Object.values(tfs).some((r) => r.adx !== null)
        );
        if (hasLiveAdx) {
          cache = { at: Date.now(), payload };
        }
        return payload;
      })().finally(() => {
        inflight = null;
      });
    }
    const payload = await inflight;
    return res.json(payload);
  } catch (e: any) {
    // При ошибке отдаём устаревший кэш, если он есть.
    if (cache) return res.json(cache.payload);
    return res.status(500).json({ error: e?.message || "Failed to compute ADX" });
  }
});

export default router;
