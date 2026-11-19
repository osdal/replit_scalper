import crypto from "crypto";
import { db, clientApiKeysTable } from "@workspace/db";
import { and, desc, eq } from "drizzle-orm";

/**
 * Шифрование ключей биржи (AES-256-GCM).
 *
 * Формат совместим с support-bot/crypto.py: ciphertext||tag в base64 + iv.
 * Мастер-ключ — base64 от 32 байт в PLATFORM_MASTER_KEY (фолбэк
 * SUPPORT_BOT_MASTER_KEY). Без него per-client ключи не используются, и
 * процесс бота запускается со старыми BINANCE_API_KEY/SECRET из окружения.
 */
function masterKey(): Buffer | null {
  // Читаем env лениво: index.ts загружает .env/.env.<env> через dotenv уже
  // после импорта роутов, поэтому константа на этапе импорта была бы пустой.
  const raw = process.env.PLATFORM_MASTER_KEY || process.env.SUPPORT_BOT_MASTER_KEY || "";
  if (!raw) return null;
  try {
    const key = Buffer.from(raw, "base64");
    return key.length === 32 ? key : null;
  } catch {
    return null;
  }
}

export function hasMasterKey(): boolean {
  return masterKey() !== null;
}

export function encryptSecret(plain: string): { ciphertext: string; iv: string } {
  const key = masterKey();
  if (!key) throw new Error("PLATFORM_MASTER_KEY is not configured");
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  const enc = Buffer.concat([cipher.update(plain, "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return {
    ciphertext: Buffer.concat([enc, tag]).toString("base64"),
    iv: iv.toString("base64"),
  };
}

export function decryptSecret(ciphertextB64: string, ivB64: string): string {
  const key = masterKey();
  if (!key) throw new Error("PLATFORM_MASTER_KEY is not configured");
  const raw = Buffer.from(ciphertextB64, "base64");
  if (raw.length <= 16) throw new Error("ciphertext too short");
  const iv = Buffer.from(ivB64, "base64");
  const decipher = crypto.createDecipheriv("aes-256-gcm", key, iv);
  decipher.setAuthTag(raw.subarray(raw.length - 16));
  const data = raw.subarray(0, raw.length - 16);
  return Buffer.concat([decipher.update(data), decipher.final()]).toString("utf8");
}

export interface ClientCredentials {
  apiKey: string;
  apiSecret: string;
  mask: string;
}

/**
 * Действующий ключ клиента. null — ключа нет или мастер-ключ не настроен:
 * вызывающий код откатывается на BINANCE_API_KEY/SECRET из окружения.
 */
export async function loadClientCredentials(clientId: number): Promise<ClientCredentials | null> {
  if (!masterKey()) return null;
  try {
    const [row] = await db
      .select()
      .from(clientApiKeysTable)
      .where(and(
        eq(clientApiKeysTable.client_id, clientId),
        eq(clientApiKeysTable.status, "active"),
      ))
      .orderBy(desc(clientApiKeysTable.id))
      .limit(1);
    if (!row) return null;
    return {
      apiKey: decryptSecret(row.api_key_enc, row.iv),
      apiSecret: decryptSecret(row.api_secret_enc, row.iv),
      mask: row.api_key_mask,
    };
  } catch (e) {
    // Не роняем запуск бота: ключ может быть повреждён/старая схема.
    console.warn(`[keystore] client #${clientId}: cannot load key — ${String(e)}`);
    return null;
  }
}
