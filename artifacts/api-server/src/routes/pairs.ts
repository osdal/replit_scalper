import { Router } from "express";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import yaml from "js-yaml";
import { db, botsTable } from "@workspace/db";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const router = Router();

router.get("/", async (_req, res) => {
  res.setHeader("Cache-Control", "no-store");

  // Источник истины — таблица bots; config_*.yaml остаётся только фолбэком.
  try {
    const rows = await db.selectDistinct({ symbol: botsTable.symbol }).from(botsTable);
    const dbSymbols = [...new Set(rows.map((r) => r.symbol))].sort();
    if (dbSymbols.length > 0) {
      return res.json(dbSymbols);
    }
  } catch { /* fall back to config files */ }

  const configDir = path.resolve(__dirname, "../../../../bot");
  const symbols: string[] = [];
  if (fs.existsSync(configDir)) {
    const files = fs.readdirSync(configDir).filter(f => f.startsWith("config_") && f.endsWith(".yaml"));
    for (const file of files) {
      try {
        const content = fs.readFileSync(path.join(configDir, file), "utf8");
        const parsed = yaml.load(content) as { symbol?: string };
        if (parsed?.symbol) {
          symbols.push(parsed.symbol);
        }
      } catch { /* ignore unreadable file */ }
    }
  }
  return res.json(symbols);
});

export default router;