import { Router } from "express";
import { notifyTokenGuard } from "../middlewares/notifyAuth";

const router = Router();

function formatStartMessage(body: any): string {
  const { pair, direction, timeframe, startPrice, tpPct, gridLevels, gate, adx } = body || {};
  const entry = Number(startPrice);
  const tp = Number(tpPct);
  const tpPrice =
    Number.isFinite(entry) && Number.isFinite(tp) ? entry * (1 + tp / 100) : null;
  const lines: (string | null)[] = [
    "▶️ START TRADING",
    pair ? `pair: ${pair}` : null,
    direction ? `direction: ${direction}` : null,
    timeframe ? `timeframe: ${timeframe}` : null,
    Number.isFinite(entry) ? `entry: ${entry}` : null,
    Number.isFinite(tp) ? `TP: +${tp}%${tpPrice != null ? ` → ${tpPrice.toFixed(4)}` : ""}` : null,
    gridLevels != null ? `grid levels: ${gridLevels}` : null,
    gate != null ? `gate: ${gate}` : null,
    adx != null ? `ADX(${timeframe || ""}): ${adx}` : null,
  ];
  return lines.filter(Boolean).join("\n");
}

function formatTpMessage(body: any): string {
  const { pair, pnl, tpPct, startPrice, closePrice } = body || {};
  const n = Number(pnl);
  return [
    `🎯 TP HIT`,
    `pair: ${pair || ""}`,
    `TP: ${tpPct ?? ""}%`,
    `entry: ${startPrice ?? ""}`,
    `exit: ${closePrice ?? ""}`,
    Number.isFinite(n) ? `PnL: ${n >= 0 ? "+" : ""}${n.toFixed(2)}%` : "",
  ]
    .filter(Boolean)
    .join("\n");
}

router.post("/telegram", notifyTokenGuard, async (req, res) => {
  try {
    const body = req.body || {};
    const { type, text } = body;

    if (type != null && type !== "start" && type !== "tp") {
      return res.status(400).json({ ok: false, error: "invalid type" });
    }
    if (typeof text === "string" && text.length > 1000) {
      return res.status(400).json({ ok: false, error: "text too long" });
    }

    const token = process.env.TELEGRAM_BOT_TOKEN;
    const chatId = process.env.TELEGRAM_CHAT_ID;
    if (!token || !chatId) {
      return res.json({ ok: false, error: "telegram not configured" });
    }

    const message =
      typeof text === "string" && text.trim()
        ? text
        : type === "start"
          ? formatStartMessage(body)
          : formatTpMessage(body);

    const url = `https://api.telegram.org/bot${token}/sendMessage`;
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text: message }),
    });
    const data: any = await r.json();
    if (!r.ok || !data.ok) {
      return res.status(500).json({ ok: false, error: data.description || "telegram error" });
    }
    return res.json({ ok: true });
  } catch (e: any) {
    return res.status(500).json({ ok: false, error: e?.message || "failed" });
  }
});

export default router;
