import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";
import * as schema from "./schema";
import path from "path";
import fs from "fs";

const dbPath = process.env.DATABASE_PATH || path.resolve("./data/bot.db");

// Создаём папку если нет
const dir = path.dirname(dbPath);
if (!fs.existsSync(dir)) {
  fs.mkdirSync(dir, { recursive: true });
}

const sqlite = new Database(dbPath);

// WAL режим для лучшей производительности
sqlite.pragma("journal_mode = WAL");

export const db = drizzle(sqlite, { schema });

export * from "./schema";
