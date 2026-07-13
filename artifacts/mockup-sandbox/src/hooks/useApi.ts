const API = "http://localhost:5000/api";

export async function fetchBots() {
  const r = await fetch(`${API}/bots`);
  return r.json();
}

export async function fetchTrades(symbol?: string, limit = 50) {
  const url = new URL(`${API}/trades`);
  if (symbol) url.searchParams.set("symbol", symbol);
  url.searchParams.set("limit", String(limit));
  const r = await fetch(url);
  return r.json();
}

export async function fetchStats() {
  const r = await fetch(`${API}/trades/stats`);
  return r.json();
}

export async function startBot(symbol: string) {
  const r = await fetch(`${API}/bots/${symbol}/start`, { method: "POST" });
  return r.json();
}

export async function stopBot(symbol: string) {
  const r = await fetch(`${API}/bots/${symbol}/stop`, { method: "POST" });
  return r.json();
}

export async function updateConfig(symbol: string, config: Record<string, unknown>) {
  const r = await fetch(`${API}/bots/${symbol}/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  return r.json();
}

export async function runBacktest(symbol: string, payload: Record<string, unknown>) {
  const r = await fetch(`${API}/backtest/${symbol}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return r.json();
}
