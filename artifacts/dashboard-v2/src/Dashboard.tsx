import { useState, useEffect, useCallback, useRef } from "react";
import { fetchPairs, fetchHistory, fetchLastPrice, fetchAdx, fetchBotsStatus } from "./hooks/useApi";
import { Button } from "./components/ui/button";
import * as lightweightCharts from "lightweight-charts";

export interface Candle {
  time: string | number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export default function Dashboard() {
  const [pairs, setPairs] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedPair, setSelectedPair] = useState<string | null>(null);
  const [chartData, setChartData] = useState<Candle[]>([]);
  const [chartLoading, setChartLoading] = useState(false);
  const [timeframe, setTimeframe] = useState<string>("1d");
  const [refreshTick, setRefreshTick] = useState(0);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [lastPrice, setLastPrice] = useState<number | null>(null);
  const [adxStatus, setAdxStatus] = useState<Record<string, { adx: number | null; gate: number; ok: boolean }>>({});
  const [botsStatus, setBotsStatus] = useState<Record<string, { is_running: boolean; position: any; current_price: number | null; last_heartbeat: string }>>({});
  const [gridNLevels, setGridNLevels] = useState<number>(10);
  const [gridGate, setGridGate] = useState<number>(15);
  const [tpPct, setTpPct] = useState<number | null>(null);
  const [tradingActive, setTradingActive] = useState<Record<string, boolean>>({});
  const [gridSim, setGridSim] = useState<Record<string, {
    startPrice: number;
    tpPct: number;
    gridLevels: number;
    unrealizedPnl: number | null;
    realizedPnl: number;
    lastResult: string | null;
    startTime: Date;
  }>>({});
  const [currentTpLine, setCurrentTpLine] = useState<number | null>(null);
  const chartInstanceRef = useRef<any>(null);
  const chartSeriesRef = useRef<any>(null);
  const chartMarkersSeriesRef = useRef<any>(null);
  const chartRef = useRef<HTMLDivElement>(null);

   useEffect(() => {
    if (!selectedPair) return;
    const symbol = selectedPair;
    const id = setInterval(async () => {
      try {
        const price = await fetchLastPrice(symbol);
        if (Number.isFinite(price as number)) {
          setLastPrice(price as number);
        }
      } catch {
        // ignore transient fetch errors
      }
    }, 10_000);
    return () => clearInterval(id);
  }, [selectedPair, timeframe]);

  useEffect(() => {
    if (!selectedPair || !lastPrice || !tradingActive[selectedPair]) return;
    const sim = gridSim[selectedPair];
    if (!sim) return;

    const pnlPct = (lastPrice - sim.startPrice) / sim.startPrice * 100;
    setGridSim((prev) => ({
      ...prev,
      [selectedPair]: { ...prev[selectedPair], unrealizedPnl: pnlPct },
    }));

    const tpTarget = sim.startPrice * (1 + sim.tpPct / 100);
    if (lastPrice >= tpTarget) {
      setGridSim((prev) => {
        const current = prev[selectedPair];
        if (!current) return prev;
        const totalPnl = current.realizedPnl + (current.unrealizedPnl ?? 0);
        return {
          ...prev,
          [selectedPair]: {
            ...current,
            realizedPnl: totalPnl,
            unrealizedPnl: null,
            lastResult: `${totalPnl.toFixed(2)}%`,
          },
        };
      });
      setCurrentTpLine(null);
      setTradingActive((prev) => ({ ...prev, [selectedPair]: false }));
    }
  }, [selectedPair, timeframe, lastPrice, tradingActive, gridSim]);

