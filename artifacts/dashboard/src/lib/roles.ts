// ============================================================================
// RBAC — единый источник правды для ролей на фронтенде.
//
// Чтобы добавить новую роль в будущем (например 'manager'):
//   1. Добавить её в ENUM в БД:
//        ALTER TYPE public.user_role ADD VALUE IF NOT EXISTS 'manager';
//   2. Добавить строку в PERSISTED_ROLES ниже.
//   3. (опционально) описать её права в ROLE_CAPABILITIES.
// ============================================================================

// Роли, которые реально хранятся в БД (public.profiles.role -> ENUM user_role).
export const PERSISTED_ROLES = ["user", "superadmin"] as const;
export type PersistedRole = (typeof PERSISTED_ROLES)[number];

// 'guest' не хранится в БД — это вычисляемая роль для неавторизованных.
export type Role = PersistedRole | "guest";

export const GUEST_ROLE: Role = "guest";
export const DEFAULT_ROLE: PersistedRole = "user";

/** Проверка, что строка из БД — валидная сохраняемая роль. */
export function isPersistedRole(value: unknown): value is PersistedRole {
  return typeof value === "string" && (PERSISTED_ROLES as readonly string[]).includes(value);
}

/** Приводит любое значение из БД к безопасной роли (fallback -> 'user'). */
export function normalizeRole(value: unknown): PersistedRole {
  return isPersistedRole(value) ? value : DEFAULT_ROLE;
}

// ── Возможности (capabilities) ──────────────────────────────────────────────
// Гранулярные права. UI опирается на них, а не на имя роли напрямую —
// это упрощает добавление новых ролей позже.
export type Capability =
  | "view_dashboard"     // просмотр дашборда
  | "control_bots"       // запуск/остановка ботов, обновление, sync
  | "admin_actions";     // опасные операции: Clear DB, Stop All & Reload

const ROLE_CAPABILITIES: Record<Role, Capability[]> = {
  guest: ["view_dashboard"],
  user: ["view_dashboard", "control_bots"],
  superadmin: ["view_dashboard", "control_bots", "admin_actions"],
};

export function roleCan(role: Role, capability: Capability): boolean {
  return ROLE_CAPABILITIES[role]?.includes(capability) ?? false;
}
