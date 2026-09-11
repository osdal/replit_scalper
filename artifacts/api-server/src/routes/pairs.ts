import { Router } from "express";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import yaml from "js-yaml";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const router = Router();

router.get("/", (_req, res) => {
  res.setHeader("Cache-Control", "no-store");
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
  res.json(symbols);
});

export default router;