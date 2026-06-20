import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "./components/ui/card";
import { Button } from "./components/ui/button";
import { Badge } from "./components/ui/badge";
import { Play, RefreshCw, TrendingUp, TrendingDown, DollarSign, BarChart2 } from "lucide-react";
import { runBacktest } from "./hooks/useApi";

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

export default function BacktestTab() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [startDate, setStartDate] = useState("2024-01-01");
  const [endDate, setEndDate] = useState("2024-04-01");
  const [config, setConfig] = useState<BotConfig>(DEFAULT_CONFIG);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleRun = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const res = await runBacktest(symbol.toUpperCase(), {
        start: startDate,
        end: endDate,
        config,
      });
      if (res.error) {
        setError(res.error);
      } else {
        setResult(res);
      }
    } catch (e) {
      setError("Failed to run backtest");
    } finally {
      setRunning(false);
    }
  };

  const updateConfig = (key: keyof BotConfig, value: number | string | boolean) => {
    setConfig(prev => ({ ...prev, [key]: value }));
  };

  return (
    <div className="space-y-4">
      {/* Config */}
      <Card className="border border-zinc-800 bg-zinc-900">
        <CardHeader className="pb-2">
          <CardTitle className="text-base text-zinc-300 flex items-center gap-2">
            <BarChart2 className="w-4 h-4" />Backtest Configuration
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {/* Symbol */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">Symbol</label>
              <input
                type="text"
                value={symbol}
                onChange={e => setSymbol(e.target.value.toUpperCase())}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none focus:ring-1 focus:ring-zinc-500"
                placeholder="BTCUSDT"
              />
            </div>

            {/* Timeframe */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">Timeframe</label>
              <select
                value={config.timeframe}
                onChange={e => updateConfig("timeframe", e.target.value)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
              >
                {TIMEFRAMES.map(tf => (
                  <option key={tf} value={tf}>{tf}</option>
                ))}
              </select>
            </div>

            {/* Start Date */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">Start Date</label>
              <input
                type="date"
                value={startDate}
                onChange={e => setStartDate(e.target.value)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
              />
            </div>

            {/* End Date */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">End Date</label>
              <input
                type="date"
                value={endDate}
                onChange={e => setEndDate(e.target.value)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
              />
            </div>

            {/* Leverage */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">Leverage</label>
              <input
                type="number"
                value={config.leverage}
                onChange={e => updateConfig("leverage", parseInt(e.target.value) || 1)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
                min={1}
                max={125}
              />
            </div>

            {/* Risk % */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">Risk %</label>
              <input
                type="number"
                step="0.1"
                value={config.risk_pct}
                onChange={e => updateConfig("risk_pct", parseFloat(e.target.value) || 0)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
              />
            </div>

            {/* SL % */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">SL %</label>
              <input
                type="number"
                step="0.05"
                value={config.sl_pct}
                onChange={e => updateConfig("sl_pct", parseFloat(e.target.value) || 0)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
              />
            </div>

            {/* TP1 % */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">TP1 %</label>
              <input
                type="number"
                step="0.05"
                value={config.tp1_pct}
                onChange={e => updateConfig("tp1_pct", parseFloat(e.target.value) || 0)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
              />
            </div>

            {/* TP2 % */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">TP2 %</label>
              <input
                type="number"
                step="0.1"
                value={config.tp2_pct}
                onChange={e => updateConfig("tp2_pct", parseFloat(e.target.value) || 0)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
              />
            </div>

            {/* EMA Fast */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">EMA Fast</label>
              <input
                type="number"
                value={config.ema_fast}
                onChange={e => updateConfig("ema_fast", parseInt(e.target.value) || 1)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
              />
            </div>

            {/* EMA Slow */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">EMA Slow</label>
              <input
                type="number"
                value={config.ema_slow}
                onChange={e => updateConfig("ema_slow", parseInt(e.target.value) || 1)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
              />
            </div>

            {/* Volume Multiplier */}
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">Volume Multiplier</label>
              <input
                type="number"
                step="0.1"
                value={config.volume_multiplier}
                onChange={e => updateConfig("volume_multiplier", parseFloat(e.target.value) || 1)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none"
              />
            </div>
          </div>

          <div className="mt-4 flex items-center gap-4">
            <Button
              onClick={handleRun}
              disabled={running}
              className="flex items-center gap-2"
            >
              {running ? (
                <><RefreshCw className="w-4 h-4 animate-spin" />Running...</>
              ) : (
                <><Play className="w-4 h-4" />Run Backtest</>
              )}
            </Button>

            {error && (
              <span className="text-red-400 text-sm">{error}</span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Results */}
      {result && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Card className="border border-zinc-800 bg-zinc-900">
              <CardContent className="pt-4 pb-3">
                <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
                  <BarChart2 className="w-4 h-4" />Total Trades
                </div>
                <div className="text-2xl font-bold font-mono text-white">{result.total_trades}</div>
              </CardContent>
            </Card>
            <Card className="border border-zinc-800 bg-zinc-900">
              <CardContent className="pt-4 pb-3">
                <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
                  <TrendingUp className="w-4 h-4 text-green-400" />Win Rate
                </div>
                <div className="text-2xl font-bold font-mono text-green-400">{result.win_rate}%</div>
                <div className="text-xs text-zinc-500 mt-1">{result.wins}W / {result.losses}L</div>
              </CardContent>
            </Card>
            <Card className="border border-zinc-800 bg-zinc-900">
              <CardContent className="pt-4 pb-3">
                <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
                  <DollarSign className="w-4 h-4" />Total PnL
                </div>
                <div className={`text-2xl font-bold font-mono ${result.total_pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {result.total_pnl >= 0 ? "+" : ""}{result.total_pnl.toFixed(4)}
                </div>
              </CardContent>
            </Card>
            <Card className="border border-zinc-800 bg-zinc-900">
              <CardContent className="pt-4 pb-3">
                <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
                  <TrendingDown className="w-4 h-4 text-red-400" />Max Drawdown
                </div>
                <div className="text-2xl font-bold font-mono text-red-400">{result.max_drawdown}%</div>
              </CardContent>
            </Card>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Card className="border border-zinc-800 bg-zinc-900">
              <CardContent className="pt-4 pb-3">
                <div className="text-xs text-zinc-400 mb-1">Initial Balance</div>
                <div className="text-lg font-bold font-mono text-white">${result.initial_balance.toFixed(2)}</div>
              </CardContent>
            </Card>
            <Card className="border border-zinc-800 bg-zinc-900">
              <CardContent className="pt-4 pb-3">
                <div className="text-xs text-zinc-400 mb-1">Final Balance</div>
                <div className={`text-lg font-bold font-mono ${result.final_balance >= result.initial_balance ? "text-green-400" : "text-red-400"}`}>
                  ${result.final_balance.toFixed(2)}
                </div>
              </CardContent>
            </Card>
            <Card className="border border-zinc-800 bg-zinc-900">
              <CardContent className="pt-4 pb-3">
                <div className="text-xs text-zinc-400 mb-1">Return</div>
                <div className={`text-lg font-bold font-mono ${result.return_pct >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {result.return_pct >= 0 ? "+" : ""}{result.return_pct}%
                </div>
              </CardContent>
            </Card>
            <Card className="border border-zinc-800 bg-zinc-900">
              <CardContent className="pt-4 pb-3">
                <div className="text-xs text-zinc-400 mb-1">Avg Win / Loss</div>
                <div className="text-lg font-bold font-mono">
                  <span className="text-green-400">+{result.avg_win.toFixed(4)}</span>
                  {" / "}
                  <span className="text-red-400">{result.avg_loss.toFixed(4)}</span>
                </div>
              </CardContent>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
