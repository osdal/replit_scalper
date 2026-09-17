import { Router } from "express";
import { createRequire } from "node:module";
import { logger } from "../lib/logger";

const router = Router();

const MARK_PRICE_WS_URL = "wss://fstream.binancefuture.com/ws/!markPrice@arr@1s";
const REST_ALL_PRICES_URL = "https://fapi.binance.com/fapi/v1/ticker/price";
const REST_POLL_INTERVAL_MS = 10_000;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 60_000;

type Transport = "global-websocket" | "ws" | "rest";

const markPriceCache = new Map<string, number>();

let transport: Transport | null = null;
let started = false;
let socket: any = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectAttempts = 0;
let restPollTimer: ReturnType<typeof setInterval> | null = null;

function upsertPrices(items: unknown): void {
  if (!Array.isArray(items)) return;
  for (const raw of items) {
    if (!raw || typeof raw !== "object") continue;
    const item = raw as { s?: unknown; symbol?: unknown; p?: unknown; price?: unknown };
    const symbol = String(item.s ?? item.symbol ?? "").toUpperCase();
    const price = Number(item.p ?? item.price);
    if (symbol && Number.isFinite(price)) {
      markPriceCache.set(symbol, price);
    }
  }
}

function handleMessage(raw: unknown): void {
  try {
    const text =
      typeof raw === "string"
        ? raw
        : Buffer.isBuffer(raw)
          ? raw.toString("utf8")
          : String(raw);
    upsertPrices(JSON.parse(text));
  } catch {
    // Ignore malformed frames; never throw from socket handlers.
  }
}

async function pollAllPrices(): Promise<void> {
  try {
    const r = await fetch(REST_ALL_PRICES_URL, { headers: { accept: "application/json" } });
    if (!r.ok) {
      logger.warn({ status: r.status }, "[ticker] batched REST price poll failed");
      return;
    }
    upsertPrices(JSON.parse(await r.text()));
  } catch (e) {
    logger.warn({ err: e }, "[ticker] batched REST price poll error");
  }
}

function startRestPolling(): void {
  transport = "rest";
  void pollAllPrices();
  if (restPollTimer) return;
  restPollTimer = setInterval(() => {
    void pollAllPrices();
  }, REST_POLL_INTERVAL_MS);
  restPollTimer.unref?.();
}

function resolveWebSocketCtor(): (new (url: string) => any) | null {
  const g = globalThis as typeof globalThis & {
    WebSocket?: new (url: string) => any;
    require?: NodeRequire;
  };
  if (typeof g.WebSocket === "function") {
    transport = "global-websocket";
    return g.WebSocket;
  }
  try {
    const req = typeof g.require === "function" ? g.require : createRequire(import.meta.url);
    const mod = req("ws");
    const Ctor = (mod as { WebSocket?: unknown })?.WebSocket ?? mod;
    if (typeof Ctor === "function") {
      transport = "ws";
      return Ctor as new (url: string) => any;
    }
  } catch {
    // "ws" is not an installed dependency; fall through to REST polling.
  }
  return null;
}

function scheduleReconnect(): void {
  if (reconnectTimer) return;
  const attempt = reconnectAttempts++;
  const backoff = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
  const jitter = Math.random() * backoff * 0.3;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectSocket();
  }, backoff + jitter);
  reconnectTimer.unref?.();
}

function connectSocket(): void {
  if (socket && (socket.readyState === 0 || socket.readyState === 1)) return;
  const Ctor = resolveWebSocketCtor();
  if (!Ctor) {
    startRestPolling();
    return;
  }
  try {
    const ws = new Ctor(MARK_PRICE_WS_URL);
    socket = ws;
    ws.onopen = () => {
      reconnectAttempts = 0;
    };
    ws.onmessage = (ev: any) => {
      handleMessage(ev?.data ?? ev);
    };
    ws.onerror = () => {
      logger.warn({ transport, url: MARK_PRICE_WS_URL }, "[ticker] mark-price websocket error");
    };
    ws.onclose = () => {
      if (socket === ws) socket = null;
      logger.warn({ transport }, "[ticker] mark-price websocket closed, reconnecting");
      scheduleReconnect();
    };
  } catch (e) {
    logger.warn({ err: e }, "[ticker] mark-price websocket init failed, falling back to REST");
    startRestPolling();
  }
}

function ensureMarkPriceFeed(): void {
  if (started) return;
  started = true;
  connectSocket();
}

ensureMarkPriceFeed();

/**
 * Текущая mark-цена из живого кэша фида (без REST-фолбэка). Возвращает null,
 * если по символу ещё нет цены. Используется серверным grid-движком.
 */
export function getMarkPrice(symbol: string): number | null {
  ensureMarkPriceFeed();
  const sym = String(symbol ?? "").trim().toUpperCase();
  if (!sym) return null;
  const cached = markPriceCache.get(sym);
  return typeof cached === "number" && Number.isFinite(cached) ? cached : null;
}

router.get("/price/:symbol", async (req, res) => {
  try {
    ensureMarkPriceFeed();
    const symbol = String(req.params.symbol || "").trim();
    if (!symbol) {
      return res.status(400).json({ error: "symbol is required" });
    }
    const cached = markPriceCache.get(symbol.toUpperCase());
    if (typeof cached === "number" && Number.isFinite(cached)) {
      return res.json({ symbol, price: cached, source: "binance-mark-price" });
    }
    const symbolLower = symbol.toLowerCase();
    const url = `https://fapi.binance.com/fapi/v1/ticker/price?symbol=${encodeURIComponent(symbolLower)}`;
    const r = await fetch(url, { headers: { accept: "application/json" } });
    const text = await r.text();
    if (!r.ok) {
      return res.status(r.status).json({ error: `binance ${r.status}: ${text.slice(0, 200)}` });
    }
    const data = JSON.parse(text);
    return res.json({ symbol: data.symbol, price: data.price, source: "binance-fapi" });
  } catch (e: any) {
    return res.status(500).json({ error: String(e?.message || e) });
  }
});

export default router;
