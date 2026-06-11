import { createClient } from "@libsql/client";
import { drizzle } from "drizzle-orm/libsql";
import * as schema from "./schema";
import path from "path";
import fs from "fs";

const dbPath = process.env.DATABASE_PATH || path.resolve("./data/bot.db");
const dir = path.dirname(dbPath);
if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });

const client = createClient({ url: `file:${dbPath}` });
export const db = drizzle(client, { schema });

export * from "./schema";
