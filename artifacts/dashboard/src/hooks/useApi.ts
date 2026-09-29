const ENV_API = import.meta.env.VITE_API_URL as string | undefined;
let API = ENV_API || "http://localhost:5000/api";

// Ручной host-override только если API не задан явно (иначе live-инстанс с
// VITE_API_URL=http://localhost:5001/api перезаписался бы обратно на :5000).
if (!ENV_API && typeof window !== "undefined") {
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

// Жёсткое убийство процесса (экстренная остановка). Позиции на бирже остаются.
export async function killBot(symbol: string) {
  return apiFetch(`${API}/bots/${symbol}/kill`, { method: "POST" });
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

export async function armBot(symbol: string) {
  return apiFetch(`${API}/bots/${symbol}/arm`, { method: "POST" });
}

export async function disarmBot(symbol: string) {
  return apiFetch(`${API}/bots/${symbol}/disarm`, { method: "POST" });
}

export async function closeAllLive() {
  return apiFetch(`${API}/live/close-all`, { method: "POST" });
}

export async function fetchDailyLoss(): Promise<{ bot_env: string; limit: number; net: number; tripped: boolean }> {
  return apiFetch(`${API}/live/daily-loss`);
}

export async function fetchDrawdown(): Promise<{ bot_env: string; equity: number; reference: number; peak: number; fixed_deposit: number; drawdown_pct: number; threshold_pct: number; enabled: boolean }> {
  return apiFetch(`${API}/live/drawdown`);
}

// Релей testnet→live: счётчики и последний сигнал.
export async function fetchRelayStats(symbol?: string): Promise<{
  symbol: string | null; pending: number; consumed: number; skipped: number;
  last: any | null; last_consumed: any | null;
}> {
  const url = symbol ? `${API}/signals/stats?symbol=${symbol}` : `${API}/signals/stats`;
  return apiFetch(url);
}

// Тумблер relay-only: пишем в БД (YAML это поле не использует).
export async function setRelayOnly(symbol: string, value: boolean): Promise<unknown> {
  return updateConfig(symbol, { relay_only: value });
}
