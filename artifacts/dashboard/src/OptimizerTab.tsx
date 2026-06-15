import { useState, useEffect, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "./components/ui/card";
import { Button } from "./components/ui/button";
import { Badge } from "./components/ui/badge";
import { Play, Square, RefreshCw, TrendingUp } from "lucide-react";

const API = "http://localhost:5000/api";

interface OptResult {
  rank: string;
  score: string;
  ema_fast: string;
  ema_slow: string;
  sl_pct: string;
  tp1_pct: string;
  tp2_pct: string;
  volume_multiplier: string;
  tp1_close_pct: string;
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

const SYMBOLS = ["ETHUSDT", "SOLUSDT", "BTCUSDT", "TRXUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT"];

interface OptimizerTabProps {
  jobId: string | null;
  job: JobStatus | null;
  setJobId: (id: string | null) => void;
  setJob: (job: JobStatus | null) => void;
}

export default function OptimizerTab({ jobId, job, setJobId, setJob }: OptimizerTabProps) {
  const [symbol, setSymbol]   = useState("ETHUSDT");
  const [start, setStart]     = useState("2026-05-01");
  const [end, setEnd]         = useState("2026-06-13");
  const [trials, setTrials]   = useState(100);
  const [loading, setLoading] = useState(false);
  const outputRef = useRef<HTMLDivElement>(null);

  // Поллинг статуса задачи
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

  // Автоскролл вывода
  useEffect(() => {
    if (outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [job?.output]);

  const handleStart = async () => {
    setLoading(true);
    try {
      const r = await fetch(`${API}/optimizer/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol, start, end, trials }),
      });
      const data = await r.json();
      setJobId(data.jobId);
      setJob(null);
    } catch (e) {
      alert("Failed to start optimizer");
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

  const isRunning = job?.status === "running";

  return (
    <div className="space-y-4">
      {/* Параметры */}
      <Card className="border border-zinc-800 bg-zinc-900">
        <CardHeader className="pb-2">
          <CardTitle className="text-base text-zinc-300 flex items-center gap-2">
            <TrendingUp className="w-4 h-4" />Parameter Optimizer
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
            <div>
              <label className="text-zinc-400 text-xs mb-1 block">Symbol</label>
              <select
                value={symbol}
                onChange={e => setSymbol(e.target.value)}
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-sm text-white"
              >
                {SYMBOLS.map(s => <option key={s}>{s}</option>)}
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
                type="number" value={trials} min={10} max={500}
                onChange={e => setTrials(parseInt(e.target.value))}
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

          {/* Прогресс */}
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

      {/* Вывод */}
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

      {/* Результаты */}
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
                    {["Rank","Score","EMA F","EMA S","SL%","TP1%","TP2%","Vol×","TP1 cl%"].map(h => (
                      <th key={h} className="text-left pb-2 pr-4">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {job.results.map((r: any, i: number) => (
                    <tr key={i} className={`border-b border-zinc-800/50 ${i === 0 ? "text-green-400 font-semibold" : "text-zinc-300"}`}>
                      <td className="py-1.5 pr-4">{r.rank}</td>
                      <td className="py-1.5 pr-4 font-mono">{r.score}</td>
                      <td className="py-1.5 pr-4">{r.params?.ema_fast ?? r.ema_fast}</td>
                      <td className="py-1.5 pr-4">{r.params?.ema_slow ?? r.ema_slow}</td>
                      <td className="py-1.5 pr-4">{r.params?.sl_pct ?? r.sl_pct}</td>
                      <td className="py-1.5 pr-4">{r.params?.tp1_pct ?? r.tp1_pct}</td>
                      <td className="py-1.5 pr-4">{r.params?.tp2_pct ?? r.tp2_pct}</td>
                      <td className="py-1.5 pr-4">{r.params?.volume_multiplier ?? r.volume_multiplier}</td>
                      <td className="py-1.5 pr-4">{r.params?.tp1_close_pct ?? r.tp1_close_pct}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
