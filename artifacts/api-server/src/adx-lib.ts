import { logger } from "./lib/logger";

const BASE_URL = "https://fapi.binance.com";

/** Загружает klines Binance Futures. Ошибки пробрасываются вызывающему. */
export async function getKlines(symbol: string, interval = "1h", limit = 100) {
  const url = new URL(`${BASE_URL}/fapi/v1/klines`);
  url.searchParams.set("symbol", symbol);
  url.searchParams.set("interval", interval);
  url.searchParams.set("limit", String(limit));
  const resp = await fetch(url.toString());
  if (!resp.ok) {
    const text = await resp.text().catch(() => "unknown error");
    throw new Error(`Binance klines ${symbol}: ${resp.status} ${text}`);
  }
  return (await resp.json()) as any[];
}

export function computeAdx(highs: number[], lows: number[], closes: number[], period = 14): number {
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

/** Лимиты klines по таймфреймам — те же, что использует routes/adx.ts. */
const TF_LIMITS: Record<string, number> = {
  "5m": 100,
  "15m": 100,
  "30m": 100,
  "1h": 100,
  "4h": 100,
  "12h": 60,
  "1d": 30,
};

const ADX_FOR_TTL_MS = 60_000;
const adxForCache = new Map<string, { at: number; value: number | null }>();
const adxForInflight = new Map<string, Promise<number | null>>();

/**
 * ADX одного (symbol, timeframe) с округлением до 0.01 и TTL-кэшем 60 c
 * (движок вызывает её каждый tick; кэш бережёт вес REST).
 * Возвращает null, если данных/расчёта нет.
 */
export async function computeAdxFor(symbol: string, timeframe: string): Promise<number | null> {
  const sym = String(symbol ?? "").trim().toUpperCase();
  const tf = String(timeframe ?? "").trim();
  if (!sym || !tf) return null;

  const key = `${sym}|${tf}`;
  const cached = adxForCache.get(key);
  if (cached && Date.now() - cached.at < ADX_FOR_TTL_MS) return cached.value;

  const existing = adxForInflight.get(key);
  if (existing) return existing;

  const promise = (async (): Promise<number | null> => {
    try {
      const klines = await getKlines(sym, tf, TF_LIMITS[tf] ?? 100);
      const highs = klines.map((k: any) => parseFloat(k[2]));
      const lows = klines.map((k: any) => parseFloat(k[3]));
      const closes = klines.map((k: any) => parseFloat(k[4]));
      const adx = computeAdx(highs, lows, closes, 14);
      const value = Number.isFinite(adx) && adx > 0 ? Number(adx.toFixed(2)) : null;
      adxForCache.set(key, { at: Date.now(), value });
      return value;
    } catch (e) {
      logger.warn(
        { symbol: sym, timeframe: tf, err: (e as Error).message },
        "[adx-lib] computeAdxFor failed",
      );
      return null;
    } finally {
      adxForInflight.delete(key);
    }
  })();

  adxForInflight.set(key, promise);
  return promise;
}
