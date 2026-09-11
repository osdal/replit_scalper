import { useState, useEffect, useCallback, useRef } from "react";
import { fetchPairs, fetchHistory, fetchLastPrice, fetchAdx } from "./hooks/useApi";
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
  const [gridNLevels, setGridNLevels] = useState<number>(10);
  const [gridGate, setGridGate] = useState<number>(15);
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
    const loadAdx = async () => {
      try {
        const data = await fetchAdx();
        const normalized: Record<string, { adx: number | null; gate: number; ok: boolean }> = {};
        Object.entries(data || {}).forEach(([symbol, tfMap]) => {
          const entry = tfMap as Record<string, { adx: number | null; gate: number; ok: boolean }>;
          if (entry["1h"] || entry["4h"]) {
            normalized[symbol] = {
              adx: entry["1h"]?.adx ?? entry["4h"]?.adx ?? null,
              gate: entry["1h"]?.gate ?? entry["4h"]?.gate ?? 15,
              ok: (entry["1h"]?.ok ?? false) || (entry["4h"]?.ok ?? false),
            };
            (normalized[symbol] as any).tf1h = entry["1h"] || null;
            (normalized[symbol] as any).tf4h = entry["4h"] || null;
          }
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
    const candlesPerDay = timeframe === "4h" ? 6 : timeframe === "1h" ? 24 : 6;
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
      const adx = adxStatus[selectedPair || ""] as any;
      const adx1h = adx?.tf1h?.adx ?? null;
      const adx4h = adx?.tf4h?.adx ?? null;
      const passesGate =
        (adx1h != null && adx1h < gridGate) || (adx4h != null && adx4h < gridGate);
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
    };
    const id = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(id);
  }, [chartData, calcLevels, calcGridLevels, gridGate, gridNLevels, selectedPair, adxStatus]);

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
          </div>
          <div className="mb-2 flex flex-wrap gap-2 text-xs">
            <span className="font-semibold text-black">ADX &lt; {gridGate} (4h):</span>
            {pairs.filter((p) => (adxStatus[p]?.tf4h?.adx ?? Infinity) < gridGate).length === 0 && (
              <span className="text-zinc-500">нет данных</span>
            )}
            {pairs
              .filter((p) => (adxStatus[p]?.tf4h?.adx ?? Infinity) < gridGate)
              .map((p) => (
                <span key={`4h-${p}`} className="rounded bg-emerald-100 px-2 py-0.5 text-emerald-700">
                  {p}
                </span>
              ))}
            <span className="ml-2 font-semibold text-black">ADX &lt; {gridGate} (1h):</span>
            {pairs.filter((p) => (adxStatus[p]?.tf1h?.adx ?? Infinity) < gridGate).length === 0 && (
              <span className="text-zinc-500">нет данных</span>
            )}
            {pairs
              .filter((p) => (adxStatus[p]?.tf1h?.adx ?? Infinity) < gridGate)
              .map((p) => (
                <span key={`1h-${p}`} className="rounded bg-sky-100 px-2 py-0.5 text-sky-700">
                  {p}
                </span>
              ))}
            <span className="ml-2 font-semibold text-black">Grid:</span>
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
          </div>
          <div ref={chartRef} className="h-[400px] w-full border" />
        </div>
      )}
    </div>
  );
}
