import fs from "fs";
import path from "path";
import { db, clientsTable } from "@workspace/db";
import { isNotNull } from "drizzle-orm";
import { BOT_ENV, PROJECT_ROOT } from "../botPaths";
import { GATE_DIR, GATE_PREFIX, currentBan } from "./binanceBan";

/**
 * Мониторинг активного бана Binance с уведомлением в Telegram.
 *
 * Источник правды — те же gate-файлы, что использует `binanceBan.currentBan()`
 * и python-боты. При обнаружении нового ban_until_ms шлём одно сообщение
 * (дедупликация по файлу data/binance_ban_notified_<env>.json, чтобы рестарт
 * API не повторял уведомление). Событие всегда пишется в локальный лог —
 * даже если Telegram ещё не настроен.
 */

const CHECK_MS = Math.max(5_000, Number(process.env.BINANCE_BAN_MONITOR_SEC || 15) * 1000);
const NOTIFIED_FILE = path.join(PROJECT_ROOT, "data", `binance_ban_notified_${BOT_ENV}.json`);
const EVENT_LOG = path.join(PROJECT_ROOT, "data", `binance_ban_events_${BOT_ENV}.log`);

let started = false;
let lastNotified = 0;

/** Агрегированный вес по живым gate-файлам (максимум, как в binance_gate.py). */
function gateWeight(): { used: number; limit: number; files: number } {
  let used = 0;
  let files = 0;
  const now = Date.now();
  try {
    for (const name of fs.readdirSync(GATE_DIR)) {
      if (!name.startsWith(GATE_PREFIX)) continue;
      try {
        const raw = JSON.parse(fs.readFileSync(path.join(GATE_DIR, name), "utf8")) as Record<string, unknown>;
        const ts = Number(raw?.ts ?? 0) * 1000;
        if (!ts || now - ts > 120_000) continue; // мусор от завершённых процессов
        used = Math.max(used, Number(raw?.used_weight_1m ?? 0) || 0);
        files++;
      } catch {
        /* битый/недописанный файл — пропускаем */
      }
    }
  } catch {
    /* каталог ещё не создан */
  }
  return { used, limit: Number(process.env.BINANCE_WEIGHT_LIMIT_1M || 2400), files };
}

function readNotified(): number {
  try {
    return Number(JSON.parse(fs.readFileSync(NOTIFIED_FILE, "utf8")).ban_until_ms) || 0;
  } catch {
    return 0;
  }
}

function writeNotified(banUntilMs: number): void {
  try {
    fs.mkdirSync(path.dirname(NOTIFIED_FILE), { recursive: true });
    fs.writeFileSync(NOTIFIED_FILE, JSON.stringify({
      ban_until_ms: banUntilMs,
      at: new Date().toISOString(),
    }, null, 2));
  } catch {
    /* не критично */
  }
}

function appendEvent(line: string): void {
  try {
    fs.mkdirSync(path.dirname(EVENT_LOG), { recursive: true });
    fs.appendFileSync(EVENT_LOG, `${new Date().toISOString()} ${line}\n`);
  } catch {
    /* не критично */
  }
}

async function telegramChatIds(): Promise<string[]> {
  const fromEnv = String(process.env.TELEGRAM_CHAT_ID || "")
    .split(",").map((s) => s.trim()).filter(Boolean);
  if (fromEnv.length > 0) return fromEnv;
  try {
    const rows = await db.select({ chatId: clientsTable.telegram_chat_id })
      .from(clientsTable)
      .where(isNotNull(clientsTable.telegram_chat_id));
    return rows.map((r) => String(r.chatId)).filter(Boolean);
  } catch {
    return [];
  }
}

async function notify(text: string): Promise<boolean> {
  const token = String(process.env.TELEGRAM_BOT_TOKEN || "").trim();
  if (!token) {
    appendEvent(`TELEGRAM NOT CONFIGURED | ${text.replace(/\n/g, " | ")}`);
    console.warn("[ban-monitor] TELEGRAM_BOT_TOKEN not set — event only written to " + EVENT_LOG);
    return false;
  }
  const chats = await telegramChatIds();
  if (chats.length === 0) {
    appendEvent(`NO CHAT ID | ${text.replace(/\n/g, " | ")}`);
    console.warn("[ban-monitor] no Telegram chat id — event only written to " + EVENT_LOG);
    return false;
  }
  let sent = false;
  for (const chatId of chats) {
    try {
      const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chatId, text }),
      });
      if (r.ok) sent = true;
      else console.warn(`[ban-monitor] Telegram rejected chat ${chatId}: ${(await r.text()).slice(0, 200)}`);
    } catch (e) {
      console.warn("[ban-monitor] Telegram send failed:", String(e));
    }
  }
  return sent;
}

async function checkBan(): Promise<void> {
  const ban = currentBan();
  if (!ban.banUntilMs || ban.banUntilMs <= Date.now()) {
    if (lastNotified && readNotified() === lastNotified) lastNotified = 0;
    return;
  }
  if (ban.banUntilMs === lastNotified || ban.banUntilMs === readNotified()) {
    lastNotified = ban.banUntilMs;
    return;
  }

  const g = gateWeight();
  const sec = Math.max(1, Math.round((ban.banUntilMs - Date.now()) / 1000));
  const until = new Date(ban.banUntilMs).toISOString().replace("T", " ").slice(0, 19) + " UTC";
  const text = [
    "🚨 Binance API BAN",
    `До: ${until} (${sec}s)`,
    `Gate weight: ${g.used}/${g.limit}`,
    `Активных ботов: ${g.files}`,
    `Источник: ${ban.source ?? "gate file"}`,
    `Окружение: ${BOT_ENV}`,
  ].join("\n");

  lastNotified = ban.banUntilMs;
  writeNotified(ban.banUntilMs);
  appendEvent(`BAN until=${ban.banUntilMs} weight=${g.used}/${g.limit} bots=${g.files}`);
  const sent = await notify(text);
  console.warn(`[ban-monitor] active ban until ${until} (${sec}s), telegram_sent=${sent}`);
}

/** Запускает фоновую проверку. Возвращает функцию остановки. */
export function startBanMonitor(): () => void {
  if (started) return () => { /* уже запущен */ };
  started = true;
  lastNotified = readNotified();
  void checkBan();
  const timer = setInterval(() => { void checkBan(); }, CHECK_MS);
  return () => {
    clearInterval(timer);
    started = false;
  };
}
