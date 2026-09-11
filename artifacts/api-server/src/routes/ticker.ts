import { Router } from "express";

const router = Router();

router.get("/price/:symbol", async (req, res) => {
  try {
    const symbol = String(req.params.symbol || "").trim();
    if (!symbol) {
      return res.status(400).json({ error: "symbol is required" });
    }
    const symbolLower = symbol.toLowerCase();
    const url = `https://fapi.binance.com/fapi/v1/ticker/price?symbol=${encodeURIComponent(symbolLower)}`;
    const r = await fetch(url, { headers: { accept: "application/json" } });
    const text = await r.text();
    if (!r.ok) {
      return res.status(r.status).json({ error: `binance ${r.status}: ${text.slice(0, 200)}` });
    }
    const data = JSON.parse(text);
    return res.json({ symbol: data.symbol, price: data.price, source: "binance-fapi" });
  } catch (e: any) {
    return res.status(500).json({ error: String(e?.message || e) });
  }
});

export default router;
