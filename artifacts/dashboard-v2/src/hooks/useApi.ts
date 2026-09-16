let API = import.meta.env.VITE_API_URL || "http://localhost:5000/api";

if (typeof window !== "undefined") {
  const host = window.location.hostname;
  if (host && host !== "localhost" && host !== "127.0.0.1") {
    API = `http://${host}:5000/api`;
  }
}

async function apiFetch(url: string, options?: RequestInit) {
  const r = await fetch(url, options);
  if (!r.ok) {
    const text = await r.text().catch(() => "Unknown error");
    throw new Error(`API ${r.status}: ${text}`);
  }
  return r.json();
}

export async function fetchBots() {
  return apiFetch(`${API}/bots`);
}

export async function fetchTrades(symbol?: string, limit = 50) {
  const url = new URL(`${API}/trades`);
  if (symbol) url.searchParams.set("symbol", symbol);
  url.searchParams.set("limit", String(limit));
  return apiFetch(url);
}

export async function fetchStats() {
  return apiFetch(`${API}/trades/stats`);
}

export async function startBot(symbol: string) {
  return apiFetch(`${API}/bots/${symbol}/start`, { method: "POST" });
}

export async function stopBot(symbol: string) {
  return apiFetch(`${API}/bots/${symbol}/stop`, { method: "POST" });
}

