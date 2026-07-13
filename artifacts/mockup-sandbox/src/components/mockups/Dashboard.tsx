import { useState, useEffect, useCallback } from "react";
import { fetchBots, fetchTrades, fetchStats, startBot, stopBot } from "../../hooks/useApi";
import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../ui/tabs";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "../ui/table";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import {
  Activity, TrendingUp, TrendingDown, DollarSign, BarChart2,
  Play, Square, RefreshCw, Settings,
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

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmt(n: number | null | undefined, decimals = 2) {
  if (n == null) return "—";
  return n.toFixed(decimals);
}

function fmtTime(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("ru-RU", {
    month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

function heartbeatAge(ts: string | null): string {
  if (!ts) return "never";
  const sec = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
}

// ── Bot Card ─────────────────────────────────────────────────────────────────

function BotCard({ bot, onToggle }: { bot: Bot; onToggle: () => void }) {
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
          className="h-7 px-3"
        >
          {bot.is_running ? <><Square className="w-3 h-3 mr-1" />Stop</> : <><Play className="w-3 h-3 mr-1" />Start</>}
        </Button>
      </CardHeader>

      <CardContent className="space-y-3">
        {/* Price & heartbeat */}
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

        {/* Config pills */}
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

        {/* Open position */}
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
            {["Symbol", "Dir", "Entry", "Exit", "Qty", "PnL", "Reason", "Open", "Close"].map((h) => (
              <TableHead key={h} className="text-zinc-400 text-xs">{h}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {trades.length === 0 && (
            <TableRow><TableCell colSpan={9} className="text-center text-zinc-500 py-8">No trades</TableCell></TableRow>
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

export default function Dashboard() {
  const [bots, setBots] = useState<Bot[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [stats, setStats] = useState<Stats[]>([]);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<Date>(new Date());

  const load = useCallback(async () => {
    try {
      const [b, t, s] = await Promise.all([fetchBots(), fetchTrades(undefined, 100), fetchStats()]);
      setBots(Array.isArray(b) ? b : []);
      setTrades(Array.isArray(t?.trades) ? t.trades : []);
      setStats(Array.isArray(s) ? s : []);
      setLastRefresh(new Date());
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

  const handleToggle = async (bot: Bot) => {
    if (bot.is_running) await stopBot(bot.symbol);
    else await startBot(bot.symbol);
    await load();
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
        <Button variant="outline" size="sm" onClick={load} className="border-zinc-700 text-zinc-300 hover:bg-zinc-800">
          <RefreshCw className="w-4 h-4 mr-2" />Refresh
        </Button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-64 text-zinc-400">
          <RefreshCw className="w-6 h-6 animate-spin mr-3" />Loading...
        </div>
      ) : (
        <div className="space-y-6">
          {/* Summary stats */}
          <StatsRow stats={stats} />

          {/* Bot cards */}
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
                  <BotCard key={bot.symbol} bot={bot} onToggle={() => handleToggle(bot)} />
                ))}
              </div>
            )}
          </div>

          {/* Tabs: chart / trades / stats */}
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
              <TradesTable trades={trades} />
            </TabsContent>

            <TabsContent value="stats" className="mt-4">
              <StatsTable stats={stats} />
            </TabsContent>
          </Tabs>
        </div>
      )}
    </div>
  );
}
