import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { User } from "@supabase/supabase-js";
import { isSupabaseConfigured, supabase } from "./supabaseClient";
import {
  GUEST_ROLE,
  isPersistedRole,
  roleCan,
  type Capability,
  type Role,
} from "./roles";

interface RoleContextValue {
  role: Role;
  user: User | null;
  loading: boolean;
  isAdmin: boolean;
  isUser: boolean;
  isGuest: boolean;
  /** Гранулярная проверка прав. Предпочтительно вместо сравнения ролей. */
  can: (capability: Capability) => boolean;
}

const RoleContext = createContext<RoleContextValue | undefined>(undefined);

/**
 * Достаёт роль пользователя из public.profiles.role.
 * Fail-closed: при любой ошибке (нет строки, RLS, сеть) возвращаем 'guest',
 * чтобы НЕ выдать привилегии по ошибке. Привилегии даём только после
 * подтверждённого успешного чтения роли.
 */
async function fetchUserRole(userId: string): Promise<Role> {
  try {
    const { data, error } = await supabase
      .from("profiles")
      .select("role")
      .eq("id", userId)
      .single();
    if (error || !data) return GUEST_ROLE;
    return isPersistedRole(data.role) ? data.role : GUEST_ROLE;
  } catch {
    return GUEST_ROLE;
  }
}

export function RoleProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [role, setRole] = useState<Role>(GUEST_ROLE);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Supabase не настроен -> всегда гость, без сетевых запросов.
    if (!isSupabaseConfigured) {
      setUser(null);
      setRole(GUEST_ROLE);
      setLoading(false);
      return;
    }

    let active = true;

    const applySession = async (nextUser: User | null) => {
      if (!active) return;
      setUser(nextUser);
      if (!nextUser) {
        setRole(GUEST_ROLE);
        setLoading(false);
        return;
      }
      const resolved = await fetchUserRole(nextUser.id);
      if (!active) return;
      setRole(resolved);
      setLoading(false);
    };

    supabase.auth.getSession().then(({ data: { session } }) => {
      applySession(session?.user ?? null);
    });

    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      (_event, session) => {
        applySession(session?.user ?? null);
      }
    );

    return () => {
      active = false;
      subscription.unsubscribe();
    };
  }, []);

  const value = useMemo<RoleContextValue>(
    () => ({
      role,
      user,
      loading,
      isAdmin: role === "superadmin",
      isUser: role === "user",
      isGuest: role === "guest",
      can: (capability: Capability) => roleCan(role, capability),
    }),
    [role, user, loading]
  );

  return <RoleContext.Provider value={value}>{children}</RoleContext.Provider>;
}

/**
 * Хук доступа к текущей роли.
 * Возвращает { role, isAdmin, isUser, isGuest, can, user, loading }.
 */
export function useRole(): RoleContextValue {
  const ctx = useContext(RoleContext);
  if (!ctx) {
    throw new Error("useRole must be used within a <RoleProvider>");
  }
  return ctx;
}
