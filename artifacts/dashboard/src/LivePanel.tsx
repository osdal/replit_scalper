import { useEffect, useState } from "react";
import { armBot, disarmBot, closeAllLive, fetchDailyLoss, fetchDrawdown, fetchRelayStats, setRelayOnly, updateConfig, fetchTradingStatus, updateGlobalLimits } from "./hooks/useApi";

type AnyBot = {
  symbol: string;
  is_running?: boolean;
  armed?: boolean | number;
  relay_only?: boolean | number;
  trade_mode?: string;
  position_size_usd?: number;
  position_size_pct?: number;
  expired_after_minutes?: number;
  slot_release_minutes?: number;
  position_loss_pct?: number;
  total_loss_pct?: number;
};

export default function LivePanel({ bots, onChanged }: { bots: AnyBot[]; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [sizes, setSizes] = useState<Record<string, string>>({});
  const [pcts, setPcts] = useState<Record<string, string>>({});
  const [expiredMins, setExpiredMins] = useState<Record<string, string>>({});
  const [slotRelease, setSlotRelease] = useState<Record<string, string>>({});
  const [positionLossPct, setPositionLossPct] = useState<Record<string, string>>({});
  const [totalLossPct, setTotalLossPct] = useState<Record<string, string>>({});
  const [loss, setLoss] = useState<Awaited<ReturnType<typeof fetchDailyLoss>> | null>(null);
  const [dd, setDd] = useState<{ drawdown_pct: number; threshold_pct: number; equity: number; reference: number; enabled: boolean } | null>(null);
  const [relay, setRelay] = useState<{ pending: number; consumed: number; skipped: number; last: any | null } | null>(null);
  const [tradingStatus, setTradingStatus] = useState<{ max_positions: number; max_positions_with_expired: number } | null>(null);
  const [globalMaxPos, setGlobalMaxPos] = useState("");
  const [globalMaxPosWithExpired, setGlobalMaxPosWithExpired] = useState("");
  const [applyingGlobalLimits, setApplyingGlobalLimits] = useState(false);
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
  const loadTradingStatus = async () => {
    try { 
      const status = await fetchTradingStatus();
      setTradingStatus(status);
      // Prefill inputs with current values
      setGlobalMaxPos(String(status.max_positions ?? 3));
      setGlobalMaxPosWithExpired(String(status.max_positions_with_expired ?? 10));
    } catch { /* ignore */ }
  };
  useEffect(() => {
    loadLoss();
    loadDd();
    loadRelay();
    loadTradingStatus();
    const id = setInterval(() => { loadLoss(); loadDd(); loadRelay(); loadTradingStatus(); }, 15000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key); setError(null);
    try { await fn(); onChanged(); } catch (e) { setError(String(e)); } finally { setBusy(null); }
  };

  const applyGlobalLimits = async () => {
    const maxPos = Number(globalMaxPos);
    const maxPosWithExpired = Number(globalMaxPosWithExpired);
    if (!Number.isFinite(maxPos) || maxPos < 0 || maxPos > 50) {
      setError("Max поз.: 0–50"); return;
    }
    if (!Number.isFinite(maxPosWithExpired) || maxPosWithExpired < 0 || maxPosWithExpired > 50) {
      setError("Max поз. + протух.: 0–50"); return;
    }
    setApplyingGlobalLimits(true); setError(null);
    try {
      await updateGlobalLimits(maxPos, maxPosWithExpired);
      setError(null);
      await loadTradingStatus();
    } catch (e) {
      setError(String(e));
    } finally {
      setApplyingGlobalLimits(false);
    }
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
          {loss?.baseline ? ` (база дня $${loss.baseline.toFixed(2)}${loss.day ? `, ${loss.day}` : ""})` : ""}
          {loss?.remaining_usd !== undefined ? `, осталось $${loss.remaining_usd.toFixed(2)}` : ""}
          {loss?.tripped ? " — ЛИМИТ СРАБОТАЛ (боты остановлены)" : ""}
          {loss?.degraded ? " · ВНИМАНИЕ: биржа не ответила, плавающий PnL не учтён" : ""}
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

      <div className="rounded-md border border-zinc-800 bg-zinc-900 p-3 text-xs">
        <div className="font-semibold text-zinc-300">Глобальные лимиты позиций (все боты)</div>
        <div className="flex flex-wrap gap-4 mt-2 text-zinc-400">
          <div className="flex items-center gap-1.5">
            <span className="text-zinc-500">Max поз.: </span>
            <span className="font-mono font-semibold text-zinc-100">{tradingStatus?.max_positions ?? "—"}</span>
            <input
              type="number"
              step="1"
              min="0"
              max="50"
              value={globalMaxPos}
              onChange={e => setGlobalMaxPos(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") void applyGlobalLimits(); }}
              className="w-14 rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-xs"
              title="Новое значение"
            />
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-zinc-500">Max поз. + протух.: </span>
            <span className="font-mono font-semibold text-zinc-100">{tradingStatus?.max_positions_with_expired ?? "—"}</span>
            <input
              type="number"
              step="1"
              min="0"
              max="50"
              value={globalMaxPosWithExpired}
              onChange={e => setGlobalMaxPosWithExpired(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") void applyGlobalLimits(); }}
              className="w-14 rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-xs"
              title="Новое значение"
            />
          </div>
        </div>
        <div className="mt-2 flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={applyGlobalLimits}
            disabled={applyingGlobalLimits || busy !== null}
            className="border-zinc-700 text-zinc-200 hover:bg-zinc-800"
          >
            {applyingGlobalLimits ? "Working..." : "Apply"}
          </Button>
          {error && <span className="text-xs text-red-400">{error}</span>}
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
              <th className="pr-3">протухание, мин</th>
              <th className="pr-3" title="Через сколько минут позиция перестаёт занимать слот max_positions (не закрывается)">слот, мин</th>
              <th className="pr-3" title="Закрыть позицию при её плавающем убытке ≥ N% депозита">убыток позиции, %</th>
              <th className="pr-3" title="Дневной стоп: net за день ≤ -N% баланса на начало дня (Киев)">дневной убыток, %</th>
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
                    <input
                      value={expiredMins[b.symbol] ?? String(b.expired_after_minutes ?? 120)}
                      onChange={(e) => setExpiredMins((s) => ({ ...s, [b.symbol]: e.target.value }))}
                      className="w-16 rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 font-mono"
                    />
                    <button
                      className="ml-1 rounded border border-zinc-700 px-1.5 py-0.5 hover:bg-zinc-800 disabled:opacity-50"
                      disabled={busy !== null}
                      onClick={() =>
                        void run(`exp-${b.symbol}`, () =>
                          updateConfig(b.symbol, { expired_after_minutes: Number(expiredMins[b.symbol]) || 0 }),
                        )
                      }
                    >
                      set
                    </button>
                  </td>
                  <td className="pr-3">
                    <input
                      value={slotRelease[b.symbol] ?? String(b.slot_release_minutes ?? 360)}
                      onChange={(e) => setSlotRelease((s) => ({ ...s, [b.symbol]: e.target.value }))}
                      className="w-16 rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 font-mono"
                    />
                    <button
                      className="ml-1 rounded border border-zinc-700 px-1.5 py-0.5 hover:bg-zinc-800 disabled:opacity-50"
                      disabled={busy !== null}
                      onClick={() =>
                        void run(`slot-${b.symbol}`, () =>
                          updateConfig(b.symbol, { slot_release_minutes: Number(slotRelease[b.symbol]) || 0 }),
                        )
                      }
                    >
                      set
                    </button>
                  </td>
                  <td className="pr-3">
                    <input
                      value={positionLossPct[b.symbol] ?? String(b.position_loss_pct ?? 5)}
                      onChange={(e) => setPositionLossPct((s) => ({ ...s, [b.symbol]: e.target.value }))}
                      className="w-14 rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 font-mono"
                    />
                    <button
                      className="ml-1 rounded border border-zinc-700 px-1.5 py-0.5 hover:bg-zinc-800 disabled:opacity-50"
                      disabled={busy !== null}
                      onClick={() =>
                        void run(`plp-${b.symbol}`, () =>
                          updateConfig(b.symbol, { position_loss_pct: Number(positionLossPct[b.symbol]) || 0 }),
                        )
                      }
                    >
                      set
                    </button>
                  </td>
                  <td className="pr-3">
                    <input
                      value={totalLossPct[b.symbol] ?? String(b.total_loss_pct ?? 10)}
                      onChange={(e) => setTotalLossPct((s) => ({ ...s, [b.symbol]: e.target.value }))}
                      className="w-14 rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 font-mono"
                    />
                    <button
                      className="ml-1 rounded border border-zinc-700 px-1.5 py-0.5 hover:bg-zinc-800 disabled:opacity-50"
                      disabled={busy !== null}
                      onClick={() =>
                        void run(`tlp-${b.symbol}`, () =>
                          updateConfig(b.symbol, { total_loss_pct: Number(totalLossPct[b.symbol]) || 0 }),
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
