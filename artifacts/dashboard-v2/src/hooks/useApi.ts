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
