import { Router } from "express";

const router = Router();
const BASE_URL = "https://fapi.binance.com";

type Kline = { t: number; o: string; h: string; l: string; c: string; v: string };

// Короткий TTL: без него график «замерзает» и не совпадает с живой ценой тикера.
const CACHE_TTL_MS = 10_000;
const historyStore = new Map<string, { ts: number; data: Kline[] }>();

function limitForInterval(interval: string): number {
  const m = interval === "5m" ? 1000 : interval === "15m" ? 1000 : interval === "30m" ? 1000 : interval === "1h" ? 720 : interval === "4h" ? 180 : interval === "12h" ? 60 : interval === "1d" ? 30 : 30;
  return m;
}

async function getKlines(symbol: string, interval = "1d", limit = 30): Promise<any[]> {
  const url = new URL(`${BASE_URL}/fapi/v1/klines`);
  url.searchParams.set("symbol", symbol);
  url.searchParams.set("interval", interval);
  url.searchParams.set("limit", String(limit));
  const resp = await fetch(url.toString());
  if (!resp.ok) {
    const text = await resp.text().catch(() => "unknown error");
    throw new Error(`Binance klines ${symbol}: ${resp.status} ${text}`);
  }
  const json = (await resp.json()) as unknown;
  return Array.isArray(json) ? (json as any[]) : [];
}

function toCandles(klines: any[]): Kline[] {
  return klines.map((k) => ({
    t: k[0],
    o: k[1],
    h: k[2],
    l: k[3],
    c: k[4],
    v: k[5],
  }));
}

router.get("/:symbol", async (req, res) => {
  const symbol = String(req.params.symbol).toUpperCase();
  const interval = String(req.query.interval || "1d").toLowerCase();
  const cacheKey = `${symbol}::${interval}`;
  const cached = historyStore.get(cacheKey);
  if (cached && cached.data.length > 0 && Date.now() - cached.ts < CACHE_TTL_MS) {
    res.json({ source: "cache", data: cached.data });
    return;
  }
  try {
    const limit = limitForInterval(interval);
    const klines = await getKlines(symbol, interval, limit);
    const data = toCandles(klines).sort((a, b) => a.t - b.t);
    historyStore.set(cacheKey, { ts: Date.now(), data });
    res.json({ source: "binance", data });
  } catch (e: any) {
    res.status(500).json({ error: e?.message || "Failed to fetch klines" });
  }
});

router.post("/download", async (_req, res) => {
  try {
    const symbols = Array.from(historyStore.keys());
    const results: Record<string, number> = {};
    for (const fullKey of symbols) {
      const parts = fullKey.split("::");
      const symbol = parts[0];
      const interval = parts[1] || "1d";
      try {
        const limit = limitForInterval(interval);
        const klines = await getKlines(symbol, interval, limit);
        historyStore.set(fullKey, { ts: Date.now(), data: toCandles(klines).sort((a, b) => a.t - b.t) });
        results[fullKey] = klines.length;
      } catch (e: any) {
        results[fullKey] = -1;
      }
    }
    res.json({ downloaded: results });
  } catch (e: any) {
    res.status(500).json({ error: e?.message || "Failed to download history" });
  }
});

export default router;
