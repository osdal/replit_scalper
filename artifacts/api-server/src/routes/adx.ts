import { Router } from "express";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import yaml from "js-yaml";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const router = Router();
const BASE_URL = "https://fapi.binance.com";

function getSymbols(): string[] {
  const configDir = path.resolve(__dirname, "../../../../bot");
  const symbols: string[] = [];
  if (!fs.existsSync(configDir)) return symbols;
  const files = fs.readdirSync(configDir).filter(f => f.startsWith("config_") && f.endsWith(".yaml"));
  for (const file of files) {
    try {
      const content = fs.readFileSync(path.join(configDir, file), "utf8");
      const parsed = yaml.load(content) as { symbol?: string };
      if (parsed?.symbol) symbols.push(parsed.symbol.toUpperCase());
    } catch { /* ignore unreadable file */ }
  }
  return [...new Set(symbols)].sort();
}

async function getKlines(symbol: string, interval = "1h", limit = 100) {
  const url = new URL(`${BASE_URL}/fapi/v1/klines`);
  url.searchParams.set("symbol", symbol);
  url.searchParams.set("interval", interval);
  url.searchParams.set("limit", String(limit));
  const resp = await fetch(url.toString());
  if (!resp.ok) {
    const text = await resp.text().catch(() => "unknown error");
    throw new Error(`Binance klines ${symbol}: ${resp.status} ${text}`);
  }
  return resp.json();
}

function computeAdx(highs: number[], lows: number[], closes: number[], period = 14): number {
  const n = closes.length;
  if (n < period + 1) return NaN;

  const tr: number[] = [];
  const plusDm: number[] = [];
  const minusDm: number[] = [];

  for (let i = 1; i < n; i++) {
    const h = highs[i];
    const l = lows[i];
    const c = closes[i - 1];
    const up = highs[i] - highs[i - 1];
    const dn = lows[i - 1] - lows[i];
    tr.push(Math.max(h - l, Math.abs(h - c), Math.abs(l - c)));
    plusDm.push(up > dn && up > 0 ? up : 0);
    minusDm.push(dn > up && dn > 0 ? dn : 0);
  }

  const smooth = (src: number[]) => {
    const out: number[] = [];
    let sum = src.slice(0, period).reduce((a, b) => a + b, 0);
    out.push(sum);
    for (let i = period; i < src.length; i++) {
      sum = sum - sum / period + src[i];
      out.push(sum);
    }
    return out;
  };

  const smoothTr = smooth(tr);
  const smoothPlus = smooth(plusDm);
  const smoothMinus = smooth(minusDm);

  const dx: number[] = [];
  for (let i = 0; i < smoothTr.length; i++) {
    const trVal = smoothTr[i];
    if (trVal === 0) {
      dx.push(0);
      continue;
    }
    const pdi = 100 * smoothPlus[i] / trVal;
    const mdi = 100 * smoothMinus[i] / trVal;
    const denom = pdi + mdi;
    dx.push(denom === 0 ? 0 : 100 * Math.abs(pdi - mdi) / denom);
  }

  if (dx.length === 0) return NaN;
  let adxSum = dx.slice(0, period).reduce((a, b) => a + b, 0);
  let adx = adxSum / period;
  for (let i = period; i < dx.length; i++) {
    adxSum = adxSum - adxSum / period + dx[i];
    adx = adxSum / period;
  }
  return Number.isFinite(adx) ? adx : NaN;
}

type TfResult = { adx: number | null; gate: number; ok: boolean };
let cache: { at: number; payload: any } | null = null;
const CACHE_MS = 30_000;

async function computeForPair(symbol: string): Promise<Record<string, TfResult>> {
  const gate = 15;
  const timeframes: ("1h" | "4h")[] = ["1h", "4h"];
  const entries = await Promise.all(
    timeframes.map(async (tf) => {
      try {
        const limit = tf === "4h" ? 100 : 200;
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
    const symbols = getSymbols();
    const pairs: Record<string, Record<string, TfResult>> = {};
    await Promise.all(
      symbols.map(async (symbol) => {
        pairs[symbol] = await computeForPair(symbol);
      })
    );
    const payload = { gate: 15, pairs };
    cache = { at: Date.now(), payload };
    res.json(payload);
  } catch (e: any) {
    res.status(500).json({ error: e?.message || "Failed to compute ADX" });
  }
});

export default router;
