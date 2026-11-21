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

/** Маскированный ключ: показываем только хвост, секрет никогда не отдаём. */
export function maskApiKey(value: string): string {
  const s = String(value || "").trim();
  if (s.length <= 8) return "*".repeat(s.length);
  return `${"*".repeat(s.length - 4)}${s.slice(-4)}`;
}

/** Сохранить (или заменить) ключ биржи клиента. */
export async function saveClientCredentials(params: {
  clientId: number;
  apiKey: string;
  apiSecret: string;
  label?: string;
  ipWhitelisted?: boolean;
  actor?: string;
}): Promise<{ id: number; mask: string; created: boolean }> {
  const clientId = Number(params.clientId);
  const apiKey = String(params.apiKey || "").trim();
  const apiSecret = String(params.apiSecret || "").trim();
  if (!Number.isFinite(clientId) || clientId <= 0) throw new Error("Некорректный clientId");
  if (!apiKey) throw new Error("Введите API key");
  if (!apiSecret) throw new Error("Введите API secret");

  const keyEnc = encryptSecret(apiKey);
  const secretEnc = encryptSecret(apiSecret);
  const mask = maskApiKey(apiKey);
  const now = new Date().toISOString();

  const [existing] = await db
    .select({ id: clientApiKeysTable.id })
    .from(clientApiKeysTable)
    .where(eq(clientApiKeysTable.client_id, clientId))
    .limit(1);

  if (existing) {
    await db
      .update(clientApiKeysTable)
      .set({
        api_key_mask: mask,
        api_key_enc: keyEnc.ciphertext,
        api_secret_enc: secretEnc.ciphertext,
        iv: keyEnc.iv,
        label: params.label || null,
        ip_whitelisted: params.ipWhitelisted ?? null,
        status: "active",
        last_error: null,
        updated_at: now,
      })
      .where(eq(clientApiKeysTable.id, existing.id));
    return { id: Number(existing.id), mask, created: false };
  }

  const [inserted] = await db
    .insert(clientApiKeysTable)
    .values({
      client_id: clientId,
      exchange: "binance",
      label: params.label || null,
      api_key_mask: mask,
      api_key_enc: keyEnc.ciphertext,
      api_secret_enc: secretEnc.ciphertext,
      iv: keyEnc.iv,
      key_version: 1,
      ip_whitelisted: params.ipWhitelisted ?? null,
      status: "active",
      created_at: now,
      updated_at: now,
    })
    .returning();
  return { id: Number(inserted.id), mask, created: true };
}

/** Метаданные ключа клиента (без секретов) для карточки. */
export async function getClientKeyInfo(clientId: number): Promise<{
  id: number;
  mask: string;
  label: string | null;
  ipWhitelisted: boolean | null;
  status: string;
  updatedAt: string;
} | null> {
  const [row] = await db
    .select()
    .from(clientApiKeysTable)
    .where(eq(clientApiKeysTable.client_id, clientId))
    .orderBy(desc(clientApiKeysTable.id))
    .limit(1);
  if (!row) return null;
  return {
    id: Number(row.id),
    mask: row.api_key_mask,
    label: row.label,
    ipWhitelisted: row.ip_whitelisted == null ? null : Boolean(row.ip_whitelisted),
    status: row.status,
    updatedAt: row.updated_at,
  };
}

/** Удалить ключ клиента (бот вернётся к ключу из окружения). */
export async function deleteClientCredentials(clientId: number): Promise<void> {
  await db
    .delete(clientApiKeysTable)
    .where(eq(clientApiKeysTable.client_id, clientId));
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
