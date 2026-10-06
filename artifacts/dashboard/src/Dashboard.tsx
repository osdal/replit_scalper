import { useState, useEffect, useCallback } from "react";
import OptimizerTab from "./OptimizerTab";
import RecoveryTab from "./RecoveryTab";
import LivePanel from "./LivePanel";
import { fetchBots, fetchTrades, fetchStats, startBot, stopBot, killBot, deleteBot, syncBinance, runBacktest, clearTrades, refreshBots, stopAllBots, clearRecoveryChains, healthz, updateConfig, closeAllAndReset } from "./hooks/useApi";
import { Card, CardContent, CardHeader, CardTitle } from "./components/ui/card";
import { Badge } from "./components/ui/badge";
import { Button } from "./components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./components/ui/tabs";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "./components/ui/table";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import {
  Activity, TrendingUp, TrendingDown, DollarSign, BarChart2, AlertTriangle, Zap,
  Play, Square, RefreshCw, Settings, Link2, Download, History, Trash2,
} from "lucide-react";

// ── Types ────────────────────────────────────────────────────────────────────

interface Position {
  direction: "LONG" | "SHORT";
  entry_price: number;
  sl_price: number;
  tp1_price: number;
  tp2_price: number;
  total_qty: number;
  remaining_qty: number;
  tp1_hit: boolean;
  realized_pnl: number;
}

interface Bot {
  symbol: string;
  mode: string;
  is_running: boolean;
  last_heartbeat: string | null;
  current_price: number | null;
  position: Position | null;
  leverage: number;
  risk_pct: number;
  sl_pct: number;
  tp1_pct: number;
  tp2_pct: number;
  timeframe: string;
  llm_status?: LLMStatus | null;
  stop_reason?: string | null;
  stop_requested?: boolean;
  reverse_chain_max?: number;
  max_position_notional_usd?: number;
  max_position_pct_equity?: number;
  position_size_usd?: number;
  trade_mode?: string;
}

// Причины самоостановки бота (пишет сам бот, API сбрасывает при следующем старте).
const STOP_REASON_LABELS: Record<string, string> = {
  watchdog_no_candles: "самостоп: нет свечей",
  process_exited: "процесс завершился",
  graceful_stop: "мягкая остановка (Stop)",
  graceful_stop_timeout: "мягкая остановка: таймаут",
};

interface LLMProviderStatus {
  name: string;
  state: string;          // idle | ok | degraded | blocked | error
  last_error: string;
  errors_since_ok: number;
}

interface LLMStatus {
  enabled: boolean;
  last_result: string;    // idle | approved | rejected | skipped | all_failed
  last_error: string;
  providers?: Record<string, LLMProviderStatus> | null;
}

interface Trade {
  id: number;
  symbol: string;
  direction: string;
  entry_price: number;
  exit_price: number | null;
  qty: number;
  pnl: number | null;
  exit_reason: string | null;
  entry_time: string;
  exit_time: string | null;
  is_open: boolean;
  mode: string;
  commission?: number | null;
  status?: string;
  reject_reason?: string | null;
  leg_pnl?: number | null;
  cycle_pnl?: number | null;
  chain_depth?: number | null;
  cycle_close_reason?: string | null;
}

interface Stats {
  symbol: string;
  total: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl: number;
  avg_win: number;
  avg_loss: number;
}

interface BacktestParams {
  symbol: string;
  ema_fast: number;
  ema_slow: number;
  sl_pct: number;
  tp1_pct: number;
  tp2_pct: number;
  volume_multiplier: number;
  tp1_close_pct: number;
  risk_pct: number;
  htf_enabled: boolean;
  htf_ema_fast: number;
  htf_ema_slow: number;
  start: string;
  end: string;
}

interface BacktestResult {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl: number;
  avg_win: number;
  avg_loss: number;
  max_drawdown: number;
  initial_balance: number;
  final_balance: number;
  return_pct: number;
}

interface BotConfig {
  timeframe: string;
  leverage: number;
  risk_pct: number;
  sl_pct: number;
  tp1_pct: number;
  tp1_close_pct: number;
  tp2_pct: number;
  ema_fast: number;
  ema_slow: number;
  volume_ma_period: number;
  volume_multiplier: number;
  htf_enabled: boolean;
  htf_timeframe: string;
  htf_ema_fast: number;
  htf_ema_slow: number;
  auto_mode: boolean;
  paper_balance: number;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmt(n: number | null | undefined, decimals = 2) {
  if (n == null) return "—";
  return n.toFixed(decimals);
}

function fmtTime(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const day   = String(d.getDate()).padStart(2, "0");
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const year  = String(d.getFullYear()).slice(2);
  const h     = String(d.getHours()).padStart(2, "0");
  const m     = String(d.getMinutes()).padStart(2, "0");
  return `${day}.${month}.${year} ${h}:${m}`;
}

function heartbeatAge(ts: string | null): string {
  if (!ts) return "never";
  const sec = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
}

function llmProviderBadges(status: LLMStatus) {
  if (!status.enabled) return <span className="px-2 py-0.5 rounded-full bg-zinc-800 text-zinc-500 text-xs">off</span>;
  const providers = status.providers ? Object.values(status.providers) : [];
  const colorFor = (state: string) => {
    if (state === "ok") return "bg-emerald-500/15 text-emerald-300 border-emerald-500/40";
    if (state === "degraded") return "bg-yellow-500/15 text-yellow-300 border-yellow-500/40";
    if (state === "blocked") return "bg-red-500/15 text-red-300 border-red-500/40";
    if (state === "error") return "bg-red-500/15 text-red-300 border-red-500/40";
    return "bg-zinc-800 text-zinc-400 border-zinc-700";
  };
  return (
    <>
      {providers.map((p) => (
        <span
          key={p.name}
          title={p.last_error || p.state}
          className={`px-2 py-0.5 rounded-full border text-xs ${colorFor(p.state)}`}
        >
          {p.name}:{p.state}
        </span>
      ))}
      {providers.length === 0 && (
        <span className="px-2 py-0.5 rounded-full bg-zinc-800 text-zinc-500 text-xs">no providers</span>
      )}
      {status.last_result && status.last_result !== "idle" && (
        <span
          className={`px-2 py-0.5 rounded-full border text-xs ${
            status.last_result === "rate_limited"
              ? "bg-yellow-500/15 text-yellow-300 border-yellow-500/40"
              : "bg-zinc-800 text-zinc-400 border-zinc-700"
          }`}
        >
          last: {status.last_result}
        </span>
      )}
    </>
  );
}

// ── Bot Card ─────────────────────────────────────────────────────────────────

function BotCard({ bot, onToggle, onKill, onSaveConfig, isToggling, onDelete }: { bot: Bot; onToggle: () => void; onKill: () => void; onSaveConfig: (symbol: string, cfg: Record<string, unknown>) => void; isToggling: boolean; onDelete: (symbol: string) => void }) {
  const [showCfg, setShowCfg] = useState(false);
  const [cfg, setCfg] = useState<Record<string, string>>({});

  const openCfg = () => {
    setCfg({
      leverage: String(bot.leverage ?? ""),
      risk_pct: String(bot.risk_pct ?? ""),
      sl_pct: String(bot.sl_pct ?? ""),
      tp1_pct: String(bot.tp1_pct ?? ""),
      reverse_chain_max: String(bot.reverse_chain_max ?? 10),
      max_position_notional_usd: String(bot.max_position_notional_usd ?? 0),
      max_position_pct_equity: String(bot.max_position_pct_equity ?? 0),
      position_size_usd: String(bot.position_size_usd ?? 0),
      trade_mode: String(bot.trade_mode ?? "manual"),
    });
    setShowCfg((v) => !v);
  };

  const saveCfg = () => {
    const payload: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(cfg)) {
      payload[k] = k === "trade_mode" ? v : Number(v);
    }
    onSaveConfig(bot.symbol, payload);
  };

  const pos = bot.position;
  const isLong = pos?.direction === "LONG";
  const unrealizedPnl = pos && bot.current_price
    ? isLong
      ? (bot.current_price - pos.entry_price) * pos.remaining_qty
      : (pos.entry_price - bot.current_price) * pos.remaining_qty
    : null;

