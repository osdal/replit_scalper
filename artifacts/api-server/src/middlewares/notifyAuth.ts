import type { Request, Response, NextFunction } from "express";
import { isIP } from "net";

// Дашборд ходит с localhost на другой порт — это валидный Origin для локального
// инструмента. Список можно расширить через DASHBOARD_ORIGINS (через запятую).
const DEFAULT_ORIGINS = [
  "http://localhost:5175",
  "http://127.0.0.1:5175",
  "http://localhost:3000",
  "http://127.0.0.1:3000",
];

export function allowedOrigins(): string[] {
  const raw = process.env.DASHBOARD_ORIGINS;
  if (!raw) return DEFAULT_ORIGINS;
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

// Приватный IPv4 по числовым октетам (не по префиксу hostname: "10.evil.com"
// не должен считаться локальной сетью).
function isPrivateIpv4(host: string): boolean {
  if (isIP(host) !== 4) return false;
  const [a, b] = host.split(".").map(Number);
  if (a === 10) return true;
  if (a === 127) return true;
  if (a === 192 && b === 168) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  return false;
}

export function isAllowedOrigin(origin: string | undefined): boolean {
  // Без Origin — не браузер (curl, нативные клиенты) или same-origin запрос.
  if (!origin) return true;
  try {
    const u = new URL(origin);
    const h = u.hostname;
    if (h === "localhost" || h === "127.0.0.1" || h === "::1" || h === "[::1]") {
      return true;
    }
    // Локальная сеть: дашборд часто открывают по LAN-IP (192.168.x.x и т.п.).
    if (isPrivateIpv4(h)) return true;
    return allowedOrigins().includes(u.origin);
  } catch {
    return false;
  }
}

/**
 * Защита мутирующих роутов: fail-closed токен + allowlist Origin.
 * Без заданного NOTIFY_TOKEN роут недоступен (503), чтобы не оставаться
 * открытым релеем/точкой записи.
 */
export function notifyTokenGuard(req: Request, res: Response, next: NextFunction) {
  const origin = req.header("origin");
  if (!isAllowedOrigin(origin)) {
    return res.status(403).json({ ok: false, error: "forbidden origin" });
  }
  const required = process.env.NOTIFY_TOKEN;
  if (!required) {
    return res.status(503).json({ ok: false, error: "NOTIFY_TOKEN not configured" });
  }
  if (req.header("x-notify-token") !== required) {
    return res.status(401).json({ ok: false, error: "unauthorized" });
  }
  return next();
}
