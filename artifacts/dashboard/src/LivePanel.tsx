import { useEffect, useState } from "react";
import { armBot, disarmBot, closeAllLive, fetchDailyLoss, fetchDrawdown, fetchRelayStats, setRelayOnly, updateConfig } from "./hooks/useApi";

type AnyBot = {
  symbol: string;
  is_running?: boolean;
  armed?: boolean | number;
  relay_only?: boolean | number;
  trade_mode?: string;
  position_size_usd?: number;
  position_size_pct?: number;
};

export default function LivePanel({ bots, onChanged }: { bots: AnyBot[]; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [sizes, setSizes] = useState<Record<string, string>>({});
  const [pcts, setPcts] = useState<Record<string, string>>({});
  const [loss, setLoss] = useState<{ limit: number; net: number; tripped: boolean } | null>(null);
  const [dd, setDd] = useState<{ drawdown_pct: number; threshold_pct: number; equity: number; reference: number; enabled: boolean } | null>(null);
  const [relay, setRelay] = useState<{ pending: number; consumed: number; skipped: number; last: any | null } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadLoss = async () => {
    try { setLoss(await fetchDailyLoss()); } catch { /* ignore */ }
  };
  const loadDd = async () => {
    try { setDd(await fetchDrawdown()); } catch { /* ignore */ }
  };
  const loadRelay = async () => {
    try { setRelay(await fetchRelayStats()); } catch { /* ignore */ }
  };
  useEffect(() => {
    loadLoss();
    loadDd();
    loadRelay();
    const id = setInterval(() => { loadLoss(); loadDd(); loadRelay(); }, 15000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key); setError(null);
    try { await fn(); onChanged(); } catch (e) { setError(String(e)); } finally { setBusy(null); }
  };

  const sorted = [...bots].sort((a, b) => a.symbol.localeCompare(b.symbol));

  return (
    <div className="space-y-4">
      <div className="rounded-md border-2 border-red-600 bg-red-950/40 p-3 text-red-300">
        <div className="font-semibold">LIVE — реальные деньги</div>
        <div className="text-xs mt-1">
          Боты по умолчанию не армированы: сначала Start, затем Arm. Объём: маржа в USD,
          позиция = маржа × плечо; либо маржа = % от СВОБОДНОГО депозита (availableBalance)
          — по умолчанию 1%. Приоритет: явный margin_usd важнее процента. Дневной лимит:{" "}
          {loss ? `${loss.net.toFixed(2)} / -${loss.limit}` : "—"}
          {loss?.tripped ? " — ЛИМИТ СРАБОТАЛ (боты остановлены)" : ""}
          <br />
          Просадка:{" "}
          {dd ? `${dd.drawdown_pct.toFixed(1)}% / ${dd.threshold_pct}% (equity $${dd.equity.toFixed(2)}, ref $${dd.reference.toFixed(2)})` : "—"}
          {dd && dd.enabled && dd.drawdown_pct >= dd.threshold_pct ? " — закрытие убыточной позиции" : ""}
        </div>
      </div>

      <div className="rounded-md border border-zinc-800 bg-zinc-900 p-3 text-xs">
        <div className="font-semibold text-zinc-300">Relay (testnet → live)</div>
        <div className="mt-1 text-zinc-400">
          pending={relay?.pending ?? "—"} · consumed={relay?.consumed ?? "—"} · skipped={relay?.skipped ?? "—"}
        </div>
        <div className="mt-1 text-zinc-400">
          last:{" "}
          {relay?.last
            ? `${relay.last.symbol} ${relay.last.direction} ${relay.last.preset ?? ""} @ ${String(relay.last.created_at).slice(11, 19)} (${relay.last.status ?? "pending"})`
            : "—"}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          className="rounded-md bg-red-700 px-3 py-1.5 text-sm text-white hover:bg-red-600 disabled:opacity-50"
          disabled={busy !== null}
          onClick={() => {
            if (confirm("Закрыть ВСЕ позиции по рынку и остановить всех live-ботов?")) {
              void run("close-all", () => closeAllLive());
            }
          }}
        >
          Close all &amp; Stop (kill-switch)
        </button>
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>

      <div className="overflow-x-auto rounded-md border border-zinc-800">
        <table className="min-w-full text-xs">
          <thead>
            <tr className="text-left text-zinc-500">
              <th className="pr-3 pl-2">symbol</th>
              <th className="pr-3">running</th>
              <th className="pr-3">armed</th>
              <th className="pr-3">relay-only</th>
              <th className="pr-3">trade_mode</th>
              <th className="pr-3">margin_usd (позиция = ×плечо)</th>
              <th className="pr-3">free % (маржа от своб. депозита)</th>
              <th className="pr-3">actions</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((b) => {
              const armed = Boolean(Number(b.armed ?? 0));
              const sizeVal = sizes[b.symbol] ?? String(b.position_size_usd ?? 0);
              const pctVal = pcts[b.symbol] ?? String(b.position_size_pct ?? 1);
              return (
                <tr key={b.symbol} className="border-t border-zinc-800">
                  <td className="pr-3 pl-2 font-mono">{b.symbol}</td>
                  <td className="pr-3">{b.is_running ? "yes" : "—"}</td>
                  <td className={`pr-3 font-mono ${armed ? "text-red-400" : "text-zinc-500"}`}>
                    {armed ? "ARMED" : "—"}
                  </td>
                  <td className="pr-3">
                    {(() => {
                      const ro = Boolean(Number(b.relay_only ?? 0));
                      return (
                        <button
                          className={`rounded border px-2 py-0.5 ${ro ? "border-red-600 text-red-300 hover:bg-red-900/40" : "border-zinc-700 text-zinc-400 hover:bg-zinc-800"}`}
                          disabled={busy !== null}
                          onClick={() => void run(`relay-${b.symbol}`, () => setRelayOnly(b.symbol, !ro))}
                        >
                          {ro ? "relay-only" : "own"}
                        </button>
                      );
                    })()}
                  </td>
                  <td className="pr-3">
                    <select
                      value={b.trade_mode ?? "manual"}
                      onChange={(e) =>
                        void run(`mode-${b.symbol}`, () => updateConfig(b.symbol, { trade_mode: e.target.value }))
                      }
                      className="rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5"
                    >
                      <option value="manual">manual</option>
                      <option value="auto">auto</option>
                    </select>
                  </td>
                  <td className="pr-3">
                    <input
                      value={sizeVal}
                      onChange={(e) => setSizes((s) => ({ ...s, [b.symbol]: e.target.value }))}
                      className="w-24 rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 font-mono"
                    />
                    <button
                      className="ml-1 rounded border border-zinc-700 px-1.5 py-0.5 hover:bg-zinc-800 disabled:opacity-50"
                      disabled={busy !== null}
                      onClick={() =>
                        void run(`size-${b.symbol}`, () =>
                          updateConfig(b.symbol, { position_size_usd: Number(sizeVal) || 0 }),
                        )
                      }
                    >
                      set
                    </button>
                  </td>
                  <td className="pr-3">
                    <input
                      value={pctVal}
                      onChange={(e) => setPcts((s) => ({ ...s, [b.symbol]: e.target.value }))}
                      className="w-16 rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 font-mono"
                    />
                    <span className="ml-0.5 text-zinc-500">%</span>
                    <button
                      className="ml-1 rounded border border-zinc-700 px-1.5 py-0.5 hover:bg-zinc-800 disabled:opacity-50"
                      disabled={busy !== null}
                      onClick={() =>
                        void run(`pct-${b.symbol}`, () =>
                          updateConfig(b.symbol, { position_size_pct: Number(pctVal) || 0 }),
                        )
                      }
                    >
                      set
                    </button>
                  </td>
                  <td className="pr-3">
                    {armed ? (
                      <button
                        className="rounded border border-zinc-700 px-2 py-0.5 hover:bg-zinc-800 disabled:opacity-50"
                        disabled={busy !== null}
                        onClick={() => void run(`disarm-${b.symbol}`, () => disarmBot(b.symbol))}
                      >
                        Disarm
                      </button>
                    ) : (
                      <button
                        className="rounded border border-red-600 px-2 py-0.5 text-red-300 hover:bg-red-900/40 disabled:opacity-50"
                        disabled={busy !== null}
                        onClick={() => void run(`arm-${b.symbol}`, () => armBot(b.symbol))}
                      >
                        Arm
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