  return (
    <Card className="border border-zinc-800 bg-zinc-900 text-white">
      <CardHeader className="pb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-lg font-bold">{bot.symbol}</CardTitle>
          <Badge variant={bot.mode === "live" ? "destructive" : "secondary"} className="text-xs">
            {bot.mode.toUpperCase()}
          </Badge>
          <Badge variant={bot.is_running ? "default" : "outline"} className="text-xs">
            {bot.is_running ? "● RUNNING" : "○ STOPPED"}
          </Badge>
          {bot.is_running && bot.stop_requested && (
            <Badge
              variant="destructive"
              className="text-xs"
              title="Мягкая остановка: новые входы запрещены. Текущая позиция доводится по логике бота (TP/SL/reverse-цепочка), поэтому после Stop количество позиций может ещё меняться. Бот выйдет, когда станет флэт."
            >
              ⏳ STOPPING · ждёт флэт
            </Badge>
          )}
          {!bot.is_running && bot.stop_reason && (
            <Badge
              variant="destructive"
              className="text-xs"
              title={`Причина самоостановки: ${bot.stop_reason}`}
            >
              ⚠ {STOP_REASON_LABELS[bot.stop_reason] ?? bot.stop_reason}
            </Badge>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
        <Button
          size="sm"
          variant={bot.is_running ? "destructive" : "default"}
          onClick={onToggle}
          disabled={isToggling}
          className="h-7 px-3"
        >
          {isToggling ? <><RefreshCw className="w-3 h-3 mr-1 animate-spin" />...</> :
           bot.is_running ? <><Square className="w-3 h-3 mr-1" />Stop</> : <><Play className="w-3 h-3 mr-1" />Start</>}
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={openCfg}
          className="h-7 px-2 border-zinc-700 text-zinc-300 hover:bg-zinc-800"
          title="Настройки бота"
        >
          <Settings className="w-3 h-3" />
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={onKill}
          disabled={isToggling || !bot.is_running}
          className="h-7 px-2 border-red-700 text-red-300 hover:bg-red-900/40"
          title="Жёстко убить процесс (позиции/ордера на бирже остаются)"
        >
          <Zap className="w-3 h-3" />
        </Button>
        <button
          onClick={() => onDelete(bot.symbol)}
          className="p-2 rounded hover:bg-red-900/50 transition-colors text-zinc-500 hover:text-red-400"
          title="Delete bot"
        >
          <Trash2 className="w-3.5 h-3.5" />
        </button>
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        <div className="flex justify-between text-sm">
          <span className="text-zinc-400">Price</span>
<span className="font-mono font-semibold">
             {bot.current_price != null ? `$${bot.current_price.toLocaleString()}` : "—"}
           </span>
        </div>
        <div className="flex justify-between text-sm">
          <span className="text-zinc-400">Heartbeat</span>
          <span className="text-zinc-300">{heartbeatAge(bot.last_heartbeat)}</span>
        </div>

        {bot.llm_status && (
          <div className="flex flex-wrap gap-1 items-center text-xs">
            <span className="text-zinc-400 mr-1">AI</span>
            {llmProviderBadges(bot.llm_status)}
          </div>
        )}

        <div className="flex flex-wrap gap-1 pt-1">
          {[
            `${bot.leverage}x`,
            `Risk ${bot.risk_pct}%`,
            `SL ${bot.sl_pct}%`,
            `TP1 ${bot.tp1_pct}%`,
            `TP2 ${bot.tp2_pct}%`,
            bot.timeframe,
          ].map((label) => (
            <span key={label} className="px-2 py-0.5 rounded-full bg-zinc-800 text-zinc-300 text-xs">
              {label}
            </span>
          ))}
        </div>

        {pos ? (
          <div className="rounded-lg bg-zinc-800 p-3 space-y-2 mt-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-zinc-400">OPEN POSITION</span>
              <Badge variant={isLong ? "default" : "destructive"} className="text-xs">
                {pos.direction}
              </Badge>
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
              <span className="text-zinc-400">Entry</span>
              <span className="font-mono text-right">${fmt(pos.entry_price, 4)}</span>
              <span className="text-zinc-400">Qty</span>
              <span className="font-mono text-right">{pos.remaining_qty}</span>
              <span className="text-zinc-400">SL</span>
              <span className="font-mono text-right text-red-400">${fmt(pos.sl_price, 4)}</span>
              <span className="text-zinc-400">TP1</span>
              <span className="font-mono text-right text-green-400">${fmt(pos.tp1_price, 4)}</span>
              <span className="text-zinc-400">TP2</span>
              <span className="font-mono text-right text-green-400">${fmt(pos.tp2_price, 4)}</span>
              {unrealizedPnl != null && (
                <>
                  <span className="text-zinc-400">Unrealized PnL</span>
                  <span className={`font-mono text-right font-semibold ${unrealizedPnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                    {unrealizedPnl >= 0 ? "+" : ""}{fmt(unrealizedPnl, 4)} USDT
                  </span>
                </>
              )}
            </div>
            {pos.tp1_hit && (
              <span className="text-xs text-yellow-400">● TP1 hit — SL at breakeven</span>
            )}
          </div>
        ) : (
          <div className="rounded-lg bg-zinc-800 p-3 text-center text-zinc-500 text-sm mt-2">
            No open position
          </div>
        )}

        {showCfg && (
          <div className="rounded-lg bg-zinc-800 p-3 space-y-2 mt-2">
            <div className="text-xs font-semibold text-zinc-400">CONFIG</div>
            <div className="grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
              {([
                ["leverage", "Плечо"],
                ["risk_pct", "Риск %"],
                ["sl_pct", "SL %"],
                ["tp1_pct", "TP1 %"],
                ["reverse_chain_max", "Лимит реверсов (0=∞)"],
                ["max_position_notional_usd", "Потолок нотионала $"],
                ["max_position_pct_equity", "Потолок % equity"],
                ["position_size_usd", "Маржа $ (0=дефолт)"],
              ] as [string, string][]).map(([key, label]) => (
                <label key={key} className="flex flex-col gap-0.5">
                  <span className="text-zinc-400">{label}</span>
                  <input
                    value={cfg[key] ?? ""}
                    onChange={(e) => setCfg((c) => ({ ...c, [key]: e.target.value }))}
                    className="rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 font-mono"
                  />
                </label>
              ))}
              <label className="flex flex-col gap-0.5">
                <span className="text-zinc-400">trade_mode</span>
                <select
                  value={cfg.trade_mode ?? "manual"}
                  onChange={(e) => setCfg((c) => ({ ...c, trade_mode: e.target.value }))}
                  className="rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5"
                >
                  <option value="manual">manual</option>
                  <option value="auto">auto</option>
                </select>
              </label>
            </div>
            <div className="flex gap-2 pt-1 items-center">
              <Button size="sm" onClick={saveCfg} className="h-7">Save</Button>
              <span className="text-[11px] text-zinc-500">применяется после Stop → Start</span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Stats Row ────────────────────────────────────────────────────────────────

function StatsRow({ stats }: { stats: Stats[] }) {
  const totalPnl = stats.reduce((s, x) => s + x.total_pnl, 0);
  const totalTrades = stats.reduce((s, x) => s + x.total, 0);
  const totalWins = stats.reduce((s, x) => s + x.wins, 0);

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
      {[
        { icon: <DollarSign className="w-4 h-4" />, label: "Total PnL", value: `${totalPnl >= 0 ? "+" : ""}${fmt(totalPnl, 2)} USDT`, color: totalPnl >= 0 ? "text-green-400" : "text-red-400" },
        { icon: <BarChart2 className="w-4 h-4" />, label: "Total Trades", value: String(totalTrades), color: "text-white" },
        { icon: <TrendingUp className="w-4 h-4" />, label: "Win Rate", value: totalTrades ? `${((totalWins / totalTrades) * 100).toFixed(1)}%` : "—", color: "text-white" },
        { icon: <Activity className="w-4 h-4" />, label: "Symbols", value: String(stats.length), color: "text-white" },
      ].map(({ icon, label, value, color }) => (
        <Card key={label} className="border border-zinc-800 bg-zinc-900">
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
              {icon}{label}
            </div>
            <div className={`text-2xl font-bold font-mono ${color}`}>{value}</div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

// ── PnL Chart ────────────────────────────────────────────────────────────────

function PnlChart({ trades }: { trades: Trade[] }) {
  const closed = [...trades]
    .filter((t) => !t.is_open && t.pnl != null && t.status !== "rejected")
    .sort((a, b) => new Date(a.entry_time).getTime() - new Date(b.entry_time).getTime());

  let cumulative = 0;
  const data = closed.map((t) => {
    cumulative += t.pnl!;
    return {
      time: fmtTime(t.entry_time),
      pnl: parseFloat(cumulative.toFixed(4)),
      trade_pnl: t.pnl,
    };
  });

  if (!data.length) return (
    <div className="flex items-center justify-center h-48 text-zinc-500">No closed trades yet</div>
  );

  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
        <XAxis dataKey="time" tick={{ fill: "#71717a", fontSize: 10 }} />
        <YAxis tick={{ fill: "#71717a", fontSize: 10 }} width={60} />
        <Tooltip
          contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", color: "#fff" }}
          formatter={(v: number) => [`${v >= 0 ? "+" : ""}${v.toFixed(4)} USDT`]}
        />
        <ReferenceLine y={0} stroke="#52525b" />
        <Line type="monotone" dataKey="pnl" stroke="#22c55e" dot={false} strokeWidth={2} />
      </LineChart>
    </ResponsiveContainer>
  );
}

// ── Trades Table ─────────────────────────────────────────────────────────────

type ReasonBadge = {
  variant: "default" | "secondary" | "destructive" | "outline";
  label: string;
  title?: string;
  className?: string;
};

function reasonBadge(reason: string): ReasonBadge {
  switch (reason) {
    case "TP1":
    case "TP2":
      return { variant: "default", label: reason };
    case "SL":
      return { variant: "destructive", label: reason };
    case "REVERSE_BE":
      return {
        variant: "secondary",
        label: "Reverse BE",
        title: "reverse cycle closed at the break-even target (≈ fees only)",
        className: "bg-blue-600/20 text-blue-300 border-blue-500/40",
      };
    case "REVERSE_BACKSTOP":
      return {
        variant: "destructive",
        label: "Reverse backstop",
        title: "reverse leg was closed by the exchange backstop — a real loss",
        className: "bg-orange-600/20 text-orange-300 border-orange-500/40",
      };
    case "REVERSE_CHAIN_STOP":
      return {
        variant: "secondary",
        label: "Reverse chain stop",
        title: "reverse chain limit reached — the bot force-closed the position at market",
        className: "bg-amber-600/20 text-amber-300 border-amber-500/40",
      };
    case "REVERSE_CUM_LOSS_CAP":
      return {
        variant: "destructive",
        label: "Cum-loss cap",
        title: "cumulative cycle loss reached the % of deposit cap — the bot force-closed the whole reverse cycle at market",
        className: "bg-red-600/20 text-red-300 border-red-500/40",
      };
    case "REVERSE":
      return {
        variant: "secondary",
        label: "Reverse",
        className: "bg-amber-600/20 text-amber-300 border-amber-500/40",
      };
    default:
      return { variant: reason.startsWith("TP") ? "default" : "destructive", label: reason };
  }
}

function isBreakEven(t: Trade): boolean {
  return t.pnl != null && Math.abs(t.pnl) <= (t.commission ?? 0) + 0.005;
}

function grossPnl(t: Trade): number | null {
  return t.pnl == null ? null : t.pnl + (t.commission ?? 0);
}

function TradesTable({ trades }: { trades: Trade[] }) {
  const sumPnl = trades.reduce((acc, t) => acc + (t.pnl ?? 0), 0);
  const sumCommission = trades.reduce((acc, t) => acc + (t.commission ?? 0), 0);
  const sumGross = sumPnl + sumCommission;
  const beCount = trades.filter((t) => t.exit_reason === "REVERSE_BE").length;
  const backstopCount = trades.filter((t) => t.exit_reason === "REVERSE_BACKSTOP").length;
  const tpCount = trades.filter((t) => t.exit_reason?.startsWith("TP")).length;
  const slCount = trades.filter((t) => t.exit_reason === "SL").length;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-x-2 px-1 text-[11px] text-zinc-500">
        <span>{trades.length} trades</span>
        <span>·</span>
        <span>
          Σ gross{" "}
          <span className={`font-mono font-semibold ${sumGross >= 0 ? "text-green-400" : "text-red-400"}`}>
            {sumGross >= 0 ? "+" : ""}{fmt(sumGross, 4)}
          </span>
        </span>
        <span>·</span>
        <span>Σ fees <span className="font-mono">{fmt(sumCommission, 4)}</span></span>
        <span>·</span>
        <span>
          Σ net{" "}
          <span className={`font-mono font-semibold ${sumPnl >= 0 ? "text-green-400" : "text-red-400"}`}>
            {sumPnl >= 0 ? "+" : ""}{fmt(sumPnl, 4)}
          </span>
        </span>
        <span>·</span>
        <span>BE {beCount}</span>
        <span>·</span>
        <span>backstop {backstopCount}</span>
        <span>·</span>
        <span>TP {tpCount}</span>
        <span>·</span>
        <span>SL {slCount}</span>
      </div>
      <div className="overflow-auto rounded-lg border border-zinc-800">
        <Table>
          <TableHeader>
            <TableRow className="border-zinc-800 hover:bg-transparent">
              {["Symbol", "Dir", "Entry", "Exit", "Qty", "Gross", "Fees", "Net", "Leg", "Cycle", "Legs", "Reason", "Mode", "Open", "Close"].map((h) => (
                <TableHead key={h} className="text-zinc-400 text-xs">{h}</TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {trades.length === 0 && (
              <TableRow><TableCell colSpan={15} className="text-center text-zinc-500 py-8">No trades</TableCell></TableRow>
            )}
            {trades.map((t) => {
              const gross = grossPnl(t);
              const grossBe = gross != null && Math.abs(gross) <= 0.005;
              return (
              <TableRow key={t.id} className="border-zinc-800 hover:bg-zinc-800/50">
                <TableCell className="font-semibold text-sm">{t.symbol}</TableCell>
                <TableCell>
                  <Badge variant={t.direction === "LONG" ? "default" : "destructive"} className="text-xs">
                    {t.direction}
                  </Badge>
                </TableCell>
                <TableCell className="font-mono text-sm">${fmt(t.entry_price, 4)}</TableCell>
                <TableCell className="font-mono text-sm">{t.exit_price ? `$${fmt(t.exit_price, 4)}` : "—"}</TableCell>
                <TableCell className="font-mono text-sm">{t.qty}</TableCell>
                <TableCell className={`font-mono text-sm font-semibold ${gross == null ? "" : grossBe ? "text-zinc-400" : gross >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {gross == null ? "—" : (
                    <>
                      {gross >= 0 ? "+" : ""}{fmt(gross, 4)}
                      {grossBe && (
                        <span className="ml-1 text-[10px] font-normal text-zinc-500" title="price PnL ≈ 0 (break-even)">≈0</span>
                      )}
                    </>
                  )}
                </TableCell>
                <TableCell className="font-mono text-sm text-zinc-400">
                  {t.commission == null ? "—" : fmt(t.commission, 6)}
                </TableCell>
                <TableCell className={`font-mono text-sm font-semibold ${t.pnl == null ? "" : isBreakEven(t) ? "text-zinc-400" : t.pnl >= 0 ? "text-green-400" : "text-red-400"}`} title="net = gross − fees">
                  {t.pnl == null ? "—" : (
                    <>
                      {t.pnl >= 0 ? "+" : ""}{fmt(t.pnl, 4)}
                      {isBreakEven(t) && (
                        <span className="ml-1 text-[10px] font-normal text-zinc-500" title="net PnL within commissions (break-even)">≈0</span>
                      )}
                    </>
                  )}
                </TableCell>
                <TableCell className={`font-mono text-sm ${t.leg_pnl == null ? "text-zinc-600" : t.leg_pnl >= 0 ? "text-green-400" : "text-red-400"}`} title="PnL последней ноги цикла">
                  {t.leg_pnl == null ? "—" : `${t.leg_pnl >= 0 ? "+" : ""}${fmt(t.leg_pnl, 4)}`}
                </TableCell>
                <TableCell className={`font-mono text-sm ${t.cycle_pnl == null ? "text-zinc-600" : t.cycle_pnl >= 0 ? "text-green-400" : "text-red-400"}`} title="Итог цикла (все ноги, net)">
                  {t.cycle_pnl == null ? "—" : `${t.cycle_pnl >= 0 ? "+" : ""}${fmt(t.cycle_pnl, 4)}`}
                </TableCell>
                <TableCell className="text-zinc-400 text-xs" title="Число ног в цикле">
                  {t.chain_depth == null ? "—" : t.chain_depth}
                </TableCell>
                <TableCell>
                  {t.status === "rejected" ? (
                    <div className="flex flex-col gap-1">
                      <Badge variant="default" className="text-xs bg-blue-600/20 text-blue-300 border-blue-500/40">
                        ⛔ REJECTED{t.reject_reason ? ` (${t.reject_reason.split(":").pop()})` : ""}
                      </Badge>
                      {t.exit_reason && (() => {
                        const b = reasonBadge(t.exit_reason);
                        return (
                          <Badge variant={b.variant} title={b.title} className={`text-xs ${b.className ?? ""}`}>
                            sim: {b.label}
                          </Badge>
                        );
                      })()}
                    </div>
                  ) : (
                    <div className="flex flex-col gap-1">
                      {t.exit_reason && (() => {
                        const b = reasonBadge(t.exit_reason);
                        return (
                          <Badge variant={b.variant} title={b.title} className={`text-xs ${b.className ?? ""}`}>
                            {b.label}
                          </Badge>
                        );
                      })()}
                      {t.cycle_close_reason && (
                        <span
                          className="text-[10px] text-zinc-500 font-mono"
                          title="Причина завершения reverse-цикла (почему не пошли на следующий круг)"
                        >
                          {t.cycle_close_reason}
                        </span>
                      )}
                    </div>
                  )}
                </TableCell>
                <TableCell>
                  <Badge variant={t.mode === "live" ? "default" : "secondary"} className="text-xs">
                    {t.mode?.toUpperCase() || "—"}
                  </Badge>
                </TableCell>
                <TableCell className="text-zinc-400 text-xs">{fmtTime(t.entry_time)}</TableCell>
                <TableCell className="text-zinc-400 text-xs">{fmtTime(t.exit_time)}</TableCell>
              </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

// ── Per-symbol Stats Table ────────────────────────────────────────────────────

const REVERSE_COLUMNS = 6;

/**
 * Счётчики реверсов по символам: R1..R6.
 *
 * R_n = сколько раз в этом символе случался ИМЕННО n-й реверс, то есть
 * сколько циклов дошло до (n+1) ног. Цикл без реверсов (chain_depth=1) в
 * счётчики не попадает. Так у монеты с шестью циклами по два реверса
 * будет R1=6, R2=6, R3=0.
 *
 * chain_depth заполняется при закрытии цикла; у части старых сделок он NULL
 * (записи до фикса), такие циклы не учитываются ни в одной колонке.
 */
function countReverses(rows: Trade[]): Record<string, number[]> {
  const out: Record<string, number[]> = {};
  for (const tr of rows) {
    if (!tr.symbol || tr.is_open) continue;
    if (tr.status === "rejected") continue;
    const depth = Number(tr.chain_depth ?? 0) || 0;
    if (depth < 2) continue;
    const arr = (out[tr.symbol] ||= Array(REVERSE_COLUMNS).fill(0));
    for (let n = 1; n <= REVERSE_COLUMNS; n++) {
      if (depth >= n + 1) arr[n - 1]++;
    }
  }
  return out;
}

function StatsTable({ stats, reversals }: { stats: Stats[]; reversals: Record<string, number[]> }) {
  return (
    <div className="overflow-auto rounded-lg border border-zinc-800">
      <Table>
        <TableHeader>
          <TableRow className="border-zinc-800 hover:bg-transparent">
            {["Symbol", "Trades", "Wins", "Losses", "Win Rate", "Total PnL", "Avg Win", "Avg Loss",
              ...Array.from({ length: REVERSE_COLUMNS }, (_, i) => `R${i + 1}`)].map((h) => (
              <TableHead
                key={h}
                className={`${h.startsWith("R") ? "text-amber-400/90" : "text-zinc-400"} text-xs`}
                title={h.startsWith("R")
                  ? `Сколько раз в этом символе случался ${h.slice(1)}-й реверс (цикл дошёл до ${Number(h.slice(1)) + 1} ног)`
                  : undefined}
              >
                {h}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {stats.map((s) => {
            const r = reversals[s.symbol];
            return (
            <TableRow key={s.symbol} className="border-zinc-800 hover:bg-zinc-800/50">
              <TableCell className="font-bold">{s.symbol}</TableCell>
              <TableCell>{s.total}</TableCell>
              <TableCell className="text-green-400">{s.wins}</TableCell>
              <TableCell className="text-red-400">{s.losses}</TableCell>
              <TableCell className="font-semibold">{fmt(s.win_rate, 1)}%</TableCell>
              <TableCell className={`font-mono font-semibold ${s.total_pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                {s.total_pnl >= 0 ? "+" : ""}{fmt(s.total_pnl, 4)}
              </TableCell>
              <TableCell className="font-mono text-green-400">+{fmt(s.avg_win, 4)}</TableCell>
              <TableCell className="font-mono text-red-400">{fmt(s.avg_loss, 4)}</TableCell>
              {Array.from({ length: REVERSE_COLUMNS }, (_, i) => (
                <TableCell key={i} className="font-mono text-zinc-300">{r ? r[i] : 0}</TableCell>
              ))}
            </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}

// ── Main Dashboard ────────────────────────────────────────────────────────────

const DEFAULT_CONFIG: BotConfig = {
  timeframe: "5m",
  leverage: 10,
  risk_pct: 1.0,
  sl_pct: 0.5,
  tp1_pct: 0.5,
  tp1_close_pct: 50,
  tp2_pct: 1.0,
  ema_fast: 9,
  ema_slow: 21,
  volume_ma_period: 20,
  volume_multiplier: 1.2,
  htf_enabled: false,
  htf_timeframe: "1h",
  htf_ema_fast: 9,
  htf_ema_slow: 21,
  auto_mode: true,
  paper_balance: 1000,
};

const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
const STORAGE_KEY = "backtest_result";

export default function Dashboard() {
  const [bots, setBots] = useState<Bot[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [stats, setStats] = useState<Stats[]>([]);
  const [reverseCounts, setReverseCounts] = useState<Record<string, number[]>>({});
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<Date>(new Date());
  const [notice, setNotice] = useState<string | null>(null);
  const [optJobId, setOptJobId] = useState<string | null>(null);
  const [optJob, setOptJob] = useState<any | null>(null);
  const [optSymbol, setOptSymbol] = useState("BTCUSDT");
  const [optStart, setOptStart] = useState("2026-05-01");
  const [optEnd, setOptEnd] = useState("2026-06-13");
  const [optTrials, setOptTrials] = useState(100);
  const [optJobs, setOptJobs] = useState(1);
  // Фильтр по торговой паре
  const [selectedSymbol, setSelectedSymbol] = useState<string>("all");
  const symbols = [...new Set([...bots.map(b => b.symbol), ...trades.map(t => t.symbol)])]
    .filter(Boolean)
    .sort();
  const filteredTrades = selectedSymbol === "all" ? trades : trades.filter(t => t.symbol === selectedSymbol);
  const IS_LIVE = import.meta.env.VITE_THEME === "live";
  // Inline backtest state
  const [btSymbol, setBtSymbol] = useState("BTCUSDT");
  const [btStartDate, setBtStartDate] = useState("2024-01-01");
  const [btEndDate, setBtEndDate] = useState("2024-04-01");
  const [btConfig, setBtConfig] = useState<BotConfig>(DEFAULT_CONFIG);
  const [btRunning, setBtRunning] = useState(false);
  const [btResult, setBtResult] = useState<BacktestResult | null>(null);
  const [btError, setBtError] = useState<string | null>(null);
  const [btResetKey, setBtResetKey] = useState(0);


  // Восстанавливаем результат из localStorage при монтировании
  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) {
        const parsed = JSON.parse(saved);
        if (parsed && typeof parsed === "object" && "total_trades" in parsed) {
          setBtResult(parsed);
        }
      }
    } catch {
      // ignore
    }
  }, []);

  // Сохраняем результат в localStorage при изменении
  useEffect(() => {
    if (btResult) {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(btResult));
      } catch {
        // ignore
      }
    }
  }, [btResult]);

  const load = useCallback(async () => {
    try {
      // Фильтруем на сервере по выбранной монете: клиентский фильтр по уже
      // загруженной странице иначе показывает меньше сделок, чем вкладка
      // Статистика (она агрегирует всю таблицу).
      const tradeSymbol = selectedSymbol === "all" ? undefined : selectedSymbol;
      // Запросы независимы: падение ЛЮБОГО из трёх раньше оставляло setBots
      // невызванным — дашборд показывал ноль карточек при живом API.
      const [b, t, s, rev] = await Promise.allSettled([
        fetchBots(),
        fetchTrades(tradeSymbol, 1000),
        fetchStats(),
        // Отдельный запрос БЕЗ фильтра по монете: счётчики реверсов нужны по
        // всем символам сразу, а trades выше отфильтрован выбранным символом.
        fetchTrades(undefined, 1000),
      ]);
      if (b.status === "fulfilled" && Array.isArray(b.value)) setBots(b.value);
      else if (b.status === "rejected") {
        console.error("[dashboard] не удалось загрузить /bots", b.reason);
        setBots([]);
      }
      if (t.status === "fulfilled") {
        const allTrades = Array.isArray(t.value?.trades) ? t.value.trades : [];
        // Internal-only "skip:*" records (loss streak filters, cycle/preset limits, cooldown)
        // clutter the trades table with an endless "cancelled" stream — hide them from the UI.
        setTrades(allTrades.filter((tr: Trade) => !String(tr.reject_reason || "").startsWith("skip:")));
      } else {
        // Раньше это молча проглатывалось, и вкладка Trades выглядела пустой
        // при живом API — теперь причина видна в консоли браузера.
        console.error("[dashboard] не удалось загрузить /trades", t.reason);
      }
      if (s.status === "fulfilled") setStats(Array.isArray(s.value) ? s.value : []);
      else console.error("[dashboard] не удалось загрузить /trades/stats", s.reason);
      if (rev.status === "fulfilled") {
        const rows = Array.isArray(rev.value?.trades) ? rev.value.trades : [];
        setReverseCounts(countReverses(rows));
      }
      setLastRefresh(new Date());
    } catch {
      // API not available yet
    } finally {
      setLoading(false);
    }
  }, [selectedSymbol]);

  useEffect(() => {
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, [load]);

  const [toggling, setToggling] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [apiUp, setApiUp] = useState(true);

  // Массовое задание размера позиции (только testnet): в live размер задаётся
  // каждому боту отдельно в LivePanel, чтобы нельзя было случайно изменить
  // объём сразу на всех ботах с реальными деньгами.
  const [sizePctAll, setSizePctAll] = useState("");
  const [applyingSize, setApplyingSize] = useState(false);
  const [restartAfterApply, setRestartAfterApply] = useState(true);
  const [restarting, setRestarting] = useState(false);

  useEffect(() => {
    healthz().then(setApiUp);
    const id = setInterval(() => healthz().then(setApiUp), 5000);
    return () => clearInterval(id);
  }, []);

  const handleSync = async () => {
    setSyncing(true);
    try {
      const result = await syncBinance();
      alert(`Synced ${result.synced} trades from Binance`);
      await load();
    } catch (e) {
      alert('Sync failed');
    } finally {
      setSyncing(false);
    }
  };

  const handleToggle = async (bot: Bot) => {
    if (toggling) return;
    setToggling(bot.symbol);
    try {
      // API отвечает 200 даже когда не смог: {success:false, message:...}
      // (например, бот уже не найден по PID). Без проверки Stop выглядел бы
      // как «ничего не произошло» — показываем причину.
      const res = bot.is_running ? await stopBot(bot.symbol) : await startBot(bot.symbol);
      if (res && res.success === false) {
        setNotice(`${bot.symbol}: ${res.message ?? "действие не выполнено"}`);
      } else {
        setNotice(null);
      }
      await new Promise(r => setTimeout(r, 1000));
      await load();
    } catch (e) {
      setNotice(`${bot.symbol}: ${String(e)}`);
    } finally {
      setToggling(null);
    }
  };

  const handleKill = async (bot: Bot) => {
    if (!confirm(
      `Жёстко убить процесс ${bot.symbol}?\n\n` +
      `Позиция и ордера на бирже останутся — управляй ими вручную или через kill-switch.`
    )) return;
    if (toggling) return;
    setToggling(bot.symbol);
    try {
      await killBot(bot.symbol);
      await new Promise(r => setTimeout(r, 1000));
      await load();
    } finally {
      setToggling(null);
    }
  };

  const handleSaveConfig = async (symbol: string, cfg: Record<string, unknown>) => {
    try {
      await updateConfig(symbol, cfg);
      await new Promise(r => setTimeout(r, 500));
      await load();
    } catch (e) {
      alert(`Save failed: ${e}`);
    }
  };

  /**
   * Перезапуск всех ботов окружения средствами самого API: stop-all, затем
   * ожидание фактической остановки и Start каждому. Раньше это делал внешний
   * скрипт restart_bots.sh через SSH — но дашборд и боты живут на одной VM,
   * так что достаточно обратиться к API напрямую. Контейнер api-server при
   * этом не перезапускается, поэтому открытые позиции переживают перезапуск:
   * боты восстанавливают их из state-файлов.
   */
  const restartAllBots = async () => {
    // Перезапускаем ТОЛЬКО реально запущенных ботов и НЕ трогаем arm.
    // Раньше сюда передавался список всех карточек, и кнопка в live поднимала
    // 35 намеренно остановленных ботов на реальные деньги, а перед стартом ещё
    // и армила каждого. Arm живёт в БД и переживает перезапуск сам.
    const before = await fetchBots();
    const symbols = Array.isArray(before) ? before.filter((b: Bot) => b.is_running).map((b: Bot) => b.symbol) : [];
    if (symbols.length === 0) {
      setNotice("Нет запущенных ботов — перезапускать нечего.");
      return { started: 0, failed: [] as string[] };
    }

    setNotice(`Перезапуск: останавливаю ${symbols.length} ботов...`);
    await stopAllBots();

    // Сигнал остановки не значит, что процесс уже мёртв: ждём, пока API
    // перестанет видеть их запущенными.
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setTimeout(r, 1000));
      const fresh = await fetchBots();
      if (!Array.isArray(fresh) || !fresh.some((b: Bot) => b.is_running)) break;
      if (i === 19) setNotice("Не все боты остановились — всё равно запускаю заново.");
    }

    let started = 0;
    const failed: string[] = [];
    for (let i = 0; i < symbols.length; i++) {
      const symbol = symbols[i];
      try {
        await startBot(symbol);
        started++;
      } catch {
        failed.push(symbol);
      }
      if (i % 5 === 4) setNotice(`Перезапуск: запущено ${i + 1}/${symbols.length}...`);
    }
    return { started, failed };
  };

  /** Перезапуск всех ЗАПУЩЕННЫХ ботов без изменения настроек (кнопка в шапке). */
  const handleRestartAll = async () => {
    const running = bots.filter(b => b.is_running);
    if (running.length === 0) {
      setNotice("Нет запущенных ботов — перезапускать нечего.");
      return;
    }
    const openCount = bots.filter(b => b.position).length;
    if (!confirm(
      `Перезапустить ${running.length} запущенных ботов?${IS_LIVE ? " (LIVE, реальные деньги)" : ""}\n\n` +
      `Остановленные боты запущены не будут. Настройки, включая Arm, не меняются.\n\n` +
      `Открытых позиций: ${openCount}. Они сохранятся — биржевые SL/TP остаются, ` +
      "а боты восстановят позиции из state-файлов. Примерно минуту позициями " +
      "не управляет никто."
    )) return;
    setRestarting(true);
    setNotice(null);
    try {
      const { started, failed } = await restartAllBots();
      setRestarting(false);
      setNotice(
        failed.length
          ? `Перезапуск: запущено ${started}/${running.length}. Ошибка: ${failed.join(", ")}`
          : `Перезапуск завершён: ${started}/${running.length} ботов работают.`
      );
      await load();
    } catch (e) {
      setRestarting(false);
      setNotice(`Ошибка перезапуска: ${String(e)}`);
    }
  };

  /**
   * Задать размер позиции (position_size_pct) сразу ВСЕМ ботам окружения.
   * Только для testnet: в live размер меняется по одному боту (LivePanel),
   * чтобы одним кликом не изменить объём сразу на реальных деньгах.
   *
   * Важно: боты читают конфиг при старте, поэтому уже запущенные боты новый
   * размер подхватят только после перезапуска. Поэтому рядом с Apply есть
   * переключатель «перезапустить после применения».
   */
  const handleApplySizeToAll = async () => {
    const raw = sizePctAll.trim().replace(",", ".");
    const pct = Number(raw);
    if (!raw || !Number.isFinite(pct) || pct <= 0 || pct > 100) {
      setNotice("Укажите корректный процент (например 0.4 или 1.5).");
      return;
    }
    const targets = bots.map(b => b.symbol);
    if (targets.length === 0) {
      setNotice("Нет ботов для обновления.");
      return;
    }
    const doRestart = restartAfterApply;
    if (!confirm(
      `Задать размер позиции ${pct}% депозита сразу ${targets.length} ботам?\n\n` +
      "Маржа = депозит × " + pct + "%, позиция = маржа × плечо.\n\n" +
      "Изменение запишется в БД и в config_*.yaml. Уже открытые позиции и их " +
      "reverse-цепочки продолжат дорабатываться на прежнем размере — новый " +
      "применится только к следующим входам." +
      (doRestart
        ? `\n\nЗапущенные боты будут перезапущены, чтобы подхватить новое значение; остановленные останутся остановленными (${targets.length} всего, перезапустятся только работающие).`
        : "\n\nБоты НЕ будут перезапущены: значение подхватится при следующем старте.")
    )) return;

    setApplyingSize(true);
    setNotice(null);
    const failed: string[] = [];
    // Небольшими параллельными волнами: каждый вызов поднимает python-процесс
    // для записи YAML, поэтому 40 запросов разом устроят мини-DDoS.
    const CONCURRENCY = 4;
    for (let i = 0; i < targets.length; i += CONCURRENCY) {
      const chunk = targets.slice(i, i + CONCURRENCY);
      await Promise.all(chunk.map(async (symbol) => {
        try {
          await updateConfig(symbol, { position_size_pct: pct });
        } catch {
          failed.push(symbol);
        }
      }));
      setNotice(`Применяю… ${Math.min(i + CONCURRENCY, targets.length)}/${targets.length}`);
    }

    let restartInfo = "";
    if (doRestart) {
      // restartAllBots сам перезапускает только запущенных, поэтому
      // «размер применён всем, перезапущены те, кто работал» — иначе Apply
      // в testnet поднял бы и намеренно остановленных ботов.
      const { started, failed: failedStart } = await restartAllBots();
      restartInfo = ` Перезапущено: ${started}.`;
      failed.push(...failedStart);
    }

    setApplyingSize(false);
    setSizePctAll("");
    setNotice(
      (failed.length
        ? `Готово с ошибками: обновлено ${targets.length - failed.length} из ${targets.length}. Проблемные: ${failed.join(", ")}.`
        : `Готово: размер позиции ${pct}% задан всем ${targets.length} ботам.`) + restartInfo
    );
    await load();
  };

  const handleCloseAllReset = async () => {
    if (!confirm(
      "Close ALL open positions (real + rejected), reset open-position counter and " +
      "loss-streak counter, and stop all bots?\n\n" +
      "Open positions will be marked closed (exit_reason=manual_reset) and bots will be stopped & reset."
    )) return;
    try {
      const r = await closeAllAndReset();
      alert(r.message || "All closed & reset");
      await load();
    } catch (e) {
      alert("Failed to close & reset: " + String(e));
    }
  };

  const handleDeleteBot = async (symbol: string) => {
    if (!confirm("Delete " + symbol + " permanently?")) return;
    try {
      await deleteBot(symbol);
      const freshBots = await fetchBots();
      setBots(freshBots);
    } catch {}
  };

  const handleExportCSV = () => {
    try {
      const headers = [
        "ID", "Symbol", "Direction", "Entry Price", "Exit Price",
        "Quantity", "PnL", "Exit Reason", "Entry Time", "Exit Time", "Mode"
      ];
      const rows = filteredTrades.map(t => [
        t.id, t.symbol, t.direction, t.entry_price,
        t.exit_price || "", t.qty, t.pnl || "",
        t.exit_reason || "", t.entry_time, t.exit_time || "", t.mode
      ]);
      const csv = [
        headers.join(","),
        ...rows.map(row => row.map(cell =>
          typeof cell === 'string' && cell.includes(',') ? `"${cell}"` : cell
        ).join(","))
      ].join("\n");
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      const suffix = selectedSymbol === "all" ? "all" : selectedSymbol;
      a.download = `trades-export-${suffix}-${new Date().toISOString().split('T')[0]}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);
    } catch (error) {
      alert('Failed to export trades. Please try again.');
    }
  };

  // Callback для переноса параметров из оптимизатора в бэктест
  const handleApplyToBacktest = (params: BacktestParams) => {
    const newConfig: BotConfig = {
      ...DEFAULT_CONFIG,
      ema_fast: params.ema_fast,
      ema_slow: params.ema_slow,
      sl_pct: params.sl_pct,
      tp1_pct: params.tp1_pct,
      tp2_pct: params.tp2_pct,
      volume_multiplier: params.volume_multiplier,
      tp1_close_pct: params.tp1_close_pct,
      risk_pct: params.risk_pct ?? DEFAULT_CONFIG.risk_pct,
      htf_enabled: params.htf_enabled ?? DEFAULT_CONFIG.htf_enabled,
      htf_ema_fast: params.htf_ema_fast ?? DEFAULT_CONFIG.htf_ema_fast,
      htf_ema_slow: params.htf_ema_slow ?? DEFAULT_CONFIG.htf_ema_slow,
    };
    setBtSymbol(params.symbol);
    setBtConfig(newConfig);
    setBtStartDate(params.start);
    setBtEndDate(params.end);
    setBtResetKey(k => k + 1);
  };

  const handleApplyBacktestToBot = async () => {
    const sym = btSymbol.toUpperCase();
    const htfStatus = btConfig.htf_enabled ? "ON" : "OFF";
    const params = [
      `ema_fast=${btConfig.ema_fast}`,
      `ema_slow=${btConfig.ema_slow}`,
      `sl_pct=${btConfig.sl_pct}`,
      `tp1_pct=${btConfig.tp1_pct}`,
      `tp2_pct=${btConfig.tp2_pct}`,
      `volume_multiplier=${btConfig.volume_multiplier}`,
      `tp1_close_pct=${btConfig.tp1_close_pct}`,
      `risk_pct=${btConfig.risk_pct}`,
      `htf=${htfStatus}`,
    ].join(", ");
    if (!confirm(`Apply current backtest parameters to ${sym} config?\n\n${params}`)) return;
    try {
      const res = await updateConfig(sym, {
        ema_fast: btConfig.ema_fast,
        ema_slow: btConfig.ema_slow,
        sl_pct: btConfig.sl_pct,
        tp1_pct: btConfig.tp1_pct,
        tp2_pct: btConfig.tp2_pct,
        volume_multiplier: btConfig.volume_multiplier,
        tp1_close_pct: btConfig.tp1_close_pct,
        risk_pct: btConfig.risk_pct,
        htf_enabled: btConfig.htf_enabled,
        htf_ema_fast: btConfig.htf_ema_fast,
        htf_ema_slow: btConfig.htf_ema_slow,
      });
      if (res.error) {
        alert("Error: " + res.error);
      } else {
        alert(`${sym} config updated. Use "Stop All & Reload" then restart the bot.`);
      }
    } catch (e) {
      alert("Failed to update config: " + String(e));
    }
  };

  const handleRunBacktest = async () => {
    setBtRunning(true);
    setBtError(null);
    setBtResult(null);
    try {
      const res = await runBacktest(btSymbol.toUpperCase(), {
        start: btStartDate,
        end: btEndDate,
        config: btConfig,
      });
      if (res.error) {
        setBtError(res.error);
      } else {
        setBtResult(res);
      }
    } catch (e) {
      setBtError("Failed to run backtest");
    } finally {
      setBtRunning(false);
    }
  };

  const updateBtConfig = (key: keyof BotConfig, value: number | string | boolean) => {
    setBtConfig({ ...btConfig, [key]: value });
  };

  return (
    <div className={IS_LIVE
      ? "min-h-screen bg-red-950 text-red-50 p-4 md:p-6"
      : "min-h-screen bg-zinc-950 text-white p-4 md:p-6"}>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
<div>
           <h1 className="text-2xl font-bold">
             Trading Bot Dashboard
             {IS_LIVE && (
               <span className="ml-2 rounded bg-red-600 px-2 py-0.5 align-middle text-sm">LIVE REAL MONEY</span>
             )}
           </h1>
            <p className="text-zinc-400 text-sm mt-0.5">
              Last updated: {lastRefresh.toLocaleTimeString("ru-RU")}
              <span className={`ml-2 inline-block w-2 h-2 rounded-full ${apiUp ? 'bg-green-400' : 'bg-red-500 animate-pulse'}`} title={apiUp ? "API connected" : "API disconnected"} />
            </p>
            {notice && (
              <p className="mt-1 rounded border border-red-800 bg-red-950/60 px-2 py-1 text-sm text-red-300">
                {notice}
              </p>
            )}

         </div>
        <div className="flex gap-2 items-start">
          <Button
            variant="outline"
            size="sm"
            onClick={handleRestartAll}
            disabled={restarting || applyingSize}
            title="Перезапустить процессы ботов, чтобы они подхватили новый код или конфиги. Позиции не закрываются — боты восстановят их из state-файлов."
            className="border-zinc-700 text-zinc-300 hover:bg-zinc-800"
          >
            <RefreshCw className={`w-4 h-4 mr-2 ${restarting ? 'animate-spin' : ''}`} />
            {restarting ? 'Restarting...' : 'Restart bots'}
          </Button>
          {!IS_LIVE && (
            <div className="flex items-center gap-1.5 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1">
              <label className="text-xs text-zinc-400 whitespace-nowrap" title="Маржа = % от депозита, позиция = маржа × плечо. Применяется только к НОВЫМ позициям: уже открытая позиция и её reverse-цепочка доторговываются на прежнем размере. Значение подхватят только перезапущенные боты.">
                Позиция, % · всем ботам
              </label>
              <input
                type="number"
                step="0.1"
                min="0"
                placeholder="0.4"
                value={sizePctAll}
                onChange={e => setSizePctAll(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter") void handleApplySizeToAll(); }}
                className="w-20 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-sm text-zinc-100 focus:outline-none focus:ring-1 focus:ring-zinc-500"
              />
              <Button
                size="sm"
                variant="outline"
                onClick={handleApplySizeToAll}
                disabled={applyingSize}
                className="border-zinc-700 text-zinc-200 hover:bg-zinc-800"
              >
                {applyingSize ? "Working..." : "Apply"}
              </Button>
              <label
                className="flex items-center gap-1 text-xs text-zinc-400 whitespace-nowrap cursor-pointer"
                title="После применения перезапустить ботов, чтобы они подхватили новое значение. Открытые позиции сохранятся: боты восстановят их из state-файлов."
              >
                <input
                  type="checkbox"
                  checked={restartAfterApply}
                  onChange={e => setRestartAfterApply(e.target.checked)}
                  disabled={applyingSize}
                  className="accent-zinc-400"
                />
                перезапустить
              </label>
            </div>
          )}
          <Button variant="outline" size="sm" onClick={handleSync} disabled={syncing} className="border-zinc-700 text-zinc-300 hover:bg-zinc-800">
            <RefreshCw className={`w-4 h-4 mr-2 ${syncing ? 'animate-spin' : ''}`} />
            {syncing ? 'Syncing...' : 'Sync Binance'}
          </Button>
          <Button variant="outline" size="sm" onClick={load} className="border-zinc-700 text-zinc-300 hover:bg-zinc-800">
            <RefreshCw className="w-4 h-4 mr-2" />Refresh
          </Button>
<Button variant="destructive" size="sm" onClick={async () => {
             if (!confirm(
               "Stop ALL running bots and reload their configs from YAML?\n\n" +
               "This will SIGKILL every bot process of this environment. Local " +
               "position state files are KEPT, so a later Start restores the " +
               "position with its SL/TP and reverse-chain. Open positions stay " +
               "protected by exchange orders (SL/TP), but won't be tracked by " +
               "the bot until you restart it."
             )) return;
             const r = await refreshBots();
             alert(r.message || "All bots stopped, configs reloaded");
             await load();
         }} className="">
           <RefreshCw className="w-4 h-4 mr-2" />Stop All & Reload Configs

          </Button>
          <Button variant="outline" size="sm" onClick={handleCloseAllReset} className="border-yellow-700 text-yellow-300 hover:bg-yellow-900">
            <Square className="w-4 h-4 mr-2" />Close All & Reset
          </Button>
          <Button variant="destructive" size="sm" onClick={async () => {
            if (confirm('Delete ALL trades and recovery chains? This cannot be undone.')) {
              const r = await clearTrades();
              const rc = await clearRecoveryChains();
              alert(`Deleted: ${r.deleted} trades, ${rc.deleted} recovery chains. Restart bots manually.`);
              window.location.reload();
            }
          }} className="border-red-700 text-red-300 hover:bg-red-900">
            � Clear DB
          </Button>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-64 text-zinc-400">
          <RefreshCw className="w-6 h-6 animate-spin mr-3" />Loading...
        </div>
      ) : (
        <div className="space-y-6">
          <StatsRow stats={stats} />

          <div>
            <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
              <Activity className="w-5 h-5 text-zinc-400" />Bots
            </h2>
            {bots.length === 0 ? (
              <Card className="border border-zinc-800 bg-zinc-900">
                <CardContent className="py-12 text-center text-zinc-500">
                  No bots configured. Add bots to the database to get started.
                </CardContent>
              </Card>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
                {bots.map((bot) => (
                  <BotCard key={bot.symbol} bot={bot} onToggle={() => handleToggle(bot)} onKill={() => handleKill(bot)} onSaveConfig={handleSaveConfig} isToggling={toggling === bot.symbol} onDelete={handleDeleteBot} />
                ))}
              </div>
            )}
          </div>

          <Tabs defaultValue="chart">
            {IS_LIVE && (
              <div className="mb-3 rounded-md border-2 border-red-600 bg-red-950/40 px-3 py-2 text-red-300">
                <span className="font-semibold">LIVE REAL MONEY</span>
                <span className="ml-2 text-xs">окружение live, API :5001 — интерфейс намеренно красный, чтобы не путать с тестнетом</span>
              </div>
            )}
            <TabsList className="bg-zinc-900 border border-zinc-800">
              <TabsTrigger value="chart" className="data-[state=active]:bg-zinc-700">
                <TrendingUp className="w-4 h-4 mr-1.5" />PnL Chart
              </TabsTrigger>
              <TabsTrigger value="trades" className="data-[state=active]:bg-zinc-700">
                <BarChart2 className="w-4 h-4 mr-1.5" />Trades
              </TabsTrigger>
              <TabsTrigger value="stats" className="data-[state=active]:bg-zinc-700">
                <Settings className="w-4 h-4 mr-1.5" />Stats
              </TabsTrigger>
              <TabsTrigger value="backtest" className="data-[state=active]:bg-zinc-700">
                <History className="w-4 h-4 mr-1.5" />Backtest
              </TabsTrigger>
              <TabsTrigger value="optimizer" className="data-[state=active]:bg-zinc-700">
                <TrendingDown className="w-4 h-4 mr-1.5" />Optimizer
              </TabsTrigger>
              <TabsTrigger value="recovery" className="data-[state=active]:bg-zinc-700">
                <Link2 className="w-4 h-4 mr-1.5" />Recovery
              </TabsTrigger>
              {IS_LIVE && (
                <TabsTrigger value="live" className="data-[state=active]:bg-red-800">
                  <AlertTriangle className="w-4 h-4 mr-1.5" />LIVE
                </TabsTrigger>
              )}
            </TabsList>

            <TabsContent value="chart" className="mt-4">
              <Card className="border border-zinc-800 bg-zinc-900">
                <CardHeader className="pb-2">
                  <CardTitle className="text-base text-zinc-300">Cumulative PnL (all symbols)</CardTitle>
                </CardHeader>
                <CardContent>
                  <PnlChart trades={trades} />
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="trades" className="mt-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <select
                  value={selectedSymbol}
                  onChange={e => setSelectedSymbol(e.target.value)}
                  className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none focus:ring-1 focus:ring-zinc-500"
                >
                  <option value="all">All symbols</option>
                  {symbols.map(s => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleExportCSV}
                  className="border-zinc-700 text-zinc-300 hover:bg-zinc-800"
                >
                  <Download className="w-4 h-4 mr-2" />
                  Export CSV
                </Button>
              </div>
              <TradesTable trades={filteredTrades} />
            </TabsContent>

            {IS_LIVE && (
              <TabsContent value="live" className="mt-4">
                <LivePanel bots={bots as any} onChanged={load} />
              </TabsContent>
            )}

            <TabsContent value="stats" className="mt-4">
              <StatsTable stats={stats} reversals={reverseCounts} />
            </TabsContent>

            <TabsContent value="backtest" className="mt-4">
              <div className="space-y-4" key={btResetKey} style={{ minHeight: '400px' }}>
                <Card className="border border-zinc-800 bg-zinc-900">
                  <CardHeader className="pb-2">
                    <CardTitle className="text-base text-zinc-300 flex items-center gap-2">
                      <BarChart2 className="w-4 h-4" />Backtest Configuration
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">Symbol</label>
                        <input
                          type="text"
                          value={btSymbol}
                          onChange={e => setBtSymbol(e.target.value.toUpperCase())}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none focus:ring-1 focus:ring-zinc-500"
                          placeholder="BTCUSDT"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">Timeframe</label>
                        <select
                          value={btConfig.timeframe}
                          onChange={e => updateBtConfig("timeframe", e.target.value)}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                        >
                          {TIMEFRAMES.map(tf => (
                            <option key={tf} value={tf}>{tf}</option>
                          ))}
                        </select>
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">Start Date</label>
                        <input
                          type="date"
                          value={btStartDate}
                          onChange={e => setBtStartDate(e.target.value)}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">End Date</label>
                        <input
                          type="date"
                          value={btEndDate}
                          onChange={e => setBtEndDate(e.target.value)}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">Leverage</label>
                        <input
                          type="number"
                          value={btConfig.leverage}
                          onChange={e => updateBtConfig("leverage", parseInt(e.target.value) || 1)}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                          min={1}
                          max={125}
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">Risk %</label>
                        <input
                          type="number"
                          step="0.1"
                          value={btConfig.risk_pct}
                          onChange={e => updateBtConfig("risk_pct", parseFloat(e.target.value) || 0)}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">SL %</label>
                        <input
                          type="number"
                          step="0.05"
                          value={btConfig.sl_pct}
                          onChange={e => updateBtConfig("sl_pct", parseFloat(e.target.value) || 0)}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">TP1 %</label>
                        <input
                          type="number"
                          step="0.05"
                          value={btConfig.tp1_pct}
                          onChange={e => updateBtConfig("tp1_pct", parseFloat(e.target.value) || 0)}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">TP2 %</label>
                        <input
                          type="number"
                          step="0.1"
                          value={btConfig.tp2_pct}
                          onChange={e => updateBtConfig("tp2_pct", parseFloat(e.target.value) || 0)}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">EMA Fast</label>
                        <input
                          type="number"
                          value={btConfig.ema_fast}
                          onChange={e => updateBtConfig("ema_fast", parseInt(e.target.value) || 1)}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">EMA Slow</label>
                        <input
                          type="number"
                          value={btConfig.ema_slow}
                          onChange={e => updateBtConfig("ema_slow", parseInt(e.target.value) || 1)}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">Volume Multiplier</label>
                        <input
                          type="number"
                          step="0.1"
                          value={btConfig.volume_multiplier}
                          onChange={e => updateBtConfig("volume_multiplier", parseFloat(e.target.value) || 1)}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                        />
                      </div>
                      <div className="flex items-end">
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={btConfig.htf_enabled}
                            onChange={e => updateBtConfig("htf_enabled", e.target.checked)}
                            className="rounded border-zinc-700 bg-zinc-800 text-green-500 focus:ring-green-500"
                          />
                          <span className="text-xs text-zinc-400">HTF Filter</span>
                        </label>
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">HTF EMA Fast</label>
                        <input
                          type="number"
                          value={btConfig.htf_ema_fast}
                          onChange={e => updateBtConfig("htf_ema_fast", parseInt(e.target.value) || 1)}
                          disabled={!btConfig.htf_enabled}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none disabled:opacity-50"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-400 mb-1 block">HTF EMA Slow</label>
                        <input
                          type="number"
                          value={btConfig.htf_ema_slow}
                          onChange={e => updateBtConfig("htf_ema_slow", parseInt(e.target.value) || 1)}
                          disabled={!btConfig.htf_enabled}
                          className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none disabled:opacity-50"
                        />
                      </div>
                    </div>

                    <div className="mt-4 flex items-center gap-4">
                      <Button
                        onClick={handleRunBacktest}
                        disabled={btRunning}
                        className="flex items-center gap-2"
                      >
                        {btRunning ? (
                          <><RefreshCw className="w-4 h-4 animate-spin" />Running...</>
                        ) : (
                          <><Play className="w-4 h-4" />Run Backtest</>
                        )}
                      </Button>
                      <Button
                        onClick={handleApplyBacktestToBot}
                        variant="outline"
                        className="border-zinc-700 text-zinc-300 hover:bg-zinc-800 flex items-center gap-2"
                        title="Save current parameters to bot config"
                      >
                        <Settings className="w-4 h-4" />Load to Config
                      </Button>
                      {btError && (
                        <span className="text-red-400 text-sm">{btError}</span>
                      )}
                    </div>
                  </CardContent>
                </Card>

                {btResult && (
                  <>
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                      <Card className="border border-zinc-800 bg-zinc-900">
                        <CardContent className="pt-4 pb-3">
                          <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
                            <BarChart2 className="w-4 h-4" />Total Trades
                          </div>
                          <div className="text-2xl font-bold font-mono text-white">{btResult.total_trades}</div>
                        </CardContent>
                      </Card>
                      <Card className="border border-zinc-800 bg-zinc-900">
                        <CardContent className="pt-4 pb-3">
                          <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
                            <TrendingUp className="w-4 h-4 text-green-400" />Win Rate
                          </div>
                          <div className="text-2xl font-bold font-mono text-green-400">{btResult.win_rate}%</div>
                          <div className="text-xs text-zinc-500 mt-1">{btResult.wins}W / {btResult.losses}L</div>
                        </CardContent>
                      </Card>
                      <Card className="border border-zinc-800 bg-zinc-900">
                        <CardContent className="pt-4 pb-3">
                          <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
                            <DollarSign className="w-4 h-4" />Total PnL
                          </div>
                          <div className={`text-2xl font-bold font-mono ${btResult.total_pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                            {btResult.total_pnl >= 0 ? "+" : ""}{btResult.total_pnl.toFixed(4)}
                          </div>
                        </CardContent>
                      </Card>
                      <Card className="border border-zinc-800 bg-zinc-900">
                        <CardContent className="pt-4 pb-3">
                          <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
                            <TrendingDown className="w-4 h-4 text-red-400" />Max Drawdown
                          </div>
                          <div className="text-2xl font-bold font-mono text-red-400">{btResult.max_drawdown}%</div>
                        </CardContent>
                      </Card>
                    </div>

                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                      <Card className="border border-zinc-800 bg-zinc-900">
                        <CardContent className="pt-4 pb-3">
                          <div className="text-xs text-zinc-400 mb-1">Initial Balance</div>
                          <div className="text-lg font-bold font-mono text-white">${btResult.initial_balance.toFixed(2)}</div>
                        </CardContent>
                      </Card>
                      <Card className="border border-zinc-800 bg-zinc-900">
                        <CardContent className="pt-4 pb-3">
                          <div className="text-xs text-zinc-400 mb-1">Final Balance</div>
                          <div className={`text-lg font-bold font-mono ${btResult.final_balance >= btResult.initial_balance ? "text-green-400" : "text-red-400"}`}>
                            ${btResult.final_balance.toFixed(2)}
                          </div>
                        </CardContent>
                      </Card>
                      <Card className="border border-zinc-800 bg-zinc-900">
                        <CardContent className="pt-4 pb-3">
                          <div className="text-xs text-zinc-400 mb-1">Return</div>
                          <div className={`text-lg font-bold font-mono ${btResult.return_pct >= 0 ? "text-green-400" : "text-red-400"}`}>
                            {btResult.return_pct >= 0 ? "+" : ""}{btResult.return_pct}%
                          </div>
                        </CardContent>
                      </Card>
                      <Card className="border border-zinc-800 bg-zinc-900">
                        <CardContent className="pt-4 pb-3">
                          <div className="text-xs text-zinc-400 mb-1">Avg Win / Loss</div>
                          <div className="text-lg font-bold font-mono">
                            <span className="text-green-400">+{btResult.avg_win.toFixed(4)}</span>
                            {" / "}
                            <span className="text-red-400">{btResult.avg_loss.toFixed(4)}</span>
                          </div>
                        </CardContent>
                      </Card>
                    </div>

                    <div className="mt-4 flex justify-start">
                      <Button
                        onClick={handleApplyBacktestToBot}
                        variant="outline"
                        className="border-zinc-700 text-zinc-300 hover:bg-zinc-800 flex items-center gap-2"
                        title="Save these parameters to bot config"
                      >
                        <Settings className="w-4 h-4" />Load to Config
                      </Button>
                    </div>
                  </>
                )}
              </div>
            </TabsContent>

            <TabsContent value="optimizer" className="mt-4">
              <OptimizerTab
                jobId={optJobId}
                job={optJob}
                setJobId={setOptJobId}
                setJob={setOptJob}
                onApplyToBacktest={handleApplyToBacktest}
                symbol={optSymbol}
                setSymbol={setOptSymbol}
                start={optStart}
                setStart={setOptStart}
                end={optEnd}
                setEnd={setOptEnd}
                trials={optTrials}
                setTrials={setOptTrials}
                jobs={optJobs}
                setJobs={setOptJobs}
              />
            </TabsContent>

            <TabsContent value="recovery" className="mt-4">
              <RecoveryTab />
            </TabsContent>
          </Tabs>
        </div>
      )}
    </div>
  );
}