  const loadPairs = useCallback(async () => {
    try {
      const data = await fetchPairs();
      const list = Array.isArray(data) ? data : [];
      setPairs([...new Set(list)]);
    } catch {
      // ignore errors, keep empty list
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadPairs();
    const id = setInterval(loadPairs, 15_000);
    return () => clearInterval(id);
  }, [loadPairs]);

  useEffect(() => {
    if (!selectedPair) return;
    let cancelled = false;
    const loadBots = async () => {
      try {
        const data = await fetchBotsStatus();
        if (!cancelled && Object.keys(data).length > 0) {
          setBotsStatus(data);
        }
      } catch {
        // ignore transient errors
      }
    };
    loadBots();
    const id = setInterval(loadBots, 15_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [selectedPair]);

  useEffect(() => {
    if (!selectedPair) return;
    let cancelled = false;
    const loadAdx = async () => {
      try {
        const data = await fetchAdx();
        const normalized: Record<string, { adx: number | null; gate: number; ok: boolean }> = {};
        Object.entries(data || {}).forEach(([symbol, tfMap]) => {
          const entry = tfMap as Record<string, { adx: number | null; gate: number; ok: boolean }>;
          const tfs = ["1h", "4h", "12h", "1d"].filter((tf) => entry[tf]);
          if (tfs.length === 0) return;
          const adx = tfs.map((tf) => entry[tf].adx).find((v) => v != null) ?? null;
          const gate = entry[tfs[0]]?.gate ?? 15;
          const ok = tfs.some((tf) => entry[tf].ok);
          normalized[symbol] = { adx, gate, ok };
          (normalized[symbol] as any).tf1h = entry["1h"] || null;
          (normalized[symbol] as any).tf4h = entry["4h"] || null;
          (normalized[symbol] as any).tf12h = entry["12h"] || null;
          (normalized[symbol] as any).tf1d = entry["1d"] || null;
        });
        if (!cancelled && Object.keys(normalized).length > 0) {
          setAdxStatus(normalized);
        }
      } catch {
        // ignore transient errors
      }
    };
    loadAdx();
    const id = setInterval(loadAdx, 60_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [selectedPair]);

  useEffect(() => {
    if (!selectedPair) return;
    const id = setInterval(() => {
      setRefreshTick((t) => t + 1);
    }, 10_000);
    return () => clearInterval(id);
  }, [selectedPair]);

  useEffect(() => {
    if (!selectedPair) return;
    let cancelled = false;
    setChartLoading(true);
    fetchHistory(selectedPair, timeframe)
      .then((res) => {
        if (cancelled) return;
        const rows: Candle[] = (res.data || [])
          .map((k) => {
            const ts = Number(k.t);
            const d = new Date(ts);
            if (timeframe === "1d") {
              const dateStr = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
              return {
                time: dateStr,
                open: parseFloat(k.o),
                high: parseFloat(k.h),
                low: parseFloat(k.l),
                close: parseFloat(k.c),
              };
            }
            const seconds = Math.floor(ts / 1000);
            return {
              time: Number.isFinite(seconds) ? seconds : "",
              open: parseFloat(k.o),
              high: parseFloat(k.h),
              low: parseFloat(k.l),
              close: parseFloat(k.c),
            };
          })
          .filter((row) => {
            if (typeof row.time === "string") return row.time.length > 0;
            return Number.isFinite(row.time);
          });
        setChartData(rows);
        if (rows.length > 0) {
          setLastPrice(rows[rows.length - 1].close);
        }
      })
      .catch(() => {
        if (!cancelled) setChartData([]);
      })
      .finally(() => {
        if (!cancelled) setChartLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedPair, timeframe, refreshTick]);

  const calcLevels = useCallback((rows: Candle[]) => {
    if (rows.length < 5) return [];
    const tail = rows.slice(-120);
    const highs: { price: number; index: number; time: Candle["time"] }[] = [];
    const lows: { price: number; index: number; time: Candle["time"] }[] = [];
    for (let i = 1; i < tail.length - 1; i++) {
      const prev = tail[i - 1];
      const curr = tail[i];
      const next = tail[i + 1];
      if (curr.high > prev.high && curr.high > next.high) {
        highs.push({ price: curr.high, index: i, time: curr.time });
      }
      if (curr.low < prev.low && curr.low < next.low) {
        lows.push({ price: curr.low, index: i, time: curr.time });
      }
    }
    highs.sort((a, b) => b.price - a.price);
    lows.sort((a, b) => a.price - b.price);
    const topHighs = highs.slice(0, 1);
    const topLows = lows.slice(0, 1);
    const levels: { price: number; type: "resistance" | "support"; time: Candle["time"] }[] = [
      ...topHighs.map((x) => ({ price: x.price, type: "resistance" as const, time: x.time })),
      ...topLows.map((x) => ({ price: x.price, type: "support" as const, time: x.time })),
    ];
    levels.sort((a, b) => b.price - a.price);
    return levels;
  }, []);

  const calcGridLevels = useCallback((rows: Candle[], nLevels = 10) => {
    if (rows.length < 20) return [];
    const candlesPerDay =
      timeframe === "4h" ? 6 : timeframe === "12h" ? 2 : timeframe === "1h" ? 24 : 1;
    const lookback = 8 * candlesPerDay;
    const tail = rows.slice(-lookback);
    if (tail.length < 10) return [];
    const lows = tail.map((r) => r.low);
    const highs = tail.map((r) => r.high);
    const lo = Math.min(...lows);
    const hi = Math.max(...highs);
    if (hi <= lo) return [];
    const step = (hi - lo) / nLevels;
    const levels: { price: number; type: "grid" }[] = [];
    for (let k = 1; k < nLevels; k++) {
      levels.push({ price: Number((lo + step * k).toFixed(8)), type: "grid" });
    }
    return levels;
  }, [timeframe, gridNLevels]);

  useEffect(() => {
    if (!chartRef.current) return;
    let chart: any;
    try {
      const createChart = (lightweightCharts as any).createChart || (lightweightCharts as any).default?.createChart;
      if (!createChart) return;
      chart = createChart(chartRef.current, {
        width: chartRef.current.clientWidth || 800,
        height: 400,
        layout: { background: { color: "#ffffff" }, textColor: "#000" },
        rightPriceScale: { scaleMargins: { top: 0.05, bottom: 0.05 } },
        timeScale: {
          timeVisible: timeframe !== "1d",
          secondsVisible: false,
          useLocalTime: true,
          tickMarkFormatter: (time: any) => {
            const d = typeof time === "number" ? new Date(time * 1000) : null;
            const pad = (n: number) => String(n).padStart(2, "0");
            const months = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
            if (d) {
              if (timeframe !== "1d") {
                return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
              }
              return `${pad(d.getDate())} ${months[d.getMonth()]}`;
            }
            if (typeof time === "string") {
              if (timeframe === "1d") {
                const parts = time.split("-");
                if (parts.length >= 2) {
                  const monthIndex = parseInt(parts[1], 10) - 1;
                  return `${parts[2]} ${months[monthIndex]}`;
                }
                return time;
              }
              return time;
            }
            return String(time);
          },
        },
      });
      const series = chart.addSeries((lightweightCharts as any).CandlestickSeries, {
        upColor: "#22c55e",
        downColor: "#ef4444",
        borderVisible: false,
        wickUpColor: "#22c55e",
        wickDownColor: "#ef4444",
      });
      const markersSeries = chart.addSeries((lightweightCharts as any).LineSeries, {
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        lineWidth: 0,
        pointMarkersVisible: true,
        pointMarkerSize: 6,
        priceScaleId: "left",
      });
      chartInstanceRef.current = chart;
      chartSeriesRef.current = series;
      chartMarkersSeriesRef.current = markersSeries;
    } catch (e) {
      console.error("Chart init error", e);
    }
    return () => {
      chartInstanceRef.current = null;
      chartSeriesRef.current = null;
      chartMarkersSeriesRef.current = null;
      try { chart?.remove?.(); } catch {}
    };
  }, [selectedPair, timeframe]);

  useEffect(() => {
    if (!chartSeriesRef.current || chartData.length === 0) return;
    try {
      const prev = chartSeriesRef.current.dataByIndex?.() ?? chartSeriesRef.current?.data?.() ?? [];
      const prevStr = JSON.stringify(prev);
      const nextStr = JSON.stringify(chartData);
      if (prevStr !== nextStr) {
        console.log("Chart data changed", chartData.length);
        chartSeriesRef.current.setData(chartData);
        chartInstanceRef.current?.timeScale()?.fitContent();
      } else {
        console.log("Chart data unchanged");
      }
    } catch (e) {
      console.error("Chart data error", e);
    }
  }, [chartData, calcLevels]);

  useEffect(() => {
    if (!chartSeriesRef.current || chartData.length === 0) return;
    const draw = () => {
      try {
        chartSeriesRef.current.priceLines?.().forEach((line: any) => {
          try { chartSeriesRef.current.removePriceLine(line); } catch {}
        });
      } catch {}
      const levels = calcLevels(chartData);
      if (levels.length > 0) {
        levels.forEach((level) => {
          try {
            chartSeriesRef.current.createPriceLine({
              price: level.price,
              color: level.type === "resistance" ? "#ef4444" : "#22c55e",
              lineWidth: 1,
              lineStyle: 2,
              axisLabelVisible: true,
              title: level.type === "resistance" ? "R" : "S",
            });
          } catch {}
        });
        const markers = levels.map((level) => ({
          time: level.time,
          value: level.price,
          marker: {
            color: level.type === "resistance" ? "#ef4444" : "#22c55e",
            shape: "circle",
            size: 6,
          },
        }));
        try {
          chartMarkersSeriesRef.current?.setData(markers);
        } catch {}
      }
      const gridLevels = calcGridLevels(chartData, gridNLevels);
      const adx = adxStatus[selectedPair || ""];
      const adx1h = adx?.tf1h?.adx ?? null;
      const adx4h = adx?.tf4h?.adx ?? null;
      const adx12h = adx?.tf12h?.adx ?? null;
      const adx1d = adx?.tf1d?.adx ?? null;
      const relevantAdx = [adx1h, adx4h, adx12h, adx1d].find((v) => v != null) ?? null;
      const passesGate = relevantAdx != null && relevantAdx < gridGate;
      if (passesGate && gridLevels.length > 0) {
        gridLevels.forEach((level) => {
          try {
            chartSeriesRef.current.createPriceLine({
              price: level.price,
              color: "#f59e0b",
              lineWidth: 1,
              lineStyle: 1,
              axisLabelVisible: true,
              title: "",
            });
          } catch {}
        });
      }
      if (currentTpLine && chartSeriesRef.current) {
        try {
          chartSeriesRef.current.createPriceLine({
            price: currentTpLine,
            color: "#3b82f6",
            lineWidth: 2,
            lineStyle: 0,
            axisLabelVisible: true,
            title: `TP ${gridSim[selectedPair || ""]?.tpPct ?? ""}%`,
          });
        } catch {}
      }
    };
    const id = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(id);
  }, [chartData, calcLevels, calcGridLevels, gridGate, gridNLevels, selectedPair, adxStatus, currentTpLine, gridSim]);

  if (loading) {
    return <div className="p-6">Loading pairs...</div>;
  }

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4 text-black">Trading Pairs</h1>
      {pairs.length === 0 ? (
        <p className="text-zinc-500">No pairs found</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {pairs.map((pair) => {
            const isActive = selectedPair === pair;
            return (
              <Button
                key={pair}
                variant="outline"
                size="sm"
                onClick={() => setSelectedPair(isActive ? null : pair)}
                className={
                  "border " +
                  (isActive
                    ? "bg-blue-600 text-white border-blue-600 hover:bg-blue-700"
                    : "bg-white text-black border-gray-300 hover:bg-gray-100")
                }
              >
                {pair}
              </Button>
            );
          })}
        </div>
      )}

      {selectedPair && (
        <div className="mt-6">
          <div className="flex items-center gap-3 mb-2">
            <h2 className="text-xl font-semibold text-black">{selectedPair}</h2>
            {lastPrice !== null && (
              <span className="text-lg font-mono text-green-600">
                ${lastPrice.toFixed(4)}
              </span>
            )}
            <div className="flex gap-1">
              {(["1h", "4h", "12h", "1d"] as const).map((iv) => (
                <Button
                  key={iv}
                  size="sm"
                  variant={timeframe === iv ? "default" : "outline"}
                  onClick={() => setTimeframe(iv)}
                  className={
                    "min-w-[40px] " +
                    (timeframe === iv
                      ? "bg-black text-white hover:bg-black/90"
                      : "bg-white text-black border-gray-300 hover:bg-gray-100")
                  }
                >
                  {iv}
                </Button>
              ))}
          </div>
          {tpPct != null && (
            <div className="mb-2 text-xs text-black">
              estimated profit: {(() => {
                const lows = chartData.map((r) => r.low);
                const highs = chartData.map((r) => r.high);
                const lo = Math.min(...lows);
                const hi = Math.max(...highs);
                const range = hi - lo;
                const estimated = range * (tpPct / 100);
                return Number.isFinite(estimated) ? `~${estimated.toFixed(4)} USDT` : "N/A";
              })()}
            </div>
          )}
          </div>
          <div className="mb-2 flex flex-wrap gap-2 text-xs">
            {(["1h", "4h", "12h", "1d"] as const).map((tf) => {
              const tfKey = `tf${tf}` as keyof typeof adxStatus[string];
              const color =
                tf === "1h"
                  ? "bg-sky-100 text-sky-700"
                  : tf === "4h"
                    ? "bg-emerald-100 text-emerald-700"
                    : tf === "12h"
                      ? "bg-amber-100 text-amber-700"
                      : "bg-purple-100 text-purple-700";
              return (
                <>
                  <span className="font-semibold text-black">ADX &lt; {gridGate} ({tf}):</span>
                  {pairs.filter((p) => (adxStatus[p]?.[tfKey]?.adx ?? Infinity) < gridGate).length === 0 && (
                    <span className="text-zinc-500">нет данных</span>
                  )}
                  {pairs
                    .filter((p) => (adxStatus[p]?.[tfKey]?.adx ?? Infinity) < gridGate)
                    .map((p) => (
                      <span key={`${tf}-${p}`} className={`rounded px-2 py-0.5 ${color}`}>
                        {p}
                      </span>
                    ))}
                </>
              );
            })}
            <span className="ml-2 font-semibold text-black">Кол-во:</span>
            <Button
              size="sm"
              variant={gridNLevels === 10 ? "default" : "outline"}
              onClick={() => setGridNLevels(10)}
              className={
                "min-w-[40px] " +
                (gridNLevels === 10
                  ? "bg-black text-white hover:bg-black/90"
                  : "bg-white text-black border-gray-300 hover:bg-gray-100")
              }
            >
              L10
            </Button>
            <Button
              size="sm"
              variant={gridNLevels === 20 ? "default" : "outline"}
              onClick={() => setGridNLevels(20)}
              className={
                "min-w-[40px] " +
                (gridNLevels === 20
                  ? "bg-black text-white hover:bg-black/90"
                  : "bg-white text-black border-gray-300 hover:bg-gray-100")
              }
            >
              L20
            </Button>
            <span className="ml-2 font-semibold text-black">Gate:</span>
            {([15, 20, 25] as const).map((g) => (
              <Button
                key={g}
                size="sm"
                variant={gridGate === g ? "default" : "outline"}
                onClick={() => setGridGate(g)}
                className={
                  "min-w-[40px] " +
                  (gridGate === g
                    ? "bg-black text-white hover:bg-black/90"
                    : "bg-white text-black border-gray-300 hover:bg-gray-100")
                }
              >
                {g}
              </Button>
            ))}
            <span className="ml-2 font-semibold text-black">TP %:</span>
            {([1, 2, 5, 10] as const).map((pct) => (
              <Button
                key={pct}
                size="sm"
                variant={tpPct === pct ? "default" : "outline"}
                onClick={() => setTpPct(tpPct === pct ? null : pct)}
                className={
                  "min-w-[40px] " +
                  (tpPct === pct
                    ? "bg-black text-white hover:bg-black/90"
                    : "bg-white text-black border-gray-300 hover:bg-gray-100")
                }
              >
                {pct}%
              </Button>
            ))}
            <Button
              size="sm"
              variant={tradingActive[selectedPair || ""] ? "default" : "outline"}
              onClick={() => {
                const pair = selectedPair || "";
                const isActive = !!tradingActive[pair];
                if (!isActive && lastPrice && tpPct) {
                  const lows = chartData.map((r) => r.low);
                  const highs = chartData.map((r) => r.high);
                  const lo = lows.length ? Math.min(...lows) : lastPrice;
                  const hi = highs.length ? Math.max(...highs) : lastPrice;
                  const gridLevels = calcGridLevels(chartData, gridNLevels);
                  setGridSim((prev) => ({
                    ...prev,
                    [pair]: {
                      startPrice: lastPrice,
                      tpPct,
                      gridLevels: gridLevels.length,
                      unrealizedPnl: 0,
                      realizedPnl: prev[pair]?.realizedPnl ?? 0,
                      lastResult: null,
                      startTime: new Date(),
                    },
                  }));
                  setCurrentTpLine(lastPrice * (1 + tpPct / 100));
                } else if (isActive) {
                  setGridSim((prev) => {
                    const current = prev[pair];
                    if (!current) return prev;
                    const updated = {
                      ...current,
                      realizedPnl: current.realizedPnl + (current.unrealizedPnl ?? 0),
                      unrealizedPnl: null,
                      lastResult: `${(current.realizedPnl + (current.unrealizedPnl ?? 0)).toFixed(2)}%`,
                    };
                    return { ...prev, [pair]: updated };
                  });
                  setCurrentTpLine(null);
                }
                setTradingActive((prev) => ({
                  ...prev,
                  [pair]: !isActive,
                }));
              }}
              className={
                "ml-2 " +
                (tradingActive[selectedPair || ""]
                  ? "bg-green-600 text-white hover:bg-green-700"
                  : "bg-white text-black border-gray-300 hover:bg-gray-100")
              }
            >
              {tradingActive[selectedPair || ""] ? "trading enabled" : "start trading"}
            </Button>
          </div>
          <div ref={chartRef} className="h-[400px] w-full border" />
          {selectedPair && (
            <div className="mt-3 rounded border bg-white p-3 text-xs text-black">
              <div className="font-semibold mb-1">Statistics</div>
              <div className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-3">
                <div>pair: <span className="font-mono">{selectedPair}</span></div>
                <div>
                  trading:{" "}
                  <span className={tradingActive[selectedPair] ? "text-green-600" : "text-zinc-500"}>
                    {tradingActive[selectedPair] ? "enabled" : "disabled"}
                  </span>
                </div>
                {tradingActive[selectedPair] && (
                  <>
                    <div>grid levels: <span className="font-mono">{gridSim[selectedPair]?.gridLevels ?? "—"}</span></div>
                    <div>
                      unrealized PnL:{" "}
                      <span className={`font-mono ${(gridSim[selectedPair]?.unrealizedPnl ?? 0) >= 0 ? "text-green-600" : "text-red-600"}`}>
                        {(gridSim[selectedPair]?.unrealizedPnl ?? 0).toFixed(2)}%
                      </span>
                    </div>
                    <div>
                      realized PnL:{" "}
                      <span className={`font-mono ${(gridSim[selectedPair]?.realizedPnl ?? 0) >= 0 ? "text-green-600" : "text-red-600"}`}>
                        {(gridSim[selectedPair]?.realizedPnl ?? 0).toFixed(2)}%
                      </span>
                    </div>
                    <div>
                      last result:{" "}
                      <span className="font-mono">{gridSim[selectedPair]?.lastResult ?? "—"}</span>
                    </div>
                  </>
                )}
                {!tradingActive[selectedPair] && (
                  <>
                    <div>grid TF: <span className="font-mono">{timeframe}</span></div>
                    <div>gate: <span className="font-mono">{gridGate}</span></div>
                    <div>levels: <span className="font-mono">{gridNLevels}</span></div>
                    <div>TP %: <span className="font-mono">{tpPct ?? "—"}</span></div>
                    <div>
                      ADX ({timeframe}):{" "}
                      <span className="font-mono">
                        {adxStatus[selectedPair]?.[`tf${timeframe}` as any]?.adx ?? "—"}
                      </span>
                    </div>
                    {(() => {
                      const lows = chartData.map((r) => r.low);
                      const highs = chartData.map((r) => r.high);
                      const lo = lows.length ? Math.min(...lows) : null;
                      const hi = highs.length ? Math.max(...highs) : null;
                      return (
                        <>
                          <div>grid lo: <span className="font-mono">{lo != null ? lo.toFixed(4) : "—"}</span></div>
                          <div>grid hi: <span className="font-mono">{hi != null ? hi.toFixed(4) : "—"}</span></div>
                        </>
                      );
                    })()}
                  </>
                )}
                <div>
                  bot:{" "}
                  <span className={botsStatus[selectedPair]?.is_running ? "text-green-600" : "text-zinc-500"}>
                    {botsStatus[selectedPair]?.is_running ? "running" : "stopped"}
                  </span>
                </div>
                <div>
                  position:{" "}
                  <span className="font-mono">
                    {botsStatus[selectedPair]?.position ? "open" : "none"}
                  </span>
                </div>
                <div>
                  last price:{" "}
                  <span className="font-mono">
                    {botsStatus[selectedPair]?.current_price ?? lastPrice != null ? (botsStatus[selectedPair]?.current_price ?? lastPrice).toFixed(4) : "—"}
                  </span>
                </div>
                <div className="col-span-2 sm:col-span-3">
                  last heartbeat:{" "}
                  <span className="font-mono">
                    {botsStatus[selectedPair]?.last_heartbeat
                      ? new Date(botsStatus[selectedPair].last_heartbeat).toLocaleString()
                      : "—"}
                  </span>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
