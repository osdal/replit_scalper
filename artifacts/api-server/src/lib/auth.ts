/**
 * Server-side RBAC / authentication.
 *
 * Модель совпадает с фронтендом (dashboard/src/lib/roles.ts):
 *   guest      — неаутентифицированный. Только чтение.
 *   user       — обычный пользователь. Управление ботами.
 *   superadmin — полный доступ, включая опасные операции.
 *   service    — доверенный server-to-server вызов (боты) по INTERNAL_API_TOKEN.
 *                Эквивалентен superadmin для внутренних маршрутов.
 *
 * ВАЖНО: это единственная точка реального контроля доступа. UI-гейтинг —
 * только UX; здесь мы проверяем Supabase JWT и роль из public.profiles.
 */
import type { Request, Response, NextFunction } from "express";
import { logger } from "./logger";

export type ServerRole = "guest" | "user" | "superadmin" | "service";

export type Capability = "view_dashboard" | "control_bots" | "admin_actions";

const ROLE_CAPABILITIES: Record<ServerRole, Capability[]> = {
  guest: ["view_dashboard"],
  user: ["view_dashboard", "control_bots"],
  superadmin: ["view_dashboard", "control_bots", "admin_actions"],
  service: ["view_dashboard", "control_bots", "admin_actions"],
};

export function roleCan(role: ServerRole, capability: Capability): boolean {
  return ROLE_CAPABILITIES[role]?.includes(capability) ?? false;
}

// ── Конфигурация окружения ───────────────────────────────────────────────────
function env(name: string): string {
  return (process.env[name] ?? "").trim();
}

// Поддерживаем разные варианты именования, чтобы не зависеть от одного .env.
function supabaseUrl(): string {
  return env("SUPABASE_URL") || env("VITE_SUPABASE_URL");
}
function supabaseServiceKey(): string {
  // Сервисный ключ нужен, чтобы читать profiles.role в обход RLS.
  return env("SUPABASE_SERVICE_ROLE_KEY") || env("SUPABASE_SERVICE_KEY");
}
function supabaseAnonKey(): string {
  return env("SUPABASE_ANON_KEY") || env("VITE_SUPABASE_ANON_KEY");
}
function internalToken(): string {
  return env("INTERNAL_API_TOKEN");
}

/**
 * Включён ли серверный auth. Если Supabase не сконфигурирован на сервере,
 * пользовательская аутентификация отключается и все запросы считаются
 * доверенными (dev / no-Supabase режим). Это осознанный компромисс:
 * без Supabase на бэкенде проверить JWT физически нечем.
 */
export function isAuthEnabled(): boolean {
  return !!supabaseUrl() && (!!supabaseServiceKey() || !!supabaseAnonKey());
}

// ── Расширение Request ───────────────────────────────────────────────────────
export interface AuthInfo {
  role: ServerRole;
  userId: string | null;
  source: "service" | "jwt" | "guest" | "disabled";
}

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Express {
    interface Request {
      auth?: AuthInfo;
    }
  }
}

// ── Верификация Supabase JWT ────────────────────────────────────────────────
async function verifySupabaseUser(accessToken: string): Promise<string | null> {
  const url = supabaseUrl();
  const apikey = supabaseServiceKey() || supabaseAnonKey();
  if (!url || !apikey) return null;
  try {
    const r = await fetch(`${url}/auth/v1/user`, {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        apikey,
      },
    });
    if (!r.ok) return null;
    const body = (await r.json()) as { id?: string };
    return body?.id ?? null;
  } catch (e) {
    logger.warn({ err: e }, "verifySupabaseUser failed");
    return null;
  }
}

/**
 * Читает роль из public.profiles через PostgREST c сервисным ключом
 * (обходя RLS). Fail-closed: при любой ошибке возвращаем 'guest'.
 */
async function fetchProfileRole(userId: string): Promise<ServerRole> {
  const url = supabaseUrl();
  const serviceKey = supabaseServiceKey();
  // Без сервисного ключа безопасно прочитать роль в обход RLS нельзя.
  // Fail-closed: считаем guest (никаких привилегий).
  if (!url || !serviceKey) return "guest";
  try {
    const r = await fetch(
      `${url}/rest/v1/profiles?id=eq.${encodeURIComponent(userId)}&select=role`,
      {
        headers: {
          apikey: serviceKey,
          Authorization: `Bearer ${serviceKey}`,
        },
      },
    );
    if (!r.ok) return "guest";
    const rows = (await r.json()) as Array<{ role?: string }>;
    const role = rows?.[0]?.role;
    if (role === "superadmin" || role === "user") return role;
    return "guest";
  } catch (e) {
    logger.warn({ err: e }, "fetchProfileRole failed");
    return "guest";
  }
}

/**
 * Middleware: определяет req.auth для каждого запроса.
 * Никогда не бросает — просто выставляет минимально-привилегированную роль.
 */
export async function authContext(
  req: Request,
  _res: Response,
  next: NextFunction,
): Promise<void> {
  try {
    // 1) Server-to-server (боты) по внутреннему токену.
    const provided = (req.header("x-internal-token") || "").trim();
    const secret = internalToken();
    if (secret && provided && provided === secret) {
      req.auth = { role: "service", userId: null, source: "service" };
      return next();
    }

    // 2) Если серверный auth выключен (нет Supabase на бэкенде) — доверяем.
    if (!isAuthEnabled()) {
      req.auth = { role: "service", userId: null, source: "disabled" };
      return next();
    }

    // 3) Пользовательский JWT из заголовка Authorization: Bearer <token>.
    const authz = req.header("authorization") || "";
    const match = /^Bearer\s+(.+)$/i.exec(authz);
    if (match) {
      const userId = await verifySupabaseUser(match[1].trim());
      if (userId) {
        const role = await fetchProfileRole(userId);
        req.auth = { role, userId, source: "jwt" };
        return next();
      }
    }

    // 4) Иначе — гость (fail-closed).
    req.auth = { role: "guest", userId: null, source: "guest" };
    return next();
  } catch (e) {
    // Любой сбой -> самый низкий уровень доступа.
    logger.warn({ err: e }, "authContext failed, defaulting to guest");
    req.auth = { role: "guest", userId: null, source: "guest" };
    return next();
  }
}

/**
 * Guard: требует наличие указанной capability.
 * Возвращает 401 для гостя (нужен вход) и 403 для недостаточной роли.
 */
export function requireCapability(capability: Capability) {
  return (req: Request, res: Response, next: NextFunction): void => {
    const auth = req.auth ?? { role: "guest" as ServerRole, userId: null, source: "guest" as const };
    if (roleCan(auth.role, capability)) {
      return next();
    }
    if (auth.role === "guest") {
      res.status(401).json({ error: "Authentication required" });
      return;
    }
    res.status(403).json({ error: "Insufficient permissions" });
  };
}
