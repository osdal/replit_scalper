import { createClient } from "@libsql/client";
import { drizzle } from "drizzle-orm/libsql";
import * as schema from "./schema";
import path from "path";
import fs from "fs";
import { config } from "dotenv";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Load .env - paths are now absolute in .env for cross-platform reliability
config({ path: path.resolve(__dirname, "../../../.env") });

// Use absolute paths from .env, fallback to project root
const projectRoot = path.resolve(__dirname, "../../..");
const dbPath = process.env.DATABASE_PATH 
  ? (process.env.DATABASE_PATH.startsWith("/") || process.env.DATABASE_PATH.match(/^[A-Za-z]:/)
      ? process.env.DATABASE_PATH
      : path.join(projectRoot, process.env.DATABASE_PATH))
  : path.join(projectRoot, "data/bot.db");
const dir = path.dirname(dbPath);
if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });

// На Windows путь должен быть file:///C:/path/to/db (три слеша, прямые слеши)
const dbUrl = dbPath.startsWith("/")
  ? `file://${dbPath}`
  : `file:///${dbPath.replace(/\\/g, "/")}`;

const client = createClient({ url: dbUrl });
export const db = drizzle(client, { schema });

export * from "./schema";
