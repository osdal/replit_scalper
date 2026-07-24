import { useState, useEffect, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "./components/ui/card";
import { Button } from "./components/ui/button";
import { Badge } from "./components/ui/badge";
import { Play, Square, TrendingUp, ArrowRight } from "lucide-react";
import { fetchBots } from "./hooks/useApi";

const API = import.meta.env.VITE_API_URL || "http://localhost:5000/api";

interface BotConfig {
  symbol: string;
}

interface OptResult {
  rank: string;
  score: string;
  trades: string;
  winRate: string;
  pnl: string;
  dd: string;
  symbol: string;
  ema_fast: string;
  ema_slow: string;
  sl_pct: string;
  tp1_pct: string;
  tp2_pct: string;
  volume_multiplier: string;
  tp1_close_pct: string;
  risk_pct: string;
  htf_ema_fast?: string;
  htf_ema_slow?: string;
  params?: Record<string, string>;
}

interface JobStatus {
  jobId: string;
  status: "running" | "done" | "error";
  progress: number;
  current: number;
  total: number;
  best: number;
  output: string[];
  results: OptResult[] | null;
}

interface OptimizerTabProps {
  jobId: string | null;
  job: JobStatus | null;
  setJobId: (id: string | null) => void;
  setJob: (job: JobStatus | null) => void;
  onApplyToBacktest?: (params: {
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
  }) => void;
  symbol: string;
  setSymbol: (s: string) => void;
  start: string;
  setStart: (s: string) => void;
  end: string;
  setEnd: (s: string) => void;
  trials: number;
  setTrials: (n: number) => void;
  jobs: number;
  setJobs: (n: number) => void;
}

export default function OptimizerTab({ jobId, job, setJobId, setJob, onApplyToBacktest, symbol, setSymbol, start, setStart, end, setEnd, trials, setTrials, jobs, setJobs }: OptimizerTabProps) {
  const [symbols, setSymbols] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const outputRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchBots().then(bots => {
      const syms = bots.map((b: BotConfig) => b.symbol).sort();
      setSymbols(syms);
      if (syms.length > 0 && !syms.includes(symbol)) {
        setSymbol(syms[0]);
      }
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!jobId) return;
    const id = setInterval(async () => {
      try {
        const r = await fetch(`${API}/optimizer/jobs/${jobId}`);
        const data = await r.json();
        setJob(data);
        if (data.status !== "running") clearInterval(id);
      } catch {}
    }, 2000);
    return () => clearInterval(id);
  }, [jobId]);

  useEffect(() => {
    if (outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [job?.output]);

  const handleStart = async () => {
    if (!symbol) {
      alert("Please select a symbol");
      return;
    }
    setLoading(true);
    try {
      const r = await fetch(`${API}/optimizer/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol, start, end, trials, jobs }),
      });
      const data = await r.json();
      if (data.error) {
        alert(data.error);
        return;
      }
      setJobId(data.jobId);
      setJob(null);
    } catch (e) {
      alert("Failed to start optimizer: " + String(e));
    } finally {
      setLoading(false);
    }
  };

  const handleStop = async () => {
    if (!jobId) return;
    await fetch(`${API}/optimizer/jobs/${jobId}`, { method: "DELETE" });
    setJobId(null);
    setJob(null);
  };

  const getParam = (result: OptResult, key: string): number => {
    const p = result.params || result;
    return parseFloat((p as any)[key] || "0");
  };

  const handleApplyToBacktest = (result: OptResult) => {
    if (!onApplyToBacktest) return;
    const htfFast = getParam(result, "htf_ema_fast");
    const htfSlow = getParam(result, "htf_ema_slow");
    const htfEnabled = htfFast > 0 && htfSlow > 0;
    onApplyToBacktest({
      symbol: result.symbol || symbol,
      ema_fast: getParam(result, "ema_fast"),
      ema_slow: getParam(result, "ema_slow"),
      sl_pct: getParam(result, "sl_pct"),
      tp1_pct: getParam(result, "tp1_pct"),
      tp2_pct: getParam(result, "tp2_pct"),
      volume_multiplier: getParam(result, "volume_multiplier"),
      tp1_close_pct: getParam(result, "tp1_close_pct"),
      risk_pct: getParam(result, "risk_pct"),
      htf_enabled: htfEnabled,
      htf_ema_fast: htfFast,
      htf_ema_slow: htfSlow,
      start,
      end,
    });
  };

  const handleApplyToBot = async (result: OptResult) => {
    const sym = result.symbol || symbol;
    const ema_f = getParam(result, "ema_fast");
    const ema_s = getParam(result, "ema_slow");
    const sl = getParam(result, "sl_pct");
    const tp1 = getParam(result, "tp1_pct");
    const tp2 = getParam(result, "tp2_pct");
    const vol = getParam(result, "volume_multiplier");
    const tp1c = getParam(result, "tp1_close_pct");
    const risk = getParam(result, "risk_pct");
    if (!confirm(`Apply to ${sym}?\nEMA ${ema_f}/${ema_s} | SL ${sl}% | TP1 ${tp1}% | TP2 ${tp2}% | Vol×${vol} | Risk ${risk}%`)) return;
    try {
      const body: Record<string, number> = { ema_fast: ema_f, ema_slow: ema_s, sl_pct: sl, tp1_pct: tp1, tp2_pct: tp2, volume_multiplier: vol, tp1_close_pct: tp1c, risk_pct: risk };
      const htf_f = getParam(result, "htf_ema_fast");
      const htf_s = getParam(result, "htf_ema_slow");
      if (htf_f > 0) body.htf_ema_fast = htf_f;
      if (htf_s > 0) body.htf_ema_slow = htf_s;
      const res = await fetch(`${API}/bots/${sym}/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.error) {
        alert("Error: " + data.error);
      } else {
        alert(`${sym} config updated. Use "Stop All & Reload" then restart the bot.`);
      }
    } catch (e) {
      alert("Failed to update config: " + String(e));
    }
  };

  const isRunning = job?.status === "running";

  return (
    <div className="space-y-4">
      <Card className="border border-zinc-800 bg-zinc-900">
        <CardHeader className="pb-2">
          <CardTitle className="text-base text-zinc-300 flex items-center gap-2">
            <TrendingUp className="w-4 h-4" />Parameter Optimizer
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mb-4">
            <div>
              <label className="text-zinc-400 text-xs mb-1 block">Symbol</label>
              <select
                value={symbol}
                onChange={e => setSymbol(e.target.value)}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm text-white"
              >
                {symbols.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            <div>
              <label className="text-zinc-400 text-xs mb-1 block">Start</label>
              <input
                type="date" value={start}
                onChange={e => setStart(e.target.value)}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm text-white"
              />
            </div>
            <div>
              <label className="text-zinc-400 text-xs mb-1 block">End</label>
              <input
                type="date" value={end}
                onChange={e => setEnd(e.target.value)}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm text-white"
              />
            </div>
            <div>
              <label className="text-zinc-400 text-xs mb-1 block">Trials</label>
              <input
                type="number" value={trials} min={10} max={2000}
                onChange={e => setTrials(parseInt(e.target.value))}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm text-white"
              />
            </div>
            <div>
              <label className="text-zinc-400 text-xs mb-1 block">Parallel Jobs</label>
              <input
                type="number" value={jobs} min={1} max={16}
                onChange={e => setJobs(parseInt(e.target.value))}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm text-white"
              />
            </div>
            <div className="flex items-end">
              {!isRunning ? (
                <Button onClick={handleStart} disabled={loading} className="w-full">
                  <Play className="w-4 h-4 mr-2" />
                  {loading ? "Starting..." : "Run"}
                </Button>
              ) : (
                <Button onClick={handleStop} variant="destructive" className="w-full">
                  <Square className="w-4 h-4 mr-2" />Stop
                </Button>
              )}
            </div>
          </div>

          {job && (
            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <span className="text-zinc-400">
                  {job.status === "running" ? `Trial ${job.current}/${job.total}` : job.status.toUpperCase()}
                </span>
                <div className="flex items-center gap-2">
                  {job.best > 0 && <span className="text-green-400 font-mono">best={job.best.toFixed(4)}</span>}
                  <Badge variant={job.status === "done" ? "default" : job.status === "error" ? "destructive" : "secondary"}>
                    {job.status}
                  </Badge>
                </div>
              </div>
              <div className="w-full bg-zinc-800 rounded-full h-2">
                <div
                  className="bg-green-500 h-2 rounded-full transition-all duration-500"
                  style={{ width: `${job.progress}%` }}
                />
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {job && job.output.length > 0 && (
        <Card className="border border-zinc-800 bg-zinc-900">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-zinc-400">Output</CardTitle>
          </CardHeader>
          <CardContent>
            <div
              ref={outputRef}
              className="bg-zinc-950 rounded p-3 h-48 overflow-auto font-mono text-xs text-zinc-300 whitespace-pre"
            >
              {job.output.join("\n")}
            </div>
          </CardContent>
        </Card>
      )}

      {job?.results && Array.isArray(job.results) && job.results.length > 0 && (
        <Card className="border border-zinc-800 bg-zinc-900">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-zinc-300">Top Results</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="overflow-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-zinc-400 text-xs border-b border-zinc-800">
                    {["","Rank","Score","Trades","WR%","PnL","DD%","EMA F","EMA S","SL%","TP1%","TP2%","Vol×","TP1cl%","Risk%","HTF F","HTF S"].map(h => (
                      <th key={h} className="text-left pb-2 pr-3">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {job.results.map((r: any, i: number) => {
                    const p = r.params || r;
                    const isBest = i === 0;
                    return (
                      <tr key={i} className={`border-b border-zinc-800/50 ${isBest ? "text-green-400 font-semibold" : "text-zinc-300"}`}>
                        <td className="py-1.5 pr-3 flex gap-1">
                          {onApplyToBacktest && (
                            <button
                              onClick={() => handleApplyToBacktest(r)}
                              className="p-1 rounded hover:bg-zinc-700 transition-colors"
                              title="Apply to Backtest"
                            >
                              <ArrowRight className="w-3.5 h-3.5" />
                            </button>
                          )}
                          <button
                            onClick={() => handleApplyToBot(r)}
                            className="p-1 rounded hover:bg-green-900 transition-colors text-green-400"
                            title="Apply to Bot (save config)"
                          >
                            ✓
                          </button>
                        </td>
                        <td className="py-1.5 pr-3">{r.rank}</td>
                        <td className="py-1.5 pr-3 font-mono">{r.score}</td>
                        <td className="py-1.5 pr-3">{r.trades ?? "-"}</td>
                        <td className="py-1.5 pr-3">{r.winRate ?? "-"}</td>
                        <td className={`py-1.5 pr-3 font-mono ${(r.pnl ?? 0) >= 0 ? "text-green-400" : "text-red-400"}`}>{r.pnl ?? "-"}</td>
                        <td className="py-1.5 pr-3">{r.dd ?? "-"}</td>
                        <td className="py-1.5 pr-3">{p.ema_fast}</td>
                        <td className="py-1.5 pr-3">{p.ema_slow}</td>
                        <td className="py-1.5 pr-3">{p.sl_pct}</td>
                        <td className="py-1.5 pr-3">{p.tp1_pct}</td>
                        <td className="py-1.5 pr-3">{p.tp2_pct}</td>
                        <td className="py-1.5 pr-3">{p.volume_multiplier}</td>
                        <td className="py-1.5 pr-3">{p.tp1_close_pct}</td>
                        <td className="py-1.5 pr-3">{p.risk_pct ?? "-"}</td>
                        <td className="py-1.5 pr-3">{p.htf_ema_fast ?? "-"}</td>
                        <td className="py-1.5 pr-3">{p.htf_ema_slow ?? "-"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
