import { useState, useEffect, useCallback } from "react";
import OptimizerTab from "./OptimizerTab";
import RecoveryTab from "./RecoveryTab";
import { fetchBots, fetchTrades, fetchStats, startBot, stopBot, syncBinance, runBacktest, clearTrades, syncClosedTrades } from "./hooks/useApi";
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
  Activity, TrendingUp, TrendingDown, DollarSign, BarChart2,
  Play, Square, RefreshCw, Settings, Link2, Download, History,
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

// ── Bot Card ─────────────────────────────────────────────────────────────────

function BotCard({ bot, onToggle, isToggling }: { bot: Bot; onToggle: () => void; isToggling: boolean }) {
  const pos = bot.position;
  const isLong = pos?.direction === "LONG";
  const unrealizedPnl = pos && bot.current_price
    ? isLong
      ? (bot.current_price - pos.entry_price) * pos.remaining_qty
      : (pos.entry_price - bot.current_price) * pos.remaining_qty
    : null;

  return (
    <Card className="border border-zinc-800 bg-zinc-900 text-white">
      <CardHeader className="pb-2 flex flex-row items-center justify-between">
        <div className="flex items-center gap-2">
          <CardTitle className="text-lg font-bold">{bot.symbol}</CardTitle>
          <Badge variant={bot.mode === "live" ? "destructive" : "secondary"} className="text-xs">
            {bot.mode.toUpperCase()}
          </Badge>
          <Badge variant={bot.is_running ? "default" : "outline"} className="text-xs">
            {bot.is_running ? "● RUNNING" : "○ STOPPED"}
          </Badge>
        </div>
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
      </CardHeader>

      <CardContent className="space-y-3">
        <div className="flex justify-between text-sm">
          <span className="text-zinc-400">Price</span>
          <span className="font-mono font-semibold">
            {bot.current_price ? `$${bot.current_price.toLocaleString()}` : "—"}
          </span>
        </div>
        <div className="flex justify-between text-sm">
          <span className="text-zinc-400">Heartbeat</span>
          <span className="text-zinc-300">{heartbeatAge(bot.last_heartbeat)}</span>
        </div>

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
    .filter((t) => !t.is_open && t.pnl != null)
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

function TradesTable({ trades }: { trades: Trade[] }) {
  return (
    <div className="overflow-auto rounded-lg border border-zinc-800">
      <Table>
        <TableHeader>
          <TableRow className="border-zinc-800 hover:bg-transparent">
            {["Symbol", "Dir", "Entry", "Exit", "Qty", "PnL", "Reason", "Mode", "Open", "Close"].map((h) => (
              <TableHead key={h} className="text-zinc-400 text-xs">{h}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {trades.length === 0 && (
            <TableRow><TableCell colSpan={10} className="text-center text-zinc-500 py-8">No trades</TableCell></TableRow>
          )}
          {trades.map((t) => (
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
              <TableCell className={`font-mono text-sm font-semibold ${t.pnl == null ? "" : t.pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                {t.pnl == null ? "—" : `${t.pnl >= 0 ? "+" : ""}${fmt(t.pnl, 4)}`}
              </TableCell>
              <TableCell>
                {t.exit_reason && (
                  <Badge variant={t.exit_reason.startsWith("TP") ? "default" : "destructive"} className="text-xs">
                    {t.exit_reason}
                  </Badge>
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
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

// ── Per-symbol Stats Table ────────────────────────────────────────────────────

function StatsTable({ stats }: { stats: Stats[] }) {
  return (
    <div className="overflow-auto rounded-lg border border-zinc-800">
      <Table>
        <TableHeader>
          <TableRow className="border-zinc-800 hover:bg-transparent">
            {["Symbol", "Trades", "Wins", "Losses", "Win Rate", "Total PnL", "Avg Win", "Avg Loss"].map((h) => (
              <TableHead key={h} className="text-zinc-400 text-xs">{h}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {stats.map((s) => (
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
            </TableRow>
          ))}
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
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<Date>(new Date());
  const [optJobId, setOptJobId] = useState<string | null>(null);
  const [optJob, setOptJob] = useState<any | null>(null);
  const [optSymbol, setOptSymbol] = useState("BTCUSDT");
  const [optStart, setOptStart] = useState("2026-05-01");
  const [optEnd, setOptEnd] = useState("2026-06-13");
  // Фильтр по торговой паре
  const [selectedSymbol, setSelectedSymbol] = useState<string>("all");
  const symbols = [...new Set(trades.map(t => t.symbol))].sort();
  const filteredTrades = selectedSymbol === "all" ? trades : trades.filter(t => t.symbol === selectedSymbol);
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
      const [b, t, s] = await Promise.all([fetchBots(), fetchTrades(undefined, 100), fetchStats()]);
      setBots(Array.isArray(b) ? b : []);
      setTrades(Array.isArray(t?.trades) ? t.trades : []);
      setStats(Array.isArray(s) ? s : []);
      setLastRefresh(new Date());
      // Синхронизация закрытых позиций
      syncClosedTrades().catch(() => {});
    } catch {
      // API not available yet
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, [load]);

  const [toggling, setToggling] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);

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
      if (bot.is_running) {
        await stopBot(bot.symbol);
      } else {
        await startBot(bot.symbol);
      }
      await new Promise(r => setTimeout(r, 1000));
      await load();
    } finally {
      setToggling(null);
    }
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
    };
    setBtSymbol(params.symbol);
    setBtConfig(newConfig);
    setBtStartDate(params.start);
    setBtEndDate(params.end);
    setBtResetKey(k => k + 1);
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
    <div className="min-h-screen bg-zinc-950 text-white p-4 md:p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">Trading Bot Dashboard</h1>
          <p className="text-zinc-400 text-sm mt-0.5">
            Last updated: {lastRefresh.toLocaleTimeString("ru-RU")}
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={handleSync} disabled={syncing} className="border-zinc-700 text-zinc-300 hover:bg-zinc-800">
            <RefreshCw className={`w-4 h-4 mr-2 ${syncing ? 'animate-spin' : ''}`} />
            {syncing ? 'Syncing...' : 'Sync Binance'}
          </Button>
          <Button variant="outline" size="sm" onClick={load} className="border-zinc-700 text-zinc-300 hover:bg-zinc-800">
            <RefreshCw className="w-4 h-4 mr-2" />Refresh
          </Button>
          <Button variant="destructive" size="sm" onClick={async () => {
            if (confirm('Delete all trades and restart bots?')) {
              const r = await clearTrades();
              // Перезапускаем ботов
              const bots = await fetchBots();
              for (const bot of bots) {
                if (bot.is_running) {
                  await stopBot(bot.symbol);
                  await startBot(bot.symbol);
                }
              }
              alert(`Deleted: ${r.deleted} trades. Bots restarted. Page will reload...`);
              window.location.reload();
            }
          }} className="border-red-700 text-red-300 hover:bg-red-900">
            🗑 Clear DB
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
                  <BotCard key={bot.symbol} bot={bot} onToggle={() => handleToggle(bot)} isToggling={toggling === bot.symbol} />
                ))}
              </div>
            )}
          </div>

          <Tabs defaultValue="chart">
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

            <TabsContent value="stats" className="mt-4">
              <StatsTable stats={stats} />
            </TabsContent>

            <TabsContent value="backtest" className="mt-4">
              <div className="space-y-4" key={btResetKey}>
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
