import { createClient } from "@libsql/client";
import { drizzle } from "drizzle-orm/libsql";
import * as schema from "./schema";
import path from "path";
import fs from "fs";
import { config } from "dotenv";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

config({ path: path.resolve(__dirname, "../../../.env") });

const dbPath = process.env.DATABASE_PATH 
  ? path.resolve(process.env.DATABASE_PATH)
  : path.resolve("./data/bot.db");
const dir = path.dirname(dbPath);
if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });

// На Windows путь должен быть file:///C:/path/to/db (три слеша, прямые слеши)
const dbUrl = dbPath.startsWith("/")
  ? `file://${dbPath}`
  : `file:///${dbPath.replace(/\\/g, "/")}`;

const client = createClient({ url: dbUrl });
export const db = drizzle(client, { schema });

export * from "./schema";