export async function updateConfig(symbol: string, config: Record<string, unknown>) {
  return apiFetch(`${API}/bots/${symbol}/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export async function runBacktest(symbol: string, payload: Record<string, unknown>) {
  return apiFetch(`${API}/backtest/${symbol}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function syncBinance(): Promise<{ success: boolean; synced: number }> {
  return apiFetch(`${API}/binance-sync`, { method: "POST" });
}

export async function clearTrades(): Promise<{ deleted: number }> {
  return apiFetch(`${API}/trades`, { method: "DELETE" });
}

export async function getRecoveryConfig(): Promise<{ recovery_enabled: boolean; recovery_bonus_pct: number; recovery_max_pct: number }> {
  return apiFetch(`${API}/recovery/config`);
}

export async function syncClosedTrades(): Promise<{ synced: number; total: number }> {
  return apiFetch(`${API}/trades/sync-closed`, { method: "POST" });
}

export async function updateRecoveryConfig(config: { recovery_enabled: boolean; recovery_bonus_pct: number; recovery_max_pct: number }): Promise<{ recovery_enabled: boolean; recovery_bonus_pct: number; recovery_max_pct: number }> {
  return apiFetch(`${API}/recovery/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export async function healthz(): Promise<boolean> {
  try {
    const r = await fetch(`${API}/healthz`, { method: "GET" });
    return r.ok;
  } catch {
    return false;
  }
}

export async function refreshBots(): Promise<{ success: boolean; message: string }> {
  return apiFetch(`${API}/refresh`, { method: "POST" });
}

export async function stopAllBots(): Promise<{ success: boolean; message: string; bots: { symbol: string; is_running: boolean }[] }> {
  return apiFetch(`${API}/bots/stop-all`, { method: "POST" });
}

export async function closeAllAndReset(): Promise<{ success: boolean; closed_trades: number; message: string }> {
  return apiFetch(`${API}/trading/close-and-reset`, { method: "POST" });
}

export async function clearRecoveryChains(): Promise<{ deleted: number }> {
  return apiFetch(`${API}/recovery/chains`, { method: "DELETE" });
}

export async function fetchPairs(): Promise<string[]> {
  const url = new URL(`${API}/pairs`);
  url.searchParams.set("_ts", String(Date.now()));
  return apiFetch(url.toString());
}

export async function fetchHistory(symbol: string, interval = "1d"): Promise<{ source: string; data: { t: number; o: string; h: string; l: string; c: string; v: string }[] }> {
  const url = new URL(`${API}/history/${encodeURIComponent(symbol)}`);
  url.searchParams.set("interval", interval);
  url.searchParams.set("_ts", String(Date.now()));
  return apiFetch(url.toString());
}

export async function fetchLastPrice(symbol: string): Promise<number | null> {
  try {
    const url = new URL(`${API}/ticker/price/${encodeURIComponent(symbol)}`);
    url.searchParams.set("_ts", String(Date.now()));
    const r = await fetch(url.toString());
    if (!r.ok) return null;
    const data = await r.json();
    const price = parseFloat(data?.price);
    return Number.isFinite(price) ? price : null;
  } catch {
    return null;
  }
}

export async function fetchAdx(): Promise<Record<string, { adx: number | null; gate: number; ok: boolean }>> {
  try {
    const url = new URL(`${API}/adx`);
    url.searchParams.set("_ts", String(Date.now()));
    const r = await fetch(url.toString());
    if (!r.ok) return {};
    const data = await r.json();
    return data?.pairs || {};
  } catch {
    return {};
  }
}

export async function fetchBotsStatus(): Promise<Record<string, { is_running: boolean; position: any; current_price: number | null; last_heartbeat: string }>> {
  try {
    const url = new URL(`${API}/bots`);
    url.searchParams.set("_ts", String(Date.now()));
    const r = await fetch(url.toString());
    if (!r.ok) return {};
    const data = await r.json();
    const out: Record<string, { is_running: boolean; position: any; current_price: number | null; last_heartbeat: string }> = {};
    for (const bot of data || []) {
      out[bot.symbol] = {
        is_running: !!bot.is_running,
        position: bot.position || null,
        current_price: bot.current_price != null ? Number(bot.current_price) : null,
        last_heartbeat: bot.last_heartbeat || "",
      };
    }
    return out;
  } catch {
    return {};
  }
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = (import.meta as any).env?.VITE_NOTIFY_TOKEN;
  const headers: Record<string, string> = { ...extra };
  if (token) headers["x-notify-token"] = token;
  return headers;
}

async function postTelegram(payload: Record<string, unknown>): Promise<boolean> {
  try {
    const url = new URL(`${API}/notify/telegram`);
    url.searchParams.set("_ts", String(Date.now()));
    const r = await fetch(url.toString(), {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    return !!data.ok;
  } catch {
    return false;
  }
}

export async function sendTelegramNotify(payload: {
  pair: string;
  pnl: number;
  tpPct: number;
  startPrice: number;
  closePrice: number;
}): Promise<boolean> {
  return postTelegram(payload);
}

export async function sendTelegramStart(payload: {
  pair: string;
  direction: string;
  timeframe: string;
  startPrice: number;
  tpPct: number;
  gridLevels: number;
  gate: number;
  adx: number | null;
}): Promise<boolean> {
  return postTelegram({ type: "start", ...payload });
}

export async function saveGridResult(payload: {
  uid: string;
  symbol: string;
  timeframe: string;
  phase: string;
  exitReason: string;
  tpPct: number;
  gate: number;
  levels: number;
  lo: number;
  hi: number;
  mid: number;
  entry: number;
  exit: number | null;
  pnl: number | null;
  pnlUsd?: number | null;
  positions?: number | null;
  createdAt: string;
  finishedAt: string;
  orderSizeUsd?: number;
}): Promise<boolean> {
  try {
    const r = await fetch(`${API}/grid-history`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    return !!data.ok;
  } catch {
    return false;
  }
}

export async function fetchGridHistory(limit = 200): Promise<any[]> {
  try {
    const url = new URL(`${API}/grid-history`);
    url.searchParams.set("limit", String(limit));
    url.searchParams.set("_ts", String(Date.now()));
    const r = await fetch(url.toString());
    const data = await r.json().catch(() => ({}));
    return Array.isArray(data?.results) ? data.results : [];
  } catch {
    return [];
  }
}

export async function clearGridHistory(): Promise<boolean> {
  try {
    const r = await fetch(`${API}/grid-history`, {
      method: "DELETE",
      headers: authHeaders(),
    });
    const data = await r.json().catch(() => ({}));
    return !!data.ok;
  } catch {
    return false;
  }
}

export async function createTestnetGridOrders(payload: {
  symbol: string;
  side?: "BUY" | "SELL";
  levels?: number[];
  orders?: { price: number; side: "BUY" | "SELL" }[];
  orderSizeUsd: number;
  leverage?: number;
  lo?: number;
  hi?: number;
  slBufferPct?: number;
  placeStops?: boolean;
}): Promise<{
  ok: boolean;
  results?: any[];
  error?: string;
  stops?: {
    long?: { algoId?: number; orderId?: number; triggerPrice?: number };
    short?: { algoId?: number; orderId?: number; triggerPrice?: number };
    errors?: any[];
  };
}> {
  try {
    const r = await fetch(`${API}/grid-orders/create`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    const results = Array.isArray(data.results) ? data.results : undefined;
    const placed = results?.filter((r: any) => Number.isFinite(r?.orderId)) ?? [];
    const firstError = results?.find((r: any) => r?.error)?.error;
    return {
      ok: placed.length > 0,
      results,
      error: data.error ?? (placed.length === 0 ? firstError : undefined),
      stops: data.stops,
    };
  } catch (e: any) {
    return { ok: false, error: e?.message || "failed" };
  }
}

export async function upsertGridTp(payload: {
  symbol: string;
  direction: "long" | "short";
  tpPrice: number | null;
  tpOrderId?: number | null;
}): Promise<{
  ok: boolean;
  tpOrderId?: number | null;
  orderId?: number;
  tpPrice?: number;
  replaced?: boolean;
  amended?: boolean;
  canceled?: boolean;
  error?: string;
}> {
  try {
    const r = await fetch(`${API}/grid-orders/tp`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    return {
      ...data,
      ok: r.ok && !data.error,
      error: data.error ?? (r.ok ? undefined : `API ${r.status}`),
    };
  } catch (e: any) {
    return { ok: false, error: e?.message || "failed" };
  }
}

// Постановка защитного STOP-ордера через Binance Algo API.
// clientAlgoId детерминирован: gridsl_<sym10>_long / gridsl_<sym10>_short.
export async function placeGridStop(payload: {
  symbol: string;
  direction: "long" | "short";
  triggerPrice: number;
}): Promise<{ ok: boolean; algoId?: number; orderId?: number; triggerPrice?: number; error?: string }> {
  try {
    const r = await fetch(`${API}/grid-orders/stop`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    return {
      ...data,
      ok: r.ok && !data.error,
      error: data.error ?? (r.ok ? undefined : `API ${r.status}`),
    };
  } catch (e: any) {
    return { ok: false, error: e?.message || "failed" };
  }
}

export interface OpenAlgoOrder {
  algoId?: number;
  orderId?: number;
  clientAlgoId?: string;
  triggerPrice?: number;
  algoStatus?: string;
  [key: string]: unknown;
}

// Открытые algo-ордера символа: нужны, чтобы принять уже выставленную
// защиту по её clientAlgoId (gridsl_*/gridtp_* + _long/_short).
export async function fetchOpenAlgo(symbol: string): Promise<{ ok: boolean; orders: OpenAlgoOrder[]; error?: string }> {
  try {
    const url = new URL(`${API}/grid-orders/open-algo`);
    url.searchParams.set("symbol", symbol);
    url.searchParams.set("_ts", String(Date.now()));
    const r = await fetch(url.toString(), { headers: authHeaders() });
    const data = await r.json().catch(() => ({}));
    return {
      ok: r.ok && !data.error,
      orders: Array.isArray(data.orders) ? data.orders : [],
      error: data.error ?? (r.ok ? undefined : `API ${r.status}`),
    };
  } catch (e: any) {
    return { ok: false, orders: [], error: e?.message || "failed" };
  }
}

export interface AlgoStatusResult {
  algoId?: number;
  algoStatus?: string;
  triggerPrice?: number;
  actualPrice?: number | string;
  actualQty?: number | string;
}

// Статусы algo-ордеров по algoId: реконсиляция исполненной защиты.
export async function fetchAlgoStatus(payload: {
  symbol: string;
  algoIds: number[];
}): Promise<{ ok: boolean; results: AlgoStatusResult[]; error?: string }> {
  try {
    const r = await fetch(`${API}/grid-orders/algo-status`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    return {
      ok: r.ok && !data.error,
      results: Array.isArray(data.results) ? data.results : [],
      error: data.error ?? (r.ok ? undefined : `API ${r.status}`),
    };
  } catch (e: any) {
    return { ok: false, results: [], error: e?.message || "failed" };
  }
}

export async function cancelTestnetGridOrders(payload: {
  symbol: string;
  orderIds?: number[];
  cancelAll?: boolean;
}): Promise<{ ok: boolean; error?: string }> {
  try {
    const r = await fetch(`${API}/grid-orders/cancel`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    return { ok: !!data.results || !!data.cancelled, error: data.error };
  } catch (e: any) {
    return { ok: false, error: e?.message || "failed" };
  }
}

export async function cancelGridStops(payload: {
  symbol: string;
  orderIds: number[];
}): Promise<{ ok: boolean; canceled?: number[]; errors?: any[]; error?: string }> {
  try {
    const r = await fetch(`${API}/grid-orders/cancel-stops`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    return {
      ok: r.ok && !data.error,
      canceled: Array.isArray(data.canceled) ? data.canceled : undefined,
      errors: Array.isArray(data.errors) ? data.errors : undefined,
      error: data.error ?? (r.ok ? undefined : `API ${r.status}`),
    };
  } catch (e: any) {
    return { ok: false, error: e?.message || "failed" };
  }
}

export async function fetchGridFills(payload: {
  symbol: string;
  orderIds: number[];
  sinceMs?: number;
}): Promise<{ ok: boolean; error?: string; results?: any[]; positionAmt?: number | null; entryPrice?: number | null }> {
  try {
    const { symbol, orderIds, sinceMs } = payload;
    const r = await fetch(`${API}/grid-orders/fills`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ symbol, orderIds, sinceMs }),
    });
    const data = await r.json().catch(() => ({}));
    return {
      ...data,
      ok: r.ok && !data.error,
      error: data.error ?? (r.ok ? undefined : `API ${r.status}`),
    };
  } catch (e: any) {
    return { ok: false, error: e?.message || "failed" };
  }
}

export async function closeGridPosition(payload: {
  symbol: string;
  direction?: "long" | "short";
  quantity?: number;
  sinceMs?: number;
}): Promise<{ ok: boolean; error?: string; avgPrice?: number; executedQty?: number; fundingUsd?: number }> {
  try {
    const r = await fetch(`${API}/grid-orders/close`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    return {
      ...data,
      ok: r.ok && !data.error,
      error: data.error ?? (r.ok ? undefined : `API ${r.status}`),
    };
  } catch (e: any) {
    return { ok: false, error: e?.message || "failed" };
  }
}

export async function resetGridAccount(): Promise<{ ok: boolean; error?: string; [key: string]: any }> {
  try {
    const r = await fetch(`${API}/grid-orders/reset`, {
      method: "POST",
      headers: authHeaders(),
    });
    const data = await r.json().catch(() => ({}));
    return {
      ...data,
      ok: r.ok && !data.error,
      error: data.error ?? (r.ok ? undefined : `API ${r.status}`),
    };
  } catch (e: any) {
    return { ok: false, error: e?.message || "failed" };
  }
}

export async function fetchGridResetStatus(): Promise<{ ok: boolean; resetAt?: number; error?: string }> {
  try {
    const r = await fetch(`${API}/grid-orders/reset-status`, {
      method: "GET",
      headers: authHeaders(),
    });
    const data = await r.json().catch(() => ({}));
    return {
      ok: r.ok && !data.error,
      resetAt: data.resetAt,
      error: data.error ?? (r.ok ? undefined : `API ${r.status}`),
    };
  } catch (e: any) {
    return { ok: false, error: e?.message || "failed" };
  }
}

export async function stopAllBotsAndReset(): Promise<{ ok: boolean; error?: string; [key: string]: any }> {
  try {
    const r = await fetch(`${API}/trading/close-and-reset`, {
      method: "POST",
      headers: authHeaders(),
    });
    const data = await r.json().catch(() => ({}));
    return {
      ...data,
      ok: r.ok && !data.error,
      error: data.error ?? (r.ok ? undefined : `API ${r.status}`),
    };
  } catch (e: any) {
    return { ok: false, error: e?.message || "failed" };
  }
}
