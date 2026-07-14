-- ============================================================================
-- RBAC: роли пользователей (Role-Based Access Control)
--
-- Как применить:
--   Supabase Dashboard -> SQL Editor -> New query -> вставить содержимое ->
--   Run. Скрипт идемпотентный: можно выполнять повторно без ошибок.
--
-- Модель ролей:
--   * guest      — НЕ хранится в БД. Это неавторизованный пользователь.
--                  Роль 'guest' вычисляется на фронтенде при отсутствии сессии.
--   * user       — обычный зарегистрированный пользователь (значение по умолчанию).
--   * superadmin — полный доступ ко всем функциям и опасным действиям.
--
-- Расширение в будущем: чтобы добавить новую роль (например 'manager'),
--   достаточно выполнить:
--     ALTER TYPE public.user_role ADD VALUE IF NOT EXISTS 'manager';
--   и добавить эту роль в тип на фронтенде (см. src/lib/roles.ts).
-- ============================================================================

-- 1. ENUM-тип ролей (ограничиваем допустимые значения на уровне БД) ------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
    CREATE TYPE public.user_role AS ENUM ('user', 'superadmin');
  END IF;
END
$$;

-- 2. Таблица профилей (1:1 с auth.users) --------------------------------------
CREATE TABLE IF NOT EXISTS public.profiles (
  id         uuid PRIMARY KEY REFERENCES auth.users (id) ON DELETE CASCADE,
  email      text,
  role       public.user_role NOT NULL DEFAULT 'user',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Если таблица уже существовала без колонки role — добавим её.
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS role public.user_role NOT NULL DEFAULT 'user';

-- 3. Row Level Security -------------------------------------------------------
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

-- Пользователь может читать только свой профиль.
DROP POLICY IF EXISTS "Profiles are viewable by owner" ON public.profiles;
CREATE POLICY "Profiles are viewable by owner"
  ON public.profiles
  FOR SELECT
  USING (auth.uid() = id);

-- Пользователь может обновлять свой профиль, НО не может менять себе роль.
-- (смена роли выполняется только суперадмином / из SQL Editor / сервисным ключом)
DROP POLICY IF EXISTS "Profiles are updatable by owner" ON public.profiles;
CREATE POLICY "Profiles are updatable by owner"
  ON public.profiles
  FOR UPDATE
  USING (auth.uid() = id)
  WITH CHECK (
    auth.uid() = id
    AND role = (SELECT p.role FROM public.profiles p WHERE p.id = auth.uid())
  );

-- 4. Автосоздание профиля при регистрации (БЕЗ авто-эскалации) ----------------
-- Каждый новый пользователь получает роль 'user'. Первый суперадмин
-- назначается ЯВНО через allow-list (см. ниже) или вручную в SQL Editor.
--
-- Почему НЕ "первый пользователь = superadmin автоматически":
--   при открытой регистрации это позволяет любому постороннему первым
--   зарегистрироваться и получить полный доступ (privilege escalation),
--   плюс возможны гонки при одновременных регистрациях.
--
-- Bootstrap суперадмина: занесите доверенные email в таблицу
-- public.superadmin_bootstrap. Триггер выдаст 'superadmin' ТОЛЬКО им.
CREATE TABLE IF NOT EXISTS public.superadmin_bootstrap (
  email text PRIMARY KEY
);
-- Никаких RLS-политик на этой таблице нет и она НЕ доступна анону/authenticated
-- (RLS включаем, политик не добавляем => полный запрет для обычных ролей;
--  доступ только у сервисного ключа / из SQL Editor).
ALTER TABLE public.superadmin_bootstrap ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  resolved_role public.user_role := 'user';
BEGIN
  -- Сериализуем конкурентные регистрации, чтобы исключить гонки при
  -- назначении привилегий.
  PERFORM pg_advisory_xact_lock(hashtext('handle_new_user'));

  -- Явный allow-list: email должен быть заранее занесён доверенным админом.
  IF NEW.email IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM public.superadmin_bootstrap b
       WHERE lower(b.email) = lower(NEW.email)
     )
  THEN
    resolved_role := 'superadmin';
  END IF;

  INSERT INTO public.profiles (id, email, role)
  VALUES (NEW.id, NEW.email, resolved_role)
  ON CONFLICT (id) DO NOTHING;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION public.handle_new_user();

-- 5. Бэкофилл: создать профили для уже существующих пользователей --------------
-- (полезно, если пользователи регистрировались до применения этой миграции)
INSERT INTO public.profiles (id, email, role)
SELECT u.id, u.email, 'user'::public.user_role
FROM auth.users u
LEFT JOIN public.profiles p ON p.id = u.id
WHERE p.id IS NULL;

-- ============================================================================
-- КАК НАЗНАЧИТЬ ПЕРВОГО СУПЕРАДМИНА (выберите один способ):
--
-- Вариант A — до регистрации (рекомендуется): занести email в allow-list,
-- затем зарегистрироваться этим email; триггер выдаст superadmin.
--   INSERT INTO public.superadmin_bootstrap (email) VALUES ('admin@example.com');
--
-- Вариант B — вручную для уже существующего пользователя (из SQL Editor):
--   UPDATE public.profiles
--   SET role = 'superadmin'
--   WHERE email = 'admin@example.com';
--
-- Рекомендуется также отключить открытую саморегистрацию в
-- Supabase (Authentication -> Providers -> Email -> "Enable sign ups"),
-- если публичный доступ к дашборду не требуется.
-- ============================================================================
