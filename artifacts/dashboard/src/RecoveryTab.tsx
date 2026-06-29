import { useState, useEffect, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "./components/ui/card";
import { Button } from "./components/ui/button";
import { Badge } from "./components/ui/badge";
import { RefreshCw, Link2, AlertTriangle, CheckCircle2 } from "lucide-react";

const API = "http://localhost:5000/api";

interface RecoveryChain {
  id: number;
  debt_amount: number;
  status: "free" | "locked" | "closed";
  locked_by: string | null;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
}

interface RecoveryConfig {
  recovery_enabled: boolean;
  recovery_bonus_pct: number;
  recovery_max_pct: number;
}

function fmtTime(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const day = String(d.getDate()).padStart(2, "0");
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const h = String(d.getHours()).padStart(2, "0");
  const m = String(d.getMinutes()).padStart(2, "0");
  return `${day}.${month} ${h}:${m}`;
}

export default function RecoveryTab() {
  const [config, setConfig] = useState<RecoveryConfig | null>(null);
  const [chains, setChains] = useState<RecoveryChain[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [bonusPct, setBonusPct] = useState(0);
  const [maxPct, setMaxPct] = useState(50);

  const load = useCallback(async () => {
    try {
      const [c, ch] = await Promise.all([
        fetch(`${API}/recovery/config`).then(r => r.json()),
        fetch(`${API}/recovery/chains`).then(r => r.json()),
      ]);
      setConfig(c);
      setChains(Array.isArray(ch) ? ch : []);
      if (c) {
        setBonusPct(c.recovery_bonus_pct);
        setMaxPct(c.recovery_max_pct);
      }
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, [load]);

  const toggleEnabled = async () => {
    if (!config) return;
    setSaving(true);
    try {
      const updated = await fetch(`${API}/recovery/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          recovery_enabled: !config.recovery_enabled,
          recovery_bonus_pct: bonusPct,
          recovery_max_pct: maxPct,
        }),
      }).then(r => r.json());
      setConfig(updated);
    } finally {
      setSaving(false);
    }
  };

  const saveConfig = async () => {
    setSaving(true);
    try {
      const updated = await fetch(`${API}/recovery/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          recovery_enabled: config?.recovery_enabled ?? false,
          recovery_bonus_pct: bonusPct,
          recovery_max_pct: maxPct,
        }),
      }).then(r => r.json());
      setConfig(updated);
    } finally {
      setSaving(false);
    }
  };

  const freeChains = chains.filter(c => c.status === "free");
  const lockedChains = chains.filter(c => c.status === "locked");
  const closedChains = chains.filter(c => c.status === "closed");
  const totalFreeDebt = freeChains.reduce((s, c) => s + c.debt_amount, 0);
  const totalLockedDebt = lockedChains.reduce((s, c) => s + c.debt_amount, 0);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48 text-zinc-400">
        <RefreshCw className="w-5 h-5 animate-spin mr-2" />Loading...
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Config */}
      <Card className="border border-zinc-800 bg-zinc-900">
        <CardHeader className="pb-2">
          <CardTitle className="text-base text-zinc-300 flex items-center gap-2">
            <Link2 className="w-4 h-4" />Recovery Mode
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-between mb-4">
            <div>
              <div className="text-sm text-zinc-400 mb-1">
                Когда сделка закрывается в убыток, следующий свободный сигнал
                компенсирует его — TP1 рассчитывается чтобы покрыть долг.
              </div>
            </div>
            <Button
              onClick={toggleEnabled}
              disabled={saving}
              variant={config?.recovery_enabled ? "destructive" : "default"}
            >
              {config?.recovery_enabled ? "Disable" : "Enable"}
            </Button>
          </div>
          <div className="grid grid-cols-2 gap-4 mb-4">
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">Bonus %</label>
              <input
                type="number"
                value={bonusPct}
                onChange={e => setBonusPct(parseFloat(e.target.value) || 0)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none focus:ring-1 focus:ring-zinc-500"
                min={0}
                step={1}
              />
            </div>
            <div>
              <label className="text-xs text-zinc-400 mb-1 block">Max % of deposit</label>
              <input
                type="number"
                value={maxPct}
                onChange={e => setMaxPct(parseFloat(e.target.value) || 0)}
                className="w-full rounded-md border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm text-zinc-300 focus:outline-none focus:ring-1 focus:ring-zinc-500"
                min={0}
                step={5}
              />
            </div>
          </div>
          <div className="flex items-center gap-4">
            <Button onClick={saveConfig} disabled={saving} size="sm">
              {saving ? "Saving..." : "Save Config"}
            </Button>
            <Badge variant={config?.recovery_enabled ? "default" : "secondary"}>
              {config?.recovery_enabled ? "● ENABLED" : "○ DISABLED"}
            </Badge>
          </div>
        </CardContent>
      </Card>

      {/* Summary */}
      <div className="grid grid-cols-3 gap-4">
        <Card className="border border-zinc-800 bg-zinc-900">
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
              <AlertTriangle className="w-4 h-4 text-yellow-500" />Free debt
            </div>
            <div className="text-2xl font-bold font-mono text-yellow-400">
              {totalFreeDebt.toFixed(4)} USDT
            </div>
            <div className="text-xs text-zinc-500 mt-1">{freeChains.length} chain(s) waiting</div>
          </CardContent>
        </Card>
        <Card className="border border-zinc-800 bg-zinc-900">
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
              <Link2 className="w-4 h-4 text-blue-400" />In recovery
            </div>
            <div className="text-2xl font-bold font-mono text-blue-400">
              {totalLockedDebt.toFixed(4)} USDT
            </div>
            <div className="text-xs text-zinc-500 mt-1">{lockedChains.length} chain(s) active</div>
          </CardContent>
        </Card>
        <Card className="border border-zinc-800 bg-zinc-900">
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2 text-zinc-400 text-xs mb-1">
              <CheckCircle2 className="w-4 h-4 text-green-400" />Resolved
            </div>
            <div className="text-2xl font-bold font-mono text-green-400">
              {closedChains.length}
            </div>
            <div className="text-xs text-zinc-500 mt-1">chains closed</div>
          </CardContent>
        </Card>
      </div>

      {/* Active chains table */}
      <Card className="border border-zinc-800 bg-zinc-900">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm text-zinc-400">Chains</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-zinc-400 text-xs border-b border-zinc-800">
                  {["ID", "Status", "Debt", "Locked By", "Created", "Updated", "Closed"].map(h => (
                    <th key={h} className="text-left pb-2 pr-4">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {chains.length === 0 && (
                  <tr><td colSpan={7} className="text-center text-zinc-500 py-6">No chains yet</td></tr>
                )}
                {[...chains].reverse().map(c => (
                  <tr key={c.id} className="border-b border-zinc-800/50 text-zinc-300">
                    <td className="py-1.5 pr-4">#{c.id}</td>
                    <td className="py-1.5 pr-4">
                      <Badge variant={
                        c.status === "free" ? "secondary" :
                        c.status === "locked" ? "default" : "outline"
                      }>
                        {c.status}
                      </Badge>
                    </td>
                    <td className="py-1.5 pr-4 font-mono">{c.debt_amount.toFixed(4)}</td>
                    <td className="py-1.5 pr-4">{c.locked_by || "—"}</td>
                    <td className="py-1.5 pr-4 text-xs text-zinc-500">{fmtTime(c.created_at)}</td>
                    <td className="py-1.5 pr-4 text-xs text-zinc-500">{fmtTime(c.updated_at)}</td>
                    <td className="py-1.5 pr-4 text-xs text-zinc-500">{fmtTime(c.closed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
