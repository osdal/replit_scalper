const ENV_API = import.meta.env.VITE_API_URL as string | undefined;

// По умолчанию ходим на ОТНОСИТЕЛЬНЫЙ /api: запрос уходит на тот же vite-сервер,
// который отдаёт дашборд, и проксируется в нужный API (см. vite.config.ts).
// Раньше дефолтом был абсолютный http://localhost:5000/api — из-за этого
// дашборд, открытый через SSH-туннель на нестандартном порту, показывал данные
// ЧУЖОГО стека: тот, что слушал этот порт локально. Относительный путь
// делает дашборд независимым от того, на каком порту он открыт.
let API = ENV_API || "/api";

// Таймаут на запрос. Без него «полуживое» соединение (оборванный SSH-туннель,
// потерянный пакет) оставляло fetch висеть минутами: кнопка Start/Stop
// показывала бесконечный спиннер, а панель LIVE — старые параметры, хотя запрос
// давно никуда не шёл. По таймауту получаем внятную ошибку и освобождаем UI.
const DEFAULT_TIMEOUT_MS = 15000;
// Старт/стоп бота — на сервере это spawn Python-процесса (проверка зависимостей
// кэшируется, но первый запуск может занять пару секунд), поэтому запас больше.
export const ACTION_TIMEOUT_MS = 30000;

type FetchOptions = RequestInit & { timeoutMs?: number };

async function apiFetch(url: string, options: FetchOptions = {}) {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...init } = options;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { ...init, signal: ctrl.signal });
    if (!r.ok) {
      const text = await r.text().catch(() => "Unknown error");
      throw new Error(`API ${r.status}: ${text}`);
    }
    return await r.json();
  } catch (e) {
    if ((e as { name?: string } | null)?.name === "AbortError") {
      throw new Error(`API не ответил за ${Math.round(timeoutMs / 1000)} с (таймаут)`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

export async function fetchBots() {
  return apiFetch(`${API}/bots`);
}

export async function fetchTrades(symbol?: string, limit = 50) {
  // Собираем query через URLSearchParams, а не через `new URL(...)`: API теперь
  // относительный ("/api"), а `new URL` без базового URL бросает TypeError.
  // Раньше ошибка глушилась в Promise.allSettled, и вкладка Trades оставалась
  // пустой при вполне рабочем API.
  const qs = new URLSearchParams();
  if (symbol) qs.set("symbol", symbol);
  qs.set("limit", String(limit));
  return apiFetch(`${API}/trades?${qs.toString()}`);
}

export async function fetchStats() {
  return apiFetch(`${API}/trades/stats`);
}

export async function startBot(symbol: string) {
  return apiFetch(`${API}/bots/${symbol}/start`, { method: "POST", timeoutMs: ACTION_TIMEOUT_MS });
}

export async function stopBot(symbol: string) {
  return apiFetch(`${API}/bots/${symbol}/stop`, { method: "POST", timeoutMs: ACTION_TIMEOUT_MS });
}

// Жёсткое убийство процесса (экстренная остановка). Позиции на бирже остаются.
export async function killBot(symbol: string) {
  return apiFetch(`${API}/bots/${symbol}/kill`, { method: "POST", timeoutMs: ACTION_TIMEOUT_MS });
}

export async function deleteBot(symbol: string) {
  return apiFetch(`${API}/bots/${symbol}`, { method: "DELETE" });
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

export async function fetchDailyLoss(): Promise<{
  bot_env: string;
  day?: string;
  limit: number;
  limit_pct?: number;
  limit_source?: string;
  net: number;
  realized?: number;
  unrealized?: number;
  baseline?: number;
  baseline_source?: string;
  remaining_usd?: number;
  degraded?: boolean;
  tripped: boolean;
}> {
  return apiFetch(`${API}/live/daily-loss`);
}

export async function fetchDrawdown(): Promise<{
  bot_env: string;
  wallet: number;
  upnl: number;
  equity: number;
  reference: number;
  threshold_pct: number;
  default_threshold_pct: number;
  enabled: boolean;
  drawdown_pct: number;
  worst_symbol: string | null;
  positions: Array<{ symbol: string; upnl: number; loss_pct: number; threshold_pct: number }>;
}> {
  return apiFetch(`${API}/live/drawdown`);
}

/** Точное число открытых сделок (не зависит от limit/offset страницы trades). */
export async function fetchOpenTradesCount(): Promise<{ count: number }> {
  return apiFetch(`${API}/trades/count-open`);
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

// Глобальный статус торговли: max_positions, max_positions_with_expired, и др.
export async function fetchTradingStatus(): Promise<{
  max_positions: number;
  max_positions_with_expired: number;
  max_position_loss_pct: number;
}> {
  return apiFetch(`${API}/trading/status`);
}

// ── Внутренний счёт (комиссия платформы 10% с прибыли) ──────────────────────

export interface InternalAccount {
  id: number;
  user_id: number;
  balance_usd: number;
  high_water_mark_usd: number;
  created_at: string;
  updated_at: string;
}

export interface InternalTransaction {
  id: number;
  user_id: number;
  type: "credit" | "commission" | "debit" | "withdrawal";
  amount_usd: number;
  balance_after_usd: number;
  related_trade_id: number | null;
  created_at: string;
}

export interface InternalMe {
  account: InternalAccount;
  transactions: InternalTransaction[];
}

export async function internalMe(): Promise<InternalMe> {
  return apiFetch(`${API}/internal-account/me`);
}

export async function internalStatement(limit = 200) {
  return apiFetch(`${API}/internal-account/statement?limit=${limit}`);
}

// ── Регистрация пользователей через Telegram ────────────────────────────────
// Отдельный сервис (127.0.0.1:5100). Через домен путь /bot-api идёт в Caddy →
// сервис регистрации; при прямом доступе к vite работает прокси из vite.config.
const REG_API = "/bot-api/registration";

export interface RegisteredUser {
  id: number;
  email: string;
  role: string;
  status: "active" | "pending" | "blocked" | string;
  source: string;
  telegram_id: number | null;
  telegram_username: string | null;
  first_name: string | null;
  last_name: string | null;
  photo_url: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
  last_login_at: string | null;
  balance_usd: number;
  hwm_usd: number;
  bots_count: number;
  consent_messages: boolean;
  consent_at: string | null;
}

export interface RegistrationStats {
  total: number;
  telegram: number;
  active: number;
  blocked: number;
  registered_today: number;
  total_balance_usd: number;
}

export async function registrationUsers(limit = 500): Promise<{ users: RegisteredUser[] }> {
  return apiFetch(`${REG_API}/users?limit=${limit}`);
}

export async function registrationStats(): Promise<RegistrationStats> {
  return apiFetch(`${REG_API}/stats`);
}

export async function registrationSetStatus(id: number, status: "active" | "blocked") {
  return apiFetch(`${REG_API}/users/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
}

// Обновление глобальных лимитов позиций (max_position_loss_pct — убыток всей
// позиции в % от депозита; один порог на всех ботов)
export async function updateGlobalLimits(
  max_positions: number,
  max_positions_with_expired: number,
  max_position_loss_pct?: number,
): Promise<{
  success: boolean;
  max_positions: number;
  max_positions_with_expired: number;
  max_position_loss_pct: number;
}> {
  return apiFetch(`${API}/trading/global-limits`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ max_positions, max_positions_with_expired, max_position_loss_pct }),
  });
}
